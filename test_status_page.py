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
    """本次全站UI国际化之后，这一行的可见文案默认是中文"GitHub 官方状态"
    （由data-i18n驱动，切到英文才显示"Official GitHub Status"），所以不能
    再只做"整页文本里出现Official GitHub Status"这种粗糙子串检查——那样的话
    即使这行文字被错误地整个删掉、只留在JS字典里也会误判通过。这里改成分别
    确认：链接本身存在、这一行对应的<span>确实带着正确的data-i18n key、
    且中英文两个翻译值都在（不会只有一种语言）。"""
    bsp, output_dir, text, tmp = _build_into_temp()
    try:
        check("页面包含GitHub官方状态链接", 'href="https://www.githubstatus.com/"' in text)
        check('对应状态行的<span>带data-i18n="name_github_official"',
              'data-i18n="name_github_official"' in text)
        check("中文默认文案「GitHub 官方状态」存在", ">GitHub 官方状态<" in text)
        check("英文翻译值Official GitHub Status存在（切换语言后显示）",
              '"Official GitHub Status"' in text)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_cloudflare_official_link_correct():
    bsp, output_dir, text, tmp = _build_into_temp()
    try:
        check("页面包含Cloudflare官方状态链接", 'href="https://www.cloudflarestatus.com/"' in text)
        check('对应状态行的<span>带data-i18n="name_cloudflare_official"',
              'data-i18n="name_cloudflare_official"' in text)
        check("中文默认文案「Cloudflare 官方状态」存在", ">Cloudflare 官方状态<" in text)
        check("英文翻译值Official Cloudflare Status存在（切换语言后显示）",
              '"Official Cloudflare Status"' in text)
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


# ============================================================
# 全站UI国际化：status.foxzen.me（本次新增）
# ============================================================

