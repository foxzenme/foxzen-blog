#!/usr/bin/env python3
"""
从生产的 html/ 目录构建一份"公开静态发布产物"到 publish/，供 GitHub Pages /
Cloudflare Pages 使用（第十六/十七节静态灾备）。

核心原则是白名单而不是黑名单：只有下面 ALLOWED_TOP_LEVEL_FILES /
_is_allowed_top_level_dir() 明确认可的内容才会被复制进 publish/，
其他任何东西（哪怕以后 html/ 里多了一个新文件）默认都不会被带进去，
不依赖"排除掉危险的"这种事后补漏的思路。

第一阶段Pages的定位是"稳定的静态文章浏览与导航"，不是把Flask的搜索/下载/
导出/刷新这些动态功能硬搬过去——所以这里会从publish版的index.html里去掉
<script src="/static/index.js">这一行：那份JS的所有功能都要打
/api/search、/api/refresh 这些Flask接口，纯静态环境下这些请求必然失败，
留着只会让访客看到一堆点了没反应还弹"请求失败"的按钮。index.html本身在
服务端渲染时已经内嵌了一份服务端生成的文章列表(#fallback-list)作为兜底，
去掉这个<script>之后，用户看到的就是这份本来就存在、内容真实有效的静态列表。

用法:
    python3 publish_build.py --host github.foxzen.me
    python3 publish_build.py --host cf.foxzen.me
"""
import argparse
import html.parser
import json
import re
import shutil
from pathlib import Path

BASE_DIR = Path(__file__).parent
HTML_DIR = BASE_DIR / "html"
DEFAULT_OUTPUT_DIR = BASE_DIR / "publish"
PAGES_JS_SOURCE = BASE_DIR / "static_pages" / "pages-index.js"

MIRROR_ROOT_URL = "https://mirror.foxzen.me"

# 明确允许原样复制的顶层文件（白名单）。不在这个列表里的顶层文件一律不进publish/。
ALLOWED_TOP_LEVEL_FILES = {
    "index.html",
    "robots.txt",
    "sitemap.xml",
}

# 明确允许复制的顶层目录（白名单）。判断逻辑见 _is_allowed_top_level_dir()。
ALLOWED_STATIC_DIR_NAMES = {"images", "posts"}

INDEX_JS_SCRIPT_TAG = '<script src="/static/index.js"></script>'
PAGES_JS_SCRIPT_TAG = '<script src="/pages-index.js"></script>'

# 插入到#app容器之前的纯静态搜索工具栏。data-role属性是pages-index.js
# 读取表单值用的钩子，不涉及任何/api/*请求。
SEARCH_TOOLBAR_HTML = """<div id="pages-search-toolbar" style="margin-bottom:20px;display:flex;gap:8px;flex-wrap:wrap;align-items:center;">
<input type="text" data-role="q" placeholder="搜索标题或正文..." style="flex:1;min-width:200px;padding:6px;">
<input type="text" data-role="tag" placeholder="标签筛选" style="width:120px;padding:6px;">
<input type="date" data-role="from" style="padding:6px;">
<input type="date" data-role="to" style="padding:6px;">
<button data-role="search-btn">搜索</button>
<select data-role="page-size" style="padding:6px;">
<option value="10">每页10篇</option>
<option value="20">每页20篇</option>
<option value="50">每页50篇</option>
</select>
</div>
"""


