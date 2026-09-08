#!/usr/bin/env python3
"""production html/ -> 独立发布副本GIT_PUBLISH_REPO_DIR/html/ 这一步rsync
同步的回归测试（架构改造：GitHub/Cloudflare fan-out改用独立Git工作树
"/root/blog-mirror-git"，production目录"/root/blog-mirror"本身永远不是
Git仓库）。

范围说明（跟已有测试明确分开，不重复测试）：
- git_publish.py本身commit/push的所有既有行为——分支校验、--only pathspec、
  B1/B2/B3各种情形、GIT_ASKPASS——完全没有改动，test_git_publish.py已经
  覆盖，这里不重测。
- content_fetch/git_publish两把锁本身的冷却/互斥/stale恢复/fencing逻辑
  是既有代码，未改动，test_refresh_lock.py已经覆盖；这里只验证新增的
  rsync这一步确实被安排在GIT_PUBLISH_LOCK临界区内、不会绕过
  CONTENT_FETCH_LOCK cross-check，不重新测锁机制本身。
- test_publish_fanout.py覆盖fan-out编排逻辑本身（谁调用_run_git_publish、
  什么时候调用、结果记到哪个target），那边的测试默认把
  _sync_html_to_publish_repo()换成no-op桩，不依赖真实rsync二进制；这个
  文件反过来专门测_sync_html_to_publish_repo()和它在_run_git_publish()
  里的接入点本身。
- 这次任务实际改动的代码只有：
  1. app.py新增_sync_html_to_publish_repo() + GIT_PUBLISH_REPO_DIR /
     GIT_PUBLISH_RSYNC_TIMEOUT_SECONDS两个常量，GIT_PUBLISH_STALE_SECONDS
     预算相应调整。
  2. app.py::_run_git_publish()在commit_and_push()之前调用它，repo_dir
     从BASE_DIR换成GIT_PUBLISH_REPO_DIR。
  3. safe_errors.py新增repository_sync_error这个error_category的对外
     文案。
  4. test_publish_fanout.py::with_temp_app_env()默认桩化
     _sync_html_to_publish_repo()（见上一条），以及
     test_fan_out_sequential_publish_produces_single_commit()改用
     HTML_DIR/GIT_PUBLISH_REPO_DIR而不是直接用BASE_DIR当repo_dir。

本机(Windows开发环境)没有安装rsync二进制——涉及"真的调用一次rsync校验
文件确实被复制/删除"的测试会在rsync不存在时显式跳过（同
test_git_publish.py::test_git_publish_ensures_askpass_executable_before_push
对POSIX-only行为的跳过手法一致，不是静默假装通过）；rsync参数构造是否
正确、失败/超时是否被正确分类并fail closed、rsync是否被安排在锁临界区内、
token是否会意外泄漏，这几类测试不依赖真rsync二进制，在任何平台都会真正
执行并计入通过/失败。

用法: python3 test_publish_repo_sync.py
"""
import shutil
import subprocess
import sys
import tempfile
import threading
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


def _rsync_available():
    return shutil.which("rsync") is not None


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


def _init_publish_repo(repo_dir):
    """建一个跟真实GIT_PUBLISH_REPO_DIR拓扑一致的临时Git仓库：有.git、
    master分支、html/目录已经追踪过至少一个文件（避免"整个未追踪目录
    折叠成一行??"这个已知的git status显示特性干扰断言，参照
    test_git_publish.py::with_temp_repo()同样的预置手法）。返回对应的
    本地bare remote目录。
    """
    remote_dir = repo_dir.parent / f"{repo_dir.name}-remote.git"
    _git_run("git", "init", "--bare", "-b", "master", str(remote_dir), cwd=repo_dir.parent)
    _git_run("git", "init", "-b", "master", str(repo_dir), cwd=repo_dir.parent)
    (repo_dir / "README.md").write_text("init\n", encoding="utf-8")
    _git_run("git", "add", "README.md", cwd=repo_dir)
    _git_run("git", "commit", "-m", "init", cwd=repo_dir,
             env=_env_with_identity("Setup", "setup@example.invalid"))
    html_dir = repo_dir / "html"
    html_dir.mkdir()
    (html_dir / ".gitkeep").write_text("", encoding="utf-8")
    _git_run("git", "add", "html/.gitkeep", cwd=repo_dir)
    _git_run("git", "commit", "-m", "seed html/", cwd=repo_dir,
             env=_env_with_identity("Setup", "setup@example.invalid"))
    _git_run("git", "remote", "add", "origin", str(remote_dir), cwd=repo_dir)
    _git_run("git", "push", "-u", "origin", "master", cwd=repo_dir)
    return remote_dir


