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
import base64
import html.parser
import json
import mimetypes
import re
import shutil
import zipfile
from pathlib import Path

BASE_DIR = Path(__file__).parent
HTML_DIR = BASE_DIR / "html"
DEFAULT_OUTPUT_DIR = BASE_DIR / "publish"
PAGES_JS_SOURCE = BASE_DIR / "static_pages" / "pages-index.js"
DOWNLOAD_JS_SOURCE = BASE_DIR / "static_pages" / "pages-download.js"
# 固定版本的JSZip，随publish artifact一起发布，不在页面里引用任何CDN——
# github.foxzen.me/cf.foxzen.me必须在VPS完全不可用时也能使用下载功能，
# 运行时依赖外部CDN跟这个目标矛盾。这份文件是构建时一次性从上游下载后
# 提交进仓库的"vendored"副本，不是页面运行时的网络依赖。
JSZIP_VENDOR_SOURCE = BASE_DIR / "static_pages" / "vendor" / "jszip.min.js"

MIRROR_ROOT_URL = "https://mirror.foxzen.me"

# 首页品牌文案的唯一权威定义跟fetch_blog.py的INDEX_TEMPLATE保持字面一致——
# 如果那边的品牌文案再改，这里也要同步改。之所以在这里单独重复一份常量
# （而不是从fetch_blog.py导入），是因为publish_build.py明确设计成不依赖
# fetch_blog.py/db.py（见文件头docstring："完全独立于data/blog.db"），
# 只读取html/里已经生成好的静态文件本身。
BRAND_HEADING = "狐斋志异 - 镜像站"

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
JSZIP_SCRIPT_TAG = '<script src="/jszip.min.js"></script>'
DOWNLOAD_JS_SCRIPT_TAG = '<script src="/pages-download.js"></script>'

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

