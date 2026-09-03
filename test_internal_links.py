#!/usr/bin/env python3
"""
问题2回归测试：Flask响应层把文章正文里"引用本站另一篇文章"的Blogger
permalink改写成本站根相对地址，让mirror/backup/github/cf四个入口各自
"点文章链接留在当前域名"，同时不破坏离线下载/导出功能、不修改磁盘源文件。

覆盖两层：
1. internal_links.rewrite_internal_links() 纯函数——不依赖DB/Flask，
   直接验证六条边界规则（discuss-btn/本文自己的permalink/未收录或外部
   Blogger链接一律不动，只有精确命中的本站permalink才改写并强制
   target="_blank" rel="noopener"）。
2. Flask端到端——用临时sqlite库+临时posts目录（绝不碰真实data/blog.db，
   见test_canonical_static.py同款约定），验证mirror/backup两种Host请求
   同一篇文章得到完全一致、不含任何协议/域名的改写结果（这就是
   "host-agnostic"能让backup天然留在backup的证据），并验证磁盘源文件
   字节不变、离线导出仍拿到原始Blogger链接。

用法: python3 test_internal_links.py
"""
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


# ---------------------------------------------------------------------------
# 1. rewrite_internal_links() 纯函数测试
# ---------------------------------------------------------------------------

def test_matched_cross_reference_is_rewritten_with_new_tab():
    from internal_links import rewrite_internal_links
    html = ('<div class="content"><p>继续阅读'
            '<a href="https://digatlas.blogspot.com/2026/08/b.html">下一篇文章</a>。</p></div>')
    permalink_to_url = {"https://digatlas.blogspot.com/2026/08/b.html": "/2026/08/b.html"}
    out = rewrite_internal_links(html, permalink_to_url,
                                  own_permalink="https://digatlas.blogspot.com/2026/08/a.html")
    check("href被改写成本站根相对地址", 'href="/2026/08/b.html"' in out)
    check('补上了target="_blank"', 'target="_blank"' in out)
    check('补上了rel="noopener"', 'rel="noopener"' in out)
    check("不再包含原Blogger绝对地址", "digatlas.blogspot.com" not in out)


def test_discuss_btn_untouched():
    from internal_links import rewrite_internal_links
    own = "https://digatlas.blogspot.com/2026/08/a.html"
    html = f'<a class="discuss-btn" href="{own}" target="_blank" rel="noopener">💬 到主站参与讨论</a>'
    # 故意让discuss-btn的href也命中映射，确认class检查优先于permalink匹配生效
    out = rewrite_internal_links(html, {own: "/2026/08/a.html"}, own_permalink=own)
    check("discuss-btn的href原样保留Blogger permalink，未被改写", out == html)


def test_own_permalink_reference_untouched():
    from internal_links import rewrite_internal_links
    own = "https://digatlas.blogspot.com/2026/08/a.html"
    html = f'<a href="{own}">指向本文自己的链接</a>'
    out = rewrite_internal_links(html, {own: "/2026/08/a.html"}, own_permalink=own)
    check("本文自己的permalink不被当成'引用另一篇文章'改写", out == html)


def test_unrecognized_blogger_url_untouched():
    from internal_links import rewrite_internal_links
    html = '<a href="https://digatlas.blogspot.com/2026/01/not-yet-fetched.html">某篇未收录的文章</a>'
    out = rewrite_internal_links(html, permalink_to_url={},
                                  own_permalink="https://digatlas.blogspot.com/2026/08/a.html")
    check("未收录/外部Blogger链接原样保留", out == html)


def test_true_external_link_untouched():
    from internal_links import rewrite_internal_links
    html = '<a href="https://example.com/some-tool">某个外部工具</a>'
    out = rewrite_internal_links(
        html, permalink_to_url={"https://digatlas.blogspot.com/2026/08/b.html": "/2026/08/b.html"},
        own_permalink=None)
    check("真正的外部链接原样保留", out == html)


def test_images_and_tag_links_not_touched_by_broad_selector():
    """确认不是简单粗暴地对所有 a[href^=http] 做处理——标签链接、图片跟
    permalink映射完全无关，即便看起来像本站/Blogger链接也不会被误伤。"""
    from internal_links import rewrite_internal_links
    html = ('<div class="tags"><a href="/index.html?tag=Firefox">#Firefox</a></div>'
            '<img src="https://cdn.example.com/x.png">'
            '<a href="https://digatlas.blogspot.com/2026/07/some-external-post.html">某外部博客</a>')
    out = rewrite_internal_links(html, permalink_to_url={}, own_permalink=None)
    check("标签链接、图片、未收录链接均未受影响", out == html)


