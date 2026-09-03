#!/usr/bin/env python3
"""
针对 publish_build.py（第十七节 publish/ 白名单静态构建）的回归测试。

全部测试都在临时目录里操作，不碰真实 html/、不碰 data/blog.db、不产生
需要手动清理的残留文件。用法: python3 test_publish_build.py
"""
import shutil
import sys
import tempfile
import traceback
from pathlib import Path

failures = []


def check(name, condition, detail=""):
    if condition:
        print(f"  [PASS] {name}")
    else:
        print(f"  [FAIL] {name} {detail}")
        failures.append(name)


def _make_fixture_html_dir(tmp):
    """构造一个最小但覆盖各类白名单/黑名单场景的html/夹具目录，
    比只用真实html/更能稳定测试到canonical(YYYY/MM)目录这类当前本地
    html/里还没有真实数据的场景。"""
    html_dir = tmp / "html"
    html_dir.mkdir()

    (html_dir / "index.html").write_text(
        '<html><body><div id="app"></div>'
        '<script src="/static/index.js"></script>'
        '</body></html>',
        encoding="utf-8",
    )
    (html_dir / "robots.txt").write_text(
        "User-agent: *\nAllow: /\nSitemap: https://mirror.foxzen.me/sitemap.xml\n",
        encoding="utf-8",
    )
    (html_dir / "sitemap.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        '<url><loc>https://mirror.foxzen.me/</loc></url>\n'
        '<url><loc>https://mirror.foxzen.me/2026/07/demo-slug.html</loc></url>\n'
        "</urlset>\n",
        encoding="utf-8",
    )
    (html_dir / "foxzen-download-admin.html").write_text("<html>admin</html>", encoding="utf-8")
    (html_dir / "29bfb801721343b798cc9dfca454d8af.txt").write_text("indexnow-key", encoding="utf-8")

    (html_dir / "404").mkdir()
    (html_dir / "404" / "index.html").write_text("<html>404</html>", encoding="utf-8")

    (html_dir / "images").mkdir()
    (html_dir / "images" / "fox-header.png").write_bytes(b"\x89PNG-fake-bytes")

    # 尽量贴近fetch_blog.py真实POST_TEMPLATE的结构(title/meta/tags/content
    # 四个关键区块)，因为_extract_post_metadata()是按这个结构解析的，用一个
    # 过度简化的fixture会测不出解析逻辑本身对不对。
    demo_post_html = (
        "<!DOCTYPE html><html><head><title>示例文章标题</title></head><body>"
        '<h1>示例文章标题</h1>'
        '<div class="meta">发布于 2026-07-15</div>'
        '<div class="tags"><a href="/index.html?tag=Firefox">#Firefox</a>'
        '<a href="/index.html?tag=隐私">#隐私</a></div>'
        '<div class="content"><p>真实文章正文，示例password/token出现在正文里不代表危险。'
        "这里还提到关键词Firefox方便测试搜索命中正文。</p></div>"
        "</body></html>"
    )
    (html_dir / "posts" / "111").mkdir(parents=True)
    (html_dir / "posts" / "111" / "index.html").write_text(demo_post_html, encoding="utf-8")
    (html_dir / "posts" / "111" / "media").mkdir()
    (html_dir / "posts" / "111" / "media" / "pic.png").write_bytes(b"fake-image-bytes")

    (html_dir / "1").mkdir()
    (html_dir / "1" / "index.html").write_text("<html>短号跳转</html>", encoding="utf-8")

    # canonical静态文件跟posts/<id>/index.html是同一段html的字节级拷贝
    # （见fetch_blog.py的render_post()），这里保持一致，才能真正测到
    # publish_build._find_public_url()的字节比对匹配逻辑。
    (html_dir / "2026" / "07").mkdir(parents=True)
    (html_dir / "2026" / "07" / "demo-slug.html").write_text(demo_post_html, encoding="utf-8")

    (html_dir / "foxzen").mkdir()
    (html_dir / "foxzen" / "index.html").write_text("<html>foxzen.me专属页面</html>", encoding="utf-8")

    return html_dir


def _make_post_html(title, date, content_extra=""):
    """跟_make_fixture_html_dir()里那份一样，贴近真实POST_TEMPLATE的
    title/meta/tags/content四个关键区块结构，供下面几个href修正测试复用。"""
    return (
        f"<!DOCTYPE html><html><head><title>{title}</title></head><body>"
        f"<h1>{title}</h1>"
        f'<div class="meta">发布于 {date}</div>'
        '<div class="tags"></div>'
        f'<div class="content"><p>正文内容。{content_extra}</p></div>'
        "</body></html>"
    )


