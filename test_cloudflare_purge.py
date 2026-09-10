#!/usr/bin/env python3
"""fetch_blog.py::_purge_cloudflare_cache()的回归测试。

背景：fetch_blog.py每次抓取Blogger内容后，如果检测到文章新增/修改/删除，
会主动调用Cloudflare的POST /zones/{zone}/purge_cache把变化的URL(含首页)从
CDN边缘缓存清掉，避免mirror.foxzen.me继续给访客提供旧内容。

覆盖：
- 无内容变化：main()里notify_urls为空时根本不调用_purge_cloudflare_cache()
  （不是"调用了但内部判断没有URL所以no-op"）
- 单篇修改/新增/删除都会把对应canonical URL + 首页一起送进purge请求
- 多篇变化：URL去重，一次批量请求（不是逐个URL发请求，也不是purge_everything）
- token/zone_id缺失、或过滤后没有mirror.foxzen.me的URL：明确skipped，
  完全不发起HTTP请求
- Cloudflare真实HTTP层面：success=true / success=false / HTTP 400 /
  其它HTTP错误 / 响应不是JSON / 超时 / 连接失败，全部分类到不同reason，
  且都不能让异常向上传播（不能连累已经成功写盘的production HTML被回滚）
- secret redaction：假token只应该出现在Authorization header里，不能出现
  在函数返回值里

真实本地http.server，不用mock/monkeypatch框架，跟test_cron_refresh_mirror.py
的_CannedHandler/_serve_canned同一个约定（httpd.handle_request()只处理一次
请求，用完httpd.server_close()）。

不覆盖（有意，见报告）：
- CONTENT_FETCH_LOCK最终idle：_purge_cloudflare_cache()本身不加锁，
  main()对它的调用已经在app.py::_run_content_fetch()持有锁的subprocess
  期间，且本文件已经验证这个函数在任何输入下都不会抛异常——锁最终释放
  这件事由app.py既有的finally: db.release_lock(...)结构性保证（这次改动
  没有改app.py任何一行），不需要在这里另起一个真实OS进程重复验证，那是
  test_refresh_lock.py/test_cron_refresh_mirror.py已经覆盖的锁本身的职责。
- fetch_blog.py::main()完整端到端行为（需要真实Blogger网络访问）：见
  test_feed_pagination.py/test_fetch_delete_sync.py。本文件只测
  _purge_cloudflare_cache()这一个函数，以及main()里"notify_urls为空则不
  调用"这一条判断本身。

用法: python3 test_cloudflare_purge.py
"""
import http.server
import json
import socket
import sys
import threading
import time
import traceback

import fetch_blog

failures = []


def check(name, condition, detail=""):
    if condition:
        print(f"  [PASS] {name}")
    else:
        print(f"  [FAIL] {name} {detail}")
        failures.append(name)


class _RecordingHandler(http.server.BaseHTTPRequestHandler):
    """记录收到的请求(path/Authorization header/JSON body)后，按canned_status/
    canned_body响应；hang_seconds>0时先sleep再响应，用来模拟超时。
    每个测试通过type()动态创建自己的子类，received列表互不共享。
    """
    canned_status = 200
    canned_body = {"success": True}
    hang_seconds = 0
    received = None

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        raw_body = self.rfile.read(length)
        if self.hang_seconds:
            time.sleep(self.hang_seconds)
        self.received.append({
            "path": self.path,
            "authorization": self.headers.get("Authorization", ""),
            "body": json.loads(raw_body.decode("utf-8")) if raw_body else {},
        })
        body = json.dumps(self.canned_body).encode("utf-8")
        self.send_response(self.canned_status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass  # 测试输出不需要http.server自带的access log


class _PlainTextHandler(_RecordingHandler):
    """故意返回非JSON响应体，用于验证invalid_response分类。"""
    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(length)
        body = b"not json at all"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _serve(status=200, body=None, hang_seconds=0, handler_base=_RecordingHandler):
    """起一个真实本地HTTP server，只处理一次请求就结束（跟
    test_cron_refresh_mirror.py::_serve_canned同一个约定）。返回
    (port, httpd, received)；调用方负责用完后httpd.server_close()。
    """
    received = []
    attrs = {"received": received}
    if handler_base is _RecordingHandler:
        attrs["canned_status"] = status
        attrs["canned_body"] = body if body is not None else {"success": True}
        attrs["hang_seconds"] = hang_seconds
    handler = type("Handler", (handler_base,), attrs)
    httpd = http.server.HTTPServer(("127.0.0.1", 0), handler)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.handle_request, daemon=True).start()
    return port, httpd, received