def with_temp_app_env(fn):
    """同test_publish_fanout.py::with_temp_app_env()同样的约定（各测试
    文件各自维护一份，不共享）：临时db + 临时"production html源"
    (HTML_DIR) + 临时GIT_PUBLISH_REPO_DIR，三者都在同一个临时目录树下，
    跟这个项目自己真实的data/blog.db、真实html/、真实的
    /root/blog-mirror-git完全隔离——fn拿到(tmp, db, app_module, prod_html)。
    """
    tmp = Path(tempfile.mkdtemp(prefix="publish_sync_test_"))
    real_db_mtime = REAL_DB.stat().st_mtime if REAL_DB.exists() else None

    import db
    orig_db_path = db.DB_PATH
    db.DB_PATH = tmp / "test.db"

    import app as app_module
    orig_html_dir = app_module.HTML_DIR
    orig_repo_dir = app_module.GIT_PUBLISH_REPO_DIR
    orig_github_token = app_module.GITHUB_TOKEN
    orig_commit_and_push = app_module.git_publish.commit_and_push
    orig_trigger_and_wait = app_module.github_actions.trigger_and_wait
    orig_subprocess_run = app_module.subprocess.run

    prod_html = tmp / "production_html"
    prod_html.mkdir()
    app_module.HTML_DIR = prod_html
    app_module.GIT_PUBLISH_REPO_DIR = tmp / "unset-publish-repo"  # 大多数测试会自己覆盖

    try:
        db.init_db()
        fn(tmp, db, app_module, prod_html)
    finally:
        db.DB_PATH = orig_db_path
        app_module.HTML_DIR = orig_html_dir
        app_module.GIT_PUBLISH_REPO_DIR = orig_repo_dir
        app_module.GITHUB_TOKEN = orig_github_token
        app_module.git_publish.commit_and_push = orig_commit_and_push
        app_module.github_actions.trigger_and_wait = orig_trigger_and_wait
        app_module.subprocess.run = orig_subprocess_run
        shutil.rmtree(tmp, ignore_errors=True)

    if real_db_mtime is not None:
        check("测试过程未修改真实data/blog.db（mtime不变）",
              REAL_DB.stat().st_mtime == real_db_mtime)


# ============================================================
# A. rsync成功：内容正确复制，git repo检测到预期变化，commit/push走
#    GIT_PUBLISH_REPO_DIR
# ============================================================

def test_rsync_copies_new_and_changed_files_into_publish_repo():
    if not _rsync_available():
        print("  [SKIP] 本机没有rsync二进制，跳过真实rsync文件复制校验（VPS上已通过手动"
              "rsync实测确认可用，见此前/root/blog-mirror-git初始化审计）")
        return

    def _run(tmp, db, app_module, prod_html):
        repo_dir = tmp / "publish_repo"
        _init_publish_repo(repo_dir)
        app_module.GIT_PUBLISH_REPO_DIR = repo_dir

        (prod_html / "index.html").write_text("<p>homepage v2</p>", encoding="utf-8")
        (prod_html / "posts").mkdir()
        (prod_html / "posts" / "new-post.html").write_text("<p>new post</p>", encoding="utf-8")

        sync_error = app_module._sync_html_to_publish_repo()
        check("rsync成功时返回None（不是错误）", sync_error is None, sync_error)

        copied_index = repo_dir / "html" / "index.html"
        copied_post = repo_dir / "html" / "posts" / "new-post.html"
        check("production的index.html被复制到发布副本",
              copied_index.exists() and copied_index.read_text(encoding="utf-8") == "<p>homepage v2</p>")
        check("production新增的posts/new-post.html也被复制",
              copied_post.exists() and copied_post.read_text(encoding="utf-8") == "<p>new post</p>")

        changed = app_module.git_publish.detect_changed_paths(repo_dir, "html")
        check("git能检测到rsync带来的变化", len(changed) >= 2, changed)
    with_temp_app_env(_run)