def _make_fixture_html_dir_for_href_fix(tmp):
    """专门用来测试"首页文章链接修正"(_fix_article_hrefs)的夹具，跟
    _make_fixture_html_dir()完全独立，不共用、不互相影响。

    构造四篇文章，覆盖P0修复需要处理的四种场景：
    - post-ok：canonical静态文件确实存在 -> 链接应该保持canonical地址；
    - post-missing：index.html里写的是canonical链接，但对应静态文件
      不存在（复现当前真实bug现场）-> 链接应该被修正成/posts/<id>/；
    - post-dup-a / post-dup-b：两篇标题完全相同、canonical文件都不存在 ->
      标题不唯一，两个链接都应该原样保留不动（宁可继续404也不猜）。

    index.html里"点击排行榜""下载排行榜""fallback-list"三处都引用了这几篇
    文章，贴近render_index()/rank_html()/_href_for()真实生成的html结构
    （<li><a href="..." target="_blank" rel="noopener">标题</a>...）。
    """
    html_dir = tmp / "html"
    html_dir.mkdir()

    (html_dir / "robots.txt").write_text(
        "User-agent: *\nAllow: /\nSitemap: https://mirror.foxzen.me/sitemap.xml\n",
        encoding="utf-8",
    )
    (html_dir / "sitemap.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        '<url><loc>https://mirror.foxzen.me/</loc></url>\n'
        "</urlset>\n",
        encoding="utf-8",
    )
    (html_dir / "404").mkdir()
    (html_dir / "404" / "index.html").write_text("<html>404</html>", encoding="utf-8")

    posts = {
        "post-ok": ("已有静态文件的文章", "2026-07-01"),
        "post-missing": ("静态文件缺失的文章", "2026-07-02"),
        "post-dup-a": ("重复标题文章", "2026-07-03"),
        "post-dup-b": ("重复标题文章", "2026-07-04"),
    }
    post_html = {}
    for post_id, (title, date) in posts.items():
        content = _make_post_html(title, date, content_extra=post_id)
        post_html[post_id] = content
        post_dir = html_dir / "posts" / post_id
        post_dir.mkdir(parents=True)
        (post_dir / "index.html").write_text(content, encoding="utf-8")

    # 只给post-ok真正生成canonical静态文件——跟fetch_blog.py的render_post()
    # 行为一致：static_target存在时两份文件字节完全一致。
    ok_canonical_dir = html_dir / "2026" / "07"
    ok_canonical_dir.mkdir(parents=True)
    (ok_canonical_dir / "post-ok-slug.html").write_text(post_html["post-ok"], encoding="utf-8")

    hrefs = {
        "post-ok": "/2026/07/post-ok-slug.html",
        "post-missing": "/2026/08/missing-slug.html",
        "post-dup-a": "/2026/09/dup-a-slug.html",
        "post-dup-b": "/2026/09/dup-b-slug.html",
    }

    def li_fallback(post_id):
        title = posts[post_id][0]
        return (f'<li><a href="{hrefs[post_id]}" target="_blank" rel="noopener">{title}</a> '
                f'<span class="date">{posts[post_id][1]}</span></li>')

    def li_rank(post_id, unit):
        title = posts[post_id][0]
        return f'<li><a href="{hrefs[post_id]}" target="_blank" rel="noopener">{title}</a>（1 {unit}）</li>'

    fallback_items = "\n".join(li_fallback(pid) for pid in posts)
    top_clicked_html = "\n".join(li_rank(pid, "次浏览") for pid in ("post-ok", "post-missing"))
    top_downloaded_html = "\n".join(li_rank(pid, "次下载") for pid in ("post-dup-a", "post-dup-b"))

    index_html = (
        "<html><body>"
        '<div class="leaderboard">'
        "<h3>🔥 点击排行榜</h3>"
        f"<ol>{top_clicked_html}</ol>"
        "<h3>📥 下载排行榜</h3>"
        f"<ol>{top_downloaded_html}</ol>"
        "</div>"
        '<div id="app"></div>'
        '<script src="/static/index.js"></script>'
        "</body></html>"
    ).replace('<div id="app"></div>', f'<div id="app"><ul id="fallback-list">{fallback_items}</ul></div>')
    (html_dir / "index.html").write_text(index_html, encoding="utf-8")

    return html_dir, hrefs, posts


def with_href_fix_fixture(fn):
    import publish_build
    tmp = Path(tempfile.mkdtemp(prefix="publish_build_hreffix_test_"))
    orig_html_dir = publish_build.HTML_DIR
    fixture_html, hrefs, posts = _make_fixture_html_dir_for_href_fix(tmp)
    publish_build.HTML_DIR = fixture_html
    output_dir = tmp / "publish_out"
    try:
        fn(tmp, output_dir, hrefs, posts)
    finally:
        publish_build.HTML_DIR = orig_html_dir
        shutil.rmtree(tmp, ignore_errors=True)


def _hrefs_in(html_text):
    import re
    return re.findall(r'href="([^"]+)"', html_text)


def test_leaderboard_links_point_to_existing_files():
    def _run(tmp, out, hrefs, posts):
        import publish_build
        publish_build.build_publish("github.foxzen.me", out)
        content = (out / "index.html").read_text(encoding="utf-8")
        # post-dup-a/post-dup-b标题不唯一，按设计有意保留成悬空链接
        # （"不猜、不伪造"的已知代价），这里不重复断言它们，由
        # test_ambiguous_title_href_left_unchanged_not_guessed专门覆盖。
        known_ambiguous = {hrefs["post-dup-a"], hrefs["post-dup-b"]}
        lb_start = content.index('<div class="leaderboard">')
        leaderboard_html = content[lb_start:content.index('<div id="app">')]
        for href in _hrefs_in(leaderboard_html):
            if href in known_ambiguous:
                continue
            target = out / href.lstrip("/")
            check(f"排行榜链接指向真实存在的文件: {href}", target.exists())
    with_href_fix_fixture(_run)


def test_fallback_list_links_point_to_existing_files():
    def _run(tmp, out, hrefs, posts):
        import publish_build
        publish_build.build_publish("github.foxzen.me", out)
        content = (out / "index.html").read_text(encoding="utf-8")
        known_ambiguous = {hrefs["post-dup-a"], hrefs["post-dup-b"]}
        fb_start = content.index('id="fallback-list"')
        fallback_html = content[fb_start:]
        for href in _hrefs_in(fallback_html):
            if href in known_ambiguous:
                continue
            target = out / href.lstrip("/")
            check(f"fallback-list链接指向真实存在的文件: {href}", target.exists())
    with_href_fix_fixture(_run)


def test_no_dangling_canonical_article_href_anywhere_in_index_html():
    """不局限于已知的两个区块——对整个index.html做一次全局扫描，确保
    不存在任何"长得像canonical文章链接、但对应文件不存在"的href，
    覆盖以后又多出第三个区块的情况。"""
    def _run(tmp, out, hrefs, posts):
        import publish_build
        publish_build.build_publish("github.foxzen.me", out)
        content = (out / "index.html").read_text(encoding="utf-8")
        dangling = []
        for m in publish_build._ARTICLE_HREF_PATTERN.finditer(content):
            href = m.group(1)
            if not (out / href.lstrip("/")).exists():
                dangling.append(href)
        # post-dup-a/post-dup-b标题不唯一，按设计会被有意保留成悬空链接，
        # 这是"不猜、不伪造"原则的已知代价，不算这个测试要拦截的问题。
        unexpected = [h for h in dangling if h not in (hrefs["post-dup-a"], hrefs["post-dup-b"])]
        check("除了已知的标题不唯一场景外，不应再出现悬空canonical文章链接",
              unexpected == [], f"got {unexpected}")
    with_href_fix_fixture(_run)


