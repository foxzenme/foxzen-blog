#!/usr/bin/env python3
"""公开"随机文章"入口（GET /random）回归测试。

背景：mirror.foxzen.me/backup.foxzen.me首页新增一个"随机文章"链接，纯只读
302跳转，从posts表当前有效文章（canonical_path非空）里随机选一篇——不触发
refresh/purge/Git/Pages任何一个既有流程，不新增数据库schema。

覆盖：
db.get_random_canonical_path()单元级（不经过HTTP层）：
0. 0篇文章 -> None；1篇文章 -> 唯一有效canonical_path；混合NULL记录时永远
   不返回None/NULL行；多篇文章始终来自有效集合；函数本身不产生写操作；
   约1000篇规模下的正确性/稳定性（面向未来"几百到上千篇"的目标规模，不是
   概率统计测试，不要求每篇文章都被抽到）

/api/random端到端（HTTP层）：
1. 正常随机跳转：302 + Location指向真实canonical路径
2. canonical_path=NULL的文章永远不会被选中
3. 删除文章后不会被选中
4. 0篇文章 -> 404，且body是跟全站其它404一致的标准404页面（不是空body）
5. 1篇文章 -> 始终该文章
6. 多篇文章的随机结果始终来自有效文章集合
7. 响应带Cache-Control: no-store（防止Cloudflare/CDN缓存302结果导致"随机"失效），
   404分支同样带这个头
8. 不触发refresh/purge/Git/Pages（不调用subprocess.run/git_publish.commit_and_push，
   不创建content_fetch/git_publish/manual_purge任一把锁）

跟test_purge_cache_button.py同样的约定：绝不碰真实data/blog.db，用临时sqlite
文件重定向db.DB_PATH，db.DB_PATH必须先于`import app`被设置。这个端点比
/api/purge-cache更简单——完全不触碰fetch_blog/Cloudflare token，所以这里的
fixture比test_purge_cache_button.py::with_temp_app_env()更精简，只搭建
/random真正需要的部分，不patch任何Cloudflare相关的东西。

用法: python3 test_random_article.py
"""
import re
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


def with_temp_app_env(fn):
    """db.DB_PATH必须先于`import app`被设置——见test_purge_cache_button.py同名
    函数的文档字符串，这里保持同样的顺序约定。

    fn(tmp, db, app_module)：三个参数都是调用方常用的。
    """
    tmp = Path(tempfile.mkdtemp(prefix="random_article_test_"))
    real_db_mtime = REAL_DB.stat().st_mtime if REAL_DB.exists() else None

    import db
    orig_db_path = db.DB_PATH
    db.DB_PATH = tmp / "test.db"

    import app as app_module

    try:
        db.init_db()
        fn(tmp, db, app_module)
    finally:
        db.DB_PATH = orig_db_path
        shutil.rmtree(tmp, ignore_errors=True)

    if real_db_mtime is not None:
        check("测试过程未修改真实data/blog.db（mtime不变）",
              REAL_DB.stat().st_mtime == real_db_mtime)


def _seed_post(db_mod, post_id, canonical_path):
    db_mod.upsert_post(post_id, f"Test Post {post_id}", "<p>hi</p>", [], "2026-01-01",
                        "2026-01-01T00:00:00Z", f"hash-{post_id}", canonical_path=canonical_path,
                        source_url=f"https://example.blogspot.com/{post_id}",
                        published_ts="2026-01-01T00:00:00Z")


# ---------------------------------------------------------------------------
# db.get_random_canonical_path()：单元级（不经过HTTP层，直接测函数本身）
# ---------------------------------------------------------------------------

def test_get_random_canonical_path_returns_none_when_empty():
    def _run(tmp, db, app_module):
        check("posts表为空时返回None", db.get_random_canonical_path() is None)
    with_temp_app_env(_run)


def test_get_random_canonical_path_single_article():
    def _run(tmp, db, app_module):
        _seed_post(db, "post-only", "2026/05/only-post")
        all_same = all(db.get_random_canonical_path() == "2026/05/only-post" for _ in range(10))
        check("单篇文章: 10次调用始终返回该文章的canonical_path", all_same)
    with_temp_app_env(_run)


def test_get_random_canonical_path_never_returns_null_when_valid_exists():
    def _run(tmp, db, app_module):
        db.upsert_post("post-null", "No Canonical", "<p>x</p>", [], "2026-01-01",
                        "2026-01-01T00:00:00Z", "hash-null", canonical_path=None,
                        source_url="https://example.blogspot.com/post-null",
                        published_ts="2026-01-01T00:00:00Z")
        _seed_post(db, "post-valid", "2026/06/valid-post")
        results = [db.get_random_canonical_path() for _ in range(30)]
        check("混合NULL记录时，30次调用永远不返回None（存在有效文章）",
              all(r is not None for r in results), results)
        check("混合NULL记录时，30次调用始终返回那篇有效文章",
              all(r == "2026/06/valid-post" for r in results), results)
    with_temp_app_env(_run)


