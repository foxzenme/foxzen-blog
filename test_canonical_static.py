#!/usr/bin/env python3
"""
针对"规范URL静态化"(canonical_static_target / render_post)的回归测试。

只测试新增的静态化逻辑本身，不联网抓Blogger、不跑main()全流程、不写生产
data/blog.db（只对它做只读连接抽样，或者复制到临时目录里操作副本）。

用法: python3 test_canonical_static.py
"""
import re
import shutil
import sqlite3
import sys
import tempfile
import traceback
from pathlib import Path

BASE_DIR = Path(__file__).parent
REAL_DB = BASE_DIR / "data" / "blog.db"

failures = []


def check(name, condition, detail=""):
    if condition:
        print(f"  [PASS] {name}")
    else:
        print(f"  [FAIL] {name} {detail}")
        failures.append(name)


def with_temp_html_dir(fn):
    """把 fetch_blog.HTML_DIR / POSTS_DIR 临时指向一个空目录，跑完自动还原，
    避免任何一次测试意外碰到真实 html/ 目录或真实 data/blog.db。
    """
    import fetch_blog
    tmp = Path(tempfile.mkdtemp(prefix="canonical_static_test_"))
    orig_html_dir = fetch_blog.HTML_DIR
    orig_posts_dir = fetch_blog.POSTS_DIR
    fetch_blog.HTML_DIR = tmp
    fetch_blog.POSTS_DIR = tmp / "posts"
    try:
        fn(tmp)
    finally:
        fetch_blog.HTML_DIR = orig_html_dir
        fetch_blog.POSTS_DIR = orig_posts_dir
        shutil.rmtree(tmp, ignore_errors=True)


def test_valid_canonical_path():
    def _run(tmp):
        import fetch_blog
        target = fetch_blog.canonical_static_target("2026/07/some-slug")
        check("合法canonical_path解析出预期路径",
              target == (tmp / "2026" / "07" / "some-slug.html").resolve(),
              f"got {target}")
    with_temp_html_dir(_run)


def test_invalid_canonical_paths_rejected():
    def _run(tmp):
        import fetch_blog
        bad_values = [
            None, "", "2026", "2026/07", "2026/07/slug/extra",
            "26/07/slug",       # 年不是4位
            "2026/7/slug",      # 月不是2位
            "abcd/07/slug",     # 年不是数字
            "2026/ab/slug",     # 月不是数字
        ]
        for v in bad_values:
            result = fetch_blog.canonical_static_target(v)
            check(f"非法canonical_path被拒绝: {v!r}", result is None, f"got {result}")
    with_temp_html_dir(_run)


def test_slug_with_dotdot_does_not_escape_html_dir():
    def _run(tmp):
        import fetch_blog
        # split("/")后slug本身是".."时，拼接成的是文件名"...html"这一个路径分量，
        # 不会被解析成上级目录跳转；这里显式验证结果确实还在HTML_DIR内部。
        target = fetch_blog.canonical_static_target("2026/07/..")
        check("slug='..'时落点仍在HTML_DIR内",
              target is not None and tmp.resolve() in target.resolve().parents,
              f"got {target}")
    with_temp_html_dir(_run)


def test_render_post_writes_identical_static_copy():
    def _run(tmp):
        import fetch_blog
        fetch_blog.render_post(
            "test-post-id", "测试标题", "2026-09-02", ["测试"],
            "<p>正文内容</p>", click_count=1, download_count=2,
            published_ts="2026-09-02T00:00:00", updated_ts="2026-09-02T00:00:00",
            finish_read_count=0, source_url="https://example.com/2026/09/test-post.html",
            canonical_path="2026/09/test-post",
        )
        post_file = tmp / "posts" / "test-post-id" / "index.html"
        static_file = tmp / "2026" / "09" / "test-post.html"
        check("posts/<id>/index.html已生成", post_file.exists())
        check("html/YYYY/MM/slug.html已生成", static_file.exists())
        if post_file.exists() and static_file.exists():
            check("两份文件字节级完全一致",
                  post_file.read_bytes() == static_file.read_bytes())
    with_temp_html_dir(_run)


def test_render_post_without_canonical_path_skips_static_file():
    def _run(tmp):
        import fetch_blog
        fetch_blog.render_post(
            "no-canonical-post", "无canonical文章", "2026-09-02", [],
            "<p>内容</p>", canonical_path=None,
        )
        post_file = tmp / "posts" / "no-canonical-post" / "index.html"
        check("没有canonical_path时posts/<id>/index.html仍正常生成", post_file.exists())
        other_entries = [p for p in tmp.iterdir() if p.name != "posts"]
        check("没有canonical_path时不产生额外静态目录", other_entries == [], f"got {other_entries}")
    with_temp_html_dir(_run)


