#!/usr/bin/env python3
"""
抓取Blogger feed -> 媒体本地化(图片/音频) -> 写入SQLite(含FTS索引+历史版本) -> 渲染静态HTML
cron每小时跑一次。视频不下载，保留原链接并标注"外部链接，需自行下载"。

本版新增：
- 从feed的 link[rel=alternate] 解析出Blogger原始永久链接，提取 年/月/slug 存为
  canonical_path。永久链接如果在Blogger后台改了，下次抓取会自动更新这个字段
  （短号/N/跳转时会用到最新值，不会失效）。
- 图片本地化路径从相对路径 media/xxx 改成绝对路径 /posts/{post_id}/media/xxx。
  原因：文章现在可能显示在 /2026/07/slug.html 这种URL下，相对路径会解析到错误位置。
- 首页加访问统计（今日/本周/本月/今年/累计）+ 点击/下载排行榜，纯HTML/CSS无JS。

用法: python3 fetch_blog.py
"""
import hashlib
import json
import os
import re
import sys
import urllib.request
from pathlib import Path
from datetime import datetime, timezone

import db
from telegram_notify import notify

BASE_DIR = Path(__file__).parent
sys.path.insert(0, str(BASE_DIR))
from app import _inline_post_as_base64  # noqa: E402  跟backup_to_hetzner.py同款用法

# 2026-07-27: blogger.foxzen.me 自定义域名已删除（跟mirror共用IP导致SSL握手错乱，
# 且绑到blogspot会连带解析到Google IP触发GFW阻断），博客地址回退到Blogger默认域名。
# 以后域名再变，只改这一行；下面首页链接和抓取地址都从这个常量派生。
BLOG_ROOT_URL = "https://digatlas.blogspot.com"
FEED_URL = f"{BLOG_ROOT_URL}/feeds/posts/default?alt=json&max-results=500"
MIRROR_ROOT_URL = "https://mirror.foxzen.me"
INDEXNOW_KEY = "29bfb801721343b798cc9dfca454d8af"
HTML_DIR = Path(__file__).parent / "html"
POSTS_DIR = HTML_DIR / "posts"

IMG_SRC_RE = re.compile(r'<img[^>]+src="([^"]+)"[^>]*>')
AUDIO_SRC_RE = re.compile(r'<audio[^>]+src="([^"]+)"[^>]*>|<source[^>]+src="([^"]+\.(?:mp3|ogg|wav))"[^>]*>')
VIDEO_LINK_RE = re.compile(r'href="([^"]+\.(?:mp4|mov|mkv|webm|avi))"')

# Blogger永久链接格式: https://www.blogger.foxzen.me/2026/07/some-slug.html
CANONICAL_PATH_RE = re.compile(r"/(\d{4})/(\d{2})/([^/]+)\.html$")

MEDIA_EXT_BY_CONTENT_TYPE = {
    "image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif",
    "image/webp": ".webp", "audio/mpeg": ".mp3", "audio/ogg": ".ogg", "audio/wav": ".wav",
}


