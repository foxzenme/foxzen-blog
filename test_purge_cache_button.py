#!/usr/bin/env python3
"""公共"刷新本站缓存"按钮（POST /api/purge-cache）回归测试。

背景：mirror.foxzen.me/backup.foxzen.me首页新增一个所有访客都能点击的公共
按钮，作为Cloudflare CDN缓存的人工兜底——不是Blogger refresh，不触发内容
抓取，不触发Git发布，只在确认存在真实的、尚未成功purge过的内容变化时，
才复用既有的fetch_blog._purge_cloudflare_cache()做一次URL purge。

覆盖（对应需求里列的10项）：
1. 无变化：不调用Cloudflare
2. changed_count>0：调用既有purge函数
3. deleted_count>0（即使changed_count=0）：同样进入purge逻辑
4. 已经成功purge过（purge_status='success'）：重复点击不再重复purge
5. 并发点击不产生重复purge
6. 端点不触发content_fetch（不调用fetch_blog.main()/subprocess）
7. 端点不触发git publish
8. 密钥不会出现在响应体或内部存储里
9. Cloudflare失败时返回安全的固定错误文案（真实本地http.server，不是mock）
10. 现有自动purge相关测试（test_cloudflare_purge.py/test_refresh_lock.py）
    不在本文件里重复覆盖，而是作为部署前的独立步骤单独跑一遍，确认没有
    被这次改动破坏。

跟test_refresh_lock.py同一个约定：绝不碰真实data/blog.db，用with_temp_db()
风格的fixture重定向db.DB_PATH到临时sqlite文件；db/fetch_blog/app三个模块
都遵守"db.DB_PATH必须先于import被设置"这条顺序（K节修复确立的既有约定）。

这个文件本身不需要test_refresh_lock.py::with_temp_app_env()那一整套
FETCH_SCRIPT桩/git_publish桩/github_actions桩基础设施——/api/purge-cache
完全不触碰content_fetch/git_publish任何一步（这正是需求6/7要验证的事），
所以这里用一个更精简的专属fixture，只搭建这个端点真正需要的部分。

用法: python3 test_purge_cache_button.py
"""
import http.server
import json
import shutil
import sys
import tempfile
import threading
import time
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
    """db.DB_PATH必须先于`import app`/`import fetch_blog`被设置——两个模块
    顶层代码本身不触碰数据库(只有函数体内部会)，但严格按这个顺序写是这个
    项目里其它测试文件已经验证过的正确做法(见test_refresh_lock.py同名
    函数的文档字符串)，这里保持一致，不依赖"顶层代码恰好没有反例"这种
    脆弱的前提。

    fn(tmp, db, app_module, fetch_blog)：四个参数都是调用方常用的。
    """
    tmp = Path(tempfile.mkdtemp(prefix="purge_cache_test_"))
    real_db_mtime = REAL_DB.stat().st_mtime if REAL_DB.exists() else None

    import db
    orig_db_path = db.DB_PATH
    db.DB_PATH = tmp / "test.db"

    import fetch_blog
    orig_cf_token, orig_cf_zone = fetch_blog.CF_API_TOKEN, fetch_blog.CF_ZONE_ID
    orig_purge_fn = fetch_blog._purge_cloudflare_cache
    fetch_blog.CF_API_TOKEN = "fake-test-token-not-real-0000"
    fetch_blog.CF_ZONE_ID = "fake-zone-id"

    import app as app_module
    orig_commit_and_push = app_module.git_publish.commit_and_push

    try:
        db.init_db()
        fn(tmp, db, app_module, fetch_blog)
    finally:
        db.DB_PATH = orig_db_path
        fetch_blog.CF_API_TOKEN, fetch_blog.CF_ZONE_ID = orig_cf_token, orig_cf_zone
        fetch_blog._purge_cloudflare_cache = orig_purge_fn
        app_module.git_publish.commit_and_push = orig_commit_and_push
        shutil.rmtree(tmp, ignore_errors=True)

    if real_db_mtime is not None:
        check("测试过程未修改真实data/blog.db（mtime不变）",
              REAL_DB.stat().st_mtime == real_db_mtime)