def test_existing_target_or_rel_normalized_not_duplicated():
    """Blogger原文有些链接自带target/rel（比如rel="nofollow"），改写后不应该
    出现重复或冲突的属性，最终必须精确是 target="_blank" rel="noopener"。"""
    from internal_links import rewrite_internal_links
    html = ('<a href="https://digatlas.blogspot.com/2026/07/b.html" '
            'rel="nofollow" target="_blank">本系列第二篇</a>')
    out = rewrite_internal_links(
        html, {"https://digatlas.blogspot.com/2026/07/b.html": "/2026/07/b.html"}, own_permalink=None)
    check("target属性只出现一次", out.count("target=") == 1)
    check("rel属性只出现一次", out.count("rel=") == 1)
    check('rel被规范成noopener', 'rel="noopener"' in out)
    check("旧的nofollow不再出现", "nofollow" not in out)


# ---------------------------------------------------------------------------
# 2. Flask端到端：mirror/backup同一篇文章
# ---------------------------------------------------------------------------

ARTICLE_A_PERMALINK = "https://digatlas.blogspot.com/2026/08/article-a.html"
ARTICLE_B_PERMALINK = "https://digatlas.blogspot.com/2026/08/article-b.html"
UNFETCHED_PERMALINK = "https://digatlas.blogspot.com/2026/01/not-fetched.html"

ARTICLE_A_HTML = f"""<!DOCTYPE html>
<html><head><title>文章A</title></head>
<body>
<div class="content"><p>继续阅读<a href="{ARTICLE_B_PERMALINK}">下一篇文章</a>，
也可以看看<a href="{UNFETCHED_PERMALINK}">这篇未收录的旧文</a>。</p></div>
<div class="discuss-cta">
  <a class="discuss-btn" href="{ARTICLE_A_PERMALINK}" target="_blank" rel="noopener">💬 到主站参与讨论</a>
</div>
</body></html>"""


def with_temp_env(fn):
    """把db.DB_PATH指向临时sqlite文件、app.POSTS_DIR指向临时目录，跑完自动
    还原/清理——绝不读写真实的data/blog.db或html/posts/（跟
    test_canonical_static.py的with_temp_html_dir同一个约定）。
    """
    import db
    import app as app_module

    tmp = Path(tempfile.mkdtemp(prefix="internal_links_test_"))
    orig_db_path = db.DB_PATH
    orig_posts_dir = app_module.POSTS_DIR
    real_db_mtime = REAL_DB.stat().st_mtime if REAL_DB.exists() else None

    db.DB_PATH = tmp / "test.db"
    app_module.POSTS_DIR = tmp / "posts"
    app_module.POSTS_DIR.mkdir(parents=True)
    try:
        db.init_db()
        fn(tmp, db, app_module)
    finally:
        db.DB_PATH = orig_db_path
        app_module.POSTS_DIR = orig_posts_dir
        shutil.rmtree(tmp, ignore_errors=True)

    if real_db_mtime is not None:
        check("测试过程未修改真实data/blog.db（mtime不变）",
              REAL_DB.stat().st_mtime == real_db_mtime)


def _seed_two_posts(db):
    db.upsert_post("post-a", "文章A", ARTICLE_A_HTML, [], "2026-08-01", "2026-08-01T00:00:00Z",
                    "hashA", canonical_path="2026/08/article-a",
                    source_url=ARTICLE_A_PERMALINK, published_ts="2026-08-01T00:00:00Z")
    db.upsert_post("post-b", "文章B", '<div class="content">B的正文</div>', [], "2026-08-02",
                    "2026-08-02T00:00:00Z", "hashB", canonical_path="2026/08/article-b",
                    source_url=ARTICLE_B_PERMALINK, published_ts="2026-08-02T00:00:00Z")