def test_lang_toggle_button_present():
    bsp, output_dir, text, tmp = _build_into_temp()
    try:
        check("页面包含右上角语言切换按钮容器", 'class="lang-toggle"' in text)
        check("包含中文切换按钮", 'data-lang-btn="zh"' in text)
        check("包含英文切换按钮", 'data-lang-btn="en"' in text)
        check(".lang-toggle使用position:fixed（右上角，桌面/移动端都可见）",
              "position: fixed" in text)
        check("存在窄屏媒体查询，移动端不会遮挡内容", "@media (max-width: 480px)" in text)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_status_strings_zh_en_key_sets_symmetric():
    import re
    bsp, output_dir, text, tmp = _build_into_temp()
    try:
        m = re.search(r"var STATUS_STRINGS = \{(.*?)\n  \};", text, re.DOTALL)
        check("能定位到STATUS_STRINGS字典", m is not None)
        if m:
            body = m.group(1)
            zh_block = re.search(r"zh:\s*\{(.*?)\n    \},\s*en:", body, re.DOTALL)
            en_block = re.search(r"en:\s*\{(.*?)\n    \}", body, re.DOTALL)
            check("能定位到STATUS_STRINGS.zh/.en两个块", bool(zh_block and en_block))
            if zh_block and en_block:
                zh_keys = set(re.findall(r"^\s*(\w+):", zh_block.group(1), re.MULTILINE))
                en_keys = set(re.findall(r"^\s*(\w+):", en_block.group(1), re.MULTILINE))
                check("STATUS_STRINGS中英文翻译键集合完全一致",
                      zh_keys == en_keys, (zh_keys - en_keys, en_keys - zh_keys))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_live_updated_badge_and_detail_never_carry_data_i18n():
    """关键的正确性边界：.badge/.detail会被checkHealthEndpoint/checkPagesTarget/
    checkGreenCloud/checkOfficialStatus这些异步JS函数在真实探测结果返回后
    覆盖内容。如果给它们的初始占位符打上data-i18n，切换语言时的sweep会把
    已经到手的真实探测结果覆盖回"检测中…"占位符——这是一个真实的时序bug，
    这里直接断言"会被实时更新"的那些.badge/.detail在源码里都不带data-i18n
    属性。row-hetzner是唯一的例外（它永远不会被任何check*函数更新，见
    test_hetzner_row_static_content_has_i18n_and_never_live_updated），
    这里明确排除它，不是漏检。"""
    import re
    bsp, output_dir, text, tmp = _build_into_temp()
    try:
        check('会被实时更新的行使用class="badge checking"这个初始状态（不是别的class）',
              text.count('<span class="badge checking">') >= 6, text.count('<span class="badge checking">'))
        check('class="badge checking"这个初始占位符不带data-i18n（避免语言切换覆盖真实探测结果）',
              'data-i18n' not in re.search(r'<span class="badge checking"[^>]*>', text).group(0))

        for m in re.finditer(r'<div class="detail"[^>]*>', text):
            row_context = text[max(0, m.start() - 300):m.start()]
            if 'id="row-hetzner"' in row_context:
                continue  # 唯一允许带data-i18n的例外，单独测过
            check(f'非Hetzner行的.detail不带data-i18n: {m.group(0)}', "data-i18n" not in m.group(0), m.group(0))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_third_party_status_description_never_wrapped_in_translation():
    """r.data.status.description(GitHub/Cloudflare官方状态API返回的原始
    文字)必须原样透传，不能被当成"已知的固定词"套上任何翻译处理——用源码
    检查确认setRow()调用点直接使用r.data.status.description本身，只有它
    缺失时才退回经过statusT()翻译的兜底词。"""
    bsp, output_dir, text, tmp = _build_into_temp()
    try:
        check("官方状态描述有值时原样使用（||前面的部分），不经过statusT()",
              'r.data.status.description || statusT("badge_reported")' in text)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_hetzner_row_static_content_has_i18n_and_never_live_updated():
    """Hetzner这一行永远不会被任何异步JS函数更新(没有对应的check*调用)，
    是纯静态内容，因此可以安全地打上data-i18n（不存在上面.badge/.detail
    那种时序冲突风险）。"""
    bsp, output_dir, text, tmp = _build_into_temp()
    try:
        check('Hetzner行的说明文字带data-i18n="detail_hetzner"', 'data-i18n="detail_hetzner"' in text)
        check('Hetzner行的"无实时检测"徽章带data-i18n="badge_no_live_check"',
              'data-i18n="badge_no_live_check"' in text)
        check("源码里没有任何setRow(\"row-hetzner\", ...)调用（确认这一行确实不会被动态更新）",
              'setRow("row-hetzner"' not in text)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# publish_status_page()：GitHub Pages卫星仓库(foxzen-status)发布机制。
# 全部对着一次性临时git仓库(work repo + 本地bare remote)操作，绝不会碰
# 这个项目自己的仓库、绝不会打真实网络/真实GitHub——跟test_git_publish.py
# 的with_temp_repo同一个思路，这里不需要它那个html/占位子目录，因为卫星
# 仓库场景下repo_dir本身就是要发布的页面根目录。
# ---------------------------------------------------------------------------

def _run_cmd(*args, cwd, env=None, check_ok=True):
    import subprocess
    result = subprocess.run(list(args), cwd=str(cwd), capture_output=True, text=True,
                             encoding="utf-8", env=env)
    if check_ok and result.returncode != 0:
        raise RuntimeError(f"{args} 失败: {result.stderr}")
    return result


def _env_with_identity(name, email):
    import os
    env = dict(os.environ)
    env.update({"GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email,
                "GIT_COMMITTER_NAME": name, "GIT_COMMITTER_EMAIL": email})
    return env