def _seed_post(db_mod, post_id="post-1", canonical_path="2026/01/test-post"):
    db_mod.upsert_post(post_id, "Test Post", "<p>hi</p>", [], "2026-01-01", "2026-01-01T00:00:00Z",
                        "hash1", canonical_path=canonical_path,
                        source_url="https://example.blogspot.com/x", published_ts="2026-01-01T00:00:00Z")


def _seed_fetch_log(db_mod, *, changed_count=0, deleted_count=0,
                     purge_status="skipped", purge_reason="no_change", purge_url_count=0):
    log_id = db_mod.log_fetch_start()
    db_mod.log_fetch_end(log_id, "ok", detail="test seed", post_count=1,
                          changed_count=changed_count, deleted_count=deleted_count,
                          purge_status=purge_status, purge_reason=purge_reason,
                          purge_url_count=purge_url_count)
    return log_id


# ---------------------------------------------------------------------------
# db.py新增函数：单元级
# ---------------------------------------------------------------------------

def test_get_last_completed_fetch_log_returns_none_when_empty():
    def _run(tmp, db, app_module, fb):
        check("空表时返回None", db.get_last_completed_fetch_log() is None)
    with_temp_app_env(_run)


def test_get_last_completed_fetch_log_skips_running_row():
    def _run(tmp, db, app_module, fb):
        _seed_fetch_log(db, changed_count=1, purge_status="success")
        running_id = db.log_fetch_start()  # 模拟一次还在进行中的抓取
        row = db.get_last_completed_fetch_log()
        check("跳过status=running的行，拿到更早那条已完成的记录",
              row is not None and row["id"] != running_id, row)
    with_temp_app_env(_run)


def test_record_manual_purge_result_updates_existing_row():
    def _run(tmp, db, app_module, fb):
        log_id = _seed_fetch_log(db, changed_count=1, purge_status="failed", purge_reason="timeout")
        db.record_manual_purge_result(log_id, "success", "ok", 5)
        row = db.get_last_completed_fetch_log()
        check("purge_status被更新为success", row["purge_status"] == "success", row)
        check("purge_url_count被更新", row["purge_url_count"] == 5, row)
    with_temp_app_env(_run)


# ---------------------------------------------------------------------------
# /api/purge-cache 端到端
# ---------------------------------------------------------------------------

def test_no_change_does_not_call_cloudflare():
    def _run(tmp, db, app_module, fb):
        _seed_post(db)
        _seed_fetch_log(db, changed_count=0, deleted_count=0, purge_status="skipped", purge_reason="no_change")
        calls = []
        fb._purge_cloudflare_cache = lambda *a, **kw: calls.append((a, kw))

        client = app_module.app.test_client()
        resp = client.post("/api/purge-cache")
        data = resp.get_json()
        check("①无变化: HTTP 200", resp.status_code == 200)
        check("①status=no_changes", data["status"] == "no_changes", data)
        check("①_purge_cloudflare_cache()完全没被调用", calls == [], calls)
    with_temp_app_env(_run)


def test_no_prior_fetch_log_is_treated_as_no_change():
    """从来没有任何一次完成的抓取时（全新部署、还没等到第一次hourly cron），
    必须保守地判定为"没有变化"，不能因为缺少历史记录就意外触发purge。
    """
    def _run(tmp, db, app_module, fb):
        calls = []
        fb._purge_cloudflare_cache = lambda *a, **kw: calls.append((a, kw))
        client = app_module.app.test_client()
        resp = client.post("/api/purge-cache")
        data = resp.get_json()
        check("从未抓取过时: status=no_changes", data["status"] == "no_changes", data)
        check("_purge_cloudflare_cache()没被调用", calls == [], calls)
    with_temp_app_env(_run)


def test_changed_url_triggers_existing_purge_function():
    def _run(tmp, db, app_module, fb):
        _seed_post(db, post_id="post-1", canonical_path="2026/01/test-post")
        _seed_fetch_log(db, changed_count=1, purge_status="failed", purge_reason="timeout")
        calls = []

        def _stub(urls, **kw):
            calls.append(urls)
            return {"status": "success", "reason": "ok", "url_count": len(urls)}
        fb._purge_cloudflare_cache = _stub

        client = app_module.app.test_client()
        resp = client.post("/api/purge-cache")
        data = resp.get_json()
        check("②changed_count>0: 调用了既有purge函数一次", len(calls) == 1, calls)
        sent_urls = calls[0]
        check("②请求包含首页URL", f"{fb.MIRROR_ROOT_URL}/" in sent_urls, sent_urls)
        check("②请求包含这篇文章的canonical URL",
              f"{fb.MIRROR_ROOT_URL}/2026/01/test-post.html" in sent_urls, sent_urls)
        check("②status=success", data["status"] == "success", data)
    with_temp_app_env(_run)


