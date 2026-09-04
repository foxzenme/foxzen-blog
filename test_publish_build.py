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
        # standalone_url/media_files是下载功能新增的字段，记录里应该正好
        # 是这8个字段，不多不少——多了说明有人不小心加了不该出现的字段
        # （比如visitor/page_hit），少了说明下载功能相关字段漏生成了。
        check("记录字段正好是这8个(含下载功能新增的standalone_url/media_files)",
              set(a.keys()) == {"id", "title", "url", "date", "tags", "text",
                                 "standalone_url", "media_files"})
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


# ---------------------------------------------------------------------------
# 首页品牌文案规范化（_normalize_brand_heading）：html/是从服务器下载下来的
# 快照，可能停留在fetch_blog.py品牌文案修改之前的旧版本，这几个测试独立
# 构造最小的index.html夹具，不复用_make_fixture_html_dir()，避免影响
# 上面其他已经通过的测试。
# ---------------------------------------------------------------------------

_OLD_BRAND_HEADING = "统计学习小议 - 镜像站"


def _make_brand_fixture_html_dir(tmp, title_and_h1_text):
    html_dir = tmp / "html"
    html_dir.mkdir()
    (html_dir / "index.html").write_text(
        "<!DOCTYPE html><html><head>"
        f"<title>{title_and_h1_text}</title></head><body>"
        f"<h1>{title_and_h1_text}</h1>"
        '<div class="archive-note">测试正文里恰好也提到"'
        f'{_OLD_BRAND_HEADING}"这几个字，不应该被品牌规范化逻辑误改。</div>'
        '<div id="app"></div><script src="/static/index.js"></script>'
        "</body></html>",
        encoding="utf-8",
    )
    return html_dir


def with_brand_fixture(title_and_h1_text, fn):
    import publish_build
    tmp = Path(tempfile.mkdtemp(prefix="publish_build_brand_test_"))
    orig_html_dir = publish_build.HTML_DIR
    fixture_html = _make_brand_fixture_html_dir(tmp, title_and_h1_text)
    publish_build.HTML_DIR = fixture_html
    output_dir = tmp / "publish_out"
    try:
        fn(tmp, output_dir)
    finally:
        publish_build.HTML_DIR = orig_html_dir
        shutil.rmtree(tmp, ignore_errors=True)


def test_stale_brand_heading_is_normalized_to_current_brand():
    """html/index.html还停留在旧品牌"统计学习小议 - 镜像站"时，构建产物的
    <title>/<h1>必须被规范化成当前品牌"狐斋志异 - 镜像站"。"""
    def _run(tmp, out):
        import publish_build
        publish_build.build_publish("github.foxzen.me", out)
        content = (out / "index.html").read_text(encoding="utf-8")
        check("旧品牌<title>被规范化成当前品牌",
              f"<title>{publish_build.BRAND_HEADING}</title>" in content)
        check("旧品牌<h1>被规范化成当前品牌",
              f"<h1>{publish_build.BRAND_HEADING}</h1>" in content)
        check("正文里恰好出现的旧品牌字样原样保留，未被全局误改",
              f'测试正文里恰好也提到"{_OLD_BRAND_HEADING}"' in content)
    with_brand_fixture(_OLD_BRAND_HEADING, _run)


def test_current_brand_heading_is_left_unchanged_idempotent():
    """html/index.html已经是当前品牌时，构建产物必须保持不变——规范化
    逻辑对"已经正确"的输入必须是幂等的，不产生重复标签或多余改动。"""
    def _run(tmp, out):
        import publish_build
        publish_build.build_publish("github.foxzen.me", out)
        content = (out / "index.html").read_text(encoding="utf-8")
        check("已经是当前品牌的<title>保持不变",
              f"<title>{publish_build.BRAND_HEADING}</title>" in content)
        check("已经是当前品牌的<h1>保持不变",
              f"<h1>{publish_build.BRAND_HEADING}</h1>" in content)
        check("只有一个<title>标签（未被重复处理）", content.count("<title>") == 1)
        check("只有一个<h1>标签（未被重复处理）", content.count("<h1>") == 1)
    import publish_build
    with_brand_fixture(publish_build.BRAND_HEADING, _run)