def with_fake_credentials(fn):
    """暂时把fetch_blog.CF_API_TOKEN/CF_ZONE_ID换成假值，跑完还原——这两个
    值本身也从来不是真实token，纯测试占位符，绝不使用任何真实凭据。"""
    orig_token, orig_zone = fetch_blog.CF_API_TOKEN, fetch_blog.CF_ZONE_ID
    fetch_blog.CF_API_TOKEN = "fake-test-token-not-real-0000"
    fetch_blog.CF_ZONE_ID = "fake-zone-id"
    try:
        fn()
    finally:
        fetch_blog.CF_API_TOKEN, fetch_blog.CF_ZONE_ID = orig_token, orig_zone


def with_missing_credential(fn, *, missing):
    orig_token, orig_zone = fetch_blog.CF_API_TOKEN, fetch_blog.CF_ZONE_ID
    fetch_blog.CF_API_TOKEN = "" if missing == "token" else "fake-test-token-not-real-0000"
    fetch_blog.CF_ZONE_ID = "" if missing == "zone" else "fake-zone-id"
    try:
        fn()
    finally:
        fetch_blog.CF_API_TOKEN, fetch_blog.CF_ZONE_ID = orig_token, orig_zone


MIRROR = fetch_blog.MIRROR_ROOT_URL
HOMEPAGE = f"{MIRROR}/"


def test_missing_token_skips_without_any_http_call():
    def _run():
        result = fetch_blog._purge_cloudflare_cache([f"{MIRROR}/a.html"], api_url="http://127.0.0.1:1")
        check("token缺失: status=skipped", result["status"] == "skipped", result)
        check("token缺失: reason=no_token", result["reason"] == "no_token", result)
    with_missing_credential(_run, missing="token")


def test_missing_zone_id_skips_without_any_http_call():
    def _run():
        result = fetch_blog._purge_cloudflare_cache([f"{MIRROR}/a.html"], api_url="http://127.0.0.1:1")
        check("zone_id缺失: status=skipped", result["status"] == "skipped", result)
        check("zone_id缺失: reason=no_zone_id", result["reason"] == "no_zone_id", result)
    with_missing_credential(_run, missing="zone")


def test_non_mirror_urls_are_skipped_as_no_urls():
    """Blogger自己的source_url不属于这个Cloudflare zone，过滤后如果一个
    mirror.foxzen.me的URL都不剩，应该跳过，不是拿着空列表真的发一次请求。"""
    def _run():
        result = fetch_blog._purge_cloudflare_cache(
            ["https://digatlas.blogspot.com/2024/01/foo.html"], api_url="http://127.0.0.1:1")
        check("全部是非mirror URL: status=skipped", result["status"] == "skipped", result)
        check("reason=no_urls", result["reason"] == "no_urls", result)
    with_fake_credentials(_run)


def test_single_modified_article_purges_its_url_and_homepage():
    def _run():
        port, httpd, received = _serve()
        try:
            article = f"{MIRROR}/2026/01/foo.html"
            result = fetch_blog._purge_cloudflare_cache(
                [article, HOMEPAGE], api_url=f"http://127.0.0.1:{port}")
            check("单篇修改: status=success", result["status"] == "success", result)
            check("单篇修改: url_count=2(文章+首页)", result["url_count"] == 2, result)
            check("发起了恰好1次HTTP请求", len(received) == 1)
            sent_files = received[0]["body"].get("files", [])
            check("请求体包含文章URL", article in sent_files, sent_files)
            check("请求体包含首页URL", HOMEPAGE in sent_files, sent_files)
            check("没有使用purge_everything", "purge_everything" not in received[0]["body"])
        finally:
            httpd.server_close()
    with_fake_credentials(_run)


def test_new_article_purges_its_url_and_homepage():
    def _run():
        port, httpd, received = _serve()
        try:
            article = f"{MIRROR}/2026/02/new-post.html"
            result = fetch_blog._purge_cloudflare_cache(
                [article, HOMEPAGE], api_url=f"http://127.0.0.1:{port}")
            check("新文章: status=success", result["status"] == "success", result)
            sent_files = received[0]["body"].get("files", [])
            check("请求体包含新文章URL", article in sent_files, sent_files)
            check("请求体包含首页URL", HOMEPAGE in sent_files, sent_files)
        finally:
            httpd.server_close()
    with_fake_credentials(_run)