def fetch_feed() -> dict:
    req = urllib.request.Request(FEED_URL, headers={"User-Agent": "blog-mirror-bot/1.0 (+https://mirror.foxzen.me)"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def slugify(entry_id: str) -> str:
    m = re.search(r"post-(\d+)", entry_id)
    return m.group(1) if m else re.sub(r"[^\w-]", "_", entry_id)


def extract_alternate_href(entry: dict) -> str | None:
    """从feed entry的link数组里找 rel=alternate 的href（即Blogger文章的真实永久链接）。"""
    for link in entry.get("link", []):
        if link.get("rel") == "alternate":
            return link.get("href")
    return None


def parse_canonical_path(blogger_url: str) -> str | None:
    """从 https://www.blogger.foxzen.me/2026/07/slug.html 提取 '2026/07/slug'。
    解析失败（比如permalink格式不是年/月/slug，例如是页面而非文章）返回None，
    调用方需要处理这种情况——不能假设一定能拿到。
    """
    if not blogger_url:
        return None
    m = CANONICAL_PATH_RE.search(blogger_url)
    if not m:
        return None
    year, month, slug = m.groups()
    return f"{year}/{month}/{slug}"


def download_media(url: str, media_dir: Path) -> str | None:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "blog-mirror-bot/1.0"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            content = resp.read()
            content_type = resp.headers.get("Content-Type", "").split(";")[0].strip()
        ext = MEDIA_EXT_BY_CONTENT_TYPE.get(content_type)
        if not ext:
            guess = re.search(r"\.(jpg|jpeg|png|gif|webp|mp3|ogg|wav)(?:$|[?&])", url, re.I)
            ext = f".{guess.group(1).lower()}" if guess else ".bin"
        file_hash = hashlib.md5(content).hexdigest()[:12]
        filename = f"{file_hash}{ext}"
        media_dir.mkdir(parents=True, exist_ok=True)
        (media_dir / filename).write_bytes(content)
        return filename
    except Exception as e:
        print(f"    [警告] 媒体下载失败 {url}: {e}")
        return None


def localize_media(content_html: str, post_id: str) -> str:
    """本地化媒体，src改成 /posts/{post_id}/media/{文件名} 的绝对路径
    （不是相对路径），因为文章展示URL现在跟物理存储路径不一定一致。
    """
    media_dir = POSTS_DIR / post_id / "media"
    html = content_html
    abs_prefix = f"/posts/{post_id}/media"

    for url in set(IMG_SRC_RE.findall(html)):
        local_name = download_media(url, media_dir)
        if local_name:
            html = html.replace(f'src="{url}"', f'src="{abs_prefix}/{local_name}"')
        else:
            print(f"    [警告] 图片本地化失败，原文中该图将保留外部链接: {url}")

    for m in AUDIO_SRC_RE.finditer(content_html):
        url = m.group(1) or m.group(2)
        if not url:
            continue
        local_name = download_media(url, media_dir)
        if local_name:
            html = html.replace(f'src="{url}"', f'src="{abs_prefix}/{local_name}"')

    for url in set(VIDEO_LINK_RE.findall(html)):
        marker = f'href="{url}"'
        if marker in html and "（外部视频链接，需自行下载）" not in html:
            html = html.replace(marker, f'{marker} target="_blank"')
    return html


def content_hash_of(html: str) -> str:
    return hashlib.md5(html.encode("utf-8")).hexdigest()


POST_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<!-- GA_START -->
<script async src="https://www.googletagmanager.com/gtag/js?id=G-WW1SLDPH1Z"></script>
<script>
  window.dataLayer = window.dataLayer || [];
  function gtag(){{dataLayer.push(arguments);}}
  gtag('js', new Date());
  gtag('config', 'G-WW1SLDPH1Z');
</script>
<!-- GA_END -->
<meta charset="UTF-8">
<link rel="icon" type="image/png" href="/images/fox-header.png">
<title>{title}</title>
<style>
body {{ max-width: 760px; margin: 40px auto; padding: 0 20px;
       font-family: -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif;
       font-size: 18px; line-height: 1.9; color: #222; }}
h1 {{ font-size: 1.6em; }}
.drop-cap-target::first-letter {{
  font-size: 2.6em; font-weight: bold; float: left; line-height: 1;
  margin: 0.05em 0.1em 0 0; color: #333;
}}
.meta {{ color: #888; font-size: 0.9em; margin-bottom: 1em; }}
.tags {{ margin-bottom: 2em; }}
.tags a {{ display: inline-block; background: #f0f0f0; padding: 2px 10px; border-radius: 10px;
          font-size: 0.85em; color: #555; text-decoration: none; margin-right: 6px; }}
img {{ max-width: 100%; height: auto; }}
.content pre {{ white-space: pre-wrap !important; word-break: break-word !important; overflow-wrap: break-word !important; }}
audio {{ width: 100%; }}
a.back {{ display: inline-block; margin-bottom: 2em; color: #06c; text-decoration: none; }}
.content a[href^="http"]:not([href*="mirror.foxzen.me"]) {{
  color: #1a73e8;
}}
.content a[href^="http"]:not([href*="mirror.foxzen.me"])::after {{
  content: " 🔗";
  font-size: 0.85em;
}}
.video-note {{ color: #b45309; font-size: 0.85em; }}
.stats-note {{ color: #999; font-size: 0.85em; margin-top: 2em; padding-top: 1em; border-top: 1px solid #eee; }}
.meta-precise {{ color: #666; margin-top: -0.5em; margin-bottom: 1em; }}
.discuss-cta {{ margin-top: 2em; padding: 16px; background: #f7f7f7; border-radius: 8px; text-align: center; }}
.discuss-cta p {{ margin: 0 0 10px; color: #555; }}
.discuss-btn {{ display: inline-block; padding: 8px 20px; background: #1a73e8; color: #fff;
                border-radius: 20px; text-decoration: none; font-size: 0.9em; }}
.discuss-btn:hover {{ background: #1558b0; }}
</style>
</head>
<body>
<a class="back" href="/" onclick="if (history.length > 1) {{ history.back(); return false; }}">&larr; 返回目录</a>
<h1>{title}</h1>
<div class="meta">发布于 {published}</div>
<div class="meta-precise">最初发布：{published_precise} · 最后修改：{updated_precise} · {reading_stats}</div>
<div class="tags">{tags_html}</div>
<div class="content">{content}</div>
{discuss_cta_block}
<div class="stats-note">本文镜像页浏览 {click_count} 次 · 离线下载 {download_count} 次 · 已有 {finish_read_count} 人读完</div>
{finish_read_block}{code_copy_block}{syntax_highlight_block}{new_tab_links_block}</body>
</html>
"""


SYNTAX_HIGHLIGHT_BLOCK = """<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/styles/github.min.css">
<style>
.content pre code .fb-comment { color: #6a737d; font-style: italic; }
.content pre code .fb-string { color: #22863a; }
.content pre code .fb-number { color: #005cc5; }
</style>
<script>
window.__fallbackHighlight = function () {
  document.querySelectorAll('.content pre code').forEach(function (block) {
    var text = block.textContent;
    var escaped = text
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;');
    escaped = escaped.replace(
      /(#.*$|\\/\\/.*$)|("(?:[^"\\\\]|\\\\.)*"|'(?:[^'\\\\]|\\\\.)*')|\\b(\\d+(?:\\.\\d+)?)\\b/gm,
      function (m, comment, str, num) {
        if (comment) return '<span class="fb-comment">' + comment + '</span>';
        if (str) return '<span class="fb-string">' + str + '</span>';
        if (num) return '<span class="fb-number">' + num + '</span>';
        return m;
      }
    );
    block.innerHTML = escaped;
  });
};
</script>
<script
  src="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/highlight.min.js"
  onload="if (window.hljs) { hljs.highlightAll(); }"
  onerror="window.__fallbackHighlight();"
></script>
"""


NEW_TAB_LINKS_BLOCK = """<script>
document.querySelectorAll('.content a[href^="http"]:not([href*="mirror.foxzen.me"])').forEach(function (a) {
  a.target = '_blank';
  a.rel = 'noopener';
});
</script>
"""


CODE_COPY_BLOCK = """<style>
.code-copy-wrap { position: relative; }
.code-copy-btn {
  position: absolute; top: 6px; right: 6px;
  background: #444; color: #fff; border: none; border-radius: 4px;
  padding: 4px 10px; font-size: 0.8em; cursor: pointer; opacity: 0.7;
}
.code-copy-btn:hover { opacity: 1; }
.code-copy-btn.copied { background: #2ecc71; }
</style>
<script>
(function () {
  var blocks = document.querySelectorAll('.content pre');
  blocks.forEach(function (pre) {
    var wrap = document.createElement('div');
    wrap.className = 'code-copy-wrap';
    pre.parentNode.insertBefore(wrap, pre);
    wrap.appendChild(pre);

    var btn = document.createElement('button');
    btn.className = 'code-copy-btn';
    btn.textContent = '复制';
    btn.onclick = function () {
      var text = pre.innerText;

      function showCopied() {
        btn.textContent = '已复制';
        btn.classList.add('copied');
        setTimeout(function () {
          btn.textContent = '复制';
          btn.classList.remove('copied');
        }, 1500);
      }

      function fallbackCopy() {
        var ta = document.createElement('textarea');
        ta.value = text;
        ta.style.position = 'fixed';
        ta.style.left = '-9999px';
        document.body.appendChild(ta);
        ta.select();
        try { document.execCommand('copy'); showCopied(); } catch (e) {}
        document.body.removeChild(ta);
      }

      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(showCopied, fallbackCopy);
      } else {
        fallbackCopy();
      }
    };
    wrap.appendChild(btn);
  });
})();
</script>
"""


FINISH_READ_BLOCK = """<!-- FINISH_READ_START -->
<style>
.finish-toast {
  position: fixed; bottom: 24px; left: 50%; transform: translateX(-50%) translateY(20px);
  background: #333; color: #fff; padding: 10px 20px; border-radius: 20px;
  font-size: 0.9em; opacity: 0; transition: opacity .4s, transform .4s; pointer-events: none; z-index: 999;
}
.finish-toast.show { opacity: 1; transform: translateX(-50%) translateY(0); }
.confetti-piece {
  position: fixed; top: -10px; width: 8px; height: 8px; pointer-events: none; z-index: 998;
  animation: confetti-fall 1.8s ease-in forwards;
}
@keyframes confetti-fall {
  to { transform: translateY(100vh) rotate(360deg); opacity: 0; }
}
</style>
<div id="finish-sentinel"></div>
<script>
(function () {
  var sentinel = document.getElementById('finish-sentinel');
  if (!sentinel || !('IntersectionObserver' in window)) return;
  var fired = false;

  var obs = new IntersectionObserver(function (entries) {
    entries.forEach(function (e) {
      if (e.isIntersecting && !fired) {
        fired = true;
        obs.disconnect();
        celebrate();
        fetch('/api/finish-read/__POST_ID__', { method: 'POST' }).catch(function () {});
      }
    });
  }, { threshold: 0.1 });
  obs.observe(sentinel);

  function celebrate() {
    var colors = ['#e74c3c', '#f1c40f', '#2ecc71', '#3498db', '#9b59b6', '#e67e22'];
    for (var i = 0; i < 24; i++) {
      var p = document.createElement('div');
      p.className = 'confetti-piece';
      p.style.left = (Math.random() * 100) + 'vw';
      p.style.background = colors[Math.floor(Math.random() * colors.length)];
      p.style.animationDelay = (Math.random() * 0.3) + 's';
      document.body.appendChild(p);
      (function (el) { setTimeout(function () { el.remove(); }, 2200); })(p);
    }
    var toast = document.createElement('div');
    toast.className = 'finish-toast';
    toast.textContent = '🎉 谢谢你读完了';
    document.body.appendChild(toast);
    requestAnimationFrame(function () { toast.classList.add('show'); });
    setTimeout(function () {
      toast.classList.remove('show');
      setTimeout(function () { toast.remove(); }, 500);
    }, 2500);
  }
})();
</script>
<!-- FINISH_READ_END -->
"""


DISCUSS_CTA_BLOCK = """<div class="discuss-cta">
  <p>发现错误、补充建议或有使用经验？欢迎到主站留言讨论。</p>
  <a class="discuss-btn" href="__SOURCE_URL__" target="_blank" rel="noopener">💬 到主站参与讨论</a>
</div>
"""


def _discuss_cta_block(source_url: str) -> str:
    """"到主站讨论"按钮，source_url缺失（极端情况，permalink解析失败）时
    不显示这个区块，总比链接指向空地址强。
    """
    if not source_url:
        return ""
    return DISCUSS_CTA_BLOCK.replace("__SOURCE_URL__", source_url)


def _finish_read_block(post_id: str) -> str:
    """完读特效的CSS+JS，post_id用简单字符串替换塞进去，不走.format()——
    这段JS/CSS里大括号太多，跟.format()的转义规则混在一起容易出上次那种bug，
    干脆用最朴素的str.replace()，绕开整个转义问题。
    """
    return FINISH_READ_BLOCK.replace("__POST_ID__", post_id)


def _format_ts(iso_str: str) -> str:
    """把Blogger返回的ISO时间戳统一转成UTC显示，精确到秒。
    不显示原始时区偏移——那个偏移大概率就是你所在时区（大概率UTC+08:00），
    换算成统一的UTC能去掉这一层信息，虽然从博客的语言/内容主题基本也能猜到大致地区，
    这层保护不算强，但换算这一步几乎零成本，能去掉就去掉。
    """
    if not iso_str:
        return "未知"
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc)
        return f"{dt.strftime('%Y-%m-%d %H:%M:%S')} (UTC)"
    except Exception:
        return iso_str  # 解析失败就原样显示，好过什么都不显示


def _reading_stats(content_html: str) -> str:
    """算全文字数（去HTML标签后的纯文本长度）+ 预计阅读时长。
    按每分钟300字算（偏慢的技术阅读速度，不是轻松阅读的400-500字/分钟），
    因为这系列文章信息密度大，用正常阅读速度算出来的时间会显得不真实地短。
    这只是个粗略估算，不是精确值，就当个参考。
    """
    text = db.strip_html_for_fts(content_html)
    char_count = len(text)
    minutes = max(1, round(char_count / 300))
    return f"全文{char_count}字 · 预计阅读{minutes}分钟"


def canonical_static_target(canonical_path):
    """把 'YYYY/MM/slug' 形式的canonical_path转成 html/YYYY/MM/slug.html 的目标路径。

    canonical_path静态化的目的是让 GitHub Pages 这类纯静态托管上，规范URL
    （/YYYY/MM/slug.html，目前只由 app.py 的 canonical_post_page 动态路由提供）
    也能对应一个真实存在的文件，不必依赖Flask（第十六节静态灾备的前置条件）。

    格式校验规则跟 parse_canonical_path/canonical_post_page 保持一致（年4位数字、
    月2位数字），额外用resolve()二次确认落点确实在HTML_DIR内部——即使上游正则
    出错或以后被改坏，这里也不会因为一个畸形的canonical_path值写到HTML_DIR外面。
    格式不对返回None，调用方跳过静态文件生成，不中断主流程。
    """
    if not canonical_path:
        return None
    parts = canonical_path.split("/")
    if len(parts) != 3:
        return None
    year, month, slug = parts
    if not (len(year) == 4 and year.isdigit()):
        return None
    if not (len(month) == 2 and month.isdigit()):
        return None
    if not slug or "/" in slug:
        return None
    target = (HTML_DIR / year / month / f"{slug}.html").resolve()
    try:
        target.relative_to(HTML_DIR.resolve())
    except ValueError:
        return None
    return target


def render_post(post_id, title, published, tags, content_html, click_count=0, download_count=0,
                 published_ts=None, updated_ts=None, finish_read_count=0, source_url=None,
                 canonical_path=None):
    tags_html = "".join(f'<a href="/index.html?tag={t}">#{t}</a>' for t in tags)
    html = POST_TEMPLATE.format(
        title=title, published=published, tags_html=tags_html, content=content_html,
        click_count=click_count, download_count=download_count,
        published_precise=_format_ts(published_ts), updated_precise=_format_ts(updated_ts),
        finish_read_count=finish_read_count, finish_read_block=_finish_read_block(post_id),
        code_copy_block=CODE_COPY_BLOCK, discuss_cta_block=_discuss_cta_block(source_url),
        new_tab_links_block=NEW_TAB_LINKS_BLOCK,
        reading_stats=_reading_stats(content_html), syntax_highlight_block=SYNTAX_HIGHLIGHT_BLOCK,
    )
    post_dir = POSTS_DIR / post_id
    post_dir.mkdir(parents=True, exist_ok=True)
    (post_dir / "index.html").write_text(html, encoding="utf-8")

    static_target = canonical_static_target(canonical_path)
    if static_target is None:
        if canonical_path:
            print(f"  [警告] canonical_path格式不对，跳过静态化: {canonical_path!r}")
    else:
        static_target.parent.mkdir(parents=True, exist_ok=True)
        static_target.write_text(html, encoding="utf-8")


def main():
    HTML_DIR.mkdir(parents=True, exist_ok=True)
    db.init_db()
    log_id = db.log_fetch_start()

    try:
        data = fetch_feed()
    except Exception as e:
        msg = f"blog-mirror抓取失败（网络/feed异常）: {e}"
        print(msg)
        db.log_fetch_end(log_id, "error", detail=str(e))
        notify(f"⚠️ {msg}")
        sys.exit(1)

    entries = data.get("feed", {}).get("entry", [])
    print(f"抓到 {len(entries)} 篇文章")

    changed_count = 0
    no_canonical_count = 0
    changed_urls = []
    for e in entries:
        title = e["title"]["$t"]
        raw_content = e["content"]["$t"]
        published_raw = e["published"]["$t"]  # 完整时间戳，含时分秒，用于排序
        published = published_raw[:10]        # 只取年月日，用于显示和日期范围筛选
        updated = e.get("updated", {}).get("$t", "")
        post_id = slugify(e["id"]["$t"])
        tags = [cat["term"] for cat in e.get("category", [])]

        blogger_url = extract_alternate_href(e)
        canonical_path = parse_canonical_path(blogger_url)
        if not canonical_path:
            no_canonical_count += 1
            print(f"  [警告] 无法从permalink解析canonical_path，短链/友好URL将不可用: {title} ({blogger_url})")

        localized_content = localize_media(raw_content, post_id)
        new_hash = content_hash_of(localized_content)
        old_hash = db.get_existing_hash(post_id)
        old_canonical = db.get_canonical_path(post_id)

        if old_hash and old_hash != new_hash:
            conn = db.get_conn()
            row = conn.execute("SELECT title, content_html, content_hash FROM posts WHERE post_id=?", (post_id,)).fetchone()
            conn.close()
            if row:
                db.save_version(post_id, row["title"], row["content_html"], row["content_hash"])
            changed_count += 1
            print(f"  [变更] {title} 内容有更新，已存档历史版本")
            if canonical_path:
                changed_urls.append(f"{MIRROR_ROOT_URL}/{canonical_path}.html")
        elif not old_hash:
            print(f"  [新增] {title}")
            if canonical_path:
                changed_urls.append(f"{MIRROR_ROOT_URL}/{canonical_path}.html")

        if canonical_path and old_canonical and canonical_path != old_canonical:
            print(f"  [permalink变更] {title}: {old_canonical} -> {canonical_path}（旧路径文件不删除，短号会自动指向新地址）")

        db.upsert_post(post_id, title, localized_content, tags, published, updated, new_hash,
                        canonical_path=canonical_path, source_url=blogger_url, published_ts=published_raw)

    # 渲染文章页需要点击/下载数，抓取完统一取一次，避免逐篇查库
    click_counts = db.get_all_post_click_counts()
    download_counts = db.get_all_post_download_counts()
    finish_counts = db.get_all_finish_read_counts()
    for e in entries:
        post_id = slugify(e["id"]["$t"])
        conn = db.get_conn()
        row = conn.execute("SELECT title, published, tags, content_html, published_ts, updated, source_url, canonical_path FROM posts WHERE post_id=?", (post_id,)).fetchone()
        conn.close()
        if not row:
            continue
        render_post(post_id, row["title"], row["published"], json.loads(row["tags"]), row["content_html"],
                    click_count=click_counts.get(post_id, 0), download_count=download_counts.get(post_id, 0),
                    published_ts=row["published_ts"], updated_ts=row["updated"],
                    finish_read_count=finish_counts.get(post_id, 0), source_url=row["source_url"],
                    canonical_path=row["canonical_path"])

    # 计算每篇文章导出成Base64离线版之后的体积，存库供首页/搜索接口显示。
    # 复用app.py的_inline_post_as_base64，跟实际下载时用的是同一份逻辑，数字不会对不上。
    for e in entries:
        post_id = slugify(e["id"]["$t"])
        exported = _inline_post_as_base64(post_id)
        if exported is not None:
            db.set_export_size(post_id, len(exported.encode("utf-8")))

    assign_short_number_links()
    render_index()
    render_seo_files()
    if changed_urls:
        _submit_indexnow(changed_urls)
        _purge_cloudflare_cache(changed_urls)
    db.log_fetch_end(log_id, "ok", detail=f"changed={changed_count}, 无canonical={no_canonical_count}", post_count=len(entries))
    print(f"完成。共 {len(entries)} 篇，{changed_count} 篇有更新，{no_canonical_count} 篇无法解析canonical_path。")


def assign_short_number_links():
    """短号只在数据库里分配，不再建文件系统symlink——跳转逻辑交给Flask处理
    （查canonical_path后302跳转），这样permalink变了短号也不会失效。
    """
    newly = db.assign_missing_numbers()
    if newly:
        print(f"  新分配短号: {newly}")


INDEX_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<!-- GA_START -->
<script async src="https://www.googletagmanager.com/gtag/js?id=G-WW1SLDPH1Z"></script>
<script>
  window.dataLayer = window.dataLayer || [];
  function gtag(){{dataLayer.push(arguments);}}
  gtag('js', new Date());
  gtag('config', 'G-WW1SLDPH1Z');
</script>
<!-- GA_END -->
<meta charset="UTF-8">
<link rel="icon" type="image/png" href="/images/fox-header.png">
<title>统计学习小议 - 镜像站</title>
<style>
body {{ max-width: 760px; margin: 40px auto; padding: 0 20px;
       font-family: -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif; }}
li {{ margin-bottom: 1em; }}
.date {{ color: #888; font-size: 0.9em; }}
.count {{ color: #aaa; font-size: 0.85em; }}
.updated {{ color: #aaa; font-size: 0.8em; margin-top: 2em; }}
.header-img {{ display: block; max-width: 320px; margin: 0 auto 2em; }}
.easter-egg {{ color: #bbb; font-size: 0.8em; margin-top: 3em; text-align: center; }}
.easter-egg a {{ color: #999; }}
.stats-box {{ background: #f7f7f7; border-radius: 8px; padding: 16px 20px; margin: 20px 0; font-size: 0.9em; }}
.stats-box b {{ color: #333; }}
.archive-note {{ background: #fff8ec; border: 1px solid #f0e0c0; border-radius: 8px;
                 padding: 14px 18px; margin: 20px 0; font-size: 0.9em; color: #7a5c1e; line-height: 1.7; }}
.leaderboard {{ margin: 20px 0; }}
.leaderboard h3 {{ font-size: 1em; margin-bottom: 8px; }}
.leaderboard ol {{ padding-left: 1.4em; }}
.leaderboard li {{ margin-bottom: 4px; }}
.daily-quote {{ color: #999; font-size: 0.85em; font-style: italic; margin: 8px 0 4px; }}
.entries-box {{ background: #f7f7f7; border-radius: 8px; padding: 16px 20px; margin: 20px 0; font-size: 0.9em; }}
.entries-box h3 {{ font-size: 1em; margin: 0 0 6px; }}
.entries-box .entries-intro {{ color: #666; margin: 0 0 12px; }}
.entries-box ul {{ list-style: none; padding: 0; margin: 0; }}
.entries-box li {{ margin: 0 0 12px; padding-bottom: 12px; border-bottom: 1px solid #e6e6e6; }}
.entries-box li:last-child {{ margin-bottom: 0; padding-bottom: 0; border-bottom: none; }}
.entries-box .entry-name {{ font-weight: bold; }}
.entries-box .entry-domain {{ color: #888; font-size: 0.85em; }}
.entries-box .entry-desc {{ color: #666; font-size: 0.85em; margin-top: 2px; }}
.entries-box .entry-planned {{ color: #aaa; }}
</style>
</head>
<body>
<img class="header-img" src="images/fox-header.png" alt="狐斋志异" onerror="this.style.display='none'">
<h1>统计学习小议 - 镜像站</h1>
<p>本站为 <a href="{blog_root_url}" target="_blank" rel="noopener">主站</a> 的静态镜像，内容定期同步。</p>
<p class="daily-quote">🦊 <!--QUOTE--></p>

<div class="entries-box">
<h3>🔗 FoxZen 的其他入口</h3>
<p class="entries-intro">FoxZen 还提供以下公开入口，分别承担不同用途；全部完全免费开放，不涉及付费、会员或管理员特权。</p>
<ul>
<li>
<span class="entry-name">当前镜像 / 主要文章入口</span><br>
<span class="entry-domain">mirror.foxzen.me（当前页面）</span>
<div class="entry-desc">你正在访问的镜像站，同步自主站内容。</div>
</li>
<li>
<a class="entry-name" href="https://foxzen.me/" target="_blank" rel="noopener">正式主站</a><br>
<span class="entry-domain">foxzen.me</span>
<div class="entry-desc">FoxZen 的正式主站。</div>
</li>
<li>
<a class="entry-name" href="https://backup.foxzen.me/" target="_blank" rel="noopener">源站备用入口</a><br>
<span class="entry-domain">backup.foxzen.me</span>
<div class="entry-desc">绕开 Cloudflare 直连 VPS 源站，主站/Cloudflare 访问异常时可以用这个地址确认源站本身是否正常。</div>
</li>
<li>
<span class="entry-name entry-planned">网站状态与公告（规划中，尚未上线）</span><br>
<span class="entry-domain entry-planned">update.foxzen.me</span>
<div class="entry-desc">用于发布维护、故障、恢复等系统级公告（不是文章更新记录），暂未正式部署。</div>
</li>
<li>
<span class="entry-name entry-planned">GitHub 静态镜像（规划中，尚未上线）</span><br>
<span class="entry-domain entry-planned">github.foxzen.me</span>
<div class="entry-desc">基于 GitHub Pages 的独立静态文章镜像，不依赖 VPS，暂未正式部署。</div>
</li>
<li>
<span class="entry-name entry-planned">Cloudflare 静态镜像（规划中，尚未上线）</span><br>
<span class="entry-domain entry-planned">cf.foxzen.me</span>
<div class="entry-desc">基于 Cloudflare Pages 的第二个独立静态发布入口，暂未正式部署。</div>
</li>
</ul>
</div>

<div class="archive-note">
如果这些文章对你有帮助，欢迎离线保存。知识的价值不仅在于被阅读，也在于能够长期保存和再次使用。欢迎下载、离线阅读和长期保存。转载或引用请注明来源。
</div>

<div class="archive-note">
联系邮箱：<a href="mailto:foxzenme@gmail.com">foxzenme@gmail.com</a>（注意：<b>foxzen@gmail.com 不是我</b>，请勿误认）
</div>

<div class="archive-note">
狐斋志异不接受商业付费推荐，也不会因为收取费用而推荐某个产品或服务。<br>
本站推荐的产品、服务和工具，原则上都是我自己使用过，并认为确实值得推荐的。<br>
如果你是一名预算非常有限的独立开发者，确实需要一些推广，但无力承担商业广告费用，欢迎<a href="mailto:foxzenme@gmail.com">直接联系我</a>。我可以在实际试用你的产品后，根据自己的真实体验决定是否推荐。<br>
推荐不能购买，赞助也不会获得推荐权限。<br>
我最终推荐与否，只取决于产品本身是否值得让读者知道。
</div>

<div class="stats-box">
<b>文章总数</b>：{post_count} 篇 &nbsp;|&nbsp;
<b>全站打包下载</b>：{site_download_count} 次 &nbsp;|&nbsp;
<b>全部导出预估体积</b>：约 {total_export_size}（未压缩，实际zip会更小）<br>
<b>访问量</b>：今日 {visits_today} · 本周 {visits_week} · 本月 {visits_month} · 今年 {visits_year} · 累计 {visits_total}
</div>

<div class="leaderboard">
<h3>🔥 点击排行榜</h3>
<ol>{top_clicked_html}</ol>
<h3>📥 下载排行榜</h3>
<ol>{top_downloaded_html}</ol>
</div>

<div id="app"></div>
<script src="/static/index.js"></script>
<div class="updated">最后更新: {updated}</div>
<div class="easter-egg">🦊 <a href="/404/">这个网站藏着一只找不到路的狐狸</a></div>
</body>
</html>
"""


def _human_size(num_bytes: int) -> str:
    n = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


def _href_for(post_id, number, canonical_path):
    if canonical_path:
        return f"/{canonical_path}.html"
    if number:
        return f"/{number}/"
    return f"/posts/{post_id}/"


def render_index():
    posts = db.get_all_posts()
    click_counts = db.get_all_post_click_counts()
    download_counts = db.get_all_post_download_counts()
    export_sizes = db.get_all_export_sizes()
    finish_counts = db.get_all_finish_read_counts()

    fallback_lines = []
    for p in posts:
        href = _href_for(p["post_id"], p["number"], p.get("canonical_path"))
        c = click_counts.get(p["post_id"], 0)
        d = download_counts.get(p["post_id"], 0)
        f = finish_counts.get(p["post_id"], 0)
        size = _human_size(export_sizes.get(p["post_id"], 0))
        fallback_lines.append(
            f'<li><a href="{href}" target="_blank" rel="noopener">{p["title"]}</a> '
            f'<span class="date">{p["published"]}</span> '
            f'<span class="count">· 浏览{c}次 · 下载{d}次 · 离线版{size} · 完读{f}次</span></li>'
        )
    fallback_items = "\n".join(fallback_lines)

    top_clicked = db.get_top_clicked(limit=10)
    top_downloaded = db.get_top_downloaded(limit=10)
    canonical_by_id = {p["post_id"]: p.get("canonical_path") for p in posts}
    number_by_id = {p["post_id"]: p["number"] for p in posts}

    def rank_html(rows, unit):
        items = []
        for r in rows:
            href = _href_for(r["post_id"], number_by_id.get(r["post_id"]), canonical_by_id.get(r["post_id"]))
            items.append(f'<li><a href="{href}" target="_blank" rel="noopener">{r["title"]}</a>（{r["count"]} {unit}）</li>')
        return "\n".join(items) if items else "<li>暂无数据</li>"

    visits = db.get_visit_stats()

    html = INDEX_TEMPLATE.format(
        updated=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        post_count=len(posts),
        site_download_count=db.get_site_download_count(),
        visits_today=visits["today"], visits_week=visits["week"],
        visits_month=visits["month"], visits_year=visits["year"], visits_total=visits["total"],
        top_clicked_html=rank_html(top_clicked, "次浏览"),
        top_downloaded_html=rank_html(top_downloaded, "次下载"),
        blog_root_url=BLOG_ROOT_URL,
        total_export_size=_human_size(db.get_total_export_size()),
    )
    html = html.replace('<div id="app"></div>', f'<div id="app"><ul id="fallback-list">{fallback_items}</ul></div>')
    (HTML_DIR / "index.html").write_text(html, encoding="utf-8")


CF_API_TOKEN = os.environ.get("CF_API_TOKEN", "")
CF_ZONE_ID = os.environ.get("CF_ZONE_ID", "")


def _purge_cloudflare_cache(urls):
    """只清真正变化的URL在Cloudflare上的缓存，不是purge整站——
    这样没变化的文章继续吃缓存加速，只有变了的这几篇立刻从Cloudflare上过期，
    读者下一次请求就能拿到新版本，不用等2小时缓存自然过期。
    只处理mirror.foxzen.me的地址，Blogger那些source_url不归这个Cloudflare zone管，
    传过去也没用，这里先过滤掉。
    失败不影响主流程，网络问题/token过期这种不该打断整个抓取任务。
    """
    if not CF_API_TOKEN or not CF_ZONE_ID:
        return
    mirror_urls = [u for u in urls if u.startswith(MIRROR_ROOT_URL)]
    if not mirror_urls:
        return
    try:
        req = urllib.request.Request(
            f"https://api.cloudflare.com/client/v4/zones/{CF_ZONE_ID}/purge_cache",
            data=json.dumps({"files": mirror_urls}).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {CF_API_TOKEN}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            if result.get("success"):
                print(f"  [Cloudflare] 已清除{len(mirror_urls)}个URL的缓存")
            else:
                print(f"  [Cloudflare] 清缓存请求被拒绝: {result.get('errors')}")
    except Exception as e:
        print(f"  [Cloudflare] 清缓存失败（不影响本次抓取其他流程）: {e}")


def _submit_indexnow(urls):
    """把新增/变更的文章URL推给Bing的IndexNow接口，让它尽快来抓取。
    只推真正变化的URL，不是每次全量推——避免被当成滥用通知。
    这一步失败不影响主流程（网络问题、Bing那边偶尔抽风都不该导致整个抓取任务失败），
    出错只打印警告。
    """
    if not INDEXNOW_KEY:
        return
    payload = {
        "host": "mirror.foxzen.me",
        "key": INDEXNOW_KEY,
        "keyLocation": f"{MIRROR_ROOT_URL}/{INDEXNOW_KEY}.txt",
        "urlList": urls,
    }
    try:
        req = urllib.request.Request(
            "https://api.indexnow.org/indexnow",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            print(f"  [IndexNow] 已推送{len(urls)}个URL，状态码{resp.status}")
    except Exception as e:
        print(f"  [IndexNow] 推送失败（不影响本次抓取其他流程）: {e}")


def render_seo_files():
    """生成robots.txt（允许爬虫）+ sitemap.xml（列出所有文章地址给爬虫）
    + IndexNow验证key文件。这几个都是静态文本，不用JS。
    """
    (HTML_DIR / "robots.txt").write_text(
        f"User-agent: *\nAllow: /\nSitemap: {MIRROR_ROOT_URL}/sitemap.xml\n",
        encoding="utf-8",
    )

    if INDEXNOW_KEY:
        (HTML_DIR / f"{INDEXNOW_KEY}.txt").write_text(INDEXNOW_KEY, encoding="utf-8")

    posts = db.get_all_posts()
    urls = [f"<url><loc>{MIRROR_ROOT_URL}/</loc></url>"]
    for p in posts:
        if not p.get("canonical_path"):
            continue
        urls.append(f"<url><loc>{MIRROR_ROOT_URL}/{p['canonical_path']}.html</loc></url>")
    sitemap = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        + "\n".join(urls) + "\n</urlset>\n"
    )
    (HTML_DIR / "sitemap.xml").write_text(sitemap, encoding="utf-8")


if __name__ == "__main__":
    main()