def test_full_publish_flow_commits_and_pushes_to_publish_repo():
    """端到端：_run_git_publish()真正跑一遍rsync+commit+push，产物落在
    GIT_PUBLISH_REPO_DIR对应的remote——直接验证repo_dir切换这个核心改动
    本身，而不是只看commit_and_push()被传了什么参数。
    """
    if not _rsync_available():
        print("  [SKIP] 本机没有rsync二进制，跳过端到端rsync+commit+push校验")
        return

    def _run(tmp, db, app_module, prod_html):
        repo_dir = tmp / "publish_repo"
        remote_dir = _init_publish_repo(repo_dir)
        app_module.GIT_PUBLISH_REPO_DIR = repo_dir
        app_module.GITHUB_TOKEN = "fake-token"

        (prod_html / "hello.html").write_text("<p>hi</p>", encoding="utf-8")

        result = app_module._run_git_publish("cf", target_cooldown=False)
        check("acquired=True", result.get("acquired") is True, result)
        check("pushed=True", result.get("pushed") is True, result)
        check("changed_file_count=1", result.get("changed_file_count") == 1, result)

        remote_head = _git_run("git", "rev-parse", "master", cwd=remote_dir).stdout.strip()
        check("发布副本对应的远程仓库真的收到了这次push",
              remote_head == result.get("commit_sha"), (remote_head, result.get("commit_sha")))
    with_temp_app_env(_run)


# ============================================================
# B. rsync删除：git repo html里预置一个production不存在的文件，rsync后
#    被--delete删除，不影响.git/
# ============================================================

def test_rsync_delete_removes_stale_file_not_in_production():
    if not _rsync_available():
        print("  [SKIP] 本机没有rsync二进制，跳过真实--delete语义校验")
        return

    def _run(tmp, db, app_module, prod_html):
        repo_dir = tmp / "publish_repo"
        _init_publish_repo(repo_dir)
        app_module.GIT_PUBLISH_REPO_DIR = repo_dir

        stale_dir = repo_dir / "html" / "posts" / "deleted-elsewhere"
        stale_dir.mkdir(parents=True)
        (stale_dir / "index.html").write_text("<p>should be deleted</p>", encoding="utf-8")
        check("准备场景：发布副本里存在一个production没有的文件", (stale_dir / "index.html").exists())

        (prod_html / "current.html").write_text("<p>still here</p>", encoding="utf-8")

        sync_error = app_module._sync_html_to_publish_repo()
        check("rsync成功", sync_error is None, sync_error)

        check("production不存在的文件被--delete清除", not (stale_dir / "index.html").exists())
        check("production存在的文件被正确复制", (repo_dir / "html" / "current.html").exists())
        check("发布副本的.git/目录本身完好，没有被rsync --delete波及",
              (repo_dir / ".git").is_dir() and (repo_dir / ".git" / "HEAD").exists())
        check("发布副本的.git/HEAD内容依然指向refs/heads/master（仓库结构未受影响）",
              "refs/heads/master" in (repo_dir / ".git" / "HEAD").read_text(encoding="utf-8"))
    with_temp_app_env(_run)


# ============================================================
# C. rsync失败：不commit、不push、不dispatch
# ============================================================

def test_rsync_nonzero_exit_blocks_commit_and_push():
    def _run(tmp, db, app_module, prod_html):
        repo_dir = tmp / "publish_repo"
        remote_dir = _init_publish_repo(repo_dir)
        app_module.GIT_PUBLISH_REPO_DIR = repo_dir
        app_module.GITHUB_TOKEN = "fake-token"

        commit_and_push_calls = []

        def spy_commit_and_push(*a, **kw):
            commit_and_push_calls.append((a, kw))
            return {"pushed": True, "commit_sha": "should-never-happen",
                    "changed_file_count": 1, "push_state": "pushed"}
        app_module.git_publish.commit_and_push = spy_commit_and_push

        orig_run = app_module.subprocess.run

        def fake_run(cmd, *a, **kw):
            if cmd and cmd[0] == "rsync":
                return subprocess.CompletedProcess(cmd, 23, stdout="", stderr="rsync: 模拟的传输错误")
            return orig_run(cmd, *a, **kw)
        app_module.subprocess.run = fake_run

        head_before = _git_run("git", "rev-parse", "master", cwd=remote_dir).stdout.strip()
        result = app_module._run_git_publish("github", target_cooldown=False)

        check("rsync失败时acquired=True但pushed=False",
              result.get("acquired") is True and result.get("pushed") is False, result)
        check("error_category=repository_sync_error",
              result.get("error_category") == "repository_sync_error", result)
        check("rsync失败时绝不调用commit_and_push", commit_and_push_calls == [], commit_and_push_calls)

        head_after = _git_run("git", "rev-parse", "master", cwd=remote_dir).stdout.strip()
        check("远程仓库完全没有变化（没有产生任何commit/push）", head_after == head_before)
    with_temp_app_env(_run)


