#!/usr/bin/env python3
"""【GreenCloud单次热更新 -> GitHub+Cloudflare自动同步】回归测试。

范围说明（跟已有的test_refresh_lock.py明确分开，不重复测试）：
- content_fetch/git_publish两把锁本身的冷却/互斥/stale恢复/fencing逻辑
  是既有代码，未改动，test_refresh_lock.py已经覆盖，这里不重测。
- 这次任务实际改动的代码只有：
  1. app.py::_run_git_publish() 新增target_cooldown参数。
  2. app.py::_publish_and_report()（新函数，从原本内联在refresh_target()
     里的github/cf发布逻辑抽出来，供手动路由和自动fan-out共用）。
  3. app.py::_start_publish_fan_out()（新函数）+ refresh_target()里
     mirror成功后调用它、github/cf分支不再调用_run_content_fetch()。
  4. db.py::init_db() 预先为4个target建refresh_targets行。
- 已知未覆盖、建议后续补充：manual github/cf点击与自动fan-out真正跨OS
  进程并发的场景——GIT_PUBLISH_LOCK互斥机制本身未改动，已经被
  test_refresh_lock.py::test_two_os_processes_gunicorn_like_concurrency
  在真实多进程下验证过，这里只用同进程内的顺序调用验证新增的编排逻辑
  本身（谁调用_run_git_publish、什么时候调用、结果记到哪个target）。

用法: python3 test_publish_fanout.py
"""
import shutil
import subprocess
import sys
import tempfile
import time
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
    import db
    tmp = Path(tempfile.mkdtemp(prefix="fanout_db_test_"))
    orig_db_path = db.DB_PATH
    real_db_mtime = REAL_DB.stat().st_mtime if REAL_DB.exists() else None
    db.DB_PATH = tmp / "test.db"
    try:
        db.init_db()
        fn(tmp, db)
    finally:
        db.DB_PATH = orig_db_path
        shutil.rmtree(tmp, ignore_errors=True)
    if real_db_mtime is not None:
        check("测试过程未修改真实data/blog.db（mtime不变）",
              REAL_DB.stat().st_mtime == real_db_mtime)


def with_temp_app_env(fn):
    """跟test_refresh_lock.py::with_temp_app_env()同一个约定（各测试文件
    各自维护一份，不共享），临时db + 临时FETCH_SCRIPT（可控stub脚本）。
    """
    tmp = Path(tempfile.mkdtemp(prefix="fanout_api_test_"))
    real_db_mtime = REAL_DB.stat().st_mtime if REAL_DB.exists() else None

    import db
    orig_db_path = db.DB_PATH
    db.DB_PATH = tmp / "test.db"

    import app as app_module
    orig_fetch_script = app_module.FETCH_SCRIPT
    orig_github_token = app_module.GITHUB_TOKEN
    orig_commit_and_push = app_module.git_publish.commit_and_push
    orig_trigger_and_wait = app_module.github_actions.trigger_and_wait
    orig_start_fan_out = app_module._start_publish_fan_out
    orig_base_dir = app_module.BASE_DIR

    stub = tmp / "stub_fetch.py"
    stub.write_text(
        "import os, sys, time\n"
        "time.sleep(float(os.environ.get('STUB_SLEEP_SECONDS', '0')))\n"
        "print('stub fetch ran')\n"
        "sys.exit(int(os.environ.get('STUB_EXIT_CODE', '0')))\n",
        encoding="utf-8",
    )
    app_module.FETCH_SCRIPT = stub
    try:
        db.init_db()
        fn(tmp, db, app_module)
    finally:
        db.DB_PATH = orig_db_path
        app_module.FETCH_SCRIPT = orig_fetch_script
        app_module.GITHUB_TOKEN = orig_github_token
        app_module.git_publish.commit_and_push = orig_commit_and_push
        app_module.github_actions.trigger_and_wait = orig_trigger_and_wait
        app_module._start_publish_fan_out = orig_start_fan_out
        app_module.BASE_DIR = orig_base_dir
        shutil.rmtree(tmp, ignore_errors=True)

    if real_db_mtime is not None:
        check("测试过程未修改真实data/blog.db（mtime不变）",
              REAL_DB.stat().st_mtime == real_db_mtime)