# ---------------------------------------------------------------------------
# <link rel="canonical">：不管mirror/backup/github/cf哪个域名访问到，都
# 固定指向mirror.foxzen.me——见render_post()里canonical_url的计算逻辑。
# ---------------------------------------------------------------------------

_CANONICAL_LINK_RE = re.compile(r'<link rel="canonical" href="([^"]*)">')


def test_render_post_canonical_link_uses_canonical_path_when_available():
    def _run(tmp):
        import fetch_blog
        fetch_blog.render_post(
            "canon-post-1", "有canonical_path的文章", "2026-09-02", [],
            "<p>正文</p>", canonical_path="2026/09/some-slug",
        )
        post_html = (tmp / "posts" / "canon-post-1" / "index.html").read_text(encoding="utf-8")
        static_html = (tmp / "2026" / "09" / "some-slug.html").read_text(encoding="utf-8")
        expected = "https://mirror.foxzen.me/2026/09/some-slug.html"
        m_post = _CANONICAL_LINK_RE.search(post_html)
        m_static = _CANONICAL_LINK_RE.search(static_html)
        check("posts/<id>/index.html里canonical指向mirror.foxzen.me的规范路径",
              m_post is not None and m_post.group(1) == expected, m_post)
        check("YYYY/MM/slug.html里canonical同样指向mirror.foxzen.me的规范路径",
              m_static is not None and m_static.group(1) == expected, m_static)
    with_temp_html_dir(_run)


def test_render_post_canonical_link_falls_back_to_post_id_without_canonical_path():
    """permalink解析失败(canonical_path=None)时，/posts/<id>/是唯一真正能
    访问到这篇文章、且不会被redirect的地址，canonical自引用这个地址是对的
    （不是bug，是_href_for()/legacy_post_link()同一套"谁是权威URL"判断的
    自然延伸）。"""
    def _run(tmp):
        import fetch_blog
        fetch_blog.render_post(
            "canon-post-2", "没有canonical_path的文章", "2026-09-02", [],
            "<p>正文</p>", canonical_path=None,
        )
        post_html = (tmp / "posts" / "canon-post-2" / "index.html").read_text(encoding="utf-8")
        m = _CANONICAL_LINK_RE.search(post_html)
        check("没有canonical_path时canonical退回/posts/<id>/自引用",
              m is not None and m.group(1) == "https://mirror.foxzen.me/posts/canon-post-2/", m)
    with_temp_html_dir(_run)


def test_canonical_link_appears_exactly_once_per_copy():
    def _run(tmp):
        import fetch_blog
        fetch_blog.render_post(
            "canon-post-3", "标题", "2026-09-02", [],
            "<p>正文</p>", canonical_path="2026/09/only-once",
        )
        post_html = (tmp / "posts" / "canon-post-3" / "index.html").read_text(encoding="utf-8")
        static_html = (tmp / "2026" / "09" / "only-once.html").read_text(encoding="utf-8")
        check("posts/<id>/index.html里canonical标签只出现一次",
              len(_CANONICAL_LINK_RE.findall(post_html)) == 1,
              _CANONICAL_LINK_RE.findall(post_html))
        check("YYYY/MM/slug.html里canonical标签只出现一次",
              len(_CANONICAL_LINK_RE.findall(static_html)) == 1,
              _CANONICAL_LINK_RE.findall(static_html))
    with_temp_html_dir(_run)


def test_canonical_link_always_uses_fixed_mirror_host():
    """canonical的host来自fetch_blog.MIRROR_ROOT_URL这个写死的常量，不读取
    任何请求上下文（fetch_blog.py本身是离线脚本，运行时根本没有Flask
    request对象）——这里同时验证常量本身的值，以及各种canonical_path输入
    产出的href都以这个固定host开头，不出现localhost/内网地址/其它域名。
    """
    def _run(tmp):
        import fetch_blog
        check("MIRROR_ROOT_URL是写死的公网mirror域名，不是变量拼出来的",
              fetch_blog.MIRROR_ROOT_URL == "https://mirror.foxzen.me")

        for post_id, canonical_path in (
            ("host-check-1", "2026/01/a"),
            ("host-check-2", None),
            ("host-check-3", "2099/12/z"),
        ):
            fetch_blog.render_post(post_id, "标题", "2026-09-02", [], "<p>正文</p>",
                                    canonical_path=canonical_path)
            post_html = (tmp / "posts" / post_id / "index.html").read_text(encoding="utf-8")
            m = _CANONICAL_LINK_RE.search(post_html)
            href = m.group(1) if m else ""
            check(f"canonical href以固定host开头({post_id})", href.startswith("https://mirror.foxzen.me/"), href)
            for bad in ("localhost", "127.0.0.1", "172.17.", "0.0.0.0", "backup.foxzen.me",
                        "github.foxzen.me", "cf.foxzen.me"):
                check(f"canonical href不出现{bad}({post_id})", bad not in href, href)
    with_temp_html_dir(_run)