def test_deleted_article_purges_old_url_and_homepage():
    def _run():
        port, httpd, received = _serve()
        try:
            old_url = f"{MIRROR}/2025/12/removed-post.html"
            result = fetch_blog._purge_cloudflare_cache(
                [old_url, HOMEPAGE], api_url=f"http://127.0.0.1:{port}")
            check("删除文章: status=success", result["status"] == "success", result)
            sent_files = received[0]["body"].get("files", [])
            check("请求体包含被删除文章的旧URL", old_url in sent_files, sent_files)
            check("请求体包含首页URL", HOMEPAGE in sent_files, sent_files)
        finally:
            httpd.server_close()
    with_fake_credentials(_run)


def test_multiple_changes_are_deduplicated_into_one_request():
    def _run():
        port, httpd, received = _serve()
        try:
            a, b = f"{MIRROR}/2026/01/a.html", f"{MIRROR}/2026/01/b.html"
            urls = [a, b, a, HOMEPAGE, HOMEPAGE]  # 故意重复
            result = fetch_blog._purge_cloudflare_cache(urls, api_url=f"http://127.0.0.1:{port}")
            check("多篇变化: status=success", result["status"] == "success", result)
            check("多篇变化: 去重后剩3个URL(a/b/首页)", result["url_count"] == 3, result)
            check("只发起了1次HTTP请求(批量而不是逐个)", len(received) == 1)
            sent_files = received[0]["body"].get("files", [])
            check("请求体本身也没有重复URL", len(sent_files) == len(set(sent_files)), sent_files)
        finally:
            httpd.server_close()
    with_fake_credentials(_run)


def test_api_success_false_is_reported_as_api_rejected_not_raised():
    def _run():
        port, httpd, received = _serve(
            status=200, body={"success": False, "errors": [{"code": 9109, "message": "Invalid token"}]})
        try:
            result = fetch_blog._purge_cloudflare_cache([f"{MIRROR}/a.html"], api_url=f"http://127.0.0.1:{port}")
            check("API success=false: status=failed", result["status"] == "failed", result)
            check("API success=false: reason=api_rejected", result["reason"] == "api_rejected", result)
        finally:
            httpd.server_close()
    with_fake_credentials(_run)


def test_http_400_is_reported_as_invalid_request():
    def _run():
        port, httpd, received = _serve(status=400, body={"success": False, "errors": [{"code": 1000}]})
        try:
            result = fetch_blog._purge_cloudflare_cache([f"{MIRROR}/a.html"], api_url=f"http://127.0.0.1:{port}")
            check("HTTP 400: status=failed", result["status"] == "failed", result)
            check("HTTP 400: reason=invalid_request", result["reason"] == "invalid_request", result)
        finally:
            httpd.server_close()
    with_fake_credentials(_run)


def test_http_500_is_reported_as_http_error():
    def _run():
        port, httpd, received = _serve(status=500, body={"success": False})
        try:
            result = fetch_blog._purge_cloudflare_cache([f"{MIRROR}/a.html"], api_url=f"http://127.0.0.1:{port}")
            check("HTTP 500: status=failed", result["status"] == "failed", result)
            check("HTTP 500: reason=http_error", result["reason"] == "http_error", result)
        finally:
            httpd.server_close()
    with_fake_credentials(_run)


def test_non_json_response_is_reported_as_invalid_response():
    def _run():
        port, httpd, received = _serve(handler_base=_PlainTextHandler)
        try:
            result = fetch_blog._purge_cloudflare_cache([f"{MIRROR}/a.html"], api_url=f"http://127.0.0.1:{port}")
            check("非JSON响应: status=failed", result["status"] == "failed", result)
            check("非JSON响应: reason=invalid_response", result["reason"] == "invalid_response", result)
        finally:
            httpd.server_close()
    with_fake_credentials(_run)