def _wait_until(predicate, timeout=3.0, interval=0.05):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


# ============================================================
# 一、db.py::init_db() 预建refresh_targets行
# ============================================================

def test_init_db_preseeds_all_four_target_rows():
    """fan-out用target_cooldown=False调用_run_git_publish()，不会触发
    refresh_targets的惰性创建（见该函数文档字符串）——如果github/cf在
    生产环境从未被手动点击过，这两行必须在init_db()阶段就已经存在，
    否则fan-out的record_target_result()会静默无效写入。
    """
    def _run(tmp, db):
        conn = db.get_conn()
        rows = {r["target_key"] for r in conn.execute("SELECT target_key FROM refresh_targets").fetchall()}
        conn.close()
        check("mirror/backup/github/cf四行全部预先存在",
              rows == {"mirror", "backup", "github", "cf"}, rows)
    with_temp_db(_run)


def test_init_db_preseed_does_not_overwrite_existing_row():
    """INSERT OR IGNORE不能覆盖已经写过真实结果的历史记录——用真实的
    record_target_result()写一条结果，再调一次init_db()，确认这条记录
    原封不动。"""
    def _run(tmp, db):
        db.record_target_result("github", "success", "之前的真实结果", commit_sha="old-sha")
        db.init_db()
        conn = db.get_conn()
        row = conn.execute("SELECT last_status, last_commit_sha FROM refresh_targets WHERE target_key='github'").fetchone()
        conn.close()
        check("重复调用init_db()不会清空已有的github结果",
              row["last_status"] == "success" and row["last_commit_sha"] == "old-sha", dict(row))
    with_temp_db(_run)


# ============================================================
# 二、手动github/cf不再触发content_fetch，但仍然有冷却保护
# ============================================================

def test_manual_github_no_longer_calls_fetch_script():
    def _run(tmp, db, app_module):
        app_module.GITHUB_TOKEN = "fake-token"
        app_module.git_publish.commit_and_push = lambda *a, **kw: {
            "pushed": True, "commit_sha": "sha1", "changed_file_count": 1, "push_state": "pushed",
        }
        app_module.github_actions.trigger_and_wait = lambda *a, **kw: {
            "outcome": "success", "run_id": 1, "run_html_url": "x",
        }
        calls = []
        orig_run = subprocess.run

        def spy_run(cmd, *a, **kw):
            calls.append(cmd)
            return orig_run(cmd, *a, **kw)
        app_module.subprocess.run = spy_run
        try:
            client = app_module.app.test_client()
            resp = client.post("/api/refresh/github")
            check("手动github请求成功", resp.status_code == 200, resp.status_code)
            fetch_calls = [c for c in calls if str(app_module.FETCH_SCRIPT) in " ".join(str(x) for x in c)]
            check("手动github完全没有调用fetch_blog.py子进程（不再重复抓Blogger）",
                  len(fetch_calls) == 0, calls)
        finally:
            app_module.subprocess.run = orig_run
    with_temp_app_env(_run)


def test_manual_cf_no_longer_calls_fetch_script():
    def _run(tmp, db, app_module):
        app_module.GITHUB_TOKEN = "fake-token"
        app_module.git_publish.commit_and_push = lambda *a, **kw: {
            "pushed": True, "commit_sha": "sha2", "changed_file_count": 1, "push_state": "pushed",
        }
        calls = []
        orig_run = subprocess.run

        def spy_run(cmd, *a, **kw):
            calls.append(cmd)
            return orig_run(cmd, *a, **kw)
        app_module.subprocess.run = spy_run
        try:
            client = app_module.app.test_client()
            resp = client.post("/api/refresh/cf")
            check("手动cf请求成功", resp.status_code == 200, resp.status_code)
            fetch_calls = [c for c in calls if str(app_module.FETCH_SCRIPT) in " ".join(str(x) for x in c)]
            check("手动cf完全没有调用fetch_blog.py子进程", len(fetch_calls) == 0, calls)
        finally:
            app_module.subprocess.run = orig_run
    with_temp_app_env(_run)