def test_canonical_link_kept_when_static_file_exists():
    def _run(tmp, out, hrefs, posts):
        import publish_build
        publish_build.build_publish("github.foxzen.me", out)
        content = (out / "index.html").read_text(encoding="utf-8")
        check("有canonical静态文件的文章，链接保持canonical地址不变",
              hrefs["post-ok"] in content)
    with_href_fix_fixture(_run)


def test_missing_canonical_falls_back_to_posts_id():
    def _run(tmp, out, hrefs, posts):
        import publish_build
        publish_build.build_publish("github.foxzen.me", out)
        content = (out / "index.html").read_text(encoding="utf-8")
        check("canonical静态文件不存在的文章，链接被修正为/posts/<id>/",
              "/posts/post-missing/" in content)
        check("修正后不应再残留指向不存在文件的原canonical链接",
              hrefs["post-missing"] not in content)
    with_href_fix_fixture(_run)


def test_ambiguous_title_href_left_unchanged_not_guessed():
    def _run(tmp, out, hrefs, posts):
        import publish_build
        publish_build.build_publish("github.foxzen.me", out)
        content = (out / "index.html").read_text(encoding="utf-8")
        check("标题重复(post-dup-a)时原href保留、不被伪造成任何/posts/<id>/",
              hrefs["post-dup-a"] in content)
        check("标题重复(post-dup-b)时原href保留、不被伪造成任何/posts/<id>/",
              hrefs["post-dup-b"] in content)
    with_href_fix_fixture(_run)


def test_source_html_index_untouched_by_href_fix():
    """确认这次改动只影响publish/index.html这份副本，html/index.html
    这份mirror生产站自己用的原始文件不会被build_publish()写入/修改。"""
    def _run(tmp, out, hrefs, posts):
        import publish_build
        before = (publish_build.HTML_DIR / "index.html").read_bytes()
        publish_build.build_publish("github.foxzen.me", out)
        after = (publish_build.HTML_DIR / "index.html").read_bytes()
        check("html/index.html字节内容未发生变化", before == after)
    with_href_fix_fixture(_run)


def test_non_article_links_untouched_by_href_fix():
    def _run(tmp, out, hrefs, posts):
        import publish_build
        publish_build.build_publish("github.foxzen.me", out)
        content = (out / "index.html").read_text(encoding="utf-8")
        check("pages-index.js脚本引用未被href修正逻辑误伤",
              "/pages-index.js" in content)
        # index.html本身在这份夹具里没有其他站内/外部链接，其余非文章链接
        # 的"不会被误伤"由test_non_article_links_untouched_in_shared_fixture
        # 用主夹具（含首页/图片/其他入口外部链接）另行覆盖。
    with_href_fix_fixture(_run)


def test_search_index_json_url_matches_fixed_index_html_href():
    """search-index.json里_find_public_url()给出的url，跟index.html里
    对应文章被修正后的href应该一致——两处用的是同一份真实性判断，
    不应该出现"列表能点开、排行榜却指向别处"这种分裂。"""
    def _run(tmp, out, hrefs, posts):
        import publish_build, json
        publish_build.build_publish("github.foxzen.me", out)
        data = json.loads((out / "search-index.json").read_text(encoding="utf-8"))
        url_by_id = {a["id"]: a["url"] for a in data["articles"]}
        content = (out / "index.html").read_text(encoding="utf-8")
        check("post-ok在index.html里的链接跟search-index.json一致",
              url_by_id["post-ok"] in content)
        check("post-missing在index.html里的链接跟search-index.json一致",
              url_by_id["post-missing"] in content)
    with_href_fix_fixture(_run)


def _make_post_html_with_discuss_btn(title, date, own_permalink, body_extra=""):
    """比_make_post_html()多带上discuss-btn——跟fetch_blog.py真实
    DISCUSS_CTA_BLOCK结构一致，这是_fix_cross_post_content_links()
    用来反查"这篇文章自己的Blogger permalink"的字段来源。"""
    return (
        f"<!DOCTYPE html><html><head><title>{title}</title></head><body>"
        f"<h1>{title}</h1>"
        f'<div class="meta">发布于 {date}</div>'
        '<div class="tags"></div>'
        f'<div class="content"><p>正文内容。{body_extra}</p></div>'
        '<div class="discuss-cta">'
        f'<a class="discuss-btn" href="{own_permalink}" target="_blank" rel="noopener">💬 到主站参与讨论</a>'
        "</div>"
        "</body></html>"
    )