def test_github_and_cf_builds_both_get_normalized_brand():
    """github.foxzen.me和cf.foxzen.me两个host的构建产物在品牌文案上
    必须一致，都是规范化之后的当前品牌。"""
    import publish_build
    tmp = Path(tempfile.mkdtemp(prefix="publish_build_brand_test2_"))
    orig_html_dir = publish_build.HTML_DIR
    fixture_html = _make_brand_fixture_html_dir(tmp, _OLD_BRAND_HEADING)
    publish_build.HTML_DIR = fixture_html
    try:
        out_gh, out_cf = tmp / "out_gh", tmp / "out_cf"
        publish_build.build_publish("github.foxzen.me", out_gh)
        publish_build.build_publish("cf.foxzen.me", out_cf)
        for out in (out_gh, out_cf):
            content = (out / "index.html").read_text(encoding="utf-8")
            check(f"{out.name}: <title>是当前品牌",
                  f"<title>{publish_build.BRAND_HEADING}</title>" in content)
            check(f"{out.name}: <h1>是当前品牌",
                  f"<h1>{publish_build.BRAND_HEADING}</h1>" in content)
    finally:
        publish_build.HTML_DIR = orig_html_dir
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# 下载/离线导出功能（方案A）：standalone HTML生成 + 固定范围三类zip
# （全站/全部/按标签）+ 浏览器端JSZip必需的本地vendor文件 + verify_publish()
# 的硬失败检查。独立夹具，贴近真实POST_TEMPLATE的meta-precise/discuss-btn/
# GA脚本块/完读特效块/图片引用这几个关键区块，不复用/不影响上面其他测试。
# ---------------------------------------------------------------------------

def _make_download_post_html(post_id, title, tags, own_permalink, cross_ref_permalink=None):
    tags_html = "".join(f'<a href="/index.html?tag={t}">#{t}</a>' for t in tags)
    cross_ref = (f'<p>参见<a href="{cross_ref_permalink}">另一篇</a>。</p>'
                 if cross_ref_permalink else "")
    return (
        "<!DOCTYPE html><html><head>"
        "<!-- GA_START --><script>ga_tracking_code</script><!-- GA_END -->\n"
        f"<title>{title}</title></head><body>"
        f"<h1>{title}</h1>"
        '<div class="meta">发布于 2026-08-01</div>'
        '<div class="meta-precise">最初发布：2026-08-01 00:00:00 (UTC) · '
        '最后修改：2026-08-02 03:04:05 (UTC) · 全文100字 · 预计阅读1分钟</div>'
        f'<div class="tags">{tags_html}</div>'
        f'<div class="content"><p>正文。<img src="/posts/{post_id}/media/pic.png"></p>{cross_ref}</div>'
        '<div class="discuss-cta">'
        f'<a class="discuss-btn" href="{own_permalink}" target="_blank" rel="noopener">💬 到主站参与讨论</a>'
        "</div>"
        '<a class="back" href="/" onclick="if (history.length > 1) '
        '{ history.back(); return false; }">&larr; 返回目录</a>'
        "<!-- FINISH_READ_START --><div>finish celebration</div><!-- FINISH_READ_END -->"
        "</body></html>"
    )


def _make_download_fixture_html_dir(tmp):
    html_dir = tmp / "html"
    html_dir.mkdir()
    (html_dir / "index.html").write_text(
        '<html><body><div id="app"></div>'
        '<script src="/static/index.js"></script>'
        "</body></html>",
        encoding="utf-8",
    )
    (html_dir / "robots.txt").write_text(
        "User-agent: *\nAllow: /\nSitemap: https://mirror.foxzen.me/sitemap.xml\n", encoding="utf-8")
    (html_dir / "sitemap.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        '<url><loc>https://mirror.foxzen.me/</loc></url>\n</urlset>\n',
        encoding="utf-8",
    )
    (html_dir / "404").mkdir()
    (html_dir / "404" / "index.html").write_text("<html>404</html>", encoding="utf-8")

    post_a_html = _make_download_post_html(
        "post-a", "文章A", ["Firefox", "理念，备份"],
        "https://digatlas.blogspot.com/2026/08/article-a.html",
        cross_ref_permalink="https://digatlas.blogspot.com/2026/08/article-b.html")
    post_b_html = _make_download_post_html(
        "post-b", "文章B", ["Firefox"],
        "https://digatlas.blogspot.com/2026/08/article-b.html")

    for post_id, post_html in (("post-a", post_a_html), ("post-b", post_b_html)):
        post_dir = html_dir / "posts" / post_id
        post_dir.mkdir(parents=True)
        (post_dir / "index.html").write_text(post_html, encoding="utf-8")
        media_dir = post_dir / "media"
        media_dir.mkdir()
        (media_dir / "pic.png").write_bytes(b"\x89PNG-fake-bytes-for-test")

    return html_dir


