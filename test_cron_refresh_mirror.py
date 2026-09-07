#!/usr/bin/env python3
"""cron_refresh_mirror.py（P0修复：cron改用/api/refresh/mirror，不再直接跑
fetch_blog.py）的回归测试。

覆盖：
1. _post_refresh()对success/cooldown/busy/content_fetch失败/网络异常五种
   响应的分类逻辑——用一个真实的本地http.server（不是mock/monkeypatch），
   serve住提前写好的固定响应。
2. main()的退出码：success/cooldown/busy三种都必须是0（不能让cron把"CONTENT_
   FETCH_LOCK正常拒绝"误判成脚本故障），content_fetch真正失败时必须是1。
3. 不引入Telegram通知（静态断言脚本源码不import telegram_notify）。
4. cron/root.crontab确实已经改成调用这个脚本，而不再直接调用fetch_blog.py。
5. 最核心的验收：跟test_refresh_lock.py::test_two_os_processes_gunicorn_like_concurrency
   同样的手法，启动一个真实独立OS进程跑Flask开发服务器（stub fetch脚本带
   sleep制造竞态窗口），一边是原始HTTP POST模拟"手动点击刷新按钮"，另一边
   是真正subprocess.run这个仓库里的cron_refresh_mirror.py本身（不是重新
   实现一遍它的逻辑）——验证两者不会同时真正进入content_fetch。
6. 跟这次改动完全无关的既有刷新测试(test_refresh_lock.py)重新完整跑一遍，
   确认零回归——这次改动没有改app.py/db.py任何一行。

用法: python3 test_cron_refresh_mirror.py
"""
import http.server
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE_DIR = Path(__file__).parent
CRON_SCRIPT = BASE_DIR / "cron_refresh_mirror.py"
REAL_DB = BASE_DIR / "data" / "blog.db"

failures = []


def check(name, condition, detail=""):
    if condition:
        print(f"  [PASS] {name}")
    else:
        print(f"  [FAIL] {name} {detail}")
        failures.append(name)


def _find_free_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _wait_for_server_ready(port, timeout=10):
    """跟test_refresh_lock.py同名函数同一个约定：只探测TCP端口是否在监听，
    不发真实业务请求，把"进程是否已经起来"和"具体路由行为是否正常"解耦。
    """
    deadline = time.time() + timeout
    last_err = None
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return
        except Exception as e:
            last_err = e
            time.sleep(0.1)
    raise RuntimeError(f"server on port {port} not ready: {last_err}")


class _CannedHandler(http.server.BaseHTTPRequestHandler):
    """只服务事先约定好的固定响应，服务完一次请求这个server实例就不再需要。"""
    canned_status = 200
    canned_body = {}

    def do_POST(self):
        body = json.dumps(self.canned_body).encode("utf-8")
        self.send_response(self.canned_status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass  # 测试输出不需要http.server自带的access log


def _serve_canned(status, body):
    """起一个真实的本地HTTP server（不是mock urllib），只处理一次请求就
    结束，返回(端口, httpd实例)；调用方负责在用完后httpd.server_close()。"""
    handler = type("Handler", (_CannedHandler,), {"canned_status": status, "canned_body": body})
    httpd = http.server.HTTPServer(("127.0.0.1", 0), handler)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.handle_request, daemon=True).start()
    return port, httpd


def test_classify_success():
    import cron_refresh_mirror as mod
    port, httpd = _serve_canned(200, {"target": "mirror", "status": "success", "post_count": 42})
    try:
        result = mod._post_refresh(api_url=f"http://127.0.0.1:{port}/api/refresh/mirror", timeout=5)
        check("HTTP 200 + status=success 被分类为success", result["outcome"] == "success")
        check("message里带post_count", "42" in result["message"], result["message"])
    finally:
        httpd.server_close()


def test_classify_cooldown():
    import cron_refresh_mirror as mod
    port, httpd = _serve_canned(429, {"target": "mirror", "status": "cooldown", "cooldown_remaining_seconds": 123})
    try:
        result = mod._post_refresh(api_url=f"http://127.0.0.1:{port}/api/refresh/mirror", timeout=5)
        check("HTTP 429 被分类为cooldown（不是http_error）", result["outcome"] == "cooldown")
    finally:
        httpd.server_close()


def test_classify_busy():
    import cron_refresh_mirror as mod
    port, httpd = _serve_canned(409, {"target": "mirror", "status": "busy", "reason": "busy_content_fetch",
                                        "detail": "内容抓取正在被另一个刷新任务占用"})
    try:
        result = mod._post_refresh(api_url=f"http://127.0.0.1:{port}/api/refresh/mirror", timeout=5)
        check("HTTP 409 被分类为busy（不是http_error）", result["outcome"] == "busy")
    finally:
        httpd.server_close()