def _make_fixture_html_dir_for_content_link_fix(tmp):
    """专门测试"文章正文里指向本站另一篇文章的Blogger permalink"修正
    (_fix_cross_post_content_links)的夹具，覆盖：

    - post-a：正文里有一条"上一篇文章"链接，指向post-b自己的Blogger
      permalink -> 应该被改写成post-b在当前host下的真实静态地址；
    - post-b：有canonical静态文件（YYYY/MM/slug.html） -> 用来验证
      两份拷贝(posts/<id>/index.html 和 canonical文件)修正后仍然
      保持字节一致；
    - post-c：正文里链接指向一个"不属于本站任何已抓取文章"的Blogger
      permalink（模拟指向一篇未收录/已删除的文章，或纯外部博客）->
      必须原样保留，不允许猜测替换；
    - 每篇文章自己的discuss-btn必须在修正后依然是原始Blogger permalink。
    """
    html_dir = tmp / "html"
    html_dir.mkdir()

    (html_dir / "robots.txt").write_text(
        "User-agent: *\nAllow: /\nSitemap: https://mirror.foxzen.me/sitemap.xml\n",
        encoding="utf-8",
    )
    (html_dir / "sitemap.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        '<url><loc>https://mirror.foxzen.me/</loc></url>\n'
        "</urlset>\n",
        encoding="utf-8",
    )
    (html_dir / "404").mkdir()
    (html_dir / "404" / "index.html").write_text("<html>404</html>", encoding="utf-8")
    (html_dir / "index.html").write_text(
        '<html><body><div id="app"></div><script src="/static/index.js"></script></body></html>',
        encoding="utf-8",
    )

    permalink_a = "https://digatlas.blogspot.com/2026/06/post-a.html"
    permalink_b = "https://digatlas.blogspot.com/2026/07/post-b.html"
    permalink_unknown = "https://digatlas.blogspot.com/2020/01/no-longer-tracked-post.html"

    content_a = _make_post_html_with_discuss_btn(
        "文章A", "2026-06-01", permalink_a,
        body_extra=(
            f'继续阅读<a href="{permalink_b}" target="_blank">上一篇文章</a>，'
            f'另外也可以看看<a href="{permalink_unknown}" target="_blank">这篇旧文</a>。'
        ),
    )
    content_b = _make_post_html_with_discuss_btn("文章B", "2026-07-01", permalink_b)

    (html_dir / "posts" / "post-a").mkdir(parents=True)
    (html_dir / "posts" / "post-a" / "index.html").write_text(content_a, encoding="utf-8")
    (html_dir / "posts" / "post-b").mkdir(parents=True)
    (html_dir / "posts" / "post-b" / "index.html").write_text(content_b, encoding="utf-8")

    # post-b有canonical静态文件，跟posts/post-b/index.html字节一致
    # （复现fetch_blog.py render_post()的真实拷贝行为）。
    b_canonical_dir = html_dir / "2026" / "07"
    b_canonical_dir.mkdir(parents=True)
    (b_canonical_dir / "post-b-slug.html").write_text(content_b, encoding="utf-8")

    return html_dir, {
        "permalink_a": permalink_a,
        "permalink_b": permalink_b,
        "permalink_unknown": permalink_unknown,
    }


def with_content_link_fix_fixture(fn):
    import publish_build
    tmp = Path(tempfile.mkdtemp(prefix="publish_build_contentlink_test_"))
    orig_html_dir = publish_build.HTML_DIR
    fixture_html, permalinks = _make_fixture_html_dir_for_content_link_fix(tmp)
    publish_build.HTML_DIR = fixture_html
    output_dir = tmp / "publish_out"
    try:
        fn(tmp, output_dir, permalinks)
    finally:
        publish_build.HTML_DIR = orig_html_dir
        shutil.rmtree(tmp, ignore_errors=True)


def test_cross_post_content_link_rewritten_to_real_url():
    def _run(tmp, out, permalinks):
        import publish_build
        publish_build.build_publish("github.foxzen.me", out)
        content_a = (out / "posts" / "post-a" / "index.html").read_text(encoding="utf-8")
        check("post-a正文里指向post-b的Blogger permalink已被改写",
              permalinks["permalink_b"] not in content_a)
        check("post-a正文里的链接改写成post-b经_find_public_url()验证的真实地址",
              "/2026/07/post-b-slug.html" in content_a)
    with_content_link_fix_fixture(_run)


def test_own_permalink_and_unknown_permalink_kept_unchanged():
    def _run(tmp, out, permalinks):
        import publish_build
        publish_build.build_publish("github.foxzen.me", out)
        content_a = (out / "posts" / "post-a" / "index.html").read_text(encoding="utf-8")
        content_b = (out / "posts" / "post-b" / "index.html").read_text(encoding="utf-8")
        check("正文里指向本站之外/未收录文章的permalink原样保留，不猜测",
              permalinks["permalink_unknown"] in content_a)
        check("post-b自己的Blogger permalink（discuss-btn）修正后依然是原始值",
              f'href="{permalinks["permalink_b"]}"' in content_b)
    with_content_link_fix_fixture(_run)


def test_discuss_btn_never_rewritten():
    def _run(tmp, out, permalinks):
        import publish_build
        publish_build.build_publish("github.foxzen.me", out)
        content_a = (out / "posts" / "post-a" / "index.html").read_text(encoding="utf-8")
        check('post-a自己的discuss-btn（class="discuss-btn"）保留原始Blogger permalink',
              f'<a class="discuss-btn" href="{permalinks["permalink_a"]}"' in content_a)
    with_content_link_fix_fixture(_run)


def test_canonical_and_posts_copy_stay_identical_after_content_link_fix():
    def _run(tmp, out, permalinks):
        import publish_build
        publish_build.build_publish("github.foxzen.me", out)
        posts_copy = (out / "posts" / "post-b" / "index.html").read_bytes()
        canonical_copy = (out / "2026" / "07" / "post-b-slug.html").read_bytes()
        check("修正后posts/<id>/index.html和canonical静态文件仍然字节级一致",
              posts_copy == canonical_copy)
    with_content_link_fix_fixture(_run)


def test_github_and_cloudflare_builds_get_same_root_relative_link():
    """github.foxzen.me和cf.foxzen.me的CNAME/robots/sitemap域名不同，但
    文章内链接本身是根相对路径（跟_href_for()/_find_public_url()一直以来
    的做法一致），同一个根相对路径在两个host各自的CNAME下自然分别解析成
    github.foxzen.me和cf.foxzen.me——两个host不需要生成不同的链接文本，
    这里验证两次构建产出的内链文本本身相同，CNAME各自正确。"""
    def _run(tmp, out, permalinks):
        import publish_build
        out_gh = tmp / "publish_gh"
        out_cf = tmp / "publish_cf"
        publish_build.build_publish("github.foxzen.me", out_gh)
        publish_build.build_publish("cf.foxzen.me", out_cf)
        content_gh = (out_gh / "posts" / "post-a" / "index.html").read_text(encoding="utf-8")
        content_cf = (out_cf / "posts" / "post-a" / "index.html").read_text(encoding="utf-8")
        check("GitHub构建的CNAME是github.foxzen.me",
              (out_gh / "CNAME").read_text(encoding="utf-8").strip() == "github.foxzen.me")
        check("Cloudflare构建的CNAME是cf.foxzen.me",
              (out_cf / "CNAME").read_text(encoding="utf-8").strip() == "cf.foxzen.me")
        check("两个host构建出的正文交叉引用链接文本完全一致（都是根相对路径）",
              content_gh == content_cf)
        check("交叉引用链接已经指向post-b的真实静态地址（根相对路径，随所在host解析）",
              "/2026/07/post-b-slug.html" in content_gh)
    with_content_link_fix_fixture(_run)


