#!/usr/bin/env python3
"""
针对"规范URL静态化"(canonical_static_target / render_post)的回归测试。

只测试新增的静态化逻辑本身，不联网抓Blogger、不跑main()全流程、不写生产
data/blog.db（只对它做只读连接抽样，或者复制到临时目录里操作副本）。

用法: python3 test_canonical_static.py
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
    真实canonical_path数据能被正确转换成静态路径。"""
    if not REAL_DB.exists():
        print("  [SKIP] 未找到 data/blog.db，跳过真实数据抽样检查")
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
        check("真实DB里至少存在一条带canonical_path的文章样本", len(rows) > 0)
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
