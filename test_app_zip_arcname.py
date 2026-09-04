#!/usr/bin/env python3
"""
app.py::_zip_arcname_for() 离线版归档命名回归测试：年/月/<安全标题>.html，
年/月来自posts.published（文章发布日期），文件名来自标题清洗，不再依赖
canonical_path/Blogger slug——即使canonical_path存在也不影响归档命名，
这是本轮明确要求的关键行为（"网页canonical URL存不存在"和"下载归档内部
文件名应该是什么"是两个独立概念）。

只测_zip_arcname_for()本身（不发真实HTTP请求、不测export_base64()整个
接口），用临时sqlite库（绝不碰真实data/blog.db），跟test_internal_links.py
的with_temp_env()同一个约定，各自独立实现（不共享代码，每个test_*.py
文件在这个项目里都是独立可运行脚本，见其余test文件同款结构）。

用法: python3 test_app_zip_arcname.py
"""
import shutil
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


def with_temp_db(fn):
    """把db.DB_PATH指向临时sqlite文件，跑完自动还原/清理——绝不读写真实
    的data/blog.db（跟test_internal_links.py::with_temp_env()同一个约定）。
    """
    import db
    tmp = Path(tempfile.mkdtemp(prefix="app_zip_arcname_test_"))
    orig_db_path = db.DB_PATH
    real_db_mtime = REAL_DB.stat().st_mtime if REAL_DB.exists() else None

    db.DB_PATH = tmp / "test.db"
    try:
        db.init_db()
        import app as app_module
        fn(db, app_module)
    finally:
        db.DB_PATH = orig_db_path
        shutil.rmtree(tmp, ignore_errors=True)

    if real_db_mtime is not None:
        check("测试过程未修改真实data/blog.db（mtime不变）",
              REAL_DB.stat().st_mtime == real_db_mtime)


def test_arcname_uses_published_year_month_not_canonical_path():
    """canonical_path故意设成跟发布月份不一致的值(2026/07)，验证归档路径
    用的是published字段(2026-08)，证明"网页canonical URL"和"下载归档命名"
    已经彻底解耦——不是因为这次测试数据凑巧一致才通过。"""
    def _run(db, app_module):
        db.upsert_post("post-x", "示例文章标题", "<div>正文</div>", [], "2026-08-15",
                        "2026-08-15T00:00:00Z", "hashx",
                        canonical_path="2026/07/some-other-slug",
                        source_url="https://digatlas.blogspot.com/2026/07/some-other-slug.html",
                        published_ts="2026-08-15T00:00:00Z")
        used = set()
        name = app_module._zip_arcname_for("post-x", used)
        check("年/月来自发布日期(2026/08)，不是canonical_path里的(2026/07)",
              name == "2026/08/示例文章标题.html", f"got {name!r}")
    with_temp_db(_run)


def test_arcname_uses_year_month_title_when_no_canonical_path():
    def _run(db, app_module):
        db.upsert_post("post-y", "无canonical的文章", "<div>正文</div>", [], "2026-03-05",
                        "2026-03-05T00:00:00Z", "hashy",
                        canonical_path=None, source_url=None, published_ts="2026-03-05T00:00:00Z")
        used = set()
        name = app_module._zip_arcname_for("post-y", used)
        check("没有canonical_path时仍按发布日期+标题命名，不是post_id",
              name == "2026/03/无canonical的文章.html", f"got {name!r}")
    with_temp_db(_run)


def test_arcname_sanitizes_illegal_characters_in_title():
    def _run(db, app_module):
        db.upsert_post("post-z", 'a/b\\c:d*e?f"g<h>i|j', "<div>正文</div>", [], "2026-01-01",
                        "2026-01-01T00:00:00Z", "hashz", published_ts="2026-01-01T00:00:00Z")
        used = set()
        name = app_module._zip_arcname_for("post-z", used)
        check("标题里的非法路径字符被清洗", name == "2026/01/a_b_c_d_e_f_g_h_i_j.html", f"got {name!r}")
    with_temp_db(_run)


def test_arcname_handles_empty_title_with_safe_fallback():
    def _run(db, app_module):
        db.upsert_post("post-empty", "   ", "<div>正文</div>", [], "2026-01-01",
                        "2026-01-01T00:00:00Z", "hashempty", published_ts="2026-01-01T00:00:00Z")
        used = set()
        name = app_module._zip_arcname_for("post-empty", used)
        check("空标题回退成untitled", name == "2026/01/untitled.html", f"got {name!r}")
    with_temp_db(_run)


def test_arcname_truncates_overlong_title():
    def _run(db, app_module):
        long_title = "标" * 100
        db.upsert_post("post-long", long_title, "<div>正文</div>", [], "2026-01-01",
                        "2026-01-01T00:00:00Z", "hashlong", published_ts="2026-01-01T00:00:00Z")
        used = set()
        name = app_module._zip_arcname_for("post-long", used)
        check("超长标题被截断到80字符", name == f"2026/01/{'标' * 80}.html", f"got {name!r}")
    with_temp_db(_run)


def test_arcname_disambiguates_same_title_same_month_without_overwrite():
    def _run(db, app_module):
        db.upsert_post("post-dup-1", "撞名文章", "<div>A</div>", [], "2026-05-01",
                        "2026-05-01T00:00:00Z", "hashdup1", published_ts="2026-05-01T00:00:00Z")
        db.upsert_post("post-dup-2", "撞名文章", "<div>B</div>", [], "2026-05-02",
                        "2026-05-02T00:00:00Z", "hashdup2", published_ts="2026-05-02T00:00:00Z")
        used = set()
        first = app_module._zip_arcname_for("post-dup-1", used)
        second = app_module._zip_arcname_for("post-dup-2", used)
        check("第一篇保留原名", first == "2026/05/撞名文章.html", f"got {first!r}")
        check("第二篇撞名后用post_id消解，不覆盖第一篇",
              second == "2026/05/撞名文章-post-dup-2.html", f"got {second!r}")
        check("两个文件名不相同(不静默覆盖)", first != second)
    with_temp_db(_run)


def test_arcname_no_path_traversal_or_absolute_path():
    def _run(db, app_module):
        db.upsert_post("post-traversal", "../../etc/passwd", "<div>正文</div>", [], "2026-02-02",
                        "2026-02-02T00:00:00Z", "hashtrav", published_ts="2026-02-02T00:00:00Z")
        used = set()
        name = app_module._zip_arcname_for("post-traversal", used)
        check("标题里的'/'被清洗掉，不产生额外路径层级",
              ".." not in name.split("/"), f"got {name!r}")
        check("不是绝对路径", not name.startswith("/"), f"got {name!r}")
    with_temp_db(_run)


def main():
    tests = [
        test_arcname_uses_published_year_month_not_canonical_path,
        test_arcname_uses_year_month_title_when_no_canonical_path,
        test_arcname_sanitizes_illegal_characters_in_title,
        test_arcname_handles_empty_title_with_safe_fallback,
        test_arcname_truncates_overlong_title,
        test_arcname_disambiguates_same_title_same_month_without_overwrite,
        test_arcname_no_path_traversal_or_absolute_path,
    ]
    for t in tests:
        print(f"--- {t.__name__} ---")
        try:
            t()
        except Exception:
            print(f"  [FAIL] {t.__name__} 抛出异常:")
            traceback.print_exc()
            failures.append(t.__name__)
    if failures:
        print(f"\n共 {len(failures)} 项失败: {failures}")
        sys.exit(1)
    print("\n全部测试通过。")


if __name__ == "__main__":
    main()