def test_rsync_failure_via_publish_and_report_never_dispatches_workflow():
    """从_publish_and_report()这一层验证：rsync失败必须在到达github专属的
    workflow_dispatch代码之前就已经返回failure，且对外detail走
    safe_errors固定模板，不是原始rsync stderr。"""
    def _run(tmp, db, app_module, prod_html):
        repo_dir = tmp / "publish_repo"
        _init_publish_repo(repo_dir)
        app_module.GIT_PUBLISH_REPO_DIR = repo_dir
        app_module.GITHUB_TOKEN = "fake-token"

        orig_run = app_module.subprocess.run

        def fake_run(cmd, *a, **kw):
            if cmd and cmd[0] == "rsync":
                return subprocess.CompletedProcess(cmd, 23, stdout="", stderr="模拟失败")
            return orig_run(cmd, *a, **kw)
        app_module.subprocess.run = fake_run

        dispatch_calls = []

        def spy_dispatch(*a, **kw):
            dispatch_calls.append(1)
            return {"outcome": "success", "run_id": 1, "run_html_url": "x"}
        app_module.github_actions.trigger_and_wait = spy_dispatch

        result = app_module._publish_and_report("github", target_cooldown=False)
        check("rsync失败时http_status=200且status=failure",
              result["http_status"] == 200 and result["body"]["status"] == "failure", result)
        check("error_category=repository_sync_error",
              result["body"]["error_category"] == "repository_sync_error", result)
        check("对外detail走safe_errors固定模板，不是原始rsync stderr",
              result["body"]["detail"] == "服务器内容同步失败，已拒绝发布", result["body"]["detail"])
        check("绝不触发workflow_dispatch", dispatch_calls == [], dispatch_calls)
        check("retry_recommended=True（rsync失败视为可重试的瞬时故障）",
              result["body"].get("retry_recommended") is True, result)
    with_temp_app_env(_run)


def test_missing_publish_repo_blocks_commit_and_push():
    """发布副本目录根本不存在/不是Git仓库时（比如还没做过初始化clone），
    同样必须fail closed，且给出比裸rsync报错更明确的detail。"""
    def _run(tmp, db, app_module, prod_html):
        app_module.GIT_PUBLISH_REPO_DIR = tmp / "does-not-exist"
        app_module.GITHUB_TOKEN = "fake-token"
        commit_and_push_calls = []

        def spy_commit_and_push(*a, **kw):
            commit_and_push_calls.append(1)
            return {"pushed": True, "commit_sha": "x", "changed_file_count": 1, "push_state": "pushed"}
        app_module.git_publish.commit_and_push = spy_commit_and_push

        result = app_module._run_git_publish("cf", target_cooldown=False)
        check("发布副本不存在时pushed=False", result.get("pushed") is False, result)
        check("error_category=repository_sync_error",
              result.get("error_category") == "repository_sync_error", result)
        check("detail里明确提到仓库不存在/不是Git仓库",
              "不存在" in result.get("detail", "") or "不是Git仓库" in result.get("detail", ""), result)
        check("完全没有调用commit_and_push", commit_and_push_calls == [], commit_and_push_calls)
    with_temp_app_env(_run)


def test_rsync_timeout_blocks_commit_and_push():
    def _run(tmp, db, app_module, prod_html):
        repo_dir = tmp / "publish_repo"
        _init_publish_repo(repo_dir)
        app_module.GIT_PUBLISH_REPO_DIR = repo_dir
        app_module.GITHUB_TOKEN = "fake-token"
        commit_and_push_calls = []

        def spy_commit_and_push(*a, **kw):
            commit_and_push_calls.append(1)
            return {"pushed": True, "commit_sha": "x", "changed_file_count": 1, "push_state": "pushed"}
        app_module.git_publish.commit_and_push = spy_commit_and_push

        orig_run = app_module.subprocess.run

        def fake_run(cmd, *a, **kw):
            if cmd and cmd[0] == "rsync":
                raise subprocess.TimeoutExpired(cmd, kw.get("timeout"))
            return orig_run(cmd, *a, **kw)
        app_module.subprocess.run = fake_run

        result = app_module._run_git_publish("github", target_cooldown=False)
        check("rsync超时时pushed=False，不抛异常", result.get("pushed") is False, result)
        check("error_category=repository_sync_error",
              result.get("error_category") == "repository_sync_error", result)
        check("detail提到超时", "超时" in result.get("detail", ""), result)
        check("完全没有调用commit_and_push", commit_and_push_calls == [], commit_and_push_calls)
    with_temp_app_env(_run)