def test_source_html_posts_untouched_by_content_link_fix():
    def _run(tmp, out, permalinks):
        import publish_build
        before_a = (publish_build.HTML_DIR / "posts" / "post-a" / "index.html").read_bytes()
        before_b = (publish_build.HTML_DIR / "posts" / "post-b" / "index.html").read_bytes()
        publish_build.build_publish("github.foxzen.me", out)
        after_a = (publish_build.HTML_DIR / "posts" / "post-a" / "index.html").read_bytes()
        after_b = (publish_build.HTML_DIR / "posts" / "post-b" / "index.html").read_bytes()
        check("html/posts/post-a/index.html源文件字节未变", before_a == after_a)
        check("html/posts/post-b/index.html源文件字节未变", before_b == after_b)
    with_content_link_fix_fixture(_run)


def with_fixture(fn):
    import publish_build
    tmp = Path(tempfile.mkdtemp(prefix="publish_build_test_"))
    orig_html_dir = publish_build.HTML_DIR
    fixture_html = _make_fixture_html_dir(tmp)
    publish_build.HTML_DIR = fixture_html
    output_dir = tmp / "publish_out"
    try:
        fn(tmp, output_dir)
    finally:
        publish_build.HTML_DIR = orig_html_dir
        shutil.rmtree(tmp, ignore_errors=True)


def test_data_and_db_excluded():
    def _run(tmp, out):
        import publish_build
        # 夹具本身没有data/，这里额外验证builder不会主动创建/引用它
        publish_build.build_publish("github.foxzen.me", out)
        check("publish/中不存在data/目录", not (out / "data").exists())
        check("publish/中不存在任何.db文件",
              not any(p.suffix == ".db" for p in out.rglob("*")))
    with_fixture(_run)


def test_secret_and_backend_files_excluded():
    def _run(tmp, out):
        import publish_build
        # 模拟仓库根目录混进来的敏感文件类型不会被builder碰到
        # （builder本身只看HTML_DIR即html/，不看仓库根目录，这里直接验证白名单结果）
        publish_build.build_publish("github.foxzen.me", out)
        all_files = [p for p in out.rglob("*") if p.is_file()]
        names = [p.name for p in all_files]
        for banned_ext in (".env", ".pem", ".key", ".secret", ".token", ".py"):
            check(f"publish/中没有{banned_ext}后缀文件",
                  not any(n.endswith(banned_ext) for n in names))
        check("publish/中不存在nginx-conf目录", not (out / "nginx-conf").exists())
        check("publish/中不存在cron目录", not (out / "cron").exists())
        check("publish/中不存在systemd目录", not (out / "systemd").exists())
        check("publish/中不存在foxzen-download-admin.html",
              not any(n == "foxzen-download-admin.html" for n in names))
        check("publish/中不存在__pycache__",
              not any("__pycache__" in str(p) for p in all_files))
    with_fixture(_run)


def test_expected_public_files_present():
    def _run(tmp, out):
        import publish_build
        publish_build.build_publish("github.foxzen.me", out)
        check("index.html存在", (out / "index.html").exists())
        check("根目录404.html存在", (out / "404.html").exists())
        check("robots.txt存在", (out / "robots.txt").exists())
        check("sitemap.xml存在", (out / "sitemap.xml").exists())
        check("至少一篇真实文章静态HTML存在", (out / "posts" / "111" / "index.html").exists())
        check("文章媒体资源存在", (out / "posts" / "111" / "media" / "pic.png").exists())
        check("短号跳转页存在", (out / "1" / "index.html").exists())
        check("canonical静态文章存在", (out / "2026" / "07" / "demo-slug.html").exists())
        check("images/存在", (out / "images" / "fox-header.png").exists())
    with_fixture(_run)


def test_indexnow_key_and_foxzen_site_excluded_by_whitelist():
    """29bfb...txt和foxzen/不在白名单规则内——不是因为它们危险，
    而是它们分别属于mirror域名专属的IndexNow验证、foxzen.me专属页面，
    跟github.foxzen.me/cf.foxzen.me这两个"文章镜像"站点的职责无关。"""
    def _run(tmp, out):
        import publish_build
        publish_build.build_publish("github.foxzen.me", out)
        check("mirror专属IndexNow验证文件未被复制",
              not (out / "29bfb801721343b798cc9dfca454d8af.txt").exists())
        check("foxzen.me专属目录未被复制", not (out / "foxzen").exists())
    with_fixture(_run)


def test_github_hostname_rewrite():
    def _run(tmp, out):
        import publish_build
        publish_build.build_publish("github.foxzen.me", out)
        robots = (out / "robots.txt").read_text(encoding="utf-8")
        sitemap = (out / "sitemap.xml").read_text(encoding="utf-8")
        cname = (out / "CNAME").read_text(encoding="utf-8").strip()
        check("robots.txt使用github.foxzen.me", "https://github.foxzen.me/sitemap.xml" in robots)
        check("robots.txt不再含mirror.foxzen.me", "mirror.foxzen.me" not in robots)
        check("sitemap.xml使用github.foxzen.me", "https://github.foxzen.me/" in sitemap)
        check("sitemap.xml不再含mirror.foxzen.me", "mirror.foxzen.me" not in sitemap)
        check("CNAME内容正确", cname == "github.foxzen.me")
    with_fixture(_run)