def test_deleted_url_enters_purge_logic():
    def _run(tmp, db, app_module, fb):
        _seed_post(db)
        _seed_fetch_log(db, changed_count=0, deleted_count=1, purge_status="failed", purge_reason="network_error")
        calls = []
        fb._purge_cloudflare_cache = lambda urls, **kw: (calls.append(urls),
                                                           {"status": "success", "reason": "ok",
                                                            "url_count": len(urls)})[1]
        client = app_module.app.test_client()
        resp = client.post("/api/purge-cache")
        check("③仅deleted_count>0(changed_count=0)也会触发purge", len(calls) == 1, calls)
        check("③status=success", resp.get_json()["status"] == "success")
    with_temp_app_env(_run)


def test_cooldown_blocks_retry_within_five_minutes_when_still_pending():
    """跟test_manual_success_is_recorded_then_second_click_is_noop不是同一件
    事：那个测试第一次purge成功，第二次点击在_manual_purge_pending_change()
    这一步就短路返回no_changes，根本不会碰MANUAL_PURGE_LOCK。这里第一次
    purge本身失败(fetch_log.purge_status不会被改成success)，第二次点击时
    仍然判定为"待purge"，会真正走到锁这一步——验证的是
    MANUAL_PURGE_COOLDOWN_SECONDS=300这个数值本身在生效，而不是状态判断
    提前拦掉了它。
    """
    def _run(tmp, db, app_module, fb):
        _seed_post(db)
        _seed_fetch_log(db, changed_count=1, purge_status="failed", purge_reason="timeout")
        fb._purge_cloudflare_cache = lambda urls, **kw: {"status": "failed", "reason": "timeout", "url_count": 0}

        client = app_module.app.test_client()
        resp1 = client.post("/api/purge-cache")
        check("第一次点击: Cloudflare失败, status=failure", resp1.get_json()["status"] == "failure", resp1.get_json())

        resp2 = client.post("/api/purge-cache")
        data2 = resp2.get_json()
        check("第一次失败后仍是pending状态，第二次点击(5分钟冷却窗口内): HTTP 429",
              resp2.status_code == 429, resp2.status_code)
        check("第二次点击: status=cooldown", data2.get("status") == "cooldown", data2)
        check("cooldown_remaining_seconds落在(0, MANUAL_PURGE_COOLDOWN_SECONDS]区间内",
              isinstance(data2.get("cooldown_remaining_seconds"), int)
              and 0 < data2["cooldown_remaining_seconds"] <= app_module.MANUAL_PURGE_COOLDOWN_SECONDS,
              data2)
    with_temp_app_env(_run)


def test_already_purged_change_does_not_repurge():
    def _run(tmp, db, app_module, fb):
        _seed_post(db)
        _seed_fetch_log(db, changed_count=1, purge_status="success", purge_reason="ok", purge_url_count=2)
        calls = []
        fb._purge_cloudflare_cache = lambda *a, **kw: calls.append((a, kw))

        client = app_module.app.test_client()
        resp = client.post("/api/purge-cache")
        data = resp.get_json()
        check("④已成功purge过: status=no_changes（不是重复success）", data["status"] == "no_changes", data)
        check("④_purge_cloudflare_cache()没有被再次调用", calls == [], calls)
    with_temp_app_env(_run)