def test_canonical_url_has_no_double_slash_query_or_fragment():
    def _run(tmp):
        import fetch_blog
        for post_id, canonical_path in (
            ("safe-1", "2026/09/some-slug"),
            ("safe-2", None),
        ):
            fetch_blog.render_post(post_id, "标题", "2026-09-02", [], "<p>正文</p>",
                                    canonical_path=canonical_path)
            post_html = (tmp / "posts" / post_id / "index.html").read_text(encoding="utf-8")
            m = _CANONICAL_LINK_RE.search(post_html)
            href = m.group(1)
            after_scheme = href[len("https://"):]
            check(f"canonical href协议后不出现连续斜杠({post_id})", "//" not in after_scheme, href)
            check(f"canonical href不带query string({post_id})", "?" not in href, href)
            check(f"canonical href不带fragment({post_id})", "#" not in href, href)
    with_temp_html_dir(_run)


def test_canonical_link_is_well_formed_html_tag():
    def _run(tmp):
        import fetch_blog
        fetch_blog.render_post(
            "canon-wellformed", "标题", "2026-09-02", [],
            "<p>正文</p>", canonical_path="2026/09/wellformed",
        )
        post_html = (tmp / "posts" / "canon-wellformed" / "index.html").read_text(encoding="utf-8")
        check("canonical标签是合法的<link rel=\"canonical\" href=\"...\">形式",
              '<link rel="canonical" href="https://mirror.foxzen.me/2026/09/wellformed.html">' in post_html)
        check("canonical标签出现在<head>...</head>范围内",
              post_html.index('<link rel="canonical"') < post_html.index("</head>"))
    with_temp_html_dir(_run)


def test_ordinary_body_links_not_mistaken_for_canonical_or_altered():
    """正文（Blogger原文）里可能本来就带<a href>甚至<link>字样（比如粘贴过来
    的HTML片段），render_post()只是把content_html原样塞进{content}占位符，
    不做任何正文内容扫描/改写——这里验证canonical标签的加入不会误伤正文
    原有的链接内容，正文原样保留。"""
    def _run(tmp):
        import fetch_blog
        body = ('<p>参考 <a href="https://example.com/foo?x=1#bar">这篇</a>，'
                '另外我贴了一段代码：<code>&lt;link rel="canonical" href="not-real"&gt;</code></p>')
        fetch_blog.render_post(
            "canon-body-links", "标题", "2026-09-02", [],
            body, canonical_path="2026/09/body-links",
        )
        post_html = (tmp / "posts" / "canon-body-links" / "index.html").read_text(encoding="utf-8")
        check("正文里原有的<a href>链接原样保留，未被改写",
              '<a href="https://example.com/foo?x=1#bar">这篇</a>' in post_html)
        check("正文里字面出现的转义后link文本原样保留，不影响真正head里的canonical",
              '&lt;link rel="canonical" href="not-real"&gt;' in post_html)
        check("head里真正的canonical标签依然只有一个、指向正确地址",
              len(_CANONICAL_LINK_RE.findall(post_html)) == 1 and
              _CANONICAL_LINK_RE.search(post_html).group(1) == "https://mirror.foxzen.me/2026/09/body-links.html")
    with_temp_html_dir(_run)


def test_canonical_matches_across_multiple_realistic_canonical_paths():
    def _run(tmp):
        import fetch_blog
        cases = [
            ("year-2026", "2026/01/hello-world"),
            ("year-2099", "2099/12/some-long-slug-name-here"),
            ("year-with-numbers", "2026/07/2026-review"),
        ]
        for post_id, canonical_path in cases:
            fetch_blog.render_post(post_id, "标题", "2026-09-02", [], "<p>正文</p>",
                                    canonical_path=canonical_path)
            post_html = (tmp / "posts" / post_id / "index.html").read_text(encoding="utf-8")
            m = _CANONICAL_LINK_RE.search(post_html)
            expected = f"https://mirror.foxzen.me/{canonical_path}.html"
            check(f"canonical_path={canonical_path!r}时href正确", m is not None and m.group(1) == expected, m)
    with_temp_html_dir(_run)