def test_rsync_binary_missing_blocks_commit_and_push_not_crashes():
    """真的用一个不存在的可执行文件模拟"rsync二进制缺失"（FileNotFoundError），
    确认走的是_sync_html_to_publish_repo()自己的兜底分支，而不是让异常
    直接冒泡把_run_git_publish()的锁释放逻辑绕过去。"""
    def _run(tmp, db, app_module, prod_html):
        repo_dir = tmp / "publish_repo"
        _init_publish_repo(repo_dir)
        app_module.GIT_PUBLISH_REPO_DIR = repo_dir
        app_module.GITHUB_TOKEN = "fake-token"

        orig_run = app_module.subprocess.run

        def fake_run(cmd, *a, **kw):
            if cmd and cmd[0] == "rsync":
                raise FileNotFoundError("rsync二进制未安装（模拟）")
            return orig_run(cmd, *a, **kw)
        app_module.subprocess.run = fake_run

        result = app_module._run_git_publish("cf", target_cooldown=False)
        check("rsync二进制缺失时不抛异常、pushed=False", result.get("pushed") is False, result)
        check("error_category=repository_sync_error",
              result.get("error_category") == "repository_sync_error", result)

        # 锁必须已经被正常释放（status变回idle）——不能因为rsync这一步抛了
        # 未预料到的异常就让GIT_PUBLISH_LOCK卡在running状态（db.py:142：
        # refresh_locks.status只有idle/running两个取值）。
        conn = db.get_conn()
        lock_row = conn.execute("SELECT status, last_status FROM refresh_locks WHERE lock_key = ?",
                                 (app_module.GIT_PUBLISH_LOCK,)).fetchone()
        conn.close()
        check("GIT_PUBLISH_LOCK已经正常释放回idle（不是被未预期异常卡在running）",
              lock_row is not None and lock_row["status"] == "idle", dict(lock_row) if lock_row else None)
        check("这次失败的诊断结果(last_status)被正确记为error",
              lock_row is not None and lock_row["last_status"] == "error", dict(lock_row) if lock_row else None)
    with_temp_app_env(_run)


# ============================================================
# D. 锁：rsync位于GIT_PUBLISH_LOCK临界区内，不会绕过CONTENT_FETCH_LOCK
#    cross-check
# ============================================================