def test_manual_success_is_recorded_then_second_click_is_noop():
    """④的另一半：手动点击自己触发的那次成功purge，也必须被记录进
    fetch_log，让"这个变化已经被purge过"这个结论对后续任何一次点击（不管
    还是不是同一个访客）都成立——第二次点击此时完全不会碰MANUAL_PURGE_LOCK
    （在_manual_purge_pending_change()这一步就短路返回no_changes了），
    这也正是需求6"cooldown已过也不能无条件purge"里"cooldown不是唯一
    判断依据"这一半在真正的HTTP层面得到验证的地方。
    """
    def _run(tmp, db, app_module, fb):
        _seed_post(db)
        _seed_fetch_log(db, changed_count=1, purge_status="failed", purge_reason="timeout")
        calls = []
        fb._purge_cloudflare_cache = lambda urls, **kw: (calls.append(urls),
                                                           {"status": "success", "reason": "ok",
                                                            "url_count": len(urls)})[1]
        client = app_module.app.test_client()

        resp1 = client.post("/api/purge-cache")
        check("第一次点击: success", resp1.get_json()["status"] == "success", resp1.get_json())
        check("第一次点击: 真的调用了purge", len(calls) == 1, calls)

        row = db.get_last_completed_fetch_log()
        check("purge成功后fetch_log.purge_status被更新为success", row["purge_status"] == "success", row)

        resp2 = client.post("/api/purge-cache")
        data2 = resp2.get_json()
        check("第二次点击(仍在5分钟冷却窗口内): status=no_changes，不是cooldown",
              data2["status"] == "no_changes", data2)
        check("第二次点击没有再调用一次purge", len(calls) == 1, calls)
    with_temp_app_env(_run)


def test_concurrent_clicks_do_not_produce_duplicate_purge():
    def _run(tmp, db, app_module, fb):
        _seed_post(db)
        _seed_fetch_log(db, changed_count=1, purge_status="failed", purge_reason="timeout")
        calls = []

        def _slow_stub(urls, **kw):
            calls.append(urls)
            time.sleep(0.5)
            return {"status": "success", "reason": "ok", "url_count": len(urls)}
        fb._purge_cloudflare_cache = _slow_stub

        client = app_module.app.test_client()
        results = {}

        def _first_call():
            results["first"] = client.post("/api/purge-cache")

        t = threading.Thread(target=_first_call)
        t.start()
        time.sleep(0.15)  # 确保第一个请求已经先acquire到锁、正在_slow_stub里sleep
        results["second"] = client.post("/api/purge-cache")
        t.join(timeout=10)

        check("⑤两次几乎同时的点击，只真正执行了1次purge", len(calls) == 1, calls)
        statuses = {results["first"].status_code, results["second"].status_code}
        check("⑤一次200(成功)，另一次409(busy)或429(cooldown)，二者恰好各占一个",
              200 in statuses and (409 in statuses or 429 in statuses), statuses)
    with_temp_app_env(_run)


def test_endpoint_does_not_trigger_content_fetch():
    def _run(tmp, db, app_module, fb):
        _seed_post(db)
        _seed_fetch_log(db, changed_count=1, purge_status="failed")
        fb._purge_cloudflare_cache = lambda urls, **kw: {"status": "success", "reason": "ok", "url_count": len(urls)}

        def _raise(*a, **kw):
            raise AssertionError("purge-cache端点绝不应该调用subprocess.run（那是_run_content_fetch()的职责）")
        orig_run = app_module.subprocess.run
        app_module.subprocess.run = _raise
        try:
            client = app_module.app.test_client()
            resp = client.post("/api/purge-cache")
            check("⑥没有因为误触发content_fetch而抛异常/失败", resp.status_code == 200, resp.status_code)
        finally:
            app_module.subprocess.run = orig_run

        conn = db.get_conn()
        row = conn.execute("SELECT 1 FROM refresh_locks WHERE lock_key='content_fetch'").fetchone()
        conn.close()
        check("⑥content_fetch锁的行完全没有被创建（从未被acquire过）", row is None)
    with_temp_app_env(_run)


def test_endpoint_does_not_trigger_git_publish():
    def _run(tmp, db, app_module, fb):
        _seed_post(db)
        _seed_fetch_log(db, changed_count=1, purge_status="failed")
        fb._purge_cloudflare_cache = lambda urls, **kw: {"status": "success", "reason": "ok", "url_count": len(urls)}

        def _raise(*a, **kw):
            raise AssertionError("purge-cache端点绝不应该调用git_publish.commit_and_push()")
        app_module.git_publish.commit_and_push = _raise

        client = app_module.app.test_client()
        resp = client.post("/api/purge-cache")
        check("⑦没有因为误触发git publish而抛异常/失败", resp.status_code == 200, resp.status_code)

        conn = db.get_conn()
        row = conn.execute("SELECT 1 FROM refresh_locks WHERE lock_key='git_publish'").fetchone()
        conn.close()
        check("⑦git_publish锁的行完全没有被创建（从未被acquire过）", row is None)
    with_temp_app_env(_run)