def test_manual_github_still_has_cooldown_via_git_publish():
    """冷却基准从content_fetch阶段挪到了git_publish阶段（_run_git_publish
    (target_cooldown=True)），这里验证端到端效果跟改动前一样：连续两次
    手动点击，第二次仍然被5分钟冷却拒绝——这是test_refresh_lock.py里
    此前没有覆盖到的一个真实缺口（那边的冷却测试只测了mirror/backup，
    以及db.py单元层面任意target_key的冷却，没有专门测github/cf走HTTP
    这一层的冷却）。"""
    def _run(tmp, db, app_module):
        app_module.GITHUB_TOKEN = "fake-token"
        app_module.git_publish.commit_and_push = lambda *a, **kw: {
            "pushed": True, "commit_sha": "sha3", "changed_file_count": 1, "push_state": "pushed",
        }
        app_module.github_actions.trigger_and_wait = lambda *a, **kw: {
            "outcome": "success", "run_id": 1, "run_html_url": "x",
        }
        client = app_module.app.test_client()
        resp1 = client.post("/api/refresh/github")
        check("第一次手动github成功", resp1.status_code == 200, resp1.status_code)
        resp2 = client.post("/api/refresh/github")
        check("5分钟内第二次手动github返回429", resp2.status_code == 429, resp2.status_code)
        check("429响应带cooldown_remaining_seconds", "cooldown_remaining_seconds" in resp2.get_json())
    with_temp_app_env(_run)


def test_manual_cf_still_has_cooldown_via_git_publish():
    def _run(tmp, db, app_module):
        app_module.GITHUB_TOKEN = "fake-token"
        app_module.git_publish.commit_and_push = lambda *a, **kw: {
            "pushed": True, "commit_sha": "sha4", "changed_file_count": 1, "push_state": "pushed",
        }
        client = app_module.app.test_client()
        resp1 = client.post("/api/refresh/cf")
        check("第一次手动cf成功", resp1.status_code == 200, resp1.status_code)
        resp2 = client.post("/api/refresh/cf")
        check("5分钟内第二次手动cf返回429", resp2.status_code == 429, resp2.status_code)
    with_temp_app_env(_run)


def test_different_manual_targets_do_not_share_cooldown():
    """github的冷却不应该挡住cf——两者虽然共享GIT_PUBLISH_LOCK这把互斥锁，
    但target_key相关的冷却各自独立（github/cf各自一行refresh_targets）。"""
    def _run(tmp, db, app_module):
        app_module.GITHUB_TOKEN = "fake-token"
        app_module.git_publish.commit_and_push = lambda *a, **kw: {
            "pushed": True, "commit_sha": "sha5", "changed_file_count": 1, "push_state": "pushed",
        }
        app_module.github_actions.trigger_and_wait = lambda *a, **kw: {
            "outcome": "success", "run_id": 1, "run_html_url": "x",
        }
        client = app_module.app.test_client()
        resp1 = client.post("/api/refresh/github")
        check("github成功", resp1.status_code == 200)
        resp2 = client.post("/api/refresh/cf")
        check("紧接着手动cf不受github冷却影响，同样成功", resp2.status_code == 200, resp2.status_code)
    with_temp_app_env(_run)


# ============================================================
# 三、mirror成功自动扩散到github/cf；backup/失败的mirror都不扩散
# ============================================================

def test_mirror_success_starts_fan_out():
    def _run(tmp, db, app_module):
        calls = []
        app_module._start_publish_fan_out = lambda: calls.append(1)
        client = app_module.app.test_client()
        resp = client.post("/api/refresh/mirror")
        check("mirror请求成功", resp.status_code == 200, resp.status_code)
        check("mirror成功后启动了一次fan-out", calls == [1], calls)
    with_temp_app_env(_run)


def test_backup_success_does_not_start_fan_out():
    def _run(tmp, db, app_module):
        calls = []
        app_module._start_publish_fan_out = lambda: calls.append(1)
        client = app_module.app.test_client()
        resp = client.post("/api/refresh/backup")
        check("backup请求成功", resp.status_code == 200, resp.status_code)
        check("backup成功不触发fan-out（backup是独立灾备入口，不参与"
              "GreenCloud->GitHub->Pages这条自动同步链路）", calls == [], calls)
    with_temp_app_env(_run)