def test_rsync_runs_while_git_publish_lock_held_blocks_content_fetch():
    """把rsync这一步人为拖慢，在它还没返回时从另一个线程尝试获取
    CONTENT_FETCH_LOCK——如果rsync真的被安排在GIT_PUBLISH_LOCK临界区内，
    这次并发的content_fetch获取必须被cross_check_idle拒绝
    （reason=busy_git_publish）；如果rsync被错误地挪到了锁外面，这次
    并发获取就会意外成功，测试会失败——跟test_refresh_lock.py::
    test_content_fetch_blocks_git_publish()反过来验证同一个互斥关系，
    这里专门确认"新插入的rsync代码是不是真的在锁里面"。
    """
    def _run(tmp, db, app_module, prod_html):
        repo_dir = tmp / "publish_repo"
        _init_publish_repo(repo_dir)
        app_module.GIT_PUBLISH_REPO_DIR = repo_dir
        app_module.GITHUB_TOKEN = "fake-token"
        app_module.git_publish.commit_and_push = lambda *a, **kw: {
            "pushed": True, "commit_sha": "sha", "changed_file_count": 0, "push_state": "noop",
        }

        orig_run = app_module.subprocess.run
        rsync_started = threading.Event()

        def slow_rsync(cmd, *a, **kw):
            if cmd and cmd[0] == "rsync":
                rsync_started.set()
                time.sleep(0.4)
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
            return orig_run(cmd, *a, **kw)
        app_module.subprocess.run = slow_rsync

        results = {}

        def _call_git_publish():
            results["publish"] = app_module._run_git_publish("github", target_cooldown=False)

        t = threading.Thread(target=_call_git_publish)
        t.start()
        check("rsync确实已经开始执行", rsync_started.wait(timeout=2.0))

        # rsync还在sleep(0.4)里，此刻GIT_PUBLISH_LOCK理应仍被持有
        concurrent_fetch = db.try_acquire_lock(
            app_module.CONTENT_FETCH_LOCK, app_module.CONTENT_FETCH_STALE_SECONDS,
            cross_check_idle=((app_module.GIT_PUBLISH_LOCK, app_module.GIT_PUBLISH_STALE_SECONDS),),
        )
        check("rsync仍在执行时，并发的content_fetch获取被拒绝(busy_git_publish)"
              "——证明rsync确实运行在GIT_PUBLISH_LOCK临界区内",
              concurrent_fetch.get("acquired") is False and concurrent_fetch.get("reason") == "busy_git_publish",
              concurrent_fetch)

        t.join(timeout=3.0)
        check("rsync结束、锁释放之后，_run_git_publish()本身正常返回成功",
              results.get("publish", {}).get("pushed") is True, results.get("publish"))

        after_release = db.try_acquire_lock(
            app_module.CONTENT_FETCH_LOCK, app_module.CONTENT_FETCH_STALE_SECONDS,
            cross_check_idle=((app_module.GIT_PUBLISH_LOCK, app_module.GIT_PUBLISH_STALE_SECONDS),),
        )
        check("git_publish结束释放锁之后，content_fetch能正常获取",
              after_release.get("acquired") is True, after_release)
        if after_release.get("acquired"):
            db.release_lock(app_module.CONTENT_FETCH_LOCK, after_release["generation"], "ok", "")
    with_temp_app_env(_run)


def test_sync_does_not_bypass_content_fetch_cross_check():
    """反过来：content_fetch正在运行时，_run_git_publish()（因此rsync这
    一步）必须在真正开始rsync之前就被拒绝，不能先rsync了再被锁拒绝。
    """
    def _run(tmp, db, app_module, prod_html):
        repo_dir = tmp / "publish_repo"
        _init_publish_repo(repo_dir)
        app_module.GIT_PUBLISH_REPO_DIR = repo_dir
        app_module.GITHUB_TOKEN = "fake-token"

        rsync_calls = []
        orig_run = app_module.subprocess.run

        def spy_run(cmd, *a, **kw):
            if cmd and cmd[0] == "rsync":
                rsync_calls.append(cmd)
            return orig_run(cmd, *a, **kw)
        app_module.subprocess.run = spy_run

        held = db.try_acquire_lock(
            app_module.CONTENT_FETCH_LOCK, app_module.CONTENT_FETCH_STALE_SECONDS,
            cross_check_idle=((app_module.GIT_PUBLISH_LOCK, app_module.GIT_PUBLISH_STALE_SECONDS),),
        )
        check("准备场景：content_fetch锁已经被占用", held.get("acquired") is True, held)

        result = app_module._run_git_publish("cf", target_cooldown=False)
        check("content_fetch占用期间，git_publish获取被拒绝",
              result.get("acquired") is False and result.get("reason") == "busy_content_fetch", result)
        check("被锁拒绝时rsync根本没有被调用过", rsync_calls == [], rsync_calls)

        db.release_lock(app_module.CONTENT_FETCH_LOCK, held["generation"], "ok", "")
    with_temp_app_env(_run)


# ============================================================
# E. 安全：production不是git仓库，token不进remote URL/日志/commit message，
#    只做presence-only检查
# ============================================================

def test_sync_never_touches_base_dir_as_a_git_repo():
    """整个rsync同步过程只读HTML_DIR（production html源）的内容，绝不在
    production目录下执行任何git命令、也绝不在里面创建.git——这是这次
    架构改造最核心的安全要求（production目录永远不是Git仓库）。
    """
    if not _rsync_available():
        print("  [SKIP] 本机没有rsync二进制，跳过（不影响下面git_calls_cwd这个核心断言"
              "本身不依赖rsync是否真的执行成功，但为了让_run_git_publish()走完整个"
              "流程到达commit_and_push()，这里选择在没有rsync时也跳过，保持跟其它"
              "端到端测试一致）")
        return

    def _run(tmp, db, app_module, prod_html):
        repo_dir = tmp / "publish_repo"
        _init_publish_repo(repo_dir)
        app_module.GIT_PUBLISH_REPO_DIR = repo_dir
        app_module.GITHUB_TOKEN = "fake-token"

        (prod_html / "post.html").write_text("<p>x</p>", encoding="utf-8")

        git_calls_cwd = []
        orig_run = app_module.subprocess.run

        def spy_run(cmd, *a, **kw):
            if cmd and cmd[0] == "git":
                git_calls_cwd.append(kw.get("cwd"))
            return orig_run(cmd, *a, **kw)
        app_module.subprocess.run = spy_run

        result = app_module._run_git_publish("github", target_cooldown=False)
        check("发布成功", result.get("pushed") is True, result)
        check("没有任何一次git命令的cwd是production目录(HTML_DIR)",
              str(prod_html) not in [str(c) for c in git_calls_cwd if c], git_calls_cwd)
        check("production html目录下确实没有出现.git", not (prod_html / ".git").exists())
    with_temp_app_env(_run)