class _PostMetaExtractor(html.parser.HTMLParser):
    """从fetch_blog.py渲染好的 posts/<id>/index.html 里提取标题/发布日期/
    标签/正文纯文本——只解析已经公开存在的静态HTML本身，不查数据库，
    所以Pages构建可以完全独立于data/blog.db（第十节要求）。

    用深度计数而不是简单正则，是因为 .content 这个div内部本来就可能嵌套
    任意多层子div（文章正文本身的排版），正则没法可靠匹配到对应的闭合标签。
    """

    def __init__(self):
        super().__init__()
        self.title = ""
        self.published = ""
        self.tags = []
        self.text_parts = []
        self._in_title = False
        self._in_meta = False
        self._meta_depth = 0
        self._in_tags = False
        self._tags_depth = 0
        self._in_content = False
        self._content_depth = 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        cls = attrs.get("class", "")

        if tag == "title":
            self._in_title = True

        if tag == "div" and cls == "meta":
            self._in_meta = True
            self._meta_depth = 1
        elif self._in_meta and tag == "div":
            self._meta_depth += 1

        if tag == "div" and cls == "tags":
            self._in_tags = True
            self._tags_depth = 1
        elif self._in_tags and tag == "div":
            self._tags_depth += 1
        if self._in_tags and tag == "a":
            m = re.search(r"[?&]tag=([^&]+)", attrs.get("href", ""))
            if m:
                from urllib.parse import unquote
                self.tags.append(unquote(m.group(1)))

        if tag == "div" and cls == "content":
            self._in_content = True
            self._content_depth = 1
        elif self._in_content and tag == "div":
            self._content_depth += 1

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        if self._in_meta and tag == "div":
            self._meta_depth -= 1
            if self._meta_depth <= 0:
                self._in_meta = False
        if self._in_tags and tag == "div":
            self._tags_depth -= 1
            if self._tags_depth <= 0:
                self._in_tags = False
        if self._in_content and tag == "div":
            self._content_depth -= 1
            if self._content_depth <= 0:
                self._in_content = False

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        if self._in_meta and not self.published:
            m = re.search(r"\d{4}-\d{2}-\d{2}", data)
            if m:
                self.published = m.group(0)
        if self._in_content:
            self.text_parts.append(data)

    def plain_text(self) -> str:
        text = " ".join(self.text_parts)
        return re.sub(r"\s+", " ", text).strip()


def _is_allowed_top_level_dir(name: str) -> bool:
    """白名单目录判断：
    - images/、posts/：文章媒体与正文，明确需要。
    - 404/：GitHub Pages要求根目录404.html，这里额外处理（见build()），
      但原始 404/index.html 本身也允许原样保留（首页彩蛋链接 /404/ 指向它）。
    - 纯数字目录（1/ 2/ 3/...）：文章短号跳转页，明确需要。
    - YYYY（4位数字）目录：canonical静态文章目录（fetch_blog.py新增的
      /YYYY/MM/slug.html机制），明确需要。

    不在这些规则内的目录一律不进publish/——包括 foxzen/（foxzen.me专属页面，
    不属于github.foxzen.me/cf.foxzen.me这两个"文章镜像"站点的职责范围）。
    """
    if name in ALLOWED_STATIC_DIR_NAMES:
        return True
    if name == "404":
        return True
    if name.isdigit():
        return True
    return False


def _rewrite_hostname(text: str, host: str) -> str:
    """把生产内容里硬编码的mirror.foxzen.me换成目标Pages域名。
    只替换协议+域名部分，不改路径结构——canonical_path本身格式不变。
    """
    return text.replace(MIRROR_ROOT_URL, f"https://{host}")


def _build_index_html(host: str) -> str:
    content = (HTML_DIR / "index.html").read_text(encoding="utf-8")
    # 生产的static/index.js全靠/api/*，纯静态环境下必然失败，换成只做浏览器
    # 本地搜索/筛选/分页的pages-index.js（第十一节），并在#app前插入一个
    # 静态搜索工具栏——原有的服务端渲染fallback-list保留，JS加载完成后
    # 会在其基础上接管展示。
    content = content.replace(INDEX_JS_SCRIPT_TAG, PAGES_JS_SCRIPT_TAG)
    content = content.replace('<div id="app">', SEARCH_TOOLBAR_HTML + '<div id="app">')
    return content


def _extract_post_metadata(post_html: str) -> dict:
    parser = _PostMetaExtractor()
    parser.feed(post_html)
    return {
        "title": parser.title.strip(),
        "date": parser.published,
        "tags": parser.tags,
        "text": parser.plain_text(),
    }