def with_download_fixture(fn):
    import publish_build
    tmp = Path(tempfile.mkdtemp(prefix="publish_build_download_test_"))
    orig_html_dir = publish_build.HTML_DIR
    fixture_html = _make_download_fixture_html_dir(tmp)
    publish_build.HTML_DIR = fixture_html
    output_dir = tmp / "publish_out"
    try:
        fn(tmp, output_dir)
    finally:
        publish_build.HTML_DIR = orig_html_dir
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# standalone zip里文章条目命名规则(_zip_arcname_for_article/_dedupe_zip_arcname)
# 专用夹具：跟mirror app.py::_zip_arcname_for()对齐，需要同时覆盖
# canonical(YYYY/MM/slug.html)、fallback(标题清洗)、撞名消解三种场景，
# 跟_make_download_fixture_html_dir()（只有fallback场景）独立，不互相影响。
# 四篇文章标题固定"发布于 2026-08-01"（同日期），_build_search_index()按
# 日期降序做稳定排序，同日期时保留posts/目录名字典序——四个目录名刻意按
# post-canon < post-collide-a < post-collide-b < post-fallback排列，
# 让处理顺序（因而撞名消解的先后）完全确定，不依赖随机性。
# ---------------------------------------------------------------------------

def _make_arcname_fixture_html_dir(tmp):
    html_dir = tmp / "html"
    html_dir.mkdir()
    (html_dir / "index.html").write_text(
        '<html><body><div id="app"></div>'
        '<script src="/static/index.js"></script>'
        "</body></html>",
        encoding="utf-8",
    )
    (html_dir / "robots.txt").write_text(
        "User-agent: *\nAllow: /\nSitemap: https://mirror.foxzen.me/sitemap.xml\n", encoding="utf-8")
    (html_dir / "sitemap.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        '<url><loc>https://mirror.foxzen.me/</loc></url>\n</urlset>\n',
        encoding="utf-8",
    )
    (html_dir / "404").mkdir()
    (html_dir / "404" / "index.html").write_text("<html>404</html>", encoding="utf-8")

    posts = [
        ("post-canon", "带Canonical的文章", True),
        ("post-collide-a", "撞名文章", False),
        ("post-collide-b", "撞名文章", False),
        ("post-fallback", "无Canonical回退文章", False),
    ]
    for post_id, title, has_canonical in posts:
        post_html = _make_download_post_html(
            post_id, title, ["ArcnameTag"],
            f"https://digatlas.blogspot.com/2026/08/{post_id}.html")
        post_dir = html_dir / "posts" / post_id
        post_dir.mkdir(parents=True)
        (post_dir / "index.html").write_text(post_html, encoding="utf-8")
        media_dir = post_dir / "media"
        media_dir.mkdir()
        (media_dir / "pic.png").write_bytes(b"\x89PNG-fake-bytes-for-test")
        if has_canonical:
            canonical_dir = html_dir / "2026" / "08"
            canonical_dir.mkdir(parents=True, exist_ok=True)
            (canonical_dir / "my-canonical-slug.html").write_text(post_html, encoding="utf-8")

    return html_dir


def with_arcname_fixture(fn):
    import publish_build
    tmp = Path(tempfile.mkdtemp(prefix="publish_build_arcname_test_"))
    orig_html_dir = publish_build.HTML_DIR
    fixture_html = _make_arcname_fixture_html_dir(tmp)
    publish_build.HTML_DIR = fixture_html
    output_dir = tmp / "publish_out"
    try:
        fn(tmp, output_dir)
    finally:
        publish_build.HTML_DIR = orig_html_dir
        shutil.rmtree(tmp, ignore_errors=True)


def test_standalone_html_generated_for_every_article():
    def _run(tmp, out):
        import publish_build
        publish_build.build_publish("github.foxzen.me", out)
        for post_id in ("post-a", "post-b"):
            check(f"standalone/{post_id}.html存在", (out / "standalone" / f"{post_id}.html").exists())
    with_download_fixture(_run)