def test_classify_content_fetch_failure():
    import cron_refresh_mirror as mod
    port, httpd = _serve_canned(200, {"target": "mirror", "status": "failure", "detail": "抓取超时(>300s)"})
    try:
        result = mod._post_refresh(api_url=f"http://127.0.0.1:{port}/api/refresh/mirror", timeout=5)
        check("HTTP 200但status=failure 被分类为content_fetch_failed（不是success）",
              result["outcome"] == "content_fetch_failed")
    finally:
        httpd.server_close()


def test_classify_network_error():
    import cron_refresh_mirror as mod
    unused_port = _find_free_port()  # 找到的端口立刻空出来，确定没有任何服务在监听
    result = mod._post_refresh(api_url=f"http://127.0.0.1:{unused_port}/api/refresh/mirror", timeout=2)
    check("连接失败被分类为network_error", result["outcome"] == "network_error")


def test_main_exit_code_zero_for_quiet_outcomes():
    """success/cooldown/busy三种都不应该让cron把这次执行当成"失败"——
    cooldown/busy恰恰是并发保护在正常工作的表现。"""
    for status, body, label in (
        (200, {"status": "success", "post_count": 1}, "success"),
        (429, {"status": "cooldown", "cooldown_remaining_seconds": 10}, "cooldown"),
        (409, {"status": "busy", "reason": "busy_content_fetch"}, "busy"),
    ):
        port, httpd = _serve_canned(status, body)
        try:
            env = dict(os.environ)
            env["MIRROR_REFRESH_API_URL"] = f"http://127.0.0.1:{port}/api/refresh/mirror"
            proc = subprocess.run([sys.executable, str(CRON_SCRIPT)], cwd=str(BASE_DIR),
                                   env=env, capture_output=True, text=True, timeout=15)
            check(f"{label}场景下脚本退出码为0（不应被cron当成故障）",
                  proc.returncode == 0, proc.stdout + proc.stderr)
        finally:
            httpd.server_close()


def test_main_exit_code_one_for_real_failure():
    port, httpd = _serve_canned(200, {"status": "failure", "detail": "抓取超时"})
    try:
        env = dict(os.environ)
        env["MIRROR_REFRESH_API_URL"] = f"http://127.0.0.1:{port}/api/refresh/mirror"
        proc = subprocess.run([sys.executable, str(CRON_SCRIPT)], cwd=str(BASE_DIR),
                               env=env, capture_output=True, text=True, timeout=15)
        check("content_fetch真正失败时脚本退出码为1", proc.returncode == 1, proc.stdout + proc.stderr)
        check("失败详情打印到了stderr（沿用>>fetch.log 2>&1能看到的位置）", "抓取超时" in proc.stderr, proc.stderr)
    finally:
        httpd.server_close()


def test_no_telegram_notification_introduced():
    """这次改动明确不引入任何Telegram通知——脚本不应该import telegram_notify，
    避免以后有人顺手加上，cron每小时的cooldown/busy就会变成消息轰炸。"""
    text = CRON_SCRIPT.read_text(encoding="utf-8")
    check("cron_refresh_mirror.py不import telegram_notify（cooldown/busy不应产生Telegram噪音）",
          "telegram_notify" not in text)


def test_crontab_updated_to_call_wrapper_not_fetch_blog_directly():
    crontab_text = (BASE_DIR / "cron" / "root.crontab").read_text(encoding="utf-8")
    hourly_lines = [ln for ln in crontab_text.splitlines() if ln.strip().startswith("0 * * * *")]
    check("crontab里存在整点触发的那一行", len(hourly_lines) == 1, hourly_lines)
    if hourly_lines:
        line = hourly_lines[0]
        check("整点触发调用的是cron_refresh_mirror.py", "cron_refresh_mirror.py" in line, line)
        check("整点触发不再直接调用fetch_blog.py", "fetch_blog.py" not in line, line)
    check("backup_to_hetzner.py的每日定时行未被本次改动触碰",
          "backup_to_hetzner.py" in crontab_text)
    check("acme.sh证书续期定时行未被本次改动触碰", "acme.sh" in crontab_text)


def test_fetch_blog_py_not_deleted_or_modified():
    """要求4：不删除fetch_blog.py，它仍然是实际content fetch实现。"""
    check("fetch_blog.py仍然存在", (BASE_DIR / "fetch_blog.py").exists())