def _with_temp_satellite_repo(fn):
    tmp = Path(tempfile.mkdtemp(prefix="satellite_repo_test_"))
    try:
        remote_dir = tmp / "remote.git"
        work_dir = tmp / "work"
        _run_cmd("git", "init", "--bare", "-b", "master", str(remote_dir), cwd=tmp)
        _run_cmd("git", "init", "-b", "master", str(work_dir), cwd=tmp)
        (work_dir / "README.md").write_text("init\n", encoding="utf-8")
        _run_cmd("git", "add", "README.md", cwd=work_dir)
        _run_cmd("git", "commit", "-m", "init", cwd=work_dir,
                 env=_env_with_identity("Setup", "setup@example.invalid"))
        _run_cmd("git", "remote", "add", "origin", str(remote_dir), cwd=work_dir)
        _run_cmd("git", "push", "-u", "origin", "master", cwd=work_dir)
        fn(work_dir, remote_dir)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _with_github_token(value, fn):
    import os
    had = "GITHUB_TOKEN" in os.environ
    saved = os.environ.get("GITHUB_TOKEN")
    try:
        if value is None:
            os.environ.pop("GITHUB_TOKEN", None)
        else:
            os.environ["GITHUB_TOKEN"] = value
        fn()
    finally:
        if had:
            os.environ["GITHUB_TOKEN"] = saved
        else:
            os.environ.pop("GITHUB_TOKEN", None)


def test_publish_status_page_writes_files_at_satellite_repo_root():
    """卫星仓库场景下repo_dir就是output_dir本身，index.html/CNAME必须直接
    落在仓库根目录——不能像update.foxzen.me的static_status/那样嵌套在
    一个子目录里，这是两者"是否作为foxzen-blog自己的子目录被提交"的
    关键区别。"""
    def _run_case(work_dir, remote_dir):
        import build_status_page as bsp

        def _do():
            result = bsp.publish_status_page(work_dir)
            check("推送成功", result["pushed"], result)
            check("index.html落在仓库根目录", (work_dir / "index.html").exists())
            check("CNAME落在仓库根目录", (work_dir / "CNAME").exists())
            check("CNAME内容是status.foxzen.me",
                  (work_dir / "CNAME").read_text(encoding="utf-8").strip() == "status.foxzen.me")
            remote_head = _run_cmd("git", "rev-parse", "master", cwd=remote_dir).stdout.strip()
            check("远程(bare repo)真的收到了这次push", remote_head == result["commit_sha"])
        _with_github_token("fake-test-token-not-a-real-credential", _do)
    _with_temp_satellite_repo(_run_case)


def test_publish_status_page_missing_token_returns_error_not_exception():
    def _run_case(work_dir, remote_dir):
        import build_status_page as bsp

        def _do():
            result = bsp.publish_status_page(work_dir)
            check("未设置GITHUB_TOKEN时不抛异常、返回credentials_missing",
                  result["pushed"] is False and result.get("error_category") == "credentials_missing", result)
            check("即使没推送，本地文件仍然已经生成(build/verify先于token检查执行)",
                  (work_dir / "index.html").exists())
        _with_github_token(None, _do)
    _with_temp_satellite_repo(_run_case)


def test_publish_status_page_second_run_is_noop():
    """PAGE_TEMPLATE是构建时确定的静态内容，同一台机器上重复发布不应该
    产生新的commit——确认build_status_page这边套上git_publish.commit_and_push()
    之后，无变化时的noop语义仍然成立。"""
    def _run_case(work_dir, remote_dir):
        import build_status_page as bsp

        def _do():
            first = bsp.publish_status_page(work_dir)
            second = bsp.publish_status_page(work_dir)
            check("第一次发布成功", first["pushed"], first)
            check("第二次发布(内容未变)push_state=noop", second.get("push_state") == "noop", second)
            check("第二次发布changed_file_count=0", second.get("changed_file_count") == 0, second)
        _with_github_token("fake-test-token-not-a-real-credential", _do)
    _with_temp_satellite_repo(_run_case)


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
        test_lang_toggle_button_present,
        test_status_strings_zh_en_key_sets_symmetric,
        test_live_updated_badge_and_detail_never_carry_data_i18n,
        test_third_party_status_description_never_wrapped_in_translation,
        test_hetzner_row_static_content_has_i18n_and_never_live_updated,
        test_publish_status_page_writes_files_at_satellite_repo_root,
        test_publish_status_page_missing_token_returns_error_not_exception,
        test_publish_status_page_second_run_is_noop,
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