def test_standalone_html_inlines_images_strips_scripts_and_back_link():
    def _run(tmp, out):
        import publish_build
        publish_build.build_publish("github.foxzen.me", out)
        content = (out / "standalone" / "post-a.html").read_text(encoding="utf-8")
        check("图片已base64内联", "data:image/png;base64," in content)
        check("不再有media/相对路径的img src", "media/pic.png" not in content)
        check("GA脚本块已剥离", "GA_START" not in content and "ga_tracking_code" not in content)
        check("完读特效脚本块已剥离", "FINISH_READ_START" not in content and "finish celebration" not in content)
        check("返回目录链接已移除", "返回目录" not in content)
        check("来源信息条包含本文自己的Blogger permalink",
              "https://digatlas.blogspot.com/2026/08/article-a.html" in content)
        check("来源信息条包含精确的最后修改时间", "最后修改时间：2026-08-02 03:04:05 (UTC)" in content)
        check("交叉引用另一篇文章的Blogger permalink已修正为相对地址",
              "https://digatlas.blogspot.com/2026/08/article-b.html" not in content)
        check("交叉引用指向post-b的真实相对地址", 'href="/posts/post-b/"' in content)
    with_download_fixture(_run)


def test_search_index_has_standalone_and_media_fields():
    def _run(tmp, out):
        import publish_build, json
        publish_build.build_publish("github.foxzen.me", out)
        data = json.loads((out / "search-index.json").read_text(encoding="utf-8"))
        by_id = {a["id"]: a for a in data["articles"]}
        check("post-a的standalone_url正确", by_id["post-a"]["standalone_url"] == "/standalone/post-a.html")
        check("post-a的media_files包含pic.png", "pic.png" in by_id["post-a"]["media_files"])
        check("standalone_url指向真实存在的文件",
              (out / by_id["post-a"]["standalone_url"].lstrip("/")).exists())
    with_download_fixture(_run)


def test_blog_full_zip_contains_original_articles_and_index():
    def _run(tmp, out):
        import publish_build, zipfile
        publish_build.build_publish("github.foxzen.me", out)
        with zipfile.ZipFile(out / "downloads" / "blog-full.zip") as zf:
            names = zf.namelist()
            check("包含post-a原始文章", "posts/post-a/index.html" in names)
            check("包含post-a的媒体文件", "posts/post-a/media/pic.png" in names)
            check("包含首页index.html", "index.html" in names)
            dangerous = [n for n in names if n.endswith((".db", ".py", ".env", ".pem", ".key", ".secret", ".token"))]
            check("不包含危险后缀文件", dangerous == [], f"found {dangerous}")
    with_download_fixture(_run)


def test_export_all_zip_contains_standalone_versions_not_raw():
    def _run(tmp, out):
        import publish_build, zipfile
        publish_build.build_publish("github.foxzen.me", out)
        with zipfile.ZipFile(out / "downloads" / "export-all.zip") as zf:
            names = zf.namelist()
            check("export-all.zip不是posts/目录结构（用的是standalone扁平文件）",
                  not any(n.startswith("posts/") for n in names))
            check("export-all.zip里的文件数等于文章数", len(names) == 2, f"got {names}")
            sample = zf.read(names[0]).decode("utf-8")
            check("zip内是已经base64内联的standalone版本", "data:image/png;base64," in sample)
    with_download_fixture(_run)


def test_export_tag_zip_only_contains_matching_tag_articles():
    def _run(tmp, out):
        import publish_build, zipfile
        publish_build.build_publish("github.foxzen.me", out)
        unique_tag_zip = out / "downloads" / "export-tag" / "理念，备份.zip"
        check("只有post-a有的标签，对应zip存在", unique_tag_zip.exists())
        with zipfile.ZipFile(unique_tag_zip) as zf:
            check("只包含1篇文章", len(zf.namelist()) == 1, f"got {zf.namelist()}")
        shared_tag_zip = out / "downloads" / "export-tag" / "Firefox.zip"
        check("两篇文章共有的标签，对应zip存在", shared_tag_zip.exists())
        with zipfile.ZipFile(shared_tag_zip) as zf:
            check("两篇文章都属于Firefox标签时zip包含2篇（有意的重叠，不去重）",
                  len(zf.namelist()) == 2, f"got {zf.namelist()}")
    with_download_fixture(_run)


# ---------------------------------------------------------------------------
# export-all.zip / export-tag/*.zip 内部文章条目命名——跟mirror
# app.py::_zip_arcname_for()对齐：canonical文章用真实YYYY/MM/slug.html目录
# 结构，fallback文章用清洗后的标题，都不直接暴露post_id；撞名时用
# -{post_id}消解，不静默覆盖。用上面的with_arcname_fixture()验证。
# ---------------------------------------------------------------------------