def test_get_random_canonical_path_multiple_articles_always_from_valid_set():
    def _run(tmp, db, app_module):
        valid = set()
        for i in range(5):
            cp = f"2026/07/post-{i}"
            _seed_post(db, f"post-{i}", cp)
            valid.add(cp)
        results = [db.get_random_canonical_path() for _ in range(60)]
        check("多篇文章: 60次调用结果始终属于有效集合",
              all(r in valid for r in results), set(results) - valid)
    with_temp_app_env(_run)


def test_get_random_canonical_path_does_not_write():
    """纯只读验证：直接比对调用前后posts表的完整内容是否逐行相同，而不是
    仅凭"函数体里没写INSERT/UPDATE"这种代码审查来自证。"""
    def _run(tmp, db, app_module):
        _seed_post(db, "post-a", "2026/08/post-a")
        _seed_post(db, "post-b", "2026/08/post-b")

        def _snapshot():
            conn = db.get_conn()
            rows = conn.execute("SELECT * FROM posts ORDER BY post_id").fetchall()
            conn.close()
            return [dict(r) for r in rows]

        before = _snapshot()
        for _ in range(20):
            db.get_random_canonical_path()
        after = _snapshot()
        check("调用20次get_random_canonical_path()后，posts表内容逐行未变", before == after)
    with_temp_app_env(_run)


_CANONICAL_SHAPE_RE = re.compile(r"^\d{4}/\d{2}/[^/]+$")


