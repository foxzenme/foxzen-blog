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
import shutil
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
# Blogger feed用的是GData协议的分页约定：start-index（从1开始）+max-results，
# 响应体feed.openSearch$totalResults/startIndex/itemsPerPage是标准OpenSearch
# 分页扩展字段——用实际正式接口验证过（不是凭记忆假设）：itemsPerPage回显的
# 是请求参数本身，不是这一页实际返回的条数，判断"是否最后一页"不能用它，
# 只能看这一页实际返回的entry数量，见fetch_all_entries()。
# 500这个每页条数沿用这个项目一直在用的值（改分页之前也是max-results=500，
# 只是从来没真正翻过页）；文章数低于500时（目前是18篇）分页循环只会请求
# 这一页，行为、请求次数跟改造前完全一致。
FEED_PAGE_SIZE = 500
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


def fetch_feed_page(start_index: int, max_results: int) -> dict:
    """抓取一页Blogger feed。start_index从1开始（Blogger/GData约定，不是0）。"""
    url = f"{BLOG_ROOT_URL}/feeds/posts/default?alt=json&max-results={max_results}&start-index={start_index}"
    req = urllib.request.Request(url, headers={"User-Agent": "blog-mirror-bot/1.0 (+https://mirror.foxzen.me)"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


class FeedPaginationError(Exception):
    """分页抓取过程中，只要"已经拿到完整、可信的当前文章集合"这个前提不成立，
    就抛这个异常，绝不返回一份不完整/不一致的部分结果——delete同步(见
    sync_deleted_posts())的安全性完全建立在entries是一次完整快照这个假设上，
    宁可这一轮整体失败重试，也不能让下游拿着残缺数据去判断"哪些文章被删除了"。

    main()按跟原来fetch_feed()网络异常完全一样的方式捕获处理（记录失败、
    Telegram通知、sys.exit(1)，不做任何upsert/删除同步），这个类不改变
    main()对"抓取失败"的既有语义，只是把"部分成功但不完整"也归到同一类失败。
    """


def fetch_all_entries() -> list:
    """分页抓取Blogger feed的全部文章，返回entries列表（跟旧版单页
    data["feed"]["entry"]形状一致，调用方不需要改）。

    完整性校验——任何一条不满足都抛FeedPaginationError，不返回部分结果：
    - 每一页请求/JSON解析必须成功。
    - 第一页必须能读到合法的openSearch$totalResults整数——这是判断
      "是否已经拿全"的唯一依据，读不到就没法做completeness判断。
    - totalResults必须在整个分页过程中保持不变；变了说明翻页期间Blogger
      上的文章集合发生了变化（比如这时候有人发了新文章，Blogger默认按
      发布时间倒序排列，会导致后续文章整体错位），这次拿到的不是同一个
      时间点的一致快照，不可信。
    - 单页返回条数不能超过请求的max_results（服务端行为异常的信号）。
    - 每篇文章必须有id字段，且不能跟之前任何一页的id重复——重复本身就是
      分页错位的直接证据。
    - 最终累计条数必须刚好等于openSearch$totalResults：多了/少了都失败。
    """
    all_entries = []
    seen_ids = set()
    expected_total = None
    start_index = 1

    while True:
        try:
            page = fetch_feed_page(start_index, FEED_PAGE_SIZE)
        except Exception as e:
            raise FeedPaginationError(f"第{start_index}条起的分页请求失败: {e}") from e

        feed = page.get("feed")
        if not isinstance(feed, dict):
            raise FeedPaginationError(f"第{start_index}条起的响应缺少feed字段")

        total_raw = feed.get("openSearch$totalResults", {}).get("$t")
        try:
            total_this_page = int(total_raw)
        except (TypeError, ValueError):
            raise FeedPaginationError(
                f"第{start_index}条起的响应缺少合法的openSearch$totalResults"
                f"（实际: {total_raw!r}），无法确认文章总数，视为不完整"
            )
        if expected_total is None:
            expected_total = total_this_page
        elif total_this_page != expected_total:
            raise FeedPaginationError(
                f"分页过程中openSearch$totalResults发生变化"
                f"（{expected_total} -> {total_this_page}，疑似翻页期间Blogger文章集合有变动），"
                "本轮结果不可信"
            )

        page_entries = feed.get("entry", [])
        if len(page_entries) > FEED_PAGE_SIZE:
            raise FeedPaginationError(
                f"第{start_index}条起的响应条数({len(page_entries)})超过请求的"
                f"max-results({FEED_PAGE_SIZE})，服务端行为异常"
            )

        for entry in page_entries:
            entry_id = entry.get("id", {}).get("$t")
            if entry_id is None:
                raise FeedPaginationError(f"第{start_index}条起的响应里有一篇文章缺少id字段")
            if entry_id in seen_ids:
                raise FeedPaginationError(
                    f"文章{entry_id!r}在分页结果中重复出现，疑似翻页期间Blogger文章集合有变动"
                )
            seen_ids.add(entry_id)
            all_entries.append(entry)

        if len(page_entries) < FEED_PAGE_SIZE:
            break  # 这一页数量不足一页，正常到达末尾（即使总数刚好是页大小的整数倍，
                   # 也会多请求一次拿到0条来确认结束，见test_exact_multiple_of_page_size）
        start_index += len(page_entries)

    if len(all_entries) != expected_total:
        raise FeedPaginationError(
            f"累计抓到{len(all_entries)}篇，跟openSearch$totalResults声明的"
            f"{expected_total}篇不一致"
        )

    return all_entries


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
<link rel="canonical" href="{canonical_url}">
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

    # canonical标签：不管这篇文章最终通过mirror/backup/github/cf哪个域名被
    # 访问到，都固定指向mirror.foxzen.me——四个域名serve的是同一份html/源
    # 文件（github/cf发布时shutil.copytree原样拷贝，publish_build.py的
    # _rewrite_hostname()只处理index.html/robots.txt/sitemap.xml，不碰
    # 逐篇文章文件，见其调用点），这里写死MIRROR_ROOT_URL就能让四处
    # 同时正确，不需要按host分别生成。优先用canonical_path对应的规范地址
    # （/YYYY/MM/slug.html，跟legacy_post_link()把/posts/<id>/ 301跳转到
    # 这个地址是同一个"谁是权威URL"的判断）；permalink解析失败时退回
    # /posts/{post_id}/（这种情况下这确实是唯一能访问到这篇文章的地址，
    # 不会被redirect，自引用是对的）。
    canonical_url = (f"{MIRROR_ROOT_URL}/{canonical_path}.html" if canonical_path
                      else f"{MIRROR_ROOT_URL}/posts/{post_id}/")

    html = POST_TEMPLATE.format(
        title=title, published=published, tags_html=tags_html, content=content_html,
        click_count=click_count, download_count=download_count,
        published_precise=_format_ts(published_ts), updated_precise=_format_ts(updated_ts),
        finish_read_count=finish_read_count, finish_read_block=_finish_read_block(post_id),
        code_copy_block=CODE_COPY_BLOCK, discuss_cta_block=_discuss_cta_block(source_url),
        new_tab_links_block=NEW_TAB_LINKS_BLOCK,
        reading_stats=_reading_stats(content_html), syntax_highlight_block=SYNTAX_HIGHLIGHT_BLOCK,
        canonical_url=canonical_url,
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


# ---------------------------------------------------------------------------
# Blogger删除文章 -> 本地镜像同步删除
#
# 权威源是Blogger：Blogger当前存在的文章 = 镜像应该存在的文章。这一组
# 函数负责找出"本地数据库里有、但这次抓取到的Blogger文章集合里已经没有"
# 的post_id，删除它们在html/下的静态文件和数据库记录。
#
# 只处理GreenCloud本地镜像（html/ + data/blog.db）：GitHub Pages/
# Cloudflare Pages的静态发布产物完全由publish_build.py从html/现场重建
# （build_publish()每次调用都shutil.rmtree(output_dir)后重新复制，见该
# 文件说明），html/里少了的文件不会被复制进publish/，不需要另外写删除
# 逻辑；git_publish.py的commit_and_push()用`git add -- html`（不是
# `git add -A`/`git add .`，但对已跟踪文件而言，显式pathspec本身就等价于
# 连删除一起加入暂存区——已实测确认），本身就能正确检测并提交html/下的
# 文件删除，同样不需要新代码。这组函数因此只用管本地文件系统和数据库
# 这两处"权威数据"，下游全部自动跟着重新生成/重新计算。
#
# 不处理：permalink变更（文章还在，只是路径变了）——那是旧canonical路径
# 文件"不删除、靠短号跳转到新地址"的既有设计（见main()里的[permalink变更]
# 打印），跟"文章彻底不存在了"是两个不同的场景，不在这组函数处理范围内。
# ---------------------------------------------------------------------------

def find_deleted_post_ids(current_ids: set) -> list:
    """current_ids：本次成功抓取到的Blogger文章集合对应的post_id集合。
    返回：本地数据库里存在、但这次抓取结果里已经不存在的post_id列表
    （按post_id排序，保证确定性输出顺序，方便日志/测试对照）。

    纯集合差集运算，不判断current_ids本身是否可信——"什么时候允许执行
    删除"由调用方sync_deleted_posts()通过_deletion_sync_allowed()把关，
    不下放到这里，避免"允许删除"的判断散落在多个函数里。
    """
    existing_ids = db.get_all_post_ids()
    return sorted(existing_ids - current_ids)


def _delete_post_static_files(post_id: str, canonical_path) -> None:
    """删除一篇文章在html/下对应的全部静态文件：posts/{post_id}/整个目录
    （含media/），以及（如果有canonical_path）对应的YYYY/MM/slug.html。

    canonical_path的路径安全校验复用canonical_static_target()——跟
    render_post()生成这个文件时是同一份边界检查，不需要另写一遍。
    posts/{post_id}/这一侧单独做一次resolve()+relative_to()确认落点确实
    在POSTS_DIR内部：post_id只可能来自slugify()的输出，结构上不含路径
    穿越字符，这里纯粹是防御性的第二道保险，不是假设它真的会失败。

    调用方（sync_deleted_posts()）必须在这个函数成功返回之后才删除对应的
    数据库记录，顺序不能反过来：如果先删数据库记录、这一步再失败或进程被
    中断，磁盘上会遗留一个数据库已经不认识、但依然能被原URL直接访问到的
    "僵尸文章"——不只是脏数据，是真的还在线上、还会被搜索引擎继续抓到，
    而且因为html/这边"看起来没有变化"，git不会检测到任何差异，这个僵尸
    文件会永远留在仓库里、永远不会通过下一次发布自动消失。反过来，这一步
    成功但数据库记录还没删（两步之间进程被杀）最坏后果只是首页/归档暂时
    还列着一条点进去404的死链接，下次抓取会重新判定这个post_id仍然待删除
    并自动重试、自愈——明显是更安全的失败模式，这也是本函数存在、不把
    "删文件"和"删数据库记录"揉进同一步的原因。
    """
    post_dir = POSTS_DIR / post_id
    if post_dir.exists():
        post_dir.resolve().relative_to(POSTS_DIR.resolve())
        shutil.rmtree(post_dir)

    static_target = canonical_static_target(canonical_path)
    if static_target is not None and static_target.exists():
        static_target.unlink()
        # 顺手清理因此变空的YYYY/MM、YYYY目录：git本来就不追踪空目录，
        # 不清理也不影响任何发布结果，只是让磁盘上的html/目录树保持干净。
        # rmdir在目录非空时抛OSError——同月/同年还有其它文章是最常见的
        # 正常情况，不是错误，静默跳过；年目录清理失败（通常是因为还有
        # 其它月份）同理静默跳过。
        for ancestor in (static_target.parent, static_target.parent.parent):
            try:
                ancestor.rmdir()
            except OSError:
                pass


def _deletion_sync_allowed(entries: list) -> bool:
    """只有entries非空时才允许执行删除同步。

    fetch_feed()请求本身失败（网络错误/HTTP错误）已经在main()里更早的
    位置直接记录失败并sys.exit(1)，走不到这里。这个检查专门防的是另一种
    更隐蔽的情况：请求"成功"了（HTTP 200，JSON也能正常解析），但feed结构
    异常、被截断，或者entry字段缺失/为空数组，解析出0篇文章——绝不能把
    "这次啥也没抓到"当成"Blogger上所有文章都被删除了"，宁可这一轮跳过
    删除同步、保留现有全部文章，等下一次抓取恢复正常再重试。这是删除同步
    最重要的安全边界。
    """
    return bool(entries)


def sync_deleted_posts(entries: list) -> list:
    """比较entries（这次成功抓取到的Blogger文章集合）跟本地数据库当前的
    post_id集合，删除本地已经不存在于Blogger的文章：先删html/下的静态
    文件，确认成功后才删数据库记录（顺序原因见_delete_post_static_files()
    文档字符串）。返回实际删除成功的[{"post_id":..., "canonical_path":...},
    ...]列表（canonical_path可能是None），供调用方拼IndexNow/Cloudflare
    缓存清除用的URL。

    安全边界：entries为空时直接返回空列表、不做任何删除，见
    _deletion_sync_allowed()。单篇文章删除过程中如果静态文件删除失败
    （比如权限问题），跳过这一篇、保留它的数据库记录，继续处理其它待删除
    文章，不因为一篇文章删除失败就让整次抓取任务失败。
    """
    if not _deletion_sync_allowed(entries):
        print("  [警告] 本次抓取到0篇文章，疑似Blogger API返回异常或数据不完整，"
              "跳过本轮删除同步（保留现有全部文章，等下次抓取恢复正常再重试）")
        return []

    current_ids = {slugify(e["id"]["$t"]) for e in entries}
    deleted_ids = find_deleted_post_ids(current_ids)
    if not deleted_ids:
        return []

    print(f"检测到{len(deleted_ids)}篇文章在Blogger已删除，开始同步删除本地镜像: {deleted_ids}")
    deleted = []
    for post_id in deleted_ids:
        canonical_path = db.get_canonical_path(post_id)
        try:
            _delete_post_static_files(post_id, canonical_path)
        except Exception as e:
            print(f"  [警告] 删除文章{post_id}的静态文件失败，本次跳过"
                  f"（数据库记录保留，下次抓取会重试）: {e}")
            continue
        db.delete_post_record(post_id)
        deleted.append({"post_id": post_id, "canonical_path": canonical_path})
        print(f"  [删除] {post_id}（canonical_path={canonical_path!r}）已从本地镜像移除")
    return deleted


def main():
    HTML_DIR.mkdir(parents=True, exist_ok=True)
    db.init_db()
    log_id = db.log_fetch_start()

    try:
        entries = fetch_all_entries()
    except Exception as e:
        msg = f"blog-mirror抓取失败（网络/feed异常/分页不完整）: {e}"
        print(msg)
        db.log_fetch_end(log_id, "error", detail=str(e))
        notify(f"⚠️ {msg}")
        sys.exit(1)
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

    # 删除同步：必须在上面entries处理完、下面render_index()/render_seo_files()
    # 重新生成首页/sitemap之前执行，这样"Blogger已删除的文章"能在同一轮抓取里
    # 从html/、数据库、首页、sitemap、归档、排行榜、ZIP缓存签名里一次性消失
    # （后几项都是从posts表现场重新计算/生成，不需要额外代码，见
    # sync_deleted_posts()文件头的架构说明）。
    deleted = sync_deleted_posts(entries)
    deleted_urls = [f"{MIRROR_ROOT_URL}/{d['canonical_path']}.html" for d in deleted if d["canonical_path"]]

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
    # 删除的文章URL跟新增/变更的URL一起，走同一套IndexNow提交/Cloudflare缓存
    # 清除机制——两边都只按URL处理，不关心URL"为什么"变化，不需要为删除
    # 场景另写一套。IndexNow：告诉搜索引擎这个URL需要重新抓取（会发现已经
    # 404/410，进而从索引移除）。Cloudflare缓存清除：避免mirror.foxzen.me
    # 边缘节点在文章已删除后仍继续返回旧的缓存内容。
    notify_urls = changed_urls + deleted_urls
    if notify_urls:
        _submit_indexnow(notify_urls)
        _purge_cloudflare_cache(notify_urls)
    db.log_fetch_end(log_id, "ok",
                      detail=f"changed={changed_count}, 删除={len(deleted)}, 无canonical={no_canonical_count}",
                      post_count=len(entries))
    print(f"完成。共 {len(entries)} 篇，{changed_count} 篇有更新，{len(deleted)} 篇已删除，{no_canonical_count} 篇无法解析canonical_path。")


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
<title>狐斋志异 - 镜像站</title>
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
<h1>狐斋志异 - 镜像站</h1>
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
Internet Archive verification: This page is maintained by the owner of foxzen.me and backup.foxzen.me and is published to verify control of these domains for archival and removal requests.
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