def test_export_all_zip_uses_year_month_directory_for_canonical_articles():
    def _run(tmp, out):
        import publish_build, zipfile
        publish_build.build_publish("github.foxzen.me", out)
        with zipfile.ZipFile(out / "downloads" / "export-all.zip") as zf:
            names = zf.namelist()
            check("有canonical地址的文章用YYYY/MM/slug.html目录结构",
                  "2026/08/my-canonical-slug.html" in names, f"got {names}")
            check("不再是拍平的YYYY-MM-slug.html", "2026-08-my-canonical-slug.html" not in names)
    with_arcname_fixture(_run)


def test_export_all_zip_filenames_not_raw_post_id():
    def _run(tmp, out):
        import publish_build, zipfile
        publish_build.build_publish("github.foxzen.me", out)
        with zipfile.ZipFile(out / "downloads" / "export-all.zip") as zf:
            names = zf.namelist()
            for post_id in ("post-canon", "post-collide-a", "post-fallback"):
                check(f"文件名不直接是post_id: {post_id}",
                      f"{post_id}.html" not in names, f"got {names}")
            check("没有canonical的文章用清洗后的标题命名",
                  "无Canonical回退文章.html" in names, f"got {names}")
    with_arcname_fixture(_run)


def test_export_all_zip_title_filename_matches_safe_filename_rule():
    """fallback文件名必须跟_safe_article_filename()（对齐app.py::
    _safe_filename()）算出来的结果完全一致，不是另一套规则。"""
    def _run(tmp, out):
        import publish_build, zipfile
        publish_build.build_publish("github.foxzen.me", out)
        expected = publish_build._safe_article_filename("无Canonical回退文章") + ".html"
        with zipfile.ZipFile(out / "downloads" / "export-all.zip") as zf:
            check("fallback文件名跟_safe_article_filename()算出的结果一致",
                  expected in zf.namelist(), f"expected {expected!r}, got {zf.namelist()}")
    with_arcname_fixture(_run)


def test_export_all_zip_no_silent_filename_collision():
    """两篇标题完全相同(都没有canonical地址)的文章，必须都出现在zip里，
    用不同的最终文件名——不能因为撞名互相覆盖丢内容。"""
    def _run(tmp, out):
        import publish_build, zipfile
        publish_build.build_publish("github.foxzen.me", out)
        with zipfile.ZipFile(out / "downloads" / "export-all.zip") as zf:
            names = zf.namelist()
            colliding = [n for n in names if n.startswith("撞名文章")]
            check("撞名的两篇文章都在zip里，各自占一个不同文件名",
                  len(colliding) == 2 and len(set(colliding)) == 2, f"got {colliding}")
            check("其中一个是原名，另一个带post_id消解后缀",
                  "撞名文章.html" in colliding and "撞名文章-post-collide-b.html" in colliding,
                  f"got {colliding}")
    with_arcname_fixture(_run)


def test_export_tag_zip_uses_same_naming_structure():
    def _run(tmp, out):
        import publish_build, zipfile
        publish_build.build_publish("github.foxzen.me", out)
        tag_zip = out / "downloads" / "export-tag" / "ArcnameTag.zip"
        check("对应标签zip存在", tag_zip.exists())
        with zipfile.ZipFile(tag_zip) as zf:
            names = zf.namelist()
            check("export-tag/*.zip里canonical文章也用YYYY/MM/slug.html",
                  "2026/08/my-canonical-slug.html" in names, f"got {names}")
            check("export-tag/*.zip里没有裸post_id文件名",
                  not any(n.startswith("post-") and n.endswith(".html") for n in names), f"got {names}")
            check("export-tag/*.zip里同样正确消解了撞名",
                  "撞名文章.html" in names and "撞名文章-post-collide-b.html" in names, f"got {names}")
    with_arcname_fixture(_run)


def test_export_all_zip_arcnames_have_no_path_traversal_or_absolute_path():
    def _run(tmp, out):
        import publish_build, zipfile
        publish_build.build_publish("github.foxzen.me", out)
        with zipfile.ZipFile(out / "downloads" / "export-all.zip") as zf:
            for name in zf.namelist():
                check(f"zip条目名不含'..': {name}", ".." not in name.split("/"))
                check(f"zip条目名不是绝对路径: {name}", not name.startswith("/"))
    with_arcname_fixture(_run)