def test_secrets_never_appear_in_response_or_internal_storage():
    def _run(tmp, db, app_module, fb):
        _seed_post(db)
        _seed_fetch_log(db, changed_count=1, purge_status="failed")
        secret_token = fb.CF_API_TOKEN  # with_temp_app_env()里设的假token，但走真实redact逻辑

        def _raise_with_token(urls, **kw):
            raise RuntimeError(f"模拟一个意外异常，异常文本里意外带上了token: {secret_token}")
        fb._purge_cloudflare_cache = _raise_with_token

        client = app_module.app.test_client()
        resp = client.post("/api/purge-cache")
        body_text = resp.get_data(as_text=True)
        check("⑧响应体不包含真实token", secret_token not in body_text, body_text)
        check("⑧status=failure", resp.get_json()["status"] == "failure", resp.get_json())
        check("⑧detail是固定安全模板，不是原始异常文本",
              resp.get_json()["detail"] == "缓存刷新失败，请稍后再试", resp.get_json())

        conn = db.get_conn()
        stored = conn.execute(
            "SELECT last_detail FROM refresh_locks WHERE lock_key='manual_purge'"
        ).fetchone()
        conn.close()
        check("⑧内部诊断存储(refresh_locks.last_detail)里token也被redact掉",
              stored is not None and secret_token not in stored["last_detail"], stored)
    with_temp_app_env(_run)


class _500Handler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(length)
        body = json.dumps({"success": False, "errors": [{"code": 1000, "message": "boom"}]}).encode("utf-8")
        self.send_response(500)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass


def test_cloudflare_failure_returns_safe_error():
    """走真实的_purge_cloudflare_cache()实现本身（不是替身函数），只是把
    api_url指向一个真实本地http.server（返回HTTP 500），证明失败路径
    确实是从fetch_blog.py那个函数的真实HTTP层面走出来的，不是靠替身
    函数假装失败——跟test_cloudflare_purge.py同样的"真实http.server，
    不mock"约定。
    """
    def _run(tmp, db, app_module, fb):
        _seed_post(db)
        _seed_fetch_log(db, changed_count=1, purge_status="failed")

        httpd = http.server.HTTPServer(("127.0.0.1", 0), _500Handler)
        port = httpd.server_address[1]
        threading.Thread(target=httpd.handle_request, daemon=True).start()

        orig_purge = fb._purge_cloudflare_cache
        fb._purge_cloudflare_cache = lambda urls, **kw: orig_purge(urls, api_url=f"http://127.0.0.1:{port}")
        try:
            client = app_module.app.test_client()
            resp = client.post("/api/purge-cache")
            data = resp.get_json()
            check("⑨Cloudflare失败时仍返回200(不是裸500)", resp.status_code == 200)
            check("⑨status=failure", data["status"] == "failure", data)
            check("⑨detail是固定安全模板文案",
                  data["detail"] == "缓存刷新失败，请稍后再试", data)
            check("⑨响应体不包含Cloudflare原始错误文本", "boom" not in resp.get_data(as_text=True))
        finally:
            fb._purge_cloudflare_cache = orig_purge
            httpd.server_close()
    with_temp_app_env(_run)


def main():
    tests = [
        test_get_last_completed_fetch_log_returns_none_when_empty,
        test_get_last_completed_fetch_log_skips_running_row,
        test_record_manual_purge_result_updates_existing_row,
        test_no_change_does_not_call_cloudflare,
        test_no_prior_fetch_log_is_treated_as_no_change,
        test_changed_url_triggers_existing_purge_function,
        test_deleted_url_enters_purge_logic,
        test_cooldown_blocks_retry_within_five_minutes_when_still_pending,
        test_already_purged_change_does_not_repurge,
        test_manual_success_is_recorded_then_second_click_is_noop,
        test_concurrent_clicks_do_not_produce_duplicate_purge,
        test_endpoint_does_not_trigger_content_fetch,
        test_endpoint_does_not_trigger_git_publish,
        test_secrets_never_appear_in_response_or_internal_storage,
        test_cloudflare_failure_returns_safe_error,
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