def test_mirror_fetch_failure_does_not_start_fan_out():
    def _run(tmp, db, app_module):
        import os
        calls = []
        app_module._start_publish_fan_out = lambda: calls.append(1)
        os.environ["STUB_EXIT_CODE"] = "1"
        try:
            client = app_module.app.test_client()
            resp = client.post("/api/refresh/mirror")
            data = resp.get_json()
            check("fetch失败时mirror返回failure", data["status"] == "failure", data)
            check("mirror抓取失败不触发fan-out（没有新内容/内容可能损坏，不应该发布）",
                  calls == [], calls)
        finally:
            del os.environ["STUB_EXIT_CODE"]
    with_temp_app_env(_run)


def test_fan_out_end_to_end_updates_github_and_cf_status():
    """不mock _start_publish_fan_out本身——让它真的起一个后台线程，验证
    最终github/cf两个target的/status都能看到真实结果（跟
    test_refresh_lock.py::test_github_timeout_spawns_background_watcher_
    that_eventually_records_real_result()同样的"轮询到结果出现"手法）。
    """
    def _run(tmp, db, app_module):
        app_module.GITHUB_TOKEN = "fake-token"
        app_module.git_publish.commit_and_push = lambda *a, **kw: {
            "pushed": True, "commit_sha": "fanout-sha", "changed_file_count": 2, "push_state": "pushed",
        }
        app_module.github_actions.trigger_and_wait = lambda *a, **kw: {
            "outcome": "success", "run_id": 42, "run_html_url": "https://github.com/x/y/actions/runs/42",
        }
        client = app_module.app.test_client()
        resp = client.post("/api/refresh/mirror")
        check("mirror本身立即返回success，不等fan-out跑完", resp.status_code == 200)

        def _github_done():
            return db.get_target_status("github", 300)["last_result"] is not None

        def _cf_done():
            return db.get_target_status("cf", 300)["last_result"] is not None

        check("后台fan-out最终让github也有了结果", _wait_until(_github_done))
        check("后台fan-out最终让cf也有了结果", _wait_until(_cf_done))
        github_state = db.get_target_status("github", 300)
        cf_state = db.get_target_status("cf", 300)
        check("github fan-out结果是success", github_state["last_result"]["status"] == "success", github_state)
        check("github fan-out结果commit正确", github_state["last_result"]["commit"] == "fanout-sha", github_state)
        check("cf fan-out结果是success", cf_state["last_result"]["status"] == "success", cf_state)
    with_temp_app_env(_run)


def test_fan_out_isolates_github_success_from_cf_failure():
    """核心故障隔离要求：github成功、cf失败时，两者各自独立记录，github
    不应该被"回滚"（这里commit_and_push对github/cf各只被调用一次，用
    调用序号模拟"github那次成功、cf那次失败"，不依赖两次调用内容完全
    相同这个巧合）。"""
    def _run(tmp, db, app_module):
        app_module.GITHUB_TOKEN = "fake-token"
        call_count = {"n": 0}

        def flaky_commit_and_push(*a, **kw):
            call_count["n"] += 1
            if call_count["n"] == 1:  # github先跑
                return {"pushed": True, "commit_sha": "github-ok-sha",
                         "changed_file_count": 1, "push_state": "pushed"}
            return {"pushed": False, "error_category": "git_push_error", "detail": "模拟push失败"}
        app_module.git_publish.commit_and_push = flaky_commit_and_push
        app_module.github_actions.trigger_and_wait = lambda *a, **kw: {
            "outcome": "success", "run_id": 7, "run_html_url": "x",
        }

        client = app_module.app.test_client()
        client.post("/api/refresh/mirror")

        def _both_done():
            return (db.get_target_status("github", 300)["last_result"] is not None
                    and db.get_target_status("cf", 300)["last_result"] is not None)
        check("github/cf都跑完了", _wait_until(_both_done))

        github_state = db.get_target_status("github", 300)
        cf_state = db.get_target_status("cf", 300)
        check("github成功且不受cf失败影响", github_state["last_result"]["status"] == "success", github_state)
        check("cf独立记录了自己的failure", cf_state["last_result"]["status"] == "failure", cf_state)
    with_temp_app_env(_run)


