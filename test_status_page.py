#!/usr/bin/env python3
"""status.foxzen.me静态状态页(build_status_page.py) + app.py相关CORS改动的
回归测试。

覆盖：
1. 页面能正常生成(index.html/CNAME)、通过自带的安全检查
2. 必需状态行/官方链接齐全
3. 页面本身不发起任何POST、不包含<form>
4. 页面不包含任何已知密钥/token模式
5. GreenCloud探测失败时的降级文案/区分逻辑确实写在源码里——这个页面的
   实际探测逻辑是浏览器端JS，本项目没有引入任何浏览器自动化/JS运行时
   测试工具，这里跟其余测试同一个约定：只做文本层面的静态断言
6. app.py新增的status.foxzen.me CORS白名单：只覆盖GET-only的
   /api/health、/api/refresh/<target>/status，不覆盖POST触发端点，
   且不影响mirror/backup/github/cf四个既有来源原有的CORS行为

用法: python3 test_status_page.py
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


def _build_into_temp():
    import build_status_page as bsp
    tmp = Path(tempfile.mkdtemp(prefix="status_page_test_"))
    output_dir = bsp.build_status_page(tmp / "publish_status")
    text = (output_dir / "index.html").read_text(encoding="utf-8")
    return bsp, output_dir, text, tmp


def test_build_produces_index_and_cname():
    bsp, output_dir, text, tmp = _build_into_temp()
    try:
        check("生成了index.html", (output_dir / "index.html").exists())
        check("生成了CNAME", (output_dir / "CNAME").exists())
        cname_text = (output_dir / "CNAME").read_text(encoding="utf-8").strip()
        check("CNAME内容是status.foxzen.me", cname_text == "status.foxzen.me", cname_text)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_verify_passes_on_fresh_build():
    bsp, output_dir, text, tmp = _build_into_temp()
    try:
        try:
            bsp.verify_status_page(output_dir)
            ok, detail = True, ""
        except bsp.StatusPageVerificationError as e:
            ok, detail = False, str(e)
        check("刚构建出来的页面通过verify_status_page()安全检查", ok, detail)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_all_required_rows_present():
    bsp, output_dir, text, tmp = _build_into_temp()
    try:
        required_ids = [
            "row-mirror", "row-backup", "row-github", "row-cf",
            "row-greencloud", "row-hetzner",
            "row-github-official", "row-cloudflare-official",
        ]
        for row_id in required_ids:
            check(f'存在id="{row_id}"的状态行', f'id="{row_id}"' in text)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_github_official_link_correct():
    bsp, output_dir, text, tmp = _build_into_temp()
    try:
        check("页面包含GitHub官方状态链接", 'href="https://www.githubstatus.com/"' in text)
        check("链接旁边明确标注Official GitHub Status字样", "Official GitHub Status" in text)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_cloudflare_official_link_correct():
    bsp, output_dir, text, tmp = _build_into_temp()
    try:
        check("页面包含Cloudflare官方状态链接", 'href="https://www.cloudflarestatus.com/"' in text)
        check("链接旁边明确标注Official Cloudflare Status字样", "Official Cloudflare Status" in text)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_links_to_update_page():
    bsp, output_dir, text, tmp = _build_into_temp()
    try:
        check("status页面包含指向update.foxzen.me的链接",
              'href="https://update.foxzen.me/"' in text)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_no_post_anywhere():
    bsp, output_dir, text, tmp = _build_into_temp()
    try:
        check("页面全文不包含POST字样(只读页面不应该发起任何写请求)", "POST" not in text)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_no_form_element():
    bsp, output_dir, text, tmp = _build_into_temp()
    try:
        check("页面不包含<form>标签", "<form" not in text.lower())
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_no_generic_secret_patterns():
    bsp, output_dir, text, tmp = _build_into_temp()
    try:
        for bad in ("GITHUB_TOKEN", "TG_BOT_TOKEN", "FOXZEN_GIT_PUSH_TOKEN",
                    "BEGIN RSA PRIVATE KEY", "BEGIN PRIVATE KEY", "BEGIN OPENSSH PRIVATE KEY"):
            check(f"页面不包含疑似密钥标识: {bad}", bad not in text)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_no_github_pat_pattern():
    bsp, output_dir, text, tmp = _build_into_temp()
    try:
        check("页面不包含GitHub PAT前缀特征(ghp_)", "ghp_" not in text)
        check("页面不包含Fine-grained PAT前缀特征(github_pat_)", "github_pat_" not in text)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_no_cloudflare_token_pattern():
    bsp, output_dir, text, tmp = _build_into_temp()
    try:
        check("页面不包含Cloudflare API token变量名", "CF_API_TOKEN" not in text)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_greencloud_failure_handling_logic_present():
    bsp, output_dir, text, tmp = _build_into_temp()
    try:
        check("源码里包含探测失败时的安全降级文案",
              "Unreachable" in text and "network error" in text)
        check("源码里明确注明检查是在访客浏览器发起的(不是中心化监控)",
              "your browser" in text.lower())
        check("mirror/backup双路径分别失败时的区分文案存在"
              "(用于区分GreenCloud自身故障与Cloudflare边缘故障)",
              "bypasses Cloudflare" in text)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _with_temp_app_for_cors(fn):
    """跟test_refresh_lock.py的with_temp_app_env()同一个约定：db.DB_PATH
    必须先于`import app`重定向，避免碰真实data/blog.db；同时把app.notify
    换成no-op——这里只测CORS响应头，/api/health内部按流程会在磁盘使用率
    超过阈值时调用notify()发Telegram通知，测试不应该依赖"当前开发机磁盘
    使用率恰好没到80%"这种运气，必须显式屏蔽掉这个真实副作用。
    """
    tmp = Path(tempfile.mkdtemp(prefix="status_cors_test_"))
    real_db_mtime = REAL_DB.stat().st_mtime if REAL_DB.exists() else None

    import db
    orig_db_path = db.DB_PATH
    db.DB_PATH = tmp / "test.db"

    import app as app_module
    orig_notify = app_module.notify
    app_module.notify = lambda *a, **k: None
    app_module.app.config["TESTING"] = True

    try:
        db.init_db()
        fn(app_module)
    finally:
        db.DB_PATH = orig_db_path
        app_module.notify = orig_notify
        shutil.rmtree(tmp, ignore_errors=True)

    if real_db_mtime is not None:
        check("测试过程未修改真实data/blog.db（mtime不变）",
              REAL_DB.stat().st_mtime == real_db_mtime)


def test_status_origin_allowed_on_get_only_endpoints():
    def run(app_module):
        client = app_module.app.test_client()
        origin = "https://status.foxzen.me"

        r = client.get("/api/health", headers={"Origin": origin})
        check("status.foxzen.me能读取/api/health的CORS响应头",
              r.headers.get("Access-Control-Allow-Origin") == origin,
              r.headers.get("Access-Control-Allow-Origin"))

        for target in ("mirror", "backup", "github", "cf"):
            r = client.get(f"/api/refresh/{target}/status", headers={"Origin": origin})
            check(f"status.foxzen.me能读取/api/refresh/{target}/status的CORS响应头",
                  r.headers.get("Access-Control-Allow-Origin") == origin,
                  r.headers.get("Access-Control-Allow-Origin"))

    _with_temp_app_for_cors(run)


def test_status_origin_excluded_from_post_trigger_endpoint():
    def run(app_module):
        client = app_module.app.test_client()
        origin = "https://status.foxzen.me"
        # 只发OPTIONS预检，不会真的触发一次刷新——POST真正执行fetch_blog.py
        # 子进程/git push不是这个测试要覆盖的范围(见test_refresh_lock.py)，
        # 这里只关心status.foxzen.me有没有被无意放进"能读POST触发端点响应"
        # 的CORS白名单（即使放进去也不代表它能发起POST——匿名公开触发本来
        # 就不看Origin——但范围应该尽量收紧，不能因为这次任务顺手放宽）。
        r = client.options("/api/refresh/mirror", headers={"Origin": origin})
        check("status.foxzen.me不应该出现在POST触发端点的CORS白名单里"
              "(只应该能读GET-only的/status端点)",
              r.headers.get("Access-Control-Allow-Origin") != origin,
              r.headers.get("Access-Control-Allow-Origin"))

    _with_temp_app_for_cors(run)


def test_existing_refresh_cors_origins_unaffected():
    def run(app_module):
        client = app_module.app.test_client()
        for origin in ("https://mirror.foxzen.me", "https://backup.foxzen.me",
                       "https://github.foxzen.me", "https://cf.foxzen.me"):
            r = client.options("/api/refresh/mirror", headers={"Origin": origin})
            check(f"{origin}仍然能读取POST触发端点的CORS响应头(未受本次改动影响)",
                  r.headers.get("Access-Control-Allow-Origin") == origin,
                  r.headers.get("Access-Control-Allow-Origin"))

            r = client.get("/api/refresh/mirror/status", headers={"Origin": origin})
            check(f"{origin}仍然能读取GET-only状态端点的CORS响应头",
                  r.headers.get("Access-Control-Allow-Origin") == origin,
                  r.headers.get("Access-Control-Allow-Origin"))

    _with_temp_app_for_cors(run)


def test_unrelated_origin_still_rejected():
    def run(app_module):
        client = app_module.app.test_client()
        origin = "https://evil.example"
        r = client.get("/api/health", headers={"Origin": origin})
        check("未授权的任意来源不会拿到/api/health的CORS响应头",
              "Access-Control-Allow-Origin" not in r.headers)
        r = client.get("/api/refresh/mirror/status", headers={"Origin": origin})
        check("未授权的任意来源不会拿到refresh status端点的CORS响应头",
              "Access-Control-Allow-Origin" not in r.headers)

    _with_temp_app_for_cors(run)


def main():
    tests = [
        test_build_produces_index_and_cname,
        test_verify_passes_on_fresh_build,
        test_all_required_rows_present,
        test_github_official_link_correct,
        test_cloudflare_official_link_correct,
        test_links_to_update_page,
        test_no_post_anywhere,
        test_no_form_element,
        test_no_generic_secret_patterns,
        test_no_github_pat_pattern,
        test_no_cloudflare_token_pattern,
        test_greencloud_failure_handling_logic_present,
        test_status_origin_allowed_on_get_only_endpoints,
        test_status_origin_excluded_from_post_trigger_endpoint,
        test_existing_refresh_cors_origins_unaffected,
        test_unrelated_origin_still_rejected,
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