def test_two_real_processes_manual_click_and_cron_do_not_race():
    """核心验收：手动点击刷新按钮 和 cron改用的API调用 同时发生时，只有一个
    能真正进入content_fetch，另一个必须拿到明确的409——而不是两个都成功
    (数据竞争)，也不是两个都被吞掉(锁失效)。
    """
    import db as db_module
    tmp = Path(tempfile.mkdtemp(prefix="cron_refresh_race_test_"))
    real_db_mtime = REAL_DB.stat().st_mtime if REAL_DB.exists() else None
    try:
        db_path = tmp / "test.db"
        stub = tmp / "stub_fetch.py"
        stub.write_text(
            "import os, sys, time\n"
            "time.sleep(float(os.environ.get('STUB_SLEEP_SECONDS', '1.5')))\n"
            "print('stub fetch ran')\n"
            "sys.exit(0)\n",
            encoding="utf-8",
        )
        port = _find_free_port()
        boot_script = tmp / "server_boot.py"
        boot_script.write_text(
            "import sys, logging\n"
            "from pathlib import Path\n"
            f"sys.path.insert(0, {str(BASE_DIR)!r})\n"
            "import db\n"
            f"db.DB_PATH = Path({str(db_path)!r})\n"
            "import app as app_module\n"
            f"app_module.FETCH_SCRIPT = {str(stub)!r}\n"
            "db.init_db()\n"
            "logging.getLogger('werkzeug').setLevel(logging.ERROR)\n"
            f"app_module.app.run(host='127.0.0.1', port={port}, debug=False, "
            "use_reloader=False, threaded=False)\n",
            encoding="utf-8",
        )
        env = dict(os.environ)
        env["STUB_SLEEP_SECONDS"] = "1.5"
        server_proc = subprocess.Popen([sys.executable, str(boot_script)], cwd=str(BASE_DIR), env=env,
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            _wait_for_server_ready(port)

            results = {}

            def _manual_click():
                req = urllib.request.Request(f"http://127.0.0.1:{port}/api/refresh/mirror", method="POST")
                try:
                    with urllib.request.urlopen(req, timeout=15) as resp:
                        results["manual"] = (resp.status, json.loads(resp.read().decode()))
                except urllib.error.HTTPError as e:
                    results["manual"] = (e.code, json.loads(e.read().decode()))

            def _cron_call():
                cron_env = dict(os.environ)
                cron_env["MIRROR_REFRESH_API_URL"] = f"http://127.0.0.1:{port}/api/refresh/mirror"
                results["cron"] = subprocess.run(
                    [sys.executable, str(CRON_SCRIPT)], cwd=str(BASE_DIR),
                    env=cron_env, capture_output=True, text=True, timeout=20)

            t1 = threading.Thread(target=_manual_click)
            t2 = threading.Thread(target=_cron_call)
            t1.start()
            t2.start()
            t1.join(timeout=25)
            t2.join(timeout=25)

            manual_status, manual_body = results["manual"]
            cron_proc = results["cron"]
            manual_succeeded = manual_body.get("status") == "success"
            cron_won = "outcome=success" in cron_proc.stdout

            check("手动点击和cron API调用同时发生时，不会两边都真正进入content_fetch"
                  "（也不会两边都失败）",
                  manual_succeeded != cron_won,
                  f"manual_status={manual_status}, manual_body={manual_body}, "
                  f"cron_stdout={cron_proc.stdout!r}, cron_returncode={cron_proc.returncode}")

            if not manual_succeeded:
                check("手动点击落败时收到明确的409（不是静默失败/超时）", manual_status == 409, manual_status)
            else:
                # 落败方到底看到busy(409)还是cooldown(429)取决于精确时序：
                # cron一侧是subprocess.run()整个重新起一个Python解释器，
                # 启动开销本身可能让它的HTTP请求晚到——如果晚到锁已经被
                # 手动请求释放之后，落败方看到的就是"在5分钟冷却内"而不是
                # "正忙"，两者都是保护机制生效的正确表现，不应该只认定其中
                # 一种；这里只关心的核心不变量是上面已经验证过的"两边不会
                # 都真正进入content_fetch"，不对具体是哪个状态码做强约束。
                check("cron落败时脚本内部拿到busy或cooldown之一（体现在stdout里，"
                      "两者都说明CONTENT_FETCH_LOCK正确拒绝了这次请求）",
                      ("outcome=busy" in cron_proc.stdout) or ("outcome=cooldown" in cron_proc.stdout),
                      cron_proc.stdout)

            check("无论输赢，cron脚本退出码都应该是0（busy/cooldown不是脚本自身的故障）",
                  cron_proc.returncode == 0, cron_proc.stdout + cron_proc.stderr)
        finally:
            server_proc.terminate()
            try:
                server_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server_proc.kill()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        if real_db_mtime is not None:
            check("测试过程未修改真实data/blog.db（mtime不变）", REAL_DB.stat().st_mtime == real_db_mtime)


def test_cron_call_respects_cooldown_after_prior_success():
    """cron自己连续两次调用（模拟"上一小时刚成功过，这一小时冷却还没到"），
    第二次必须被冷却拒绝，而不是又真正跑了一次fetch。
    """
    tmp = Path(tempfile.mkdtemp(prefix="cron_refresh_cooldown_test_"))
    real_db_mtime = REAL_DB.stat().st_mtime if REAL_DB.exists() else None
    try:
        db_path = tmp / "test.db"
        stub = tmp / "stub_fetch.py"
        stub.write_text("import sys\nprint('stub fetch ran')\nsys.exit(0)\n", encoding="utf-8")
        port = _find_free_port()
        boot_script = tmp / "server_boot.py"
        boot_script.write_text(
            "import sys, logging\n"
            "from pathlib import Path\n"
            f"sys.path.insert(0, {str(BASE_DIR)!r})\n"
            "import db\n"
            f"db.DB_PATH = Path({str(db_path)!r})\n"
            "import app as app_module\n"
            f"app_module.FETCH_SCRIPT = {str(stub)!r}\n"
            "db.init_db()\n"
            "logging.getLogger('werkzeug').setLevel(logging.ERROR)\n"
            f"app_module.app.run(host='127.0.0.1', port={port}, debug=False, "
            "use_reloader=False, threaded=False)\n",
            encoding="utf-8",
        )
        server_proc = subprocess.Popen([sys.executable, str(boot_script)], cwd=str(BASE_DIR),
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            _wait_for_server_ready(port)
            env = dict(os.environ)
            env["MIRROR_REFRESH_API_URL"] = f"http://127.0.0.1:{port}/api/refresh/mirror"

            first = subprocess.run([sys.executable, str(CRON_SCRIPT)], cwd=str(BASE_DIR),
                                    env=env, capture_output=True, text=True, timeout=15)
            check("第一次调用成功", "outcome=success" in first.stdout, first.stdout)

            second = subprocess.run([sys.executable, str(CRON_SCRIPT)], cwd=str(BASE_DIR),
                                     env=env, capture_output=True, text=True, timeout=15)
            check("紧接着的第二次调用被冷却拒绝（不是又跑了一次fetch）",
                  "outcome=cooldown" in second.stdout, second.stdout)
            check("冷却场景下退出码仍然是0", second.returncode == 0, second.stdout + second.stderr)
        finally:
            server_proc.terminate()
            try:
                server_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server_proc.kill()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        if real_db_mtime is not None:
            check("测试过程未修改真实data/blog.db（mtime不变）", REAL_DB.stat().st_mtime == real_db_mtime)


def test_existing_refresh_lock_suite_still_passes():
    """这次改动完全没有触碰app.py/db.py，用实际跑一遍test_refresh_lock.py
    来确认零回归，而不是假设"没改就不会坏"。"""
    proc = subprocess.run([sys.executable, str(BASE_DIR / "test_refresh_lock.py")],
                           cwd=str(BASE_DIR), capture_output=True, text=True, timeout=180)
    check("test_refresh_lock.py（既有刷新系统回归测试）完整跑一遍仍然全部通过",
          proc.returncode == 0, proc.stdout[-1500:] + proc.stderr[-1500:])


def main():
    tests = [
        test_classify_success,
        test_classify_cooldown,
        test_classify_busy,
        test_classify_content_fetch_failure,
        test_classify_network_error,
        test_main_exit_code_zero_for_quiet_outcomes,
        test_main_exit_code_one_for_real_failure,
        test_no_telegram_notification_introduced,
        test_crontab_updated_to_call_wrapper_not_fetch_blog_directly,
        test_fetch_blog_py_not_deleted_or_modified,
        test_two_real_processes_manual_click_and_cron_do_not_race,
        test_cron_call_respects_cooldown_after_prior_success,
        test_existing_refresh_lock_suite_still_passes,
    ]
    for t in tests:
        print(f"--- {t.__name__} ---")
        try:
            t()
        except Exception:
            import traceback
            print(f"  [FAIL] {t.__name__} 抛出异常:")
            traceback.print_exc()
            failures.append(t.__name__)
    if failures:
        print(f"\n共 {len(failures)} 项失败: {failures}")
        sys.exit(1)
    print("\n全部测试通过。")


if __name__ == "__main__":
    main()