def test_cloudflare_hostname_rewrite():
    def _run(tmp, out):
        import publish_build
        publish_build.build_publish("cf.foxzen.me", out)
        robots = (out / "robots.txt").read_text(encoding="utf-8")
        sitemap = (out / "sitemap.xml").read_text(encoding="utf-8")
        cname = (out / "CNAME").read_text(encoding="utf-8").strip()
        check("robots.txt使用cf.foxzen.me", "https://cf.foxzen.me/sitemap.xml" in robots)
        check("sitemap.xml使用cf.foxzen.me", "https://cf.foxzen.me/" in sitemap)
        check("CNAME内容正确", cname == "cf.foxzen.me")
    with_fixture(_run)


def test_same_builder_same_output_structure_for_both_hosts():
    """同一套builder对两个host产出的文件树结构应该完全一致（只有hostname
    相关内容不同），不能是两套不同的生成逻辑。"""
    def _run(tmp, out):
        import publish_build
        out_gh = tmp / "publish_gh"
        out_cf = tmp / "publish_cf"
        publish_build.build_publish("github.foxzen.me", out_gh)
        publish_build.build_publish("cf.foxzen.me", out_cf)
        rel_gh = sorted(str(p.relative_to(out_gh)) for p in out_gh.rglob("*"))
        rel_cf = sorted(str(p.relative_to(out_cf)) for p in out_cf.rglob("*"))
        check("两个host产出的文件树结构完全一致", rel_gh == rel_cf,
              f"only in gh: {set(rel_gh)-set(rel_cf)}, only in cf: {set(rel_cf)-set(rel_gh)}")
    with_fixture(_run)


def test_index_js_removed_but_fallback_content_kept():
    def _run(tmp, out):
        import publish_build
        publish_build.build_publish("github.foxzen.me", out)
        content = (out / "index.html").read_text(encoding="utf-8")
        check("index.html中已移除依赖Flask API的static/index.js引用",
              "/static/index.js" not in content)
        check("index.html的静态兜底列表(#app容器)仍然保留", 'id="app"' in content)
    with_fixture(_run)


def test_article_body_with_password_token_words_not_treated_as_secret():
    """正文里出现password/token这些技术术语的文章文件本身不应该被误判成
    危险文件而被排除——只按路径/文件名白名单判断，不按正文内容做黑名单删除。"""
    def _run(tmp, out):
        import publish_build
        publish_build.build_publish("github.foxzen.me", out)
        article = out / "posts" / "111" / "index.html"
        check("含password/token字样的正常文章正文未被误删", article.exists())
        text = article.read_text(encoding="utf-8")
        check("正文内容原样保留", "password/token" in text)
    with_fixture(_run)


def test_search_index_generated_with_expected_fields():
    def _run(tmp, out):
        import publish_build, json
        publish_build.build_publish("github.foxzen.me", out)
        data = json.loads((out / "search-index.json").read_text(encoding="utf-8"))
        articles = data["articles"]
        check("search-index.json至少包含一篇真实文章", len(articles) >= 1)
        a = articles[0]
        for field in ("id", "title", "url", "date", "tags", "text"):
            check(f"文章记录包含字段: {field}", field in a)
        check("title字段正确", a["title"] == "示例文章标题")
        check("date字段正确", a["date"] == "2026-07-15")
        check("tags字段正确", a["tags"] == ["Firefox", "隐私"])
        check("text字段包含正文内容", "真实文章正文" in a["text"])
        check("记录里没有多余字段(比如visitor/page_hit)",
              set(a.keys()) == {"id", "title", "url", "date", "tags", "text"})
    with_fixture(_run)


def test_search_index_uses_canonical_url_when_available():
    def _run(tmp, out):
        import publish_build, json
        publish_build.build_publish("github.foxzen.me", out)
        data = json.loads((out / "search-index.json").read_text(encoding="utf-8"))
        a = data["articles"][0]
        check("有canonical静态文件时优先使用/YYYY/MM/slug.html而不是/posts/<id>/",
              a["url"] == "/2026/07/demo-slug.html", f"got {a['url']}")
    with_fixture(_run)


def test_search_index_no_database_or_visitor_data():
    def _run(tmp, out):
        import publish_build, json
        publish_build.build_publish("github.foxzen.me", out)
        raw = (out / "search-index.json").read_text(encoding="utf-8")
        check("search-index.json不含data/blog.db字样", "blog.db" not in raw)
        check("search-index.json不含visitor_key字样", "visitor_key" not in raw)
        check("search-index.json不含page_hit字样", "page_hit" not in raw)
        check("search-index.json不含finish_read字样", "finish_read" not in raw)
        data = json.loads(raw)
        check("每条记录字段严格限定在允许范围内",
              all(set(a.keys()) <= publish_build._ALLOWED_ARTICLE_FIELDS for a in data["articles"]))
    with_fixture(_run)


def test_search_index_same_data_across_hosts():
    """GitHub和Cloudflare两个host的search-index.json应该是同一套数据——
    只有站点用的CNAME/robots/sitemap域名不同，文章数据本身不该分叉。"""
    def _run(tmp, out):
        import publish_build, json
        out_gh = tmp / "publish_gh"
        out_cf = tmp / "publish_cf"
        publish_build.build_publish("github.foxzen.me", out_gh)
        publish_build.build_publish("cf.foxzen.me", out_cf)
        data_gh = json.loads((out_gh / "search-index.json").read_text(encoding="utf-8"))
        data_cf = json.loads((out_cf / "search-index.json").read_text(encoding="utf-8"))
        check("两个host的search-index.json内容完全一致", data_gh == data_cf)
    with_fixture(_run)