def test_fan_out_cf_failure_does_not_block_immediate_manual_retry():
    """这是本轮架构设计里最重要的一条新增要求：GitHub自动成功、Cloudflare
    自动失败后，用户立即手动点"同步cf"必须能真正重试（不是被自己都不
    知道发生过的fan-out尝试卡在5分钟冷却里）。"""
    def _run(tmp, db, app_module):
        app_module.GITHUB_TOKEN = "fake-token"
        call_count = {"n": 0}

        def flaky_commit_and_push(*a, **kw):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return {"pushed": True, "commit_sha": "gh-sha",
                         "changed_file_count": 1, "push_state": "pushed"}
            if call_count["n"] == 2:
                return {"pushed": False, "error_category": "git_push_error", "detail": "第一次cf失败"}
            return {"pushed": True, "commit_sha": "cf-retry-sha",
                     "changed_file_count": 1, "push_state": "pushed"}
        app_module.git_publish.commit_and_push = flaky_commit_and_push
        app_module.github_actions.trigger_and_wait = lambda *a, **kw: {
            "outcome": "success", "run_id": 7, "run_html_url": "x",
        }

        client = app_module.app.test_client()
        client.post("/api/refresh/mirror")

        check("fan-out的cf确实失败了一次", _wait_until(
            lambda: (db.get_target_status("cf", 300).get("last_result") or {}).get("status") == "failure"))

        retry_resp = client.post("/api/refresh/cf")
        retry_data = retry_resp.get_json()
        check("fan-out失败后立即手动重试cf不会被冷却拒绝（不是429/cooldown）",
              retry_resp.status_code != 429 and retry_data.get("status") != "cooldown", retry_data)
        check("手动重试真正执行了，拿到了新的成功结果",
              retry_resp.status_code == 200 and retry_data.get("status") == "success", retry_data)
    with_temp_app_env(_run)


def test_fan_out_noop_skips_github_dispatch():
    """html/在fan-out开始前就已经没有可提交的变化（比如mirror这次fetch
    没有拉到任何新内容）：github的publish阶段检测到push_state=noop，
    不应该触发workflow_dispatch。"""
    def _run(tmp, db, app_module):
        app_module.GITHUB_TOKEN = "fake-token"
        app_module.git_publish.commit_and_push = lambda *a, **kw: {
            "pushed": True, "commit_sha": "unchanged-sha", "changed_file_count": 0, "push_state": "noop",
        }
        dispatch_calls = []

        def recording_dispatch(*a, **kw):
            dispatch_calls.append(1)
            return {"outcome": "success", "run_id": 1, "run_html_url": "x"}
        app_module.github_actions.trigger_and_wait = recording_dispatch

        client = app_module.app.test_client()
        client.post("/api/refresh/mirror")
        check("fan-out（github）最终有结果", _wait_until(
            lambda: db.get_target_status("github", 300)["last_result"] is not None))
        check("noop时没有触发workflow_dispatch", dispatch_calls == [], dispatch_calls)
        github_state = db.get_target_status("github", 300)
        check("noop仍然报告success（不是错误）",
              github_state["last_result"]["status"] == "success", github_state)
    with_temp_app_env(_run)


# ============================================================
# 四、真实git仓库：fan-out顺序发布只产生一个commit（不需要额外去重逻辑）
# ============================================================

def _git_run(*args, cwd, env=None, check_ok=True):
    result = subprocess.run(list(args), cwd=str(cwd), capture_output=True, text=True, env=env)
    if check_ok and result.returncode != 0:
        raise RuntimeError(f"{args} 失败: {result.stderr}")
    return result


def _env_with_identity(name, email):
    import os
    env = dict(os.environ)
    env.update({"GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email,
                "GIT_COMMITTER_NAME": name, "GIT_COMMITTER_EMAIL": email})
    return env