def test_sync_error_detail_never_contains_github_token():
    """rsync失败的detail文本必须经过redact_known_secrets()处理——这里让
    rsync stderr故意"意外"包含真实token值，验证最终detail里不会出现它
    （跟test_git_publish.py::test_push_stderr_never_leaks_the_real_push_token_
    even_if_git_echoed_it()同样的防御性验证手法，这里换成rsync这条新路径）。
    """
    def _run(tmp, db, app_module, prod_html):
        repo_dir = tmp / "publish_repo"
        _init_publish_repo(repo_dir)
        app_module.GIT_PUBLISH_REPO_DIR = repo_dir
        real_token = "ghp_THIS_IS_THE_REAL_TOKEN_VALUE_FOR_THIS_TEST"
        app_module.GITHUB_TOKEN = real_token

        orig_run = app_module.subprocess.run

        def leaky_rsync(cmd, *a, **kw):
            if cmd and cmd[0] == "rsync":
                return subprocess.CompletedProcess(
                    cmd, 1, stdout="", stderr=f"rsync: auth failed, token was {real_token}")
            return orig_run(cmd, *a, **kw)
        app_module.subprocess.run = leaky_rsync

        result = app_module._run_git_publish("cf", target_cooldown=False)
        check("rsync失败", result.get("pushed") is False, result)
        check("即使rsync stderr意外包含真实token，返回的detail里也不包含它",
              real_token not in result.get("detail", ""), result.get("detail"))
        check("detail仍然保留了其它诊断信息，不是整段被吞掉",
              "auth failed" in result.get("detail", ""), result.get("detail"))
    with_temp_app_env(_run)


def test_rsync_command_never_uses_shell():
    """command injection防线的静态验证，跟test_git_publish.py::
    test_all_subprocess_calls_are_argument_lists_never_shell()同样的手法：
    rsync这一步的subprocess.run调用必须是参数列表，不能传shell=True。"""
    def _run(tmp, db, app_module, prod_html):
        repo_dir = tmp / "publish_repo"
        _init_publish_repo(repo_dir)
        app_module.GIT_PUBLISH_REPO_DIR = repo_dir

        calls = []
        orig_run = app_module.subprocess.run

        def recording_run(cmd, *a, **kw):
            if cmd and cmd[0] == "rsync":
                calls.append((cmd, kw.get("shell", False)))
            return orig_run(cmd, *a, **kw)
        app_module.subprocess.run = recording_run

        app_module._sync_html_to_publish_repo()
        check("rsync调用是参数列表且shell!=True",
              len(calls) == 1 and isinstance(calls[0][0], list) and not calls[0][1], calls)
    with_temp_app_env(_run)


def main():
    tests = [
        test_rsync_copies_new_and_changed_files_into_publish_repo,
        test_full_publish_flow_commits_and_pushes_to_publish_repo,
        test_rsync_delete_removes_stale_file_not_in_production,
        test_rsync_nonzero_exit_blocks_commit_and_push,
        test_rsync_failure_via_publish_and_report_never_dispatches_workflow,
        test_missing_publish_repo_blocks_commit_and_push,
        test_rsync_timeout_blocks_commit_and_push,
        test_rsync_binary_missing_blocks_commit_and_push_not_crashes,
        test_rsync_runs_while_git_publish_lock_held_blocks_content_fetch,
        test_sync_does_not_bypass_content_fetch_cross_check,
        test_sync_never_touches_base_dir_as_a_git_repo,
        test_sync_error_detail_never_contains_github_token,
        test_rsync_command_never_uses_shell,
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