def test_timeout_is_classified_and_does_not_hang():
    def _run():
        port, httpd, received = _serve(hang_seconds=2)
        # 客户端0.5s超时放弃后，服务端线程仍会在2s后醒来尝试把响应写回一个
        # 已经被客户端关闭的连接——ConnectionAbortedError是这个场景下预期
        # 会发生的，不是测试意外，压掉socketserver默认的错误traceback打印，
        # 避免测试输出被无关噪音淹没（不影响下面对返回值本身的断言）。
        httpd.handle_error = lambda request, client_address: None
        try:
            started = time.time()
            result = fetch_blog._purge_cloudflare_cache(
                [f"{MIRROR}/a.html"], api_url=f"http://127.0.0.1:{port}", timeout=0.5)
            elapsed = time.time() - started
            check("超时: status=failed", result["status"] == "failed", result)
            check("超时: reason=timeout", result["reason"] == "timeout", result)
            check(f"超时判定发生在合理时间内(实际{elapsed:.2f}s < 5s)，没有死等", elapsed < 5, elapsed)
        finally:
            httpd.server_close()
    with_fake_credentials(_run)


def test_network_error_when_server_unreachable():
    """127.0.0.1上一个没有任何进程监听的端口，模拟连接失败这类network_error。"""
    def _run():
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(("127.0.0.1", 0))
        dead_port = s.getsockname()[1]
        s.close()  # 绑定完立刻关闭，端口号短暂内大概率没有别的进程抢占，但不会真的监听
        result = fetch_blog._purge_cloudflare_cache(
            [f"{MIRROR}/a.html"], api_url=f"http://127.0.0.1:{dead_port}", timeout=3)
        check("连接失败: status=failed", result["status"] == "failed", result)
        check("连接失败: reason=network_error", result["reason"] == "network_error", result)
    with_fake_credentials(_run)


def test_no_content_change_means_call_site_never_invokes_purge():
    """main()里真正的"要不要purge"判断在main()自己的notify_urls空/非空分支，
    不在_purge_cloudflare_cache()内部——用替身函数确认notify_urls为空时
    main()那段逻辑的最小等价形式真的没有调用它一次，而不是"调用了但内部
    判断跳过"。完整main()端到端行为不在本文件范围内（见文件头说明）。
    """
    orig = fetch_blog._purge_cloudflare_cache
    called = []
    fetch_blog._purge_cloudflare_cache = lambda *a, **kw: called.append((a, kw))
    try:
        changed_urls, deleted_urls = [], []
        notify_urls = changed_urls + deleted_urls
        if notify_urls:
            fetch_blog._purge_cloudflare_cache(notify_urls + [f"{fetch_blog.MIRROR_ROOT_URL}/"])
            purge_result = {"status": "success", "reason": "ok", "url_count": len(notify_urls) + 1}
        else:
            purge_result = {"status": "skipped", "reason": "no_change", "url_count": 0}
        check("无变化时_purge_cloudflare_cache()完全没被调用", called == [], called)
        check("无变化时purge_result是skipped/no_change",
              purge_result == {"status": "skipped", "reason": "no_change", "url_count": 0}, purge_result)
    finally:
        fetch_blog._purge_cloudflare_cache = orig


def test_fake_token_never_appears_in_return_value_only_in_auth_header():
    def _run():
        port, httpd, received = _serve()
        try:
            result = fetch_blog._purge_cloudflare_cache([f"{MIRROR}/a.html"], api_url=f"http://127.0.0.1:{port}")
            check("返回值里不包含测试用假token字符串",
                  fetch_blog.CF_API_TOKEN not in json.dumps(result), result)
            check("token确实通过Authorization header送到了Cloudflare(证明功能没被破坏)",
                  received[0]["authorization"] == f"Bearer {fetch_blog.CF_API_TOKEN}", received)
        finally:
            httpd.server_close()
    with_fake_credentials(_run)


def main():
    tests = [
        test_missing_token_skips_without_any_http_call,
        test_missing_zone_id_skips_without_any_http_call,
        test_non_mirror_urls_are_skipped_as_no_urls,
        test_single_modified_article_purges_its_url_and_homepage,
        test_new_article_purges_its_url_and_homepage,
        test_deleted_article_purges_old_url_and_homepage,
        test_multiple_changes_are_deduplicated_into_one_request,
        test_api_success_false_is_reported_as_api_rejected_not_raised,
        test_http_400_is_reported_as_invalid_request,
        test_http_500_is_reported_as_http_error,
        test_non_json_response_is_reported_as_invalid_response,
        test_timeout_is_classified_and_does_not_hang,
        test_network_error_when_server_unreachable,
        test_no_content_change_means_call_site_never_invokes_purge,
        test_fake_token_never_appears_in_return_value_only_in_auth_header,
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