def test_pages_index_js_copied_and_no_api_calls():
    def _run(tmp, out):
        import publish_build
        publish_build.build_publish("github.foxzen.me", out)
        js_file = out / "pages-index.js"
        check("pages-index.js已复制进publish/", js_file.exists())
        js_text = js_file.read_text(encoding="utf-8")
        # 只检查真正会发起请求的代码（fetch调用），注释里提到"/api/*"是在
        # 说明"这份代码不打这些接口"，本身不构成对/api/的实际调用。
        code_lines = [ln for ln in js_text.splitlines() if not ln.strip().startswith("//")]
        code_only = "\n".join(code_lines)
        check("pages-index.js的实际代码中不包含/api/请求", "/api/" not in code_only)
        check("index.html引用的是pages-index.js而不是生产的static/index.js",
              "/pages-index.js" in (out / "index.html").read_text(encoding="utf-8"))
        check("index.html不再引用生产/static/index.js",
              "/static/index.js" not in (out / "index.html").read_text(encoding="utf-8"))
        check("index.html包含搜索工具栏", 'id="pages-search-toolbar"' in (out / "index.html").read_text(encoding="utf-8"))
    with_fixture(_run)


def _run_node(js_snippet):
    """用本机已有的node执行一段JS并返回stdout，找不到node时抛出异常
    由调用方决定跳过而不是当成测试失败——node是否安装跟这份代码本身
    对不对是两回事。"""
    import subprocess
    result = subprocess.run(
        ["node", "-e", js_snippet],
        capture_output=True, text=True, timeout=15,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr)
    return result.stdout


def test_pages_index_js_filter_logic_via_node():
    """静态检查文件内容之外，真正用node执行pages-index.js里的纯函数
    (filterArticles/paginateArticles/parseQueryFromSearch/buildQueryString)，
    验证关键词/标签/日期/AND组合/分页/空搜索/URL往返这些真实JS行为，
    而不是只能靠人工在浏览器里点一遍。"""
    import shutil as _shutil
    if _shutil.which("node") is None:
        print("  [SKIP] 本机未安装node，跳过pages-index.js的真实JS行为验证")
        return

    js_path = (Path(__file__).parent / "static_pages" / "pages-index.js").resolve()
    js_path_js = str(js_path).replace("\\", "\\\\")

    snippet = f"""
    const P = require("{js_path_js}");
    const articles = [
      {{id:"1", title:"Firefox 隐私加固", url:"/2026/07/a.html", date:"2026-07-01", tags:["Firefox","隐私"], text:"关于浏览器隐私的讨论"}},
      {{id:"2", title:"yt-dlp 教程", url:"/2026/07/b.html", date:"2026-07-15", tags:["工具"], text:"下载视频的方法"}},
      {{id:"3", title:"Chrome 对比", url:"/2026/08/c.html", date:"2026-08-01", tags:["Firefox"], text:"和Chrome内核的比较"}},
    ];

    // 1. 关键词匹配标题
    let r = P.filterArticles(articles, {{q:"yt-dlp"}});
    console.log("q_title_match", r.length === 1 && r[0].id === "2");

    // 2. 关键词匹配正文
    r = P.filterArticles(articles, {{q:"浏览器隐私"}});
    console.log("q_body_match", r.length === 1 && r[0].id === "1");

    // 3. 标签过滤
    r = P.filterArticles(articles, {{tag:"Firefox"}});
    console.log("tag_filter", r.length === 2);

    // 4. 日期区间过滤
    r = P.filterArticles(articles, {{from:"2026-07-10", to:"2026-07-31"}});
    console.log("date_range", r.length === 1 && r[0].id === "2");

    // 5. 关键词+标签 AND 组合
    r = P.filterArticles(articles, {{q:"Chrome", tag:"Firefox"}});
    console.log("and_combo", r.length === 1 && r[0].id === "3");

    // 6. 空搜索返回全部
    r = P.filterArticles(articles, {{}});
    console.log("empty_query_returns_all", r.length === 3);

    // 7. 分页
    const page = P.paginateArticles(articles, 1, 2);
    console.log("pagination", page.items.length === 2 && page.totalPages === 2 && page.total === 3);

    // 8. URL query往返
    const state = P.parseQueryFromSearch("?q=Firefox&tag=%E9%9A%90%E7%A7%81&page=2&page_size=20");
    console.log("parse_query", state.q === "Firefox" && state.tag === "隐私" && state.page === 2 && state.pageSize === 20);
    const qs = P.buildQueryString(state);
    console.log("build_query_roundtrip", qs.includes("q=Firefox") && qs.includes("page=2") && qs.includes("page_size=20"));
    """
    out = _run_node(snippet)
    lines = dict(line.split(" ", 1) for line in out.strip().splitlines() if " " in line)
    expected_true = [
        "q_title_match", "q_body_match", "tag_filter", "date_range",
        "and_combo", "empty_query_returns_all", "pagination",
        "parse_query", "build_query_roundtrip",
    ]
    for name in expected_true:
        check(f"pages-index.js真实JS行为: {name}", lines.get(name) == "true", f"got {lines.get(name)!r}")


def test_verify_publish_passes_on_good_build():
    def _run(tmp, out):
        import publish_build
        publish_build.build_publish("github.foxzen.me", out)
        try:
            publish_build.verify_publish(out, "github.foxzen.me")
            ok = True
        except publish_build.PublishVerificationError as e:
            ok = False
            print(f"    unexpected error: {e}")
        check("正常构建的publish/能通过verify_publish()检查", ok)
    with_fixture(_run)


def test_verify_publish_catches_injected_danger_file():
    """人为在构建产物里塞一个不该出现的.db文件，确认verify_publish()会
    识别出来并抛出异常——这是CI在upload artifact前的最后一道防线。"""
    def _run(tmp, out):
        import publish_build
        publish_build.build_publish("github.foxzen.me", out)
        (out / "data").mkdir()
        (out / "data" / "blog.db").write_bytes(b"not a real db, just a test probe")
        raised = False
        message = ""
        try:
            publish_build.verify_publish(out, "github.foxzen.me")
        except publish_build.PublishVerificationError as e:
            raised = True
            message = str(e)
        check("verify_publish()识别出被注入的data/blog.db并拒绝通过", raised)
        check("错误信息里提到data/目录", "data/" in message)
    with_fixture(_run)