def test_fan_out_sequential_publish_produces_single_commit():
    """跟test_git_publish.py::with_temp_repo()同样的真实临时仓库手法：
    github的_publish_and_report()先跑，真的commit+push一次；紧接着cf的
    _publish_and_report()再跑，此时html/已经没有变化，必须落进noop分支，
    不产生第二个commit——不依赖任何"只让第一个target真正提交"的额外
    判断逻辑，纯粹是git_publish.commit_and_push()本身"没变化不commit"
    这个既有行为的自然结果（见_publish_and_report()里的注释）。
    """
    def _run(tmp, db, app_module):
        import os
        remote_dir = tmp / "remote.git"
        work_dir = tmp / "work"
        _git_run("git", "init", "--bare", "-b", "master", str(remote_dir), cwd=tmp)
        _git_run("git", "init", "-b", "master", str(work_dir), cwd=tmp)
        (work_dir / "README.md").write_text("init\n", encoding="utf-8")
        _git_run("git", "add", "README.md", cwd=work_dir)
        _git_run("git", "commit", "-m", "init", cwd=work_dir,
                 env=_env_with_identity("Setup", "setup@example.invalid"))
        html_dir = work_dir / "html"
        html_dir.mkdir()
        (html_dir / ".gitkeep").write_text("", encoding="utf-8")
        _git_run("git", "add", "html/.gitkeep", cwd=work_dir)
        _git_run("git", "commit", "-m", "seed html/", cwd=work_dir,
                 env=_env_with_identity("Setup", "setup@example.invalid"))
        _git_run("git", "remote", "add", "origin", str(remote_dir), cwd=work_dir)
        _git_run("git", "push", "-u", "origin", "master", cwd=work_dir)

        # 真正的一次"新内容"：新增一篇文章文件，模拟这一轮fetch确实抓到了变化
        (html_dir / "posts_index.html").write_text("<p>新文章</p>", encoding="utf-8")

        app_module.GITHUB_TOKEN = "fake-token"
        app_module.BASE_DIR = work_dir
        app_module.github_actions.trigger_and_wait = lambda *a, **kw: {
            "outcome": "success", "run_id": 1, "run_html_url": "x",
        }
        # GIT_ASKPASS机制在真实推送到本地文件系统路径的远程时不会被
        # 真正用到（本地remote不需要HTTP认证），FOXZEN_GIT_PUSH_TOKEN/
        # GIT_ASKPASS这些环境变量的设置对本地push是无害的no-op。

        commits_before = _git_run("git", "rev-list", "--count", "master", cwd=remote_dir).stdout.strip()

        github_result = app_module._publish_and_report("github", target_cooldown=False)
        check("github发布成功", github_result["body"]["status"] == "success", github_result)
        cf_result = app_module._publish_and_report("cf", target_cooldown=False)
        check("紧接着cf发布也报告成功（noop）", cf_result["body"]["status"] == "success", cf_result)
        check("cf这一步changed_file_count=0（确认真的走的是noop，不是又提交了一次）",
              cf_result["body"].get("changed_file_count") == 0, cf_result)

        commits_after = _git_run("git", "rev-list", "--count", "master", cwd=remote_dir).stdout.strip()
        check("远程仓库只增加了一个commit（github那次），cf没有产生第二个",
              int(commits_after) == int(commits_before) + 1,
              (commits_before, commits_after))
        check("github和cf报告的是同一个commit_sha", github_result["body"]["commit"] == cf_result["body"]["commit"],
              (github_result["body"]["commit"], cf_result["body"]["commit"]))
    with_temp_app_env(_run)


def main():
    tests = [
        test_init_db_preseeds_all_four_target_rows,
        test_init_db_preseed_does_not_overwrite_existing_row,
        test_manual_github_no_longer_calls_fetch_script,
        test_manual_cf_no_longer_calls_fetch_script,
        test_manual_github_still_has_cooldown_via_git_publish,
        test_manual_cf_still_has_cooldown_via_git_publish,
        test_different_manual_targets_do_not_share_cooldown,
        test_mirror_success_starts_fan_out,
        test_backup_success_does_not_start_fan_out,
        test_mirror_fetch_failure_does_not_start_fan_out,
        test_fan_out_end_to_end_updates_github_and_cf_status,
        test_fan_out_isolates_github_success_from_cf_failure,
        test_fan_out_cf_failure_does_not_block_immediate_manual_retry,
        test_fan_out_noop_skips_github_dispatch,
        test_fan_out_sequential_publish_produces_single_commit,
    ]
    for t in tests:
        print(f"--- {t.__name__} ---")
        try:
            t()
        except Exception as e:
            print(f"  [FAIL] {t.__name__} 抛出异常: {e!r}")
            failures.append(t.__name__)

    print()
    if failures:
        print(f"共{len(failures)}项失败: {failures}")
        sys.exit(1)
    print("全部通过")


if __name__ == "__main__":
    main()