# 下载/离线导出工具栏，跟mirror首页的按钮排布保持一致的用户体验。全部
# 五个都是<button>（不是<a>），触发逻辑由pages-download.js绑定：
# 全站/全部导出直接跳转到构建时预生成的静态zip；按标签导出需要读当前
# 标签筛选框的值现场拼URL；已勾选两个走JSZip浏览器端现场打包。
# data-role是pages-download.js读取的钩子，不涉及任何/api/*请求。
DOWNLOAD_TOOLBAR_HTML = """<div id="pages-download-toolbar" style="margin-bottom:20px;padding:12px 16px;background:#f7f7f7;border-radius:8px;">
<div style="font-size:0.9em;color:#666;margin-bottom:8px;">下载 / 离线导出（完全由本站静态文件生成，不依赖任何其他服务器）</div>
<div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center;">
<button type="button" data-role="download-all-btn">打包下载全站</button>
<button type="button" data-role="download-selected-btn">下载已勾选</button>
<button type="button" data-role="export-selected-btn">导出离线版(已勾选)</button>
<button type="button" data-role="export-tag-btn">导出离线版(当前标签)</button>
<button type="button" data-role="export-all-btn">导出离线版(全部)</button>
</div>
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



# fetch_blog.py 的 render_index()/_href_for() 只要一篇文章有 canonical_path
# 就无条件写 /YYYY/MM/slug.html，从不检查这个静态文件在html/里是否真的
# 生成过（mirror.foxzen.me用不着关心这个——Flask的canonical_post_page路由
# 是按canonical_path查数据库直接渲染，文件存不存在无所谓）。但纯静态托管的
# Pages只能靠真实文件，这个正则就是用来在index.html里找出"点击排行榜/
# 下载排行榜/fallback-list"里长得像canonical文章链接的<a>标签——只匹配
# 站内根相对路径 + /YYYY/MM/xxx.html这个精确形状，天然不会碰到首页链接"/"、
# 图片链接、"其他入口"区块的外部https链接、/404/彩蛋链接。
_ARTICLE_HREF_PATTERN = re.compile(
    r'href="(/\d{4}/\d{2}/[^"]+\.html)"([^>]*)>([^<]*)</a>'
)


def _fix_article_hrefs(content: str, output_dir: Path) -> str:
    """把index.html文本里"看起来是canonical文章链接"的href，替换成
    _find_public_url()验证过的真实地址——同一个post_id如果对应的canonical
    静态文件确实存在就保留canonical地址，不存在就退回到确认存在的
    /posts/<id>/，跟search-index.json用的是完全同一套判断逻辑，不另外
    发明一套"猜测"规则。

    用锚文本（文章标题）反查是哪篇文章：render_index()生成的锚文本就是
    posts.title本身，跟_extract_post_metadata()从posts/<id>/index.html里
    解析出的标题是同一个字段，天然可以拿来做join key，不需要读数据库。
    比对前统一做.strip()——_extract_post_metadata()对<title>标签内容会
    strip()，但index.html锚文本前后可能保留了模板里原有的空白（例如
    "...内核浏览器 </a>"这种Blogger标题自带的尾随空格），不strip会导致
    明明是同一篇文章却匹配不上、被误判成"找不到对应文章"。

    如果标题在articles里找不到、或者两篇文章标题完全相同导致无法唯一
    确定是哪一篇，一律保留原href不动——宁可继续404，也不要猜。
    """
    articles = _build_search_index(output_dir)
    title_counts = {}
    title_to_url = {}
    for a in articles:
        title_counts[a["title"]] = title_counts.get(a["title"], 0) + 1
        title_to_url[a["title"]] = a["url"]

    def _replace(m: re.Match) -> str:
        original_href, rest_attrs, raw_title = m.group(1), m.group(2), m.group(3)
        title = raw_title.strip()
        if title_counts.get(title) != 1:
            if title in title_counts:
                print(f"  [警告] 首页文章链接标题不唯一，跳过修正: {title!r}")
            else:
                print(f"  [警告] 首页文章链接找不到对应文章，跳过修正: {title!r} (href={original_href})")
            return m.group(0)
        real_url = title_to_url[title]
        return f'href="{real_url}"{rest_attrs}>{raw_title}</a>'

    return _ARTICLE_HREF_PATTERN.sub(_replace, content)


_TITLE_TAG_PATTERN = re.compile(r"<title>.*?</title>", re.DOTALL)
_H1_TAG_PATTERN = re.compile(r"<h1>.*?</h1>", re.DOTALL)


def _normalize_brand_heading(content: str) -> str:
    """把html/index.html顶部的<title>/<h1>规范化成当前品牌文案。

    存在原因：html/是从生产服务器下载下来的静态快照，可能停留在
    fetch_blog.py的INDEX_TEMPLATE品牌文案修改之前的旧版本（这个仓库里
    发生过不止一次），而Pages构建流程明确设计成"只读html/现有内容，
    不重新跑fetch_blog.py"（不依赖VPS/数据库）。如果不在这里补一步，
    品牌文案改了代码却上不了线，除非专门重新在服务器上跑一次抓取。

    只精确替换首页最前面那一个<title>/<h1>标签（count=1），不做全局
    字符串替换——文章正文、其他区块如果碰巧提到品牌旧名字不受影响；
    如果<title>/<h1>已经是当前品牌，替换结果跟原文完全一样，天然幂等。
    """
    content = _TITLE_TAG_PATTERN.sub(f"<title>{BRAND_HEADING}</title>", content, count=1)
    content = _H1_TAG_PATTERN.sub(f"<h1>{BRAND_HEADING}</h1>", content, count=1)
    return content


def _build_index_html(host: str, output_dir: Path) -> str:
    content = (HTML_DIR / "index.html").read_text(encoding="utf-8")
    content = _normalize_brand_heading(content)
    # 生产的static/index.js全靠/api/*，纯静态环境下必然失败，换成只做浏览器
    # 本地搜索/筛选/分页的pages-index.js（第十一节），并在#app前插入一个
    # 静态搜索工具栏——原有的服务端渲染fallback-list保留，JS加载完成后
    # 会在其基础上接管展示。
    content = content.replace(
        INDEX_JS_SCRIPT_TAG,
        PAGES_JS_SCRIPT_TAG + "\n" + JSZIP_SCRIPT_TAG + "\n" + DOWNLOAD_JS_SCRIPT_TAG,
    )
    content = content.replace('<div id="app">', SEARCH_TOOLBAR_HTML + DOWNLOAD_TOOLBAR_HTML + '<div id="app">')
    # 点击排行榜/下载排行榜/fallback-list三处都用_href_for()同一套逻辑生成
    # canonical链接，这里统一修正，不用区分是哪个区块。必须在html/的目录
    # （posts/、YYYY/等）已经复制进output_dir之后才能调用，见build_publish()
    # 里的调用顺序。
    content = _fix_article_hrefs(content, output_dir)
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


_OWN_PERMALINK_PATTERN = re.compile(r'<a class="discuss-btn" href="([^"]+)"')
_ANCHOR_OPEN_TAG_PATTERN = re.compile(r'<a\b[^>]*>')


def _fix_cross_post_content_links(output_dir: Path) -> None:
    """把文章正文里"指向本站另一篇文章的Blogger permalink"改写成当前host
    下经过_find_public_url()验证的真实静态地址，同时保证一系列"绝不能碰"
    的例外：

    - "💬 到主站参与讨论"按钮（class="discuss-btn"）用的就是本文自己的
      Blogger permalink，语义上不是"引用别的文章"，必须原样保留——这里
      直接跳过任何带这个class的<a>标签，不去解析它的href。
    - 每篇文章自己的permalink（不管出现在哪里）也不重写——它不是"指向
      另一篇文章"，天然被下面"跳过等于own_permalink的href"这条规则排除。
    - 指向Blogger上其他内容（非本站已抓取文章、外部链接、页面而非文章）
      的permalink，在permalink_to_url里找不到对应项，原样保留，不猜测。

    "本站另一篇文章"的判定：每篇文章自己的discuss-btn href就是它在Blogger
    的permalink，这个值已经明明白白写在它自己的posts/<id>/index.html里，
    不需要读数据库——用这个字段反过来建"permalink -> 这篇文章在当前host下
    的真实地址"的映射表，跟上一轮排行榜/列表href修复用标题做join key是
    同一个思路，只是这次的key更精确（permalink天然唯一，不像标题可能重复）。

    必须在html/的目录（posts/、YYYY/等）已经复制进output_dir之后调用，
    因为要用到_find_public_url()对真实文件存在性的判断。修改后会把同一份
    新内容同步写回posts/<id>/index.html和它对应的YYYY/MM/slug.html
    （如果存在），保持fetch_blog.py render_post()原有的"两份拷贝字节一致"
    这个不变式。
    """
    posts_dir = output_dir / "posts"
    if not posts_dir.exists():
        return

    post_entries = []
    permalink_to_url = {}
    for post_dir in sorted(posts_dir.iterdir()):
        index_file = post_dir / "index.html"
        if not index_file.exists():
            continue
        post_id = post_dir.name
        content = index_file.read_text(encoding="utf-8")
        m = _OWN_PERMALINK_PATTERN.search(content)
        own_permalink = m.group(1) if m else None
        real_url = _find_public_url(output_dir, post_id, content)
        canonical_dup = (
            output_dir / real_url.lstrip("/")
            if re.match(r"^/\d{4}/\d{2}/", real_url) else None
        )
        post_entries.append((post_id, index_file, canonical_dup, content, own_permalink))
        if own_permalink:
            permalink_to_url[own_permalink] = real_url

    for post_id, index_file, canonical_dup, content, own_permalink in post_entries:
        def _rewrite_tag(match, _own=own_permalink):
            tag_text = match.group(0)
            if 'class="discuss-btn"' in tag_text:
                return tag_text
            href_match = re.search(r'href="([^"]+)"', tag_text)
            if not href_match:
                return tag_text
            href = href_match.group(1)
            if href == _own:
                return tag_text
            real_url = permalink_to_url.get(href)
            if real_url is None:
                return tag_text
            return tag_text.replace(f'href="{href}"', f'href="{real_url}"', 1)

        new_content = _ANCHOR_OPEN_TAG_PATTERN.sub(_rewrite_tag, content)
        if new_content == content:
            continue
        index_file.write_text(new_content, encoding="utf-8")
        if canonical_dup is not None and canonical_dup.exists():
            canonical_dup.write_text(new_content, encoding="utf-8")


# ---------------------------------------------------------------------------
# 离线standalone版本 + 固定范围（全站/全部/按标签）静态下载产物
#
# 只处理"构建时就已经知道范围"的三类：打包下载全站、导出离线版(全部)、
# 导出离线版(按标签)——范围在构建时是有限、已知的集合，可以直接预生成
# 静态文件。"下载已勾选"/"导出离线版(已勾选)"这两个是运行时任意组合，
# 组合数是指数级的，不可能在构建时穷举，交给浏览器端pages-download.js
# 用JSZip现场从已经公开的静态文件里现拼，见static_pages/pages-download.js。
#
# 这里的转换逻辑（剥GA/完读特效脚本块、图片base64内联、插入来源信息条）
# 移植自app.py的_inline_post_as_base64()，但特意不import app.py——
# 那样会把Flask整条依赖链拖进这个明确设计成"不依赖Flask/DB"的构建脚本。
# 两边各自独立实现，唯一的联系是"逻辑意图一致"，不是共享代码，这是本轮
# 明确的取舍（不为了100%代码复用而破坏publish_build.py的独立性）。
# ---------------------------------------------------------------------------

_GA_BLOCK_RE = re.compile(r"<!-- GA_START -->.*?<!-- GA_END -->\s*", re.DOTALL)
_FINISH_READ_BLOCK_RE = re.compile(r"<!-- FINISH_READ_START -->.*?<!-- FINISH_READ_END -->\s*", re.DOTALL)
_BACK_LINK_HTML = ('<a class="back" href="/" onclick="if (history.length > 1) '
                    '{ history.back(); return false; }">&larr; 返回目录</a>')
_META_PRECISE_RE = re.compile(r'<div class="meta-precise">(.*?)</div>')
_UPDATED_PRECISE_RE = re.compile(r"最后修改：([^·<]+)")

# 标签名可能含逗号、中文、空格（真实数据里出现过"理念，备份方式，计算机知识"
# 这种标签），拿去做zip文件名之前必须清洗——字符类跟app.py::_safe_filename()
# 完全一致，pages-download.js里的safeTagFilename()也要跟这条规则严格对齐，
# 否则浏览器现场拼的URL和构建时生成的文件名会对不上（互相印证靠两边各自的
# 回归测试，见test_publish_build.py）。同一份字符类下面_safe_article_filename()
# 也复用——跟app.py::_safe_filename()是同一条SAFE_FILENAME_RE。
_TAG_FILENAME_UNSAFE_RE = re.compile(r'[\\/:*?"<>|]')


def _safe_tag_filename(tag: str) -> str:
    name = _TAG_FILENAME_UNSAFE_RE.sub("_", tag).strip()
    name = re.sub(r"\s+", " ", name)
    return name or "untitled"


def _safe_article_filename(title: str, max_len: int = 80) -> str:
    """跟app.py::_safe_filename()逐行对齐的纯字符串清洗规则（字符类/去空白/
    截断长度全部一致），故意不import app.py复用——那样会把Flask整条依赖链
    拖进这个明确设计成"不依赖Flask/DB"的构建脚本（见文件头docstring）。
    两边各自独立实现，一致性靠test_publish_build.py的回归测试互相印证，
    跟_safe_tag_filename/pages-download.js::safeTagFilename()是同一个思路。
    """
    name = _TAG_FILENAME_UNSAFE_RE.sub("_", title).strip()
    name = re.sub(r"\s+", " ", name)
    return name[:max_len] if name else "untitled"


def _inline_media_as_base64(html_text: str, post_id: str, media_dir: Path) -> str:
    img_src_re = re.compile(rf'src="(?:/posts/{re.escape(post_id)}/)?media/([^"]+)"')

    def _replace(m: re.Match) -> str:
        filename = m.group(1)
        file_path = media_dir / filename
        if not file_path.exists():
            return m.group(0)
        mime, _ = mimetypes.guess_type(filename)
        mime = mime or "application/octet-stream"
        data = base64.b64encode(file_path.read_bytes()).decode("ascii")
        return f'src="data:{mime};base64,{data}"'

    return img_src_re.sub(_replace, html_text)


def _standalone_source_note_html(source_url: str | None, updated: str) -> str:
    updated_text = updated or "未知"
    if source_url:
        source_line = f'本文镜像自 <a href="{source_url}">{source_url}</a>'
    else:
        source_line = "本文镜像自主站（原始地址暂缺，请在主站搜索标题核对）"
    return (
        '<div style="border-bottom:1px solid #ddd;padding-bottom:12px;margin-bottom:20px;'
        'font-size:0.85em;color:#666;">'
        f'{source_line}<br>最后修改时间：{updated_text}'
        '</div>'
    )


def _render_standalone_html(post_html: str, post_id: str, media_dir: Path) -> str:
    """把一篇已经渲染好的posts/<id>/index.html转成离线单文件版：
    图片base64内联、去掉GA/完读特效脚本块、去掉"返回目录"链接（离线文件
    点它没有意义）、插入来源信息条。source_url/updated不查数据库，直接从
    这篇文章自己的discuss-btn href和.meta-precise文本里解析——这两个值
    本来就已经原样公开写在HTML里，不是需要额外权限才能拿到的信息。
    """
    html_text = _GA_BLOCK_RE.sub("", post_html)
    html_text = _FINISH_READ_BLOCK_RE.sub("", html_text)
    html_text = _inline_media_as_base64(html_text, post_id, media_dir)
    html_text = html_text.replace(_BACK_LINK_HTML, "")

    permalink_match = _OWN_PERMALINK_PATTERN.search(post_html)
    source_url = permalink_match.group(1) if permalink_match else None
    meta_precise_match = _META_PRECISE_RE.search(post_html)
    updated = ""
    if meta_precise_match:
        updated_match = _UPDATED_PRECISE_RE.search(meta_precise_match.group(1))
        if updated_match:
            updated = updated_match.group(1).strip()

    note = _standalone_source_note_html(source_url, updated)
    if '<div class="content">' in html_text:
        html_text = html_text.replace('<div class="content">', note + '<div class="content">', 1)
    else:
        html_text = note + html_text
    return html_text


def _build_standalone_articles(output_dir: Path) -> dict:
    """给每篇文章生成一份standalone/<id>.html，返回{post_id: 根相对URL}。

    必须在_fix_cross_post_content_links()之后调用——这样standalone版本里
    "本站另一篇文章"的引用也是修正后的相对地址，不是残留的Blogger permalink。
    """
    posts_dir = output_dir / "posts"
    if not posts_dir.exists():
        return {}
    standalone_dir = output_dir / "standalone"
    standalone_dir.mkdir(parents=True, exist_ok=True)
    result = {}
    for post_dir in sorted(posts_dir.iterdir()):
        index_file = post_dir / "index.html"
        if not index_file.exists():
            continue
        post_id = post_dir.name
        post_html = index_file.read_text(encoding="utf-8")
        standalone_html = _render_standalone_html(post_html, post_id, post_dir / "media")
        (standalone_dir / f"{post_id}.html").write_text(standalone_html, encoding="utf-8")
        result[post_id] = f"/standalone/{post_id}.html"
    return result


def _media_files_for(output_dir: Path, post_id: str) -> list:
    media_dir = output_dir / "posts" / post_id / "media"
    if not media_dir.exists():
        return []
    return sorted(f.name for f in media_dir.iterdir() if f.is_file())


def _zip_arcname_for_article(article: dict) -> str:
    """standalone(base64内联)zip里每篇文章的条目名，规则跟app.py::
    _zip_arcname_for()完全对齐（mirror"导出离线版"用的就是这个函数）：
    年/月/<安全标题>.html——年/月来自article["date"]（"YYYY-MM-DD"，跟
    posts.published同一语义来源，都是_extract_post_metadata()从"发布于
    YYYY-MM-DD"这行文本解析出来的），文件名来自标题清洗，不用
    canonical地址/Blogger slug/post_id拼路径。

    这里不再依赖article["url"]是不是canonical地址：'网页canonical URL
    存不存在'（受_find_public_url()能否在output_dir里找到匹配的
    /YYYY/MM/slug.html静态文件影响，本地html/快照没有这类文件时就是
    fallback地址）和'下载归档内部文件名应该是什么'是两个独立概念——即使
    当前html/快照完全没有canonical静态文件，归档命名依然按发布日期+标题
    稳定生成，不受影响。

    这里只返回"理想"文件名，不处理碰撞——碰撞消解统一交给
    _dedupe_zip_arcname()，跟app.py::_zip_arcname_for()把两件事拆开、
    但_write_standalone_zip()里合起来调用是同一个思路。pages-download.js
    里的zipArcnameForArticle()是同一套规则的JS镜像实现。
    """
    date = article.get("date") or ""
    safe_title = _safe_article_filename(article.get("title") or "")
    if len(date) >= 7 and date[4] == "-":
        return f"{date[:4]}/{date[5:7]}/{safe_title}.html"
    # 发布日期缺失/格式异常的兜底：理论上不该发生（_extract_post_metadata()
    # 解析不到日期时meta["date"]就是空字符串，属于源数据异常），发生了
    # 也不能让整个构建失败，退回不带年/月的纯标题命名。
    return f"{safe_title}.html"


def _dedupe_zip_arcname(name: str, post_id: str, used_names: set) -> str:
    """跟app.py::_zip_arcname_for()里的碰撞消解规则完全一致：撞名时在扩展名
    前插入"-{post_id}"（post_id天然全局唯一，不会自己再跟别的文章撞），
    不静默覆盖——两个不同的文章绝不会产出同一个最终归档文件名。"""
    if name in used_names:
        base, ext = name.rsplit(".", 1)
        name = f"{base}-{post_id}.{ext}"
    used_names.add(name)
    return name


# zipfile.ZipFile.write()默认按源文件的mtime写入zip条目时间戳，会导致
# "内容完全一样、只是构建时刻不同"的两次构建产出字节不同的zip——直接违反
# build_publish()文档开头就承诺的"同样的html/内容+同样的host参数 =>
# 同样的publish/"可重复性。固定成一个常量时间戳，让zip的可重复性只取决于
# 内容本身，不取决于构建发生的具体时刻。
_ZIP_FIXED_DATE_TIME = (2020, 1, 1, 0, 0, 0)


def _zip_write_bytes(zf: zipfile.ZipFile, data: bytes, arcname: str) -> None:
    info = zipfile.ZipInfo(arcname, date_time=_ZIP_FIXED_DATE_TIME)
    info.compress_type = zipfile.ZIP_DEFLATED
    # create_system/external_attr默认值依赖运行构建脚本的操作系统（Windows上
    # 是0/0，Linux上create_system会自动变成3）——不显式设置的话，同样的内容在
    # 不同平台构建出的zip字节不同，且Linux/macOS上部分unzip实现会在
    # create_system=3但external_attr=0时把解出的文件权限设成000（不可读）。
    # 显式固定成"Unix普通文件+0644权限"，让这两个字段不再依赖构建平台。
    info.create_system = 3
    info.external_attr = 0o644 << 16
    zf.writestr(info, data)


def _build_fixed_scope_zips(output_dir: Path, articles: list) -> None:
    """生成三类"构建时范围已知"的下载产物到downloads/：
    - blog-full.zip：原始文章(posts/<id>/*，相对路径图片)+首页，跟mirror
      现有"打包下载全站"内容对等，区别只是这里是构建时预生成的静态文件。
    - export-all.zip：全部文章的standalone(base64内联)版本打包。
    - export-tag/<tag>.zip：按标签的standalone版本打包。标签之间允许重叠——
      一篇文章同时属于多个标签时，会出现在每个对应标签的zip里，这是有意的：
      用户按标签导出应该拿到该标签下的完整文章集合，不该因为文章也属于别的
      标签就被排除。
    """
    downloads_dir = output_dir / "downloads"
    downloads_dir.mkdir(parents=True, exist_ok=True)
    posts_dir = output_dir / "posts"
    standalone_dir = output_dir / "standalone"

    with zipfile.ZipFile(downloads_dir / "blog-full.zip", "w", zipfile.ZIP_DEFLATED) as zf:
        for article in articles:
            post_dir = posts_dir / article["id"]
            if not post_dir.exists():
                continue
            for f in sorted(p for p in post_dir.rglob("*") if p.is_file()):
                _zip_write_bytes(zf, f.read_bytes(), f"posts/{article['id']}/{f.relative_to(post_dir).as_posix()}")
        index_file = output_dir / "index.html"
        if index_file.exists():
            _zip_write_bytes(zf, index_file.read_bytes(), "index.html")

    def _write_standalone_zip(path: Path, selected_articles: list) -> None:
        used_names = set()
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
            for article in selected_articles:
                standalone_file = standalone_dir / f"{article['id']}.html"
                if not standalone_file.exists():
                    continue
                name = _dedupe_zip_arcname(_zip_arcname_for_article(article), article["id"], used_names)
                _zip_write_bytes(zf, standalone_file.read_bytes(), name)

    _write_standalone_zip(downloads_dir / "export-all.zip", articles)

    tag_export_dir = downloads_dir / "export-tag"
    tag_export_dir.mkdir(parents=True, exist_ok=True)
    articles_by_tag = {}
    for article in articles:
        for tag in article.get("tags") or []:
            articles_by_tag.setdefault(tag, []).append(article)
    for tag, tag_articles in articles_by_tag.items():
        _write_standalone_zip(tag_export_dir / f"{_safe_tag_filename(tag)}.zip", tag_articles)


def _build_search_index(output_dir: Path, standalone_urls: dict | None = None) -> list:
    """从已经复制进output_dir的 posts/<id>/index.html 里提取搜索索引，
    只在html/白名单内容都已经复制完之后调用——不查数据库、不读访问统计，
    只包含公开文章搜索/下载需要的字段。

    standalone_urls为None时（_fix_article_hrefs()内部那次调用，只是为了
    拿title->url做首页链接修正）新增的两个字段就是空值，不影响那次调用的
    用途；真正写入search-index.json的那次调用会传入_build_standalone_
    articles()的返回值。
    """
    articles = []
    posts_dir = output_dir / "posts"
    if not posts_dir.exists():
        return articles
    standalone_urls = standalone_urls or {}
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
            "standalone_url": standalone_urls.get(post_id, ""),
            "media_files": _media_files_for(output_dir, post_id),
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

    for child in HTML_DIR.iterdir():
        if not child.is_dir():
            continue
        if not _is_allowed_top_level_dir(child.name):
            continue
        shutil.copytree(child, output_dir / child.name)

    # 文章正文里"指向本站另一篇文章"的Blogger permalink交叉引用修正，
    # 必须在目录复制之后（要用output_dir里已经存在的真实文件做校验）、
    # 在下面index.html生成之前（_build_index_html里的_build_search_index()
    # 会读取posts/<id>/index.html的内容，应该读到修正后的版本）执行。
    _fix_cross_post_content_links(output_dir)

    # 离线standalone版本必须在交叉引用修正之后生成（同样的理由：让standalone
    # 版本里的"本站另一篇文章"链接也是修正后的地址），必须在下面写入
    # search-index.json之前完成（每条记录的standalone_url字段要用到这里
    # 的返回值）。
    standalone_urls = _build_standalone_articles(output_dir)

    # index.html的生成放在目录复制之后：_build_index_html()内部要修正
    # 排行榜/fallback-list里的文章链接，需要用output_dir/posts、
    # output_dir/YYYY这些已经复制好的真实文件去验证链接是否可达
    # （见_fix_article_hrefs()），顺序不能反过来。
    for name in ALLOWED_TOP_LEVEL_FILES:
        src = HTML_DIR / name
        if not src.exists():
            continue
        text = src.read_text(encoding="utf-8")
        if name == "index.html":
            text = _build_index_html(host, output_dir)
        elif name in ("robots.txt", "sitemap.xml"):
            text = _rewrite_hostname(text, host)
        (output_dir / name).write_text(text, encoding="utf-8")

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
    if DOWNLOAD_JS_SOURCE.exists():
        shutil.copy2(DOWNLOAD_JS_SOURCE, output_dir / "pages-download.js")
    if JSZIP_VENDOR_SOURCE.exists():
        shutil.copy2(JSZIP_VENDOR_SOURCE, output_dir / "jszip.min.js")

    articles = _build_search_index(output_dir, standalone_urls)
    (output_dir / "search-index.json").write_text(
        json.dumps({"articles": articles}, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )

    # 固定范围（全站/全部/按标签）的下载产物：范围在这里已经完全确定
    # （用的就是刚写进search-index.json的这份articles列表），构建时一次性
    # 生成好，运行时只是普通静态文件下载，不需要任何JS参与。
    _build_fixed_scope_zips(output_dir, articles)

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
                    "search-index.json", "pages-index.js",
                    # 下载功能是本轮明确声明的正式功能，不是可选增强——
                    # 这几个缺一个都必须让整个构建失败，而不是悄悄发布一个
                    # 看起来正常、实际缺下载能力的Pages站点。
                    "pages-download.js", "jszip.min.js",
                    "downloads/blog-full.zip", "downloads/export-all.zip")

# search-index.json里每条记录只允许出现这些字段——如果以后有人不小心往
# _build_search_index()里加了别的字段（比如手滑传入了visitor_key），
# 这里会直接拒绝通过，而不是靠人工审查发现。
_ALLOWED_ARTICLE_FIELDS = {"id", "title", "url", "date", "tags", "text",
                           "standalone_url", "media_files"}


def _scan_zip_for_dangerous_entries(zip_path: Path, output_dir: Path) -> list:
    """打开一个zip逐条目按文件名/后缀比对危险名单——跟output_dir里裸文件
    用的是同一份_DANGEROUS_SUFFIXES/_DANGEROUS_NAMES标准，"包在zip里"
    不能成为绕过这个标准的方式。"""
    found = []
    label = zip_path.relative_to(output_dir)
    with zipfile.ZipFile(zip_path) as zf:
        for name in zf.namelist():
            base = name.rsplit("/", 1)[-1]
            if Path(base).suffix in _DANGEROUS_SUFFIXES or base in _DANGEROUS_NAMES:
                found.append(f"{label}!{name}")
    return found


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

    # 下载功能完整性：每篇文章都必须有对应的离线standalone版本，固定范围的
    # 两个zip（全站/全部）必须存在且内容干净——这几项已经在_REQUIRED_FILES/
    # 下面的zip扫描里覆盖了"存在与否"，这里补上"每篇文章都有、不是碰巧有几篇"
    # 这一层，同时确认没有可执行代码/密钥类文件混进任何一个zip。
    posts_dir_for_standalone = output_dir / "posts"
    if posts_dir_for_standalone.exists():
        for post_dir in sorted(posts_dir_for_standalone.iterdir()):
            if not (post_dir / "index.html").exists():
                continue
            standalone_file = output_dir / "standalone" / f"{post_dir.name}.html"
            if not standalone_file.exists():
                errors.append(f"缺少离线standalone版本: standalone/{post_dir.name}.html")

    for fixed_zip_name in ("downloads/blog-full.zip", "downloads/export-all.zip"):
        fixed_zip_path = output_dir / fixed_zip_name
        if fixed_zip_path.exists():
            errors.extend(f"zip内发现危险文件: {e}"
                           for e in _scan_zip_for_dangerous_entries(fixed_zip_path, output_dir))

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
                all_tags = set()
                for a in articles:
                    extra_fields = set(a.keys()) - _ALLOWED_ARTICLE_FIELDS
                    if extra_fields:
                        errors.append(f"search-index.json 记录出现不允许的字段: {extra_fields}")
                    for field in ("title", "url", "date"):
                        if not a.get(field):
                            errors.append(f"search-index.json 记录缺少必需字段: {field} (id={a.get('id')})")
                    if MIRROR_ROOT_URL in str(a.get("url", "")):
                        errors.append(f"search-index.json 的url字段仍写死了mirror.foxzen.me (id={a.get('id')})")

                    # standalone_url/media_files是下载功能依赖的字段，缺失或
                    # 指向不存在的文件必须硬失败——这是"已经声明为正式功能"的
                    # 核心产物，不接受"生成失败但静默发布"。
                    standalone_url = a.get("standalone_url")
                    if not standalone_url:
                        errors.append(f"search-index.json 记录缺少standalone_url (id={a.get('id')})")
                    elif not (output_dir / standalone_url.lstrip("/")).exists():
                        errors.append(f"standalone_url指向的文件不存在: {standalone_url} (id={a.get('id')})")
                    for media_name in a.get("media_files") or []:
                        media_path = output_dir / "posts" / str(a.get("id", "")) / "media" / media_name
                        if not media_path.exists():
                            errors.append(f"media_files列出的文件不存在: {media_name} (id={a.get('id')})")

                    all_tags.update(a.get("tags") or [])

                for tag in sorted(all_tags):
                    tag_zip = output_dir / "downloads" / "export-tag" / f"{_safe_tag_filename(tag)}.zip"
                    if not tag_zip.exists():
                        errors.append(f"缺少按标签的离线导出zip: downloads/export-tag/"
                                      f"{_safe_tag_filename(tag)}.zip (tag={tag!r})")
                    else:
                        errors.extend(f"zip内发现危险文件: {e}"
                                      for e in _scan_zip_for_dangerous_entries(tag_zip, output_dir))

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