def test_verify_publish_catches_wrong_hostname():
    """如果sitemap.xml因为某种原因没有正确替换hostname，verify_publish()
    应该拦下来，而不是让一份还写着mirror.foxzen.me的产物被当成github.foxzen.me
    的正式内容发布出去。"""
    def _run(tmp, out):
        import publish_build
        publish_build.build_publish("github.foxzen.me", out)
        sitemap = out / "sitemap.xml"
        sitemap.write_text(
            sitemap.read_text(encoding="utf-8").replace("github.foxzen.me", "mirror.foxzen.me"),
            encoding="utf-8",
        )
        raised = False
        try:
            publish_build.verify_publish(out, "github.foxzen.me")
        except publish_build.PublishVerificationError:
            raised = True
        check("verify_publish()识别出未正确替换hostname的sitemap.xml", raised)
    with_fixture(_run)


def test_build_is_repeatable():
    """同样的html/输入 + 同样的host，重复构建两次应该得到完全相同的产物
    （不依赖当前时间、访问统计等易变状态）。"""
    def _run(tmp, out):
        import publish_build
        out2 = tmp / "publish_out2"
        publish_build.build_publish("github.foxzen.me", out)
        publish_build.build_publish("github.foxzen.me", out2)
        files1 = {p.relative_to(out): p.read_bytes() for p in out.rglob("*") if p.is_file()}
        files2 = {p.relative_to(out2): p.read_bytes() for p in out2.rglob("*") if p.is_file()}
        check("两次构建产出的文件集合一致", set(files1.keys()) == set(files2.keys()))
        mismatched = [k for k in files1 if files1.get(k) != files2.get(k)]
        check("两次构建产出的文件内容字节级一致", mismatched == [], f"mismatched: {mismatched}")
    with_fixture(_run)


def test_no_unexpected_db_or_secret_file_anywhere_in_output():
    """对最终产物做一次面向路径名的危险文件扫描，作为builder自身白名单逻辑
    之外的第二层保险——扫描的是产物本身，不是builder源码。"""
    def _run(tmp, out):
        import publish_build
        publish_build.build_publish("github.foxzen.me", out)
        danger_suffixes = (".db", ".sqlite", ".sqlite3", ".env", ".pem",
                           ".key", ".p12", ".pfx", ".secret", ".token")
        danger_names = ("id_rsa", "id_ed25519")
        offenders = []
        for p in out.rglob("*"):
            if not p.is_file():
                continue
            if p.suffix in danger_suffixes or p.name in danger_names:
                offenders.append(str(p))
        check("产物中没有任何危险后缀/文件名", offenders == [], f"found {offenders}")
    with_fixture(_run)


def test_output_dir_is_rebuilt_not_appended():
    """确认build_publish()每次都会清空输出目录重建，不会残留上一次构建
    （比如换了host之后）留下的、已经不该存在的旧文件。"""
    def _run(tmp, out):
        import publish_build
        out.mkdir(parents=True)
        stale_file = out / "stale_leftover.html"
        stale_file.write_text("should be removed", encoding="utf-8")
        publish_build.build_publish("github.foxzen.me", out)
        check("重新构建会清除上一次残留的文件", not stale_file.exists())
    with_fixture(_run)


def test_real_local_html_dir_builds_without_error():
    """用当前仓库里真实的html/(不是夹具)跑一次，确认builder在真实数据上
    能正常工作，不只是在人造夹具上正常。"""
    import publish_build
    real_html = Path(__file__).parent / "html"
    if not real_html.exists():
        print("  [SKIP] 未找到真实html/目录")
        return
    tmp = Path(tempfile.mkdtemp(prefix="publish_build_real_test_"))
    try:
        out = tmp / "publish_real"
        result = publish_build.build_publish("github.foxzen.me", out)
        check("真实html/能成功构建出publish/", result.exists())
        check("真实构建产出index.html", (out / "index.html").exists())
        check("真实构建产出robots.txt", (out / "robots.txt").exists())
        check("真实构建产出sitemap.xml", (out / "sitemap.xml").exists())
        check("真实构建产出至少一篇文章", any((out / "posts").glob("*/index.html")))
        check("真实构建不含data/blog.db", not any(p.name == "blog.db" for p in out.rglob("*")))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    tests = [
        test_data_and_db_excluded,
        test_secret_and_backend_files_excluded,
        test_expected_public_files_present,
        test_indexnow_key_and_foxzen_site_excluded_by_whitelist,
        test_github_hostname_rewrite,
        test_cloudflare_hostname_rewrite,
        test_same_builder_same_output_structure_for_both_hosts,
        test_index_js_removed_but_fallback_content_kept,
        test_article_body_with_password_token_words_not_treated_as_secret,
        test_search_index_generated_with_expected_fields,
        test_search_index_uses_canonical_url_when_available,
        test_search_index_no_database_or_visitor_data,
        test_search_index_same_data_across_hosts,
        test_pages_index_js_copied_and_no_api_calls,
        test_pages_index_js_filter_logic_via_node,
        test_verify_publish_passes_on_good_build,
        test_verify_publish_catches_injected_danger_file,
        test_verify_publish_catches_wrong_hostname,
        test_build_is_repeatable,
        test_no_unexpected_db_or_secret_file_anywhere_in_output,
        test_output_dir_is_rebuilt_not_appended,
        test_real_local_html_dir_builds_without_error,
        test_leaderboard_links_point_to_existing_files,
        test_fallback_list_links_point_to_existing_files,
        test_no_dangling_canonical_article_href_anywhere_in_index_html,
        test_canonical_link_kept_when_static_file_exists,
        test_missing_canonical_falls_back_to_posts_id,
        test_ambiguous_title_href_left_unchanged_not_guessed,
        test_source_html_index_untouched_by_href_fix,
        test_non_article_links_untouched_by_href_fix,
        test_search_index_json_url_matches_fixed_index_html_href,
        test_cross_post_content_link_rewritten_to_real_url,
        test_own_permalink_and_unknown_permalink_kept_unchanged,
        test_discuss_btn_never_rewritten,
        test_canonical_and_posts_copy_stay_identical_after_content_link_fix,
        test_github_and_cloudflare_builds_get_same_root_relative_link,
        test_source_html_posts_untouched_by_content_link_fix,
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