def test_get_random_canonical_path_scale_1000_articles():
    """面向未来"几百到上千篇文章"目标规模的正确性/稳定性验证——不是概率
    统计测试，不要求"每篇文章都必须被抽到"，只验证每次结果都合法、且来自
    有效集合、不出现NULL/空字符串/站外路径。

    用executemany()一次性批量插入，而不是循环调用1000次db.upsert_post()：
    后者每次都会开关一次连接、维护FTS索引、跑INSERT+DELETE+INSERT三条语句，
    1000次这样跑会明显拖慢整个测试套件；get_random_canonical_path()本身
    只读posts表的canonical_path一列，不需要真实标题/正文/FTS索引，直接
    构造满足posts表NOT NULL约束的最小行即可，不影响这个测试要验证的行为。
    """
    def _run(tmp, db, app_module):
        conn = db.get_conn()
        now = "2026-01-01T00:00:00"
        rows = [
            (f"post-{i:04d}", f"Title {i}", "<p>x</p>", "2026-01-01", "2026-01-01T00:00:00Z",
             now, f"hash-{i:04d}", f"2026/{(i % 12) + 1:02d}/post-{i:04d}")
            for i in range(1000)
        ]
        conn.executemany("""
            INSERT INTO posts (post_id, title, content_html, published, updated, fetched_at, content_hash, canonical_path)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, rows)
        conn.commit()
        conn.close()

        valid_canonicals = {r[7] for r in rows}
        results = [db.get_random_canonical_path() for _ in range(200)]
        check("1000篇规模: 200次随机调用结果全部非空、非None、且属于有效集合",
              all(r for r in results) and all(r in valid_canonicals for r in results))
        check("1000篇规模: 200次结果全部符合YYYY/MM/slug形状（不出现站外/异常路径）",
              all(r and _CANONICAL_SHAPE_RE.match(r) for r in results))
    with_temp_app_env(_run)


# ---------------------------------------------------------------------------
# /api/random 端到端
# ---------------------------------------------------------------------------

def test_random_redirect_to_valid_canonical_path():
    def _run(tmp, db, app_module):
        _seed_post(db, "post-1", "2026/01/first-post")
        client = app_module.app.test_client()
        resp = client.get("/random", follow_redirects=False)
        check("①随机文章: HTTP 302", resp.status_code == 302, resp.status_code)
        check("①Location是根相对canonical路径",
              resp.headers.get("Location") == "/2026/01/first-post.html",
              resp.headers.get("Location"))
    with_temp_app_env(_run)


def test_null_canonical_path_never_selected():
    def _run(tmp, db, app_module):
        db.upsert_post("post-null", "No Canonical", "<p>x</p>", [], "2026-01-01",
                        "2026-01-01T00:00:00Z", "hash-null", canonical_path=None,
                        source_url="https://example.blogspot.com/post-null",
                        published_ts="2026-01-01T00:00:00Z")
        _seed_post(db, "post-valid", "2026/02/valid-post")
        client = app_module.app.test_client()
        for _ in range(20):
            resp = client.get("/random", follow_redirects=False)
            check("②canonical_path=NULL的文章不会被选中",
                  resp.headers.get("Location") == "/2026/02/valid-post.html",
                  resp.headers.get("Location"))
    with_temp_app_env(_run)


def test_deleted_post_not_selected():
    def _run(tmp, db, app_module):
        _seed_post(db, "post-a", "2026/01/post-a")
        _seed_post(db, "post-b", "2026/02/post-b")
        db.delete_post_record("post-a")
        client = app_module.app.test_client()
        for _ in range(20):
            resp = client.get("/random", follow_redirects=False)
            check("③删除文章不会被选中",
                  resp.headers.get("Location") == "/2026/02/post-b.html",
                  resp.headers.get("Location"))
    with_temp_app_env(_run)


def test_zero_articles_returns_404():
    def _run(tmp, db, app_module):
        client = app_module.app.test_client()
        resp = client.get("/random")
        check("④0篇文章: HTTP 404", resp.status_code == 404, resp.status_code)
        body = resp.get_data(as_text=True)
        check("④404响应body不是空的（跟全站其它abort(404)风格一致，不是裸空页）",
              len(body) > 0, repr(body))
        check("④404响应body是标准404页面文案", "404" in body and "Not Found" in body, body)
    with_temp_app_env(_run)


def test_single_article_always_selected():
    def _run(tmp, db, app_module):
        _seed_post(db, "post-only", "2026/03/only-post")
        client = app_module.app.test_client()
        for _ in range(10):
            resp = client.get("/random", follow_redirects=False)
            check("⑤单篇文章: 始终跳到该文章",
                  resp.headers.get("Location") == "/2026/03/only-post.html",
                  resp.headers.get("Location"))
    with_temp_app_env(_run)


def test_multiple_articles_random_result_always_from_valid_set():
    def _run(tmp, db, app_module):
        valid_locations = set()
        for i in range(5):
            cp = f"2026/04/post-{i}"
            _seed_post(db, f"post-{i}", cp)
            valid_locations.add(f"/{cp}.html")

        client = app_module.app.test_client()
        seen = set()
        all_valid = True
        for _ in range(60):
            resp = client.get("/random", follow_redirects=False)
            loc = resp.headers.get("Location")
            if loc not in valid_locations:
                all_valid = False
            seen.add(loc)
        check("⑥60次随机结果全部落在有效文章集合内", all_valid, seen)
        check("⑥60次随机调用里出现了不止1种结果（确认真的在随机，不是碰巧卡在同一篇）",
              len(seen) > 1, seen)
    with_temp_app_env(_run)


def test_response_has_no_store_cache_control():
    def _run(tmp, db, app_module):
        _seed_post(db, "post-1", "2026/01/first-post")
        client = app_module.app.test_client()
        resp = client.get("/random", follow_redirects=False)
        check("⑦响应带Cache-Control: no-store（防止CDN缓存住某一次随机结果）",
              resp.headers.get("Cache-Control") == "no-store", resp.headers.get("Cache-Control"))
    with_temp_app_env(_run)


def test_zero_articles_response_also_has_no_store_cache_control():
    """404分支同样必须no-store：CDN理论上也可能缓存住一次404，之后新文章
    发布了，缓存却仍然认为"当前没有文章"。"""
    def _run(tmp, db, app_module):
        client = app_module.app.test_client()
        resp = client.get("/random")
        check("⑦'404响应同样带Cache-Control: no-store",
              resp.headers.get("Cache-Control") == "no-store", resp.headers.get("Cache-Control"))
    with_temp_app_env(_run)


def test_random_article_does_not_trigger_any_refresh_or_publish_flow():
    def _run(tmp, db, app_module):
        _seed_post(db, "post-1", "2026/01/first-post")

        def _raise_subprocess(*a, **kw):
            raise AssertionError("/random不应该调用subprocess.run（那是_run_content_fetch()的职责）")
        orig_run = app_module.subprocess.run
        app_module.subprocess.run = _raise_subprocess

        def _raise_git_publish(*a, **kw):
            raise AssertionError("/random不应该调用git_publish.commit_and_push()")
        orig_commit = app_module.git_publish.commit_and_push
        app_module.git_publish.commit_and_push = _raise_git_publish

        try:
            client = app_module.app.test_client()
            resp = client.get("/random")
            check("⑧没有因为误触发content_fetch/git_publish而抛异常/失败",
                  resp.status_code == 302, resp.status_code)
        finally:
            app_module.subprocess.run = orig_run
            app_module.git_publish.commit_and_push = orig_commit

        conn = db.get_conn()
        for lock_key in ("content_fetch", "git_publish", "manual_purge"):
            row = conn.execute("SELECT 1 FROM refresh_locks WHERE lock_key=?", (lock_key,)).fetchone()
            check(f"⑧{lock_key}锁的行完全没有被创建（从未被acquire过）", row is None, lock_key)
        conn.close()
    with_temp_app_env(_run)


def main():
    tests = [
        test_get_random_canonical_path_returns_none_when_empty,
        test_get_random_canonical_path_single_article,
        test_get_random_canonical_path_never_returns_null_when_valid_exists,
        test_get_random_canonical_path_multiple_articles_always_from_valid_set,
        test_get_random_canonical_path_does_not_write,
        test_get_random_canonical_path_scale_1000_articles,
        test_random_redirect_to_valid_canonical_path,
        test_null_canonical_path_never_selected,
        test_deleted_post_not_selected,
        test_zero_articles_returns_404,
        test_single_article_always_selected,
        test_multiple_articles_random_result_always_from_valid_set,
        test_response_has_no_store_cache_control,
        test_zero_articles_response_also_has_no_store_cache_control,
        test_random_article_does_not_trigger_any_refresh_or_publish_flow,
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