def test_canonical_link_identical_in_both_copies_when_static_target_exists():
    """mirror/backup直接serve posts/<id>/index.html这份拷贝，github/cf发布
    时拷贝的是YYYY/MM/slug.html这份——两份内容字节级一致（既有的
    test_render_post_writes_identical_static_copy已经验证过这一点），这里
    专门确认canonical这个新字段在两份拷贝里也是同一个值，不会出现"mirror
    自己指向自己、github/cf又指向别的地方"这种不一致。"""
    def _run(tmp):
        import fetch_blog
        fetch_blog.render_post(
            "canon-both-copies", "标题", "2026-09-02", [],
            "<p>正文</p>", canonical_path="2026/09/both-copies",
        )
        post_html = (tmp / "posts" / "canon-both-copies" / "index.html").read_text(encoding="utf-8")
        static_html = (tmp / "2026" / "09" / "both-copies.html").read_text(encoding="utf-8")
        check("两份拷贝canonical href完全一致（这就是mirror/backup/github/cf统一指向同一个URL的根本原因）",
              _CANONICAL_LINK_RE.search(post_html).group(1) == _CANONICAL_LINK_RE.search(static_html).group(1))
    with_temp_html_dir(_run)


def test_canonical_link_untouched_by_cross_post_link_rewrite():
    """publish_build.py发布github/cf产物时，会给html/posts/、html/YYYY/
    这些目录做shutil.copytree()（标准库保证内容原样拷贝，不需要重复验证），
    之后唯一会touch文章正文文件内容的函数是_fix_cross_post_content_links()
    ——这里直接对着这个函数验证：跑完之后canonical标签依然存在、依然是
    mirror.foxzen.me，且不会跟正文里普通的<a href>互相干扰。不跑完整的
    build_publish()（那会引入首页/搜索索引渲染等一堆跟canonical无关的
    细节，让这个测试变脆弱，也没有必要——那部分行为由test_publish_build.py
    自己负责，这里只测canonical这一个新字段的存活路径）。
    """
    def _run(tmp):
        import fetch_blog
        import publish_build
        fetch_blog.render_post(
            "pub-canon-post", "发布测试文章", "2026-09-02", [],
            '<p>正文，包含一个普通链接 <a href="https://example.com">example</a></p>',
            canonical_path="2026/09/pub-canon-post",
        )
        output_dir = tmp / "publish_out"
        shutil.copytree(tmp / "posts", output_dir / "posts")
        shutil.copytree(tmp / "2026", output_dir / "2026")

        expected_tag = '<link rel="canonical" href="https://mirror.foxzen.me/2026/09/pub-canon-post.html">'
        before = (output_dir / "posts" / "pub-canon-post" / "index.html").read_text(encoding="utf-8")
        check("准备阶段：拷贝后的产物已经带着canonical标签", expected_tag in before)

        publish_build._fix_cross_post_content_links(output_dir)

        after_post = (output_dir / "posts" / "pub-canon-post" / "index.html").read_text(encoding="utf-8")
        after_static = (output_dir / "2026" / "09" / "pub-canon-post.html").read_text(encoding="utf-8")
        check("_fix_cross_post_content_links()跑完后posts/<id>/index.html里canonical仍在、仍正确",
              expected_tag in after_post, after_post[:500])
        check("_fix_cross_post_content_links()跑完后YYYY/MM/slug.html里canonical仍在、仍正确",
              expected_tag in after_static, after_static[:500])
        check("正文里普通的<a href>链接没有被误认成canonical标签而改写",
              'href="https://example.com"' in after_post)
    with_temp_html_dir(_run)


def test_publish_build_hostname_rewrite_never_applied_to_article_files():
    """静态确认_rewrite_hostname()（会把mirror.foxzen.me替换成目标host，
    例如github.foxzen.me）只在处理index.html/robots.txt/sitemap.xml这几个
    顶层文件时被调用（build_publish()里ALLOWED_TOP_LEVEL_FILES那个循环），
    没有被应用到posts/<id>/index.html或YYYY/MM/slug.html这些逐篇文章文件
    ——这正是canonical标签在github/cf发布产物里能保持固定mirror.foxzen.me
    不被改写的前提。用代码层面的静态检查把这个前提固定下来，防止以后有人
    不小心把_rewrite_hostname()的调用范围扩大到逐篇文章文件。
    """
    src = (BASE_DIR / "publish_build.py").read_text(encoding="utf-8")
    check("_rewrite_hostname()只有1个定义+1个调用点，调用范围没有被扩大",
          src.count("_rewrite_hostname(") == 2, src.count("_rewrite_hostname("))