def _find_public_url(output_dir: Path, post_id: str, post_content: str) -> str:
    """优先用canonical静态URL（/YYYY/MM/slug.html），因为这是mirror生产站
    也在用的正式地址；只有解析不出canonical_path（fetch_blog.py没能从
    permalink提取出年/月/slug）时才退回 /posts/<id>/。

    判断"哪个YYYY/MM/slug.html对应这篇文章"用字节内容比对——canonical_
    static_target()生成的静态文件跟posts/<id>/index.html本来就是同一段
    html字符串的两份拷贝（见fetch_blog.py的render_post()），不是巧合。
    当前文章数量只有十几篇，逐个比对没有性能问题。
    """
    for year_dir in sorted(p for p in output_dir.glob("[0-9][0-9][0-9][0-9]") if p.is_dir()):
        for slug_file in year_dir.glob("*/*.html"):
            if slug_file.read_text(encoding="utf-8") == post_content:
                return "/" + slug_file.relative_to(output_dir).as_posix()
    return f"/posts/{post_id}/"


def _build_search_index(output_dir: Path) -> list:
    """从已经复制进output_dir的 posts/<id>/index.html 里提取搜索索引，
    只在html/白名单内容都已经复制完之后调用——不查数据库、不读访问统计，
    只包含公开文章搜索需要的字段(title/url/date/tags/text)。
    """
    articles = []
    posts_dir = output_dir / "posts"
    if not posts_dir.exists():
        return articles
    for post_dir in sorted(posts_dir.iterdir()):
        index_file = post_dir / "index.html"
        if not index_file.exists():
            continue
        post_id = post_dir.name
        content = index_file.read_text(encoding="utf-8")
        meta = _extract_post_metadata(content)
        articles.append({
            "id": post_id,
            "title": meta["title"],
            "url": _find_public_url(output_dir, post_id, content),
            "date": meta["date"],
            "tags": meta["tags"],
            "text": meta["text"],
        })
    articles.sort(key=lambda a: a["date"], reverse=True)
    return articles