def test_export_all_zip_still_contains_correct_standalone_html_content():
    """改的只是zip里的文件名/目录结构，内容本身必须仍然是_render_standalone_
    html()生成的正确standalone HTML（图片base64内联、GA/完读脚本已剥离），
    不能因为改命名规则顺带改坏了内容。"""
    def _run(tmp, out):
        import publish_build, zipfile
        publish_build.build_publish("github.foxzen.me", out)
        with zipfile.ZipFile(out / "downloads" / "export-all.zip") as zf:
            content = zf.read("2026/08/my-canonical-slug.html").decode("utf-8")
            check("archive内仍是base64内联后的standalone内容", "data:image/png;base64," in content)
            check("archive内容已剥离GA脚本块", "GA_START" not in content and "ga_tracking_code" not in content)
            check("archive内容已剥离完读脚本块", "FINISH_READ_START" not in content)
    with_arcname_fixture(_run)


def test_verify_publish_hard_fails_when_standalone_missing():
    """核心下载产物生成失败必须让整个构建失败，不能静默发布一个缺下载
    功能的Pages——这是本轮明确要求的hard fail，不是warning。"""
    def _run(tmp, out):
        import publish_build
        publish_build.build_publish("github.foxzen.me", out)
        (out / "standalone" / "post-a.html").unlink()
        try:
            publish_build.verify_publish(out, "github.foxzen.me")
            check("standalone文件缺失时verify_publish应该抛异常", False, "但没有抛出")
        except publish_build.PublishVerificationError as e:
            check("standalone缺失被判定为硬失败", "post-a.html" in str(e))
    with_download_fixture(_run)


def test_verify_publish_hard_fails_when_fixed_zip_missing():
    def _run(tmp, out):
        import publish_build
        publish_build.build_publish("github.foxzen.me", out)
        (out / "downloads" / "blog-full.zip").unlink()
        try:
            publish_build.verify_publish(out, "github.foxzen.me")
            check("blog-full.zip缺失时verify_publish应该抛异常", False, "但没有抛出")
        except publish_build.PublishVerificationError as e:
            check("blog-full.zip缺失被判定为硬失败（_REQUIRED_FILES）", "blog-full.zip" in str(e))
    with_download_fixture(_run)


def test_verify_publish_hard_fails_when_tag_zip_missing():
    def _run(tmp, out):
        import publish_build
        publish_build.build_publish("github.foxzen.me", out)
        (out / "downloads" / "export-tag" / "Firefox.zip").unlink()
        try:
            publish_build.verify_publish(out, "github.foxzen.me")
            check("标签zip缺失时verify_publish应该抛异常", False, "但没有抛出")
        except publish_build.PublishVerificationError as e:
            check("标签zip缺失被判定为硬失败", "Firefox" in str(e))
    with_download_fixture(_run)


def test_verify_publish_hard_fails_on_dangerous_file_inside_zip():
    def _run(tmp, out):
        import publish_build, zipfile
        publish_build.build_publish("github.foxzen.me", out)
        zip_path = out / "downloads" / "export-all.zip"
        with zipfile.ZipFile(zip_path, "a") as zf:
            zf.writestr("leaked_secret.env", "SECRET=1")
        try:
            publish_build.verify_publish(out, "github.foxzen.me")
            check("zip内混入.env文件时verify_publish应该抛异常", False, "但没有抛出")
        except publish_build.PublishVerificationError as e:
            check("zip内的危险文件被检测出来", "leaked_secret.env" in str(e))
    with_download_fixture(_run)


def test_jszip_vendored_locally_not_via_cdn():
    def _run(tmp, out):
        import publish_build
        publish_build.build_publish("github.foxzen.me", out)
        jszip_file = out / "jszip.min.js"
        check("jszip.min.js已作为本地静态文件发布", jszip_file.exists())
        content = jszip_file.read_text(encoding="utf-8", errors="ignore")
        check("确实是JSZip库本身而不是空文件/占位符", "JSZip" in content and len(content) > 10000)
        index_content = (out / "index.html").read_text(encoding="utf-8")
        check('index.html引用的是本地"/jszip.min.js"',
              '<script src="/jszip.min.js"></script>' in index_content)
        for banned in ("cdnjs.cloudflare.com", "unpkg.com", "jsdelivr.net"):
            check(f"index.html不引用外部CDN: {banned}", banned not in index_content)
    with_fixture(_run)


def test_index_html_has_all_five_download_buttons():
    def _run(tmp, out):
        import publish_build
        publish_build.build_publish("github.foxzen.me", out)
        content = (out / "index.html").read_text(encoding="utf-8")
        for role in ("download-all-btn", "download-selected-btn", "export-selected-btn",
                     "export-tag-btn", "export-all-btn"):
            check(f"首页包含下载按钮: {role}", f'data-role="{role}"' in content)
        check("首页引用pages-download.js", '<script src="/pages-download.js"></script>' in content)
    with_fixture(_run)