def test_app_dynamic_routes_untouched():
    """静态检查app.py里canonical路由/短链跳转的关键代码没被误改，确保这次
    改动只新增静态文件生成，不影响Flask原有动态行为。
    """
    src = (BASE_DIR / "app.py").read_text(encoding="utf-8")
    check("canonical_post_page路由仍存在", '@app.route("/<int:year>/<int:month>/<slug>.html"' in src)
    check("legacy_post_link (/posts/<id>/) 路由仍存在", '@app.route("/posts/<post_id>/"' in src)
    check("canonical_post_page仍按canonical_path查库", "db.get_post_by_canonical_path(canonical_path)" in src)


def test_real_db_sample_canonical_path_resolves_correctly():
    """用真实data/blog.db做只读抽样（不写入、不复制整份数据库），确认现有
    真实canonical_path数据能被正确转换成静态路径。

    这个测试要同时兼容两种环境：
    1. 本地开发环境：data/blog.db是真实生产数据库，posts表里有带
       canonical_path的真实文章——这种情况下必须真正执行下面的解析验证，
       不能因为"支持CI"就顺便把本地的真实验证也弱化掉。
    2. CI环境（GitHub Actions）：CI不应该也不会拿到生产数据库，pages.yml
       里只用项目自带的db.init_db()建一份空schema（见.github/workflows/
       pages.yml的说明）——这种情况下data/blog.db这个文件本身是存在的
       （sqlite3.connect会自动建文件），posts表也存在，但里面没有任何
       真实文章行。区分"文件不存在"和"文件存在但没有真实样本"很重要：
       只判断文件是否存在不够，还要在查询后发现"一条真实样本都没有"时
       同样按SKIP处理，而不是断言"必须至少有一条"从而在CI里失败——
       CI里没有真实数据本来就是设计上的预期状态，不是bug。
    """
    if not REAL_DB.exists():
        print("  [SKIP] 未找到 data/blog.db，跳过真实DB canonical_path验证")
        return

    def _run(tmp):
        import fetch_blog
        conn = sqlite3.connect(f"file:{REAL_DB.as_posix()}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT post_id, canonical_path FROM posts WHERE canonical_path IS NOT NULL LIMIT 5"
            ).fetchall()
        finally:
            conn.close()

        if not rows:
            print("  [SKIP] 当前环境没有真实数据库文章样本，跳过真实DB canonical_path验证")
            return

        for row in rows:
            target = fetch_blog.canonical_static_target(row["canonical_path"])
            year, month, slug = row["canonical_path"].split("/")
            expected = (tmp / year / month / f"{slug}.html").resolve()
            check(f"真实canonical_path解析正确: {row['canonical_path']}", target == expected)
    with_temp_html_dir(_run)


def test_no_blog_db_copy_produced():
    """确认整个测试过程没有在任何临时目录留下data/blog.db的副本
    （只做了只读sqlite3连接，从未write/copy过数据库文件本身）。"""
    leaked = list(Path(tempfile.gettempdir()).glob("canonical_static_test_*/**/*.db"))
    check("临时目录未残留任何.db文件", leaked == [], f"found {leaked}")


def main():
    tests = [
        test_valid_canonical_path,
        test_invalid_canonical_paths_rejected,
        test_slug_with_dotdot_does_not_escape_html_dir,
        test_render_post_writes_identical_static_copy,
        test_render_post_without_canonical_path_skips_static_file,
        test_render_post_canonical_link_uses_canonical_path_when_available,
        test_render_post_canonical_link_falls_back_to_post_id_without_canonical_path,
        test_canonical_link_appears_exactly_once_per_copy,
        test_canonical_link_always_uses_fixed_mirror_host,
        test_canonical_url_has_no_double_slash_query_or_fragment,
        test_canonical_link_is_well_formed_html_tag,
        test_ordinary_body_links_not_mistaken_for_canonical_or_altered,
        test_canonical_matches_across_multiple_realistic_canonical_paths,
        test_canonical_link_identical_in_both_copies_when_static_target_exists,
        test_canonical_link_untouched_by_cross_post_link_rewrite,
        test_publish_build_hostname_rewrite_never_applied_to_article_files,
        test_app_dynamic_routes_untouched,
        test_real_db_sample_canonical_path_resolves_correctly,
        test_no_blog_db_copy_produced,
    ]
    for t in tests:
        print(f"--- {t.__name__} ---")
        try:
            t()
        except Exception:
            print(f"  [FAIL] {t.__name__} 抛出异常:")
            traceback.print_exc()
            failures.append(t.__name__)

    print()
    if failures:
        print(f"共 {len(failures)} 项失败: {failures}")
        sys.exit(1)
    print("全部测试通过。")


if __name__ == "__main__":
    main()