def test_mirror_and_backup_get_identical_host_agnostic_rewrite():
    def _run(tmp, db, app_module):
        _seed_two_posts(db)
        post_dir = app_module.POSTS_DIR / "post-a"
        post_dir.mkdir(parents=True)
        index_file = post_dir / "index.html"
        index_file.write_text(ARTICLE_A_HTML, encoding="utf-8")
        original_bytes = index_file.read_bytes()

        client = app_module.app.test_client()
        resp_mirror = client.get("/2026/08/article-a.html", headers={"Host": "mirror.foxzen.me"})
        resp_backup = client.get("/2026/08/article-a.html", headers={"Host": "backup.foxzen.me"})

        check("mirror请求返回200", resp_mirror.status_code == 200)
        check("backup请求返回200", resp_backup.status_code == 200)
        body_mirror = resp_mirror.get_data(as_text=True)
        body_backup = resp_backup.get_data(as_text=True)

        check("mirror/backup两次响应字节完全一致（app.py不区分request.host）",
              body_mirror == body_backup)
        check("交叉引用改写成本站根相对地址",
              'href="/2026/08/article-b.html"' in body_mirror)
        check('改写后的本站文章链接带 target="_blank" rel="noopener"',
              'href="/2026/08/article-b.html" target="_blank" rel="noopener"' in body_mirror)
        check("响应里不包含任何mirror/backup/github绝对域名（host-agnostic的直接证据，"
              "证明浏览器会按当前访问的域名自行解析）",
              all(host not in body_mirror
                  for host in ("mirror.foxzen.me", "backup.foxzen.me", "github.foxzen.me")))
        check("discuss-btn仍指向本文自己的Blogger permalink，未被改写",
              f'<a class="discuss-btn" href="{ARTICLE_A_PERMALINK}"' in body_mirror)
        check("未收录的旧文链接原样保留（未被误判成本站文章）",
              f'href="{UNFETCHED_PERMALINK}"' in body_mirror)

        check("磁盘源文件html/posts/<id>/index.html字节完全未被修改",
              index_file.read_bytes() == original_bytes)

        # 离线导出走的是_inline_post_as_base64，直接读磁盘文件，必须拿到原始
        # Blogger链接——这就是"改写不能写回磁盘"这条边界要保护的东西。
        exported = app_module._inline_post_as_base64("post-a")
        check("离线导出内容仍包含原始Blogger permalink，未被相对路径替换",
              ARTICLE_B_PERMALINK in exported)
        check("离线导出内容不包含改写后的本站相对地址",
              'href="/2026/08/article-b.html"' not in exported)

    with_temp_env(_run)


def test_legacy_posts_path_redirects_without_rewriting():
    """有canonical_path时/posts/<id>/是301跳转，不在这一步做任何改写
    （改写只发生在真正返回文章内容的canonical路由/无canonical兜底分支里）。"""
    def _run(tmp, db, app_module):
        _seed_two_posts(db)
        post_dir = app_module.POSTS_DIR / "post-a"
        post_dir.mkdir(parents=True)
        (post_dir / "index.html").write_text(ARTICLE_A_HTML, encoding="utf-8")

        client = app_module.app.test_client()
        resp = client.get("/posts/post-a/", headers={"Host": "backup.foxzen.me"}, follow_redirects=False)
        check("有canonical_path时/posts/<id>/是301跳转", resp.status_code == 301)
        check("跳转目标是根相对canonical路径（不带协议/域名）",
              resp.headers.get("Location") == "/2026/08/article-a.html")

    with_temp_env(_run)


# ---------------------------------------------------------------------------
# 3. Task B：首页/排行/搜索/分页文章链接默认新标签页（静态源码检查，
#    跟pages-index.js的纯函数node测试同一个精神：不需要真的起浏览器，
#    但要检查到真实会执行的那一行代码）
# ---------------------------------------------------------------------------

def test_static_index_js_search_result_links_open_new_tab():
    src = (BASE_DIR / "static" / "index.js").read_text(encoding="utf-8")
    check('static/index.js的搜索结果链接带 target: "_blank"',
          'el("a", { href, text: p.title, target: "_blank", rel: "noopener" })' in src)


def test_pages_index_js_search_result_links_open_new_tab():
    src = (BASE_DIR / "static_pages" / "pages-index.js").read_text(encoding="utf-8")
    check('pages-index.js的搜索/分页结果链接带 target: "_blank"',
          'el("a", { href: a.url, text: a.title, target: "_blank", rel: "noopener" })' in src)


def main():
    tests = [
        test_matched_cross_reference_is_rewritten_with_new_tab,
        test_discuss_btn_untouched,
        test_own_permalink_reference_untouched,
        test_unrecognized_blogger_url_untouched,
        test_true_external_link_untouched,
        test_images_and_tag_links_not_touched_by_broad_selector,
        test_existing_target_or_rel_normalized_not_duplicated,
        test_mirror_and_backup_get_identical_host_agnostic_rewrite,
        test_legacy_posts_path_redirects_without_rewriting,
        test_static_index_js_search_result_links_open_new_tab,
        test_pages_index_js_search_result_links_open_new_tab,
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