def build_publish(host: str, output_dir: Path = DEFAULT_OUTPUT_DIR) -> Path:
    """从 html/ 白名单构建一份发布产物到 output_dir，返回该目录路径。

    每次调用都会先清空 output_dir 再重新生成，保证"同样的html/内容+同样的
    host参数 => 同样的publish/"这个可重复性——不依赖上一次残留的文件。
    构建过程只读取 html/ 里的静态文件本身，不查询数据库、不读取运行时
    访问统计，不依赖当前时间（唯一算"变量"的CNAME/hostname是显式传入的参数）。
    """
    if output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True)

    for name in ALLOWED_TOP_LEVEL_FILES:
        src = HTML_DIR / name
        if not src.exists():
            continue
        text = src.read_text(encoding="utf-8")
        if name == "index.html":
            text = _build_index_html(host)
        elif name in ("robots.txt", "sitemap.xml"):
            text = _rewrite_hostname(text, host)
        (output_dir / name).write_text(text, encoding="utf-8")

    for child in HTML_DIR.iterdir():
        if not child.is_dir():
            continue
        if not _is_allowed_top_level_dir(child.name):
            continue
        shutil.copytree(child, output_dir / child.name)

    # GitHub Pages / Cloudflare Pages 都会自动把根目录的404.html当成
    # 未匹配路径的兜底页面；生产环境用的是 /404/index.html（Nginx按目录
    # 处理），这里额外复制一份到根目录，两份内容完全一致，不重新渲染。
    legacy_404 = output_dir / "404" / "index.html"
    if legacy_404.exists():
        shutil.copy2(legacy_404, output_dir / "404.html")

    CNAME = output_dir / "CNAME"
    CNAME.write_text(host + "\n", encoding="utf-8")

    if PAGES_JS_SOURCE.exists():
        shutil.copy2(PAGES_JS_SOURCE, output_dir / "pages-index.js")

    articles = _build_search_index(output_dir)
    (output_dir / "search-index.json").write_text(
        json.dumps({"articles": articles}, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )

    return output_dir


class PublishVerificationError(RuntimeError):
    """publish/产物没有通过安全/完整性检查时抛出，携带全部发现的问题
    （不是只报第一个），方便本地和CI一次性看到所有需要修的地方。"""


_DANGEROUS_SUFFIXES = (
    ".db", ".sqlite", ".sqlite3", ".env", ".pem",
    ".key", ".p12", ".pfx", ".secret", ".token", ".py",
)
_DANGEROUS_NAMES = {"id_rsa", "id_ed25519", "foxzen-download-admin.html"}
_REQUIRED_FILES = ("index.html", "404.html", "robots.txt", "sitemap.xml", "CNAME",
                    "search-index.json", "pages-index.js")

# search-index.json里每条记录只允许出现这些字段——如果以后有人不小心往
# _build_search_index()里加了别的字段（比如手滑传入了visitor_key），
# 这里会直接拒绝通过，而不是靠人工审查发现。
_ALLOWED_ARTICLE_FIELDS = {"id", "title", "url", "date", "tags", "text"}


def verify_publish(output_dir: Path, host: str) -> None:
    """对已经构建好的publish/做一次面向路径名的安全/完整性检查，在打包成
    Pages artifact之前拦截问题——不依赖人工目视检查一遍文件列表。

    这里只做"文件名/路径名"层面的检查（危险后缀、必需文件是否存在、
    hostname是否正确），不检查文章正文内容——文章正文里出现password/token
    这类技术词汇是正常内容，不该被当成危险信号（区分见test_publish_build.py
    里 test_article_body_with_password_token_words_not_treated_as_secret）。
    """
    errors = []

    for name in _REQUIRED_FILES:
        if not (output_dir / name).exists():
            errors.append(f"缺少必需文件: {name}")

    if (output_dir / "data").exists():
        errors.append("存在不应出现的 data/ 目录")

    has_article = any((output_dir / "posts").glob("*/index.html")) or any(
        re.match(r"^\d{4}$", d.name) for d in output_dir.iterdir() if d.is_dir()
    )
    if not has_article:
        errors.append("没有找到任何文章静态HTML（posts/<id>/index.html 或 YYYY/MM/slug.html）")

    for p in output_dir.rglob("*"):
        if not p.is_file():
            continue
        if p.suffix in _DANGEROUS_SUFFIXES or p.name in _DANGEROUS_NAMES:
            errors.append(f"发现危险文件: {p.relative_to(output_dir)}")

    for name in ("robots.txt", "sitemap.xml", "CNAME"):
        f = output_dir / name
        if not f.exists():
            continue
        text = f.read_text(encoding="utf-8")
        if MIRROR_ROOT_URL in text:
            errors.append(f"{name} 仍然包含 mirror.foxzen.me，未正确替换为 {host}")
        if name != "CNAME" and host not in text:
            errors.append(f"{name} 没有包含目标hostname {host}")

    index_file = output_dir / "search-index.json"
    if index_file.exists():
        try:
            index_data = json.loads(index_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            errors.append(f"search-index.json 不是合法JSON: {e}")
            index_data = None
        if index_data is not None:
            articles = index_data.get("articles")
            if not isinstance(articles, list) or not articles:
                errors.append("search-index.json 里没有任何文章记录")
            else:
                for a in articles:
                    extra_fields = set(a.keys()) - _ALLOWED_ARTICLE_FIELDS
                    if extra_fields:
                        errors.append(f"search-index.json 记录出现不允许的字段: {extra_fields}")
                    for field in ("title", "url", "date"):
                        if not a.get(field):
                            errors.append(f"search-index.json 记录缺少必需字段: {field} (id={a.get('id')})")
                    if MIRROR_ROOT_URL in str(a.get("url", "")):
                        errors.append(f"search-index.json 的url字段仍写死了mirror.foxzen.me (id={a.get('id')})")

    if errors:
        raise PublishVerificationError("；".join(errors))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True, help="目标Pages域名，例如 github.foxzen.me 或 cf.foxzen.me")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT_DIR), help="输出目录，默认 publish/")
    args = parser.parse_args()

    output_dir = build_publish(args.host, Path(args.output))
    verify_publish(output_dir, args.host)
    file_count = sum(1 for _ in output_dir.rglob("*") if _.is_file())
    print(f"已构建并通过安全检查 {output_dir}（host={args.host}，共{file_count}个文件）")


if __name__ == "__main__":
    main()