def test_pages_download_js_no_absolute_domains_or_cdn():
    src = (Path(__file__).parent / "static_pages" / "pages-download.js").read_text(encoding="utf-8")
    for domain in ("github.foxzen.me", "cf.foxzen.me", "backup.foxzen.me", "mirror.foxzen.me"):
        check(f"pages-download.js不写死域名: {domain}", domain not in src)
    for cdn in ("cdnjs.cloudflare.com", "unpkg.com", "jsdelivr.net"):
        check(f"pages-download.js不引用外部CDN: {cdn}", cdn not in src)


def test_python_and_js_tag_filename_sanitization_agree_on_python_side():
    """publish_build.py和pages-download.js里的文件名清洗/命名规则必须完全
    一致，否则浏览器现场拼的下载文件名会跟构建时生成的zip条目名对不上。
    两边各自独立实现（不共享代码），一致性靠这里和
    test_pages_download_js_pure_functions_via_node()用同一批输入分别断言
    同一个期望值来间接印证。这几条规则本身又是跟app.py::_safe_filename()/
    _zip_arcname_for()对齐的（见publish_build.py里的说明注释），不是本轮
    自创的清洗规则。"""
    import publish_build
    check("Python: 特殊字符清洗(标签)", publish_build._safe_tag_filename('a/b\\c:d*e?f"g<h>i|j') == "a_b_c_d_e_f_g_h_i_j")
    check("Python: 逗号中文标签原样保留",
          publish_build._safe_tag_filename("理念，备份方式，计算机知识") == "理念，备份方式，计算机知识")
    check("Python: 特殊字符清洗(文章标题)",
          publish_build._safe_article_filename('a/b\\c:d*e?f"g<h>i|j') == "a_b_c_d_e_f_g_h_i_j")
    check("Python: 文章标题超过80字符会被截断",
          publish_build._safe_article_filename("啊" * 100) == "啊" * 80)
    check("Python: 空标题回退成untitled", publish_build._safe_article_filename("   ") == "untitled")
    check("Python: zip条目名(canonical地址)——保留YYYY/MM目录结构，不拍平",
          publish_build._zip_arcname_for_article({"id": "x", "url": "/2026/08/slug.html", "title": "无关"}) == "2026/08/slug.html")
    check("Python: zip条目名(fallback地址)——用标题而不是post_id",
          publish_build._zip_arcname_for_article({"id": "post-x", "url": "/posts/post-x/", "title": "回退标题"}) == "回退标题.html")
    used = set()
    first = publish_build._dedupe_zip_arcname("撞名文章.html", "post-a", used)
    second = publish_build._dedupe_zip_arcname("撞名文章.html", "post-b", used)
    check("Python: 撞名消解——第一个保留原名", first == "撞名文章.html")
    check("Python: 撞名消解——第二个在扩展名前插入post_id", second == "撞名文章-post-b.html")
    check("Python: 撞名消解——两次结果不相同(不静默覆盖)", first != second)


def test_pages_download_js_pure_functions_via_node():
    import shutil as _shutil
    if _shutil.which("node") is None:
        print("  [SKIP] 本机未安装node，跳过pages-download.js的真实JS行为验证")
        return

    js_path = (Path(__file__).parent / "static_pages" / "pages-download.js").resolve()
    js_path_js = str(js_path).replace("\\", "\\\\")
    snippet = f"""
    const P = require("{js_path_js}");
    console.log("safe_tag_basic", P.safeTagFilename("Firefox") === "Firefox");
    console.log("safe_tag_special_chars", P.safeTagFilename('a/b\\\\c:d*e?f"g<h>i|j') === "a_b_c_d_e_f_g_h_i_j");
    console.log("safe_tag_comma_chinese", P.safeTagFilename("理念，备份方式，计算机知识") === "理念，备份方式，计算机知识");
    console.log("safe_article_special_chars", P.safeArticleFilename('a/b\\\\c:d*e?f"g<h>i|j') === "a_b_c_d_e_f_g_h_i_j");
    console.log("safe_article_truncated", P.safeArticleFilename("啊".repeat(100)) === "啊".repeat(80));
    console.log("safe_article_empty", P.safeArticleFilename("   ") === "untitled");
    console.log("arcname_canonical", P.zipArcnameForArticle({{id:"x", url:"/2026/08/slug.html", title:"无关"}}) === "2026/08/slug.html");
    console.log("arcname_fallback", P.zipArcnameForArticle({{id:"post-x", url:"/posts/post-x/", title:"回退标题"}}) === "回退标题.html");
    const used = {{}};
    const first = P.dedupeZipArcname("撞名文章.html", "post-a", used);
    const second = P.dedupeZipArcname("撞名文章.html", "post-b", used);
    console.log("dedupe_first_unchanged", first === "撞名文章.html");
    console.log("dedupe_second_disambiguated", second === "撞名文章-post-b.html");
    """
    out = _run_node(snippet)
    lines = dict(line.split(" ", 1) for line in out.strip().splitlines() if " " in line)
    for name in ("safe_tag_basic", "safe_tag_special_chars", "safe_tag_comma_chinese",
                 "safe_article_special_chars", "safe_article_truncated", "safe_article_empty",
                 "arcname_canonical", "arcname_fallback",
                 "dedupe_first_unchanged", "dedupe_second_disambiguated"):
        check(f"pages-download.js真实JS行为: {name}", lines.get(name) == "true", f"got {lines.get(name)!r}")


def test_download_artifacts_identical_across_hosts():
    """下载产物本身不含任何host相关信息(跟mirror.foxzen.me/host参数无关)，
    github和cf两个构建的standalone/downloads内容应该完全一致。"""
    def _run(tmp, out):
        import publish_build
        out_gh, out_cf = tmp / "out_gh", tmp / "out_cf"
        publish_build.build_publish("github.foxzen.me", out_gh)
        publish_build.build_publish("cf.foxzen.me", out_cf)
        check("blog-full.zip两个host字节完全一致",
              (out_gh / "downloads" / "blog-full.zip").read_bytes()
              == (out_cf / "downloads" / "blog-full.zip").read_bytes())
        check("export-all.zip两个host字节完全一致",
              (out_gh / "downloads" / "export-all.zip").read_bytes()
              == (out_cf / "downloads" / "export-all.zip").read_bytes())
        check("standalone/post-a.html两个host字节完全一致",
              (out_gh / "standalone" / "post-a.html").read_bytes()
              == (out_cf / "standalone" / "post-a.html").read_bytes())
    with_download_fixture(_run)


def test_html_source_files_untouched_by_download_build():
    """构建下载产物的过程只应该在output_dir里读写，绝不修改html/源目录
    本身——跟_fix_cross_post_content_links()对posts/<id>/index.html的
    "只改output_dir的拷贝"这条既有约定完全一致。"""
    def _run(tmp, out):
        import publish_build
        source_files = {
            p: p.read_bytes()
            for p in publish_build.HTML_DIR.rglob("*") if p.is_file()
        }
        publish_build.build_publish("github.foxzen.me", out)
        for p, original_bytes in source_files.items():
            check(f"html/源文件未被修改: {p.relative_to(publish_build.HTML_DIR)}",
                  p.read_bytes() == original_bytes)
    with_download_fixture(_run)


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
        test_stale_brand_heading_is_normalized_to_current_brand,
        test_current_brand_heading_is_left_unchanged_idempotent,
        test_github_and_cf_builds_both_get_normalized_brand,
        test_standalone_html_generated_for_every_article,
        test_standalone_html_inlines_images_strips_scripts_and_back_link,
        test_search_index_has_standalone_and_media_fields,
        test_blog_full_zip_contains_original_articles_and_index,
        test_export_all_zip_contains_standalone_versions_not_raw,
        test_export_tag_zip_only_contains_matching_tag_articles,
        test_export_all_zip_uses_year_month_directory_for_canonical_articles,
        test_export_all_zip_filenames_not_raw_post_id,
        test_export_all_zip_title_filename_matches_safe_filename_rule,
        test_export_all_zip_no_silent_filename_collision,
        test_export_tag_zip_uses_same_naming_structure,
        test_export_all_zip_arcnames_have_no_path_traversal_or_absolute_path,
        test_export_all_zip_still_contains_correct_standalone_html_content,
        test_verify_publish_hard_fails_when_standalone_missing,
        test_verify_publish_hard_fails_when_fixed_zip_missing,
        test_verify_publish_hard_fails_when_tag_zip_missing,
        test_verify_publish_hard_fails_on_dangerous_file_inside_zip,
        test_jszip_vendored_locally_not_via_cdn,
        test_index_html_has_all_five_download_buttons,
        test_pages_download_js_no_absolute_domains_or_cdn,
        test_python_and_js_tag_filename_sanitization_agree_on_python_side,
        test_pages_download_js_pure_functions_via_node,
        test_download_artifacts_identical_across_hosts,
        test_html_source_files_untouched_by_download_build,
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
