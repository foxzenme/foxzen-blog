#!/usr/bin/env python3
"""公开匿名刷新系统（mirror/backup/github/cf四个target）回归测试。

覆盖四层：
1. db.py原子函数本身的业务逻辑（单进程，临时sqlite库，绝不碰真实
   data/blog.db）——冷却/共享锁/stale恢复/content_fetch与git_publish
   双向互斥/fencing token。
2. /api/refresh/<target> 端到端（Flask test_client，单进程）——mirror/
   backup用可控的stub脚本代替真正的fetch_blog.py；github/cf额外把
   git_publish.commit_and_push()/github_actions.trigger_and_wait()换成
   可控的假函数代替，不会真的commit/push/打GitHub API。
3. **真正的多进程并发**（test_process_level_race_only_one_winner和
   test_two_os_processes_gunicorn_like_concurrency）：test_client是同
   进程内直接函数调用，测不出跨OS进程的竞态，用subprocess.Popen启动
   独立Python解释器模拟生产环境gunicorn -w 2的两个worker各自独立进程。
4. git_publish.py / github_actions.py各自的独立测试见test_git_publish.py /
   test_github_actions.py（不在这个文件里）。

用法: python3 test_refresh_lock.py
"""
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.request
from datetime import datetime, timedelta
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
    """跟test_app_zip_arcname.py的with_temp_db()同一个约定：把db.DB_PATH
    指向临时sqlite文件，跑完自动还原/清理，绝不读写真实的data/blog.db。
    """
    import db
    tmp = Path(tempfile.mkdtemp(prefix="refresh_lock_test_"))
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
    """跟test_internal_links.py的with_temp_env()同一个约定：临时db +
    临时FETCH_SCRIPT（指向一个可控的stub脚本，代替真正的fetch_blog.py），
    跑完自动还原。

    **关键顺序**（K节修复的一部分）：db.DB_PATH必须先于`import app`被设置。
    Python模块只在进程内第一次import时才真正执行顶层代码，如果`import app`
    在db.DB_PATH指向临时文件之前发生，app.py内部任何"首次访问数据库"的
    调用（哪怕本身不是这次测试主动触发的）都可能落到当时db.DB_PATH指向的
    真实路径上。app.py现在已经不再在模块顶层无条件调用db.init_db()（改成
    db.py内部的懒初始化，见db._ensure_schema()），所以这一步严格来说已经
    有db.py那一层兜底；这里仍然按正确顺序写，是双重防线，不依赖别处的
    兜底才算安全，也是本来就该有的正确写法。
    """
    tmp = Path(tempfile.mkdtemp(prefix="refresh_api_test_"))
    real_db_mtime = REAL_DB.stat().st_mtime if REAL_DB.exists() else None

    import db
    orig_db_path = db.DB_PATH
    db.DB_PATH = tmp / "test.db"

    import app as app_module
    orig_fetch_script = app_module.FETCH_SCRIPT
    orig_timeout = app_module.FETCH_SUBPROCESS_TIMEOUT_SECONDS
    orig_stale = app_module.CONTENT_FETCH_STALE_SECONDS
    orig_github_token = app_module.GITHUB_TOKEN
    orig_commit_and_push = app_module.git_publish.commit_and_push
    orig_trigger_and_wait = app_module.github_actions.trigger_and_wait
    orig_poll_until_conclusion = app_module.github_actions.poll_until_conclusion
    orig_background_wait = app_module.GITHUB_ACTIONS_BACKGROUND_WAIT_SECONDS

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
        app_module.FETCH_SUBPROCESS_TIMEOUT_SECONDS = orig_timeout
        app_module.CONTENT_FETCH_STALE_SECONDS = orig_stale
        app_module.GITHUB_TOKEN = orig_github_token
        app_module.git_publish.commit_and_push = orig_commit_and_push
        app_module.github_actions.trigger_and_wait = orig_trigger_and_wait
        app_module.github_actions.poll_until_conclusion = orig_poll_until_conclusion
        app_module.GITHUB_ACTIONS_BACKGROUND_WAIT_SECONDS = orig_background_wait
        shutil.rmtree(tmp, ignore_errors=True)

    if real_db_mtime is not None:
        check("测试过程未修改真实data/blog.db（mtime不变）",
              REAL_DB.stat().st_mtime == real_db_mtime)


def _reset_lock(db_mod, lock_key):
    """多轮测试之间把某把锁强制复位成idle：先读出当前generation再走正常的
    release_lock()（真正的fencing校验路径），不是绕开fencing的裸SQL写入。
    """
    conn = db_mod.get_conn()
    row = conn.execute("SELECT generation FROM refresh_locks WHERE lock_key=?", (lock_key,)).fetchone()
    conn.close()
    if row:
        db_mod.release_lock(lock_key, row["generation"], "ok", "reset-for-next-trial")


# ---------------------------------------------------------------------------
# 1. db.py 原子函数业务逻辑
# ---------------------------------------------------------------------------

def test_first_acquire_succeeds():
    def _run(tmp, db):
        r = db.try_acquire_lock("content_fetch", 420, target_key="mirror",
                                 cooldown_seconds=300, triggered_by="mirror")
        check("首次获取成功", r["acquired"] is True)
        check("返回了generation(fencing token)", isinstance(r.get("generation"), int) and r["generation"] > 0, r)
    with_temp_db(_run)


def test_same_target_cooldown_rejects_immediate_retry():
    def _run(tmp, db):
        r1 = db.try_acquire_lock("content_fetch", 420, target_key="mirror",
                                  cooldown_seconds=300, triggered_by="mirror")
        db.release_lock("content_fetch", r1["generation"], "ok", "test")
        r = db.try_acquire_lock("content_fetch", 420, target_key="mirror",
                                 cooldown_seconds=300, triggered_by="mirror")
        check("5分钟内同一target第二次请求被拒绝", r["acquired"] is False)
        check("拒绝原因是cooldown", r["reason"] == "cooldown")
        check("返回了合理的剩余秒数",
              isinstance(r["cooldown_remaining_seconds"], int)
              and 0 < r["cooldown_remaining_seconds"] <= 300,
              f"got {r['cooldown_remaining_seconds']!r}")
    with_temp_db(_run)


def test_different_target_not_blocked_by_other_targets_cooldown():
    def _run(tmp, db):
        r1 = db.try_acquire_lock("content_fetch", 420, target_key="mirror",
                                  cooldown_seconds=300, triggered_by="mirror")
        db.release_lock("content_fetch", r1["generation"], "ok", "test")
        r = db.try_acquire_lock("content_fetch", 420, target_key="backup",
                                 cooldown_seconds=300, triggered_by="backup")
        check("mirror冷却中不影响backup自己独立的冷却窗口", r["acquired"] is True)
    with_temp_db(_run)


def test_shared_lock_blocks_second_target_while_running():
    """content_fetch现在由mirror/backup/github/cf四个target共享，这里用
    mirror+github两个不同target验证同一把共享锁的互斥（原mirror/backup
    两个用例已经等价覆盖，这里换一对更能体现"4个target共享"这件事）。
    """
    def _run(tmp, db):
        r1 = db.try_acquire_lock("content_fetch", 420, target_key="mirror",
                                  cooldown_seconds=300, triggered_by="mirror")
        check("mirror拿到共享锁", r1["acquired"] is True)
        r2 = db.try_acquire_lock("content_fetch", 420, target_key="github",
                                  cooldown_seconds=300, triggered_by="github")
        check("github被拒绝（共享content_fetch锁被mirror占用，即使github自己没有冷却）",
              r2["acquired"] is False)
        check("拒绝原因是busy_content_fetch，不是cooldown", r2["reason"] == "busy_content_fetch")
    with_temp_db(_run)


def test_release_then_different_target_can_acquire_shared_lock():
    def _run(tmp, db):
        r1 = db.try_acquire_lock("content_fetch", 420, target_key="mirror",
                                  cooldown_seconds=300, triggered_by="mirror")
        db.release_lock("content_fetch", r1["generation"], "ok", "done")
        r = db.try_acquire_lock("content_fetch", 420, target_key="backup",
                                 cooldown_seconds=300, triggered_by="backup")
        check("mirror释放锁后，backup可以拿到共享锁", r["acquired"] is True)
    with_temp_db(_run)


def test_failure_result_recorded_and_lock_released():
    def _run(tmp, db):
        r1 = db.try_acquire_lock("content_fetch", 420, target_key="mirror",
                                  cooldown_seconds=300, triggered_by="mirror")
        db.release_lock("content_fetch", r1["generation"], "error", "抓取失败：网络超时")
        conn = db.get_conn()
        row = dict(conn.execute("SELECT * FROM refresh_locks WHERE lock_key='content_fetch'").fetchone())
        conn.close()
        check("失败后状态记录为idle（锁被释放）", row["status"] == "idle")
        check("失败结果被记录", row["last_status"] == "error" and "网络超时" in row["last_detail"])
        r = db.try_acquire_lock("content_fetch", 420, target_key="backup",
                                 cooldown_seconds=300, triggered_by="backup")
        check("失败后锁不会永久卡死，别的target仍可获取共享锁", r["acquired"] is True)
    with_temp_db(_run)


def test_stale_running_lock_is_recovered_based_on_age_not_guessed():
    def _run(tmp, db):
        db.try_acquire_lock("content_fetch", 420, target_key="mirror",
                             cooldown_seconds=300, triggered_by="mirror")
        # 模拟"进程被kill，release_lock从未被调用"：手动把started_at
        # 改到很久以前，制造一个"年龄超过stale_after_seconds"的死锁证据。
        conn = sqlite3.connect(db.DB_PATH)
        old_ts = (datetime.now() - timedelta(seconds=1000)).isoformat(timespec="seconds")
        conn.execute("UPDATE refresh_locks SET started_at=? WHERE lock_key='content_fetch'", (old_ts,))
        conn.commit()
        conn.close()

        r_still_running = db.try_acquire_lock("content_fetch", 2000, target_key="backup",
                                               cooldown_seconds=300, triggered_by="backup")
        check("锁年龄未超过stale_after_seconds时，按真实running拒绝（不误判还在跑的任务已死）",
              r_still_running["acquired"] is False and r_still_running["reason"] == "busy_content_fetch")

        r_recovered = db.try_acquire_lock("content_fetch", 420, target_key="backup",
                                           cooldown_seconds=300, triggered_by="backup")
        check("锁年龄超过stale_after_seconds后，基于年龄证据自动恢复并成功获取",
              r_recovered["acquired"] is True)
    with_temp_db(_run)


def test_content_fetch_blocks_git_publish():
    """双向互斥的第一个方向：content_fetch running时git_publish不能开始。"""
    def _run(tmp, db):
        acquire_cf = db.try_acquire_lock("content_fetch", 420, target_key="mirror",
                                          cooldown_seconds=300,
                                          cross_check_idle=(("git_publish", 120),),
                                          triggered_by="mirror")
        check("content_fetch获取成功", acquire_cf["acquired"])

        acquire_gp = db.try_acquire_lock("git_publish", 120,
                                          cross_check_idle=(("content_fetch", 420),),
                                          triggered_by="github")
        check("content_fetch running时git_publish被拒绝", acquire_gp["acquired"] is False)
        check("拒绝原因是busy_content_fetch（明确指出是哪个资源忙）",
              acquire_gp["reason"] == "busy_content_fetch", acquire_gp)
    with_temp_db(_run)


def test_git_publish_blocks_content_fetch():
    """双向互斥的第二个方向：git_publish running时content_fetch也不能开始
    ——不能为了减少409而只做单向检查。
    """
    def _run(tmp, db):
        acquire_gp = db.try_acquire_lock("git_publish", 120,
                                          cross_check_idle=(("content_fetch", 420),),
                                          triggered_by="github")
        check("git_publish获取成功", acquire_gp["acquired"])

        acquire_cf = db.try_acquire_lock("content_fetch", 420, target_key="mirror",
                                          cooldown_seconds=300,
                                          cross_check_idle=(("git_publish", 120),),
                                          triggered_by="mirror")
        check("git_publish running时content_fetch被拒绝（反向互斥同样生效）",
              acquire_cf["acquired"] is False)
        check("拒绝原因是busy_git_publish", acquire_cf["reason"] == "busy_git_publish", acquire_cf)
    with_temp_db(_run)


def test_fencing_token_prevents_stale_release_from_clobbering_new_holder():
    """D-2的直接回归测试：worker A卡死超过stale阈值，worker B基于年龄证据
    原子恢复并接管锁（generation前进）；worker A随后才姗姗来迟地尝试用它
    当初拿到的旧generation释放——这次release必须静默失效，绝不能把worker B
    正在running的状态覆盖掉，更不能把它错误地标记回idle（会导致第三个
    worker C误以为锁空闲，跟worker B同时跑两个content_fetch）。
    """
    def _run(tmp, db):
        acquire1 = db.try_acquire_lock("content_fetch", 420, target_key="mirror",
                                        cooldown_seconds=0, triggered_by="mirror")
        check("worker A首次获取成功", acquire1["acquired"])
        old_generation = acquire1["generation"]

        conn = sqlite3.connect(db.DB_PATH)
        old_ts = (datetime.now() - timedelta(seconds=1000)).isoformat(timespec="seconds")
        conn.execute("UPDATE refresh_locks SET started_at=? WHERE lock_key='content_fetch'", (old_ts,))
        conn.commit()
        conn.close()

        acquire2 = db.try_acquire_lock("content_fetch", 420, target_key="backup",
                                        cooldown_seconds=0, triggered_by="backup")
        check("worker B基于stale证据自动恢复并成功获取", acquire2["acquired"])
        new_generation = acquire2["generation"]
        check("worker B拿到的generation比worker A的更新（严格更大）",
              new_generation > old_generation, (old_generation, new_generation))

        released = db.release_lock("content_fetch", old_generation, "ok", "worker A迟到的release")
        check("worker A用旧generation释放时被静默拒绝(返回False)，不覆盖worker B的状态",
              released is False)

        conn = db.get_conn()
        row = dict(conn.execute(
            "SELECT status, generation, last_detail FROM refresh_locks WHERE lock_key='content_fetch'"
        ).fetchone())
        conn.close()
        check("worker A的迟到release没有把锁状态改回idle（worker B仍在running）",
              row["status"] == "running", row)
        check("worker A的迟到release没有污染last_detail",
              row["last_detail"] != "worker A迟到的release", row["last_detail"])
        check("锁的generation仍然是worker B的那个值，没有被回退",
              row["generation"] == new_generation, row)

        released_b = db.release_lock("content_fetch", new_generation, "ok", "worker B正常完成")
        check("worker B用自己正确的generation释放成功", released_b is True)
    with_temp_db(_run)


def test_record_target_result_fencing_rejects_stale_expected_generation():
    """record_target_result()的expected_generation fencing——跟
    release_lock()的generation fencing同一个思路，这里保护的是
    refresh_targets这一行：如果调用时提供的expected_generation已经不是
    这个target当前记录的generation（说明期间已经有更新一轮的刷新发生），
    写入必须被静默拒绝、返回False，不能用过时的结果覆盖新一轮的状态——
    这是github有界等待超时后台watcher机制能正确工作的关键前提。

    这里特意用cooldown_seconds=0连续acquire两轮（而不是等待真实300秒
    冷却过期），直接对应发现这个问题时的真实场景：try_acquire_lock()
    的原始实现用last_started_at（秒级精度时间戳）当fencing值，两轮
    acquire如果落在同一秒内会完全相同，无法分辨"是不是同一轮"——这不是
    假设性的边界情况，是这个测试本身第一次跑起来时就真的踩中过的问题，
    修复后改用refresh_targets.generation（严格递增的整数）当fencing值，
    不再依赖时间精度。
    """
    def _run(tmp, db):
        acquire1 = db.try_acquire_lock("content_fetch", 420, target_key="github",
                                        cooldown_seconds=0, triggered_by="github")
        first_generation = acquire1["target_generation"]
        db.release_lock("content_fetch", acquire1["generation"], "ok", "first run")

        acquire2 = db.try_acquire_lock("content_fetch", 420, target_key="github",
                                        cooldown_seconds=0, triggered_by="github")
        second_generation = acquire2["target_generation"]
        db.release_lock("content_fetch", acquire2["generation"], "ok", "second run")
        check("两轮的target_generation严格递增、确实不同",
              second_generation == first_generation + 1, (first_generation, second_generation))

        written_stale = db.record_target_result(
            "github", "success", "", commit_sha="old-run-sha", expected_generation=first_generation,
        )
        check("用过时的expected_generation写入被拒绝，返回False", written_stale is False)

        state = db.get_target_status("github", 300)
        check("过时写入没有污染当前状态（last_result应为None，新一轮还没写过结果）",
              state["last_result"] is None, state)

        written_fresh = db.record_target_result(
            "github", "success", "", commit_sha="new-run-sha", expected_generation=second_generation,
        )
        check("用匹配的expected_generation写入成功，返回True", written_fresh is True)
        state_after = db.get_target_status("github", 300)
        check("新一轮结果正确写入", state_after["last_result"]["commit"] == "new-run-sha", state_after)
    with_temp_db(_run)


def test_process_level_race_only_one_winner():
    """真正启动两个独立OS进程（不是线程），同时对同一个临时sqlite文件调用
    db.try_acquire_lock('content_fetch', ...)——直接验证SQLite层面的原子性
    本身，不经过Flask/HTTP这一层。跑5轮，每轮都必须恰好只有一个进程获胜。
    """
    def _run(tmp, db):
        db_path = db.DB_PATH
        worker_script = tmp / "race_worker.py"
        worker_code = (
            "import sys, os, time\n"
            "from pathlib import Path\n"
            f"sys.path.insert(0, {str(BASE_DIR)!r})\n"
            "import db\n"
            f"db.DB_PATH = Path({str(db_path)!r})\n"
            "target = sys.argv[1]\n"
            "ready_file = sys.argv[2]\n"
            "go_file = sys.argv[3]\n"
            "result_file = sys.argv[4]\n"
            "open(ready_file, 'w').close()\n"
            "while not os.path.exists(go_file):\n"
            "    time.sleep(0.0005)\n"
            # cooldown_seconds传0：这个测试只验证共享content_fetch锁本身的
            # 竞态互斥，5轮连续跑在同一秒内，如果传真实的300秒冷却，第2轮起
            # 两个target各自的冷却窗口都还没过，会导致两边都被cooldown拒绝
            # 而不是被busy拒绝，干扰了要测的东西——target冷却本身已经由
            # test_same_target_cooldown_rejects_immediate_retry等测试单独
            # 覆盖过了。
            "r = db.try_acquire_lock('content_fetch', 420, target_key=target, cooldown_seconds=0,\n"
            "                        cross_check_idle=(('git_publish', 120),), triggered_by=target)\n"
            "open(result_file, 'w').write(str(r['acquired']))\n"
        )
        worker_script.write_text(worker_code, encoding="utf-8")

        results = []
        N_TRIALS = 5
        for trial in range(N_TRIALS):
            _reset_lock(db, "content_fetch")
            ready1, ready2 = tmp / "ready1", tmp / "ready2"
            go_file = tmp / "go"
            result1, result2 = tmp / "result1", tmp / "result2"
            for f in (ready1, ready2, go_file, result1, result2):
                f.unlink(missing_ok=True)

            p1 = subprocess.Popen([sys.executable, str(worker_script), "mirror",
                                    str(ready1), str(go_file), str(result1)], cwd=str(BASE_DIR))
            p2 = subprocess.Popen([sys.executable, str(worker_script), "backup",
                                    str(ready2), str(go_file), str(result2)], cwd=str(BASE_DIR))

            deadline = time.time() + 10
            while (not ready1.exists() or not ready2.exists()) and time.time() < deadline:
                time.sleep(0.005)
            go_file.write_text("go")
            p1.wait(timeout=15)
            p2.wait(timeout=15)

            acquired1 = result1.read_text().strip() == "True"
            acquired2 = result2.read_text().strip() == "True"
            results.append((acquired1, acquired2))

        all_exactly_one = all((a1 != a2) for a1, a2 in results)
        check(f"{N_TRIALS}轮两个独立OS进程同时竞争同一把content_fetch锁，"
              f"每轮都恰好只有一个成功: {results}", all_exactly_one)
    with_temp_db(_run)


# ---------------------------------------------------------------------------
# 2. /api/refresh/<target> 端到端（Flask test_client，单进程）
# ---------------------------------------------------------------------------

def test_scenario_1_first_mirror_refresh_succeeds():
    def _run(tmp, db, app_module):
        client = app_module.app.test_client()
        resp = client.post("/api/refresh/mirror")
        data = resp.get_json()
        check("①首次Mirror刷新返回200", resp.status_code == 200)
        check("①status=success", data["status"] == "success", data)
        check("①commit字段为None（mirror不产生git提交）", data["commit"] is None)
    with_temp_app_env(_run)


def test_scenario_2_second_mirror_refresh_within_5min_rejected_with_remaining():
    def _run(tmp, db, app_module):
        client = app_module.app.test_client()
        client.post("/api/refresh/mirror")
        resp = client.post("/api/refresh/mirror")
        data = resp.get_json()
        check("②5分钟内第二次Mirror刷新返回429", resp.status_code == 429)
        check("②status=cooldown", data["status"] == "cooldown", data)
        check("②返回了剩余秒数", isinstance(data["cooldown_remaining_seconds"], int), data)
    with_temp_app_env(_run)


def test_scenario_3_mirror_cooldown_does_not_block_backup():
    def _run(tmp, db, app_module):
        client = app_module.app.test_client()
        client.post("/api/refresh/mirror")
        resp = client.post("/api/refresh/backup")
        data = resp.get_json()
        check("③Mirror冷却中，Backup仍可请求并成功",
              resp.status_code == 200 and data["status"] == "success")
    with_temp_app_env(_run)


def test_scenario_6_fetch_failure_recorded_and_not_stuck():
    def _run(tmp, db, app_module):
        os.environ["STUB_EXIT_CODE"] = "1"
        try:
            client = app_module.app.test_client()
            resp = client.post("/api/refresh/mirror")
            data = resp.get_json()
            check("⑥fetch失败时请求本身仍返回200+status=failure",
                  resp.status_code == 200 and data["status"] == "failure", data)
        finally:
            os.environ.pop("STUB_EXIT_CODE", None)

        conn = db.get_conn()
        row = dict(conn.execute("SELECT status FROM refresh_locks WHERE lock_key='content_fetch'").fetchone())
        conn.close()
        check("⑥失败后锁被释放为idle，不会永久卡死", row["status"] == "idle")
    with_temp_app_env(_run)


def test_scenario_7_fetch_timeout_recorded_and_not_stuck():
    def _run(tmp, db, app_module):
        app_module.FETCH_SUBPROCESS_TIMEOUT_SECONDS = 1
        os.environ["STUB_SLEEP_SECONDS"] = "3"
        try:
            client = app_module.app.test_client()
            resp = client.post("/api/refresh/mirror")
            data = resp.get_json()
            check("⑦超时时请求本身仍返回200+status=failure",
                  resp.status_code == 200 and data["status"] == "failure", data)
            check("⑦detail提到超时", "超时" in data["detail"], data)
        finally:
            os.environ.pop("STUB_SLEEP_SECONDS", None)

        conn = db.get_conn()
        row = dict(conn.execute("SELECT status FROM refresh_locks WHERE lock_key='content_fetch'").fetchone())
        conn.close()
        check("⑦超时后锁被释放为idle，后续刷新仍可工作", row["status"] == "idle")
    with_temp_app_env(_run)


def test_scenario_5_running_rejection_returns_409():
    """⑤第一个fetch正在执行时，第二个target请求不能启动第二个fetch——用
    一个会sleep的stub模拟"正在执行"，从另一个线程在它跑完前发第二个请求
    （同进程内两个线程共享同一个db.DB_PATH；真正跨OS进程的竞态由
    test_process_level_race_only_one_winner()和下面的多进程HTTP测试
    覆盖，这里只快速验证HTTP层409分支本身接线正确）。
    """
    def _run(tmp, db, app_module):
        os.environ["STUB_SLEEP_SECONDS"] = "1.5"
        try:
            client = app_module.app.test_client()
            results = {}

            def _call_mirror():
                results["mirror"] = client.post("/api/refresh/mirror")

            t = threading.Thread(target=_call_mirror)
            t.start()
            time.sleep(0.3)
            resp_backup = client.post("/api/refresh/backup")
            t.join(timeout=10)

            check("⑤mirror仍在执行时，backup被拒绝，HTTP 409", resp_backup.status_code == 409)
            data = resp_backup.get_json()
            check("⑤reason=busy_content_fetch", data["reason"] == "busy_content_fetch", data)
            check("⑤mirror自己最终成功", results["mirror"].status_code == 200)
        finally:
            os.environ.pop("STUB_SLEEP_SECONDS", None)
    with_temp_app_env(_run)


def test_illegal_target_rejected():
    def _run(tmp, db, app_module):
        client = app_module.app.test_client()
        resp = client.post("/api/refresh/not-a-real-target")
        check("⑨非法target被路由层的any()converter直接挡掉，返回404，不进入业务代码",
              resp.status_code == 404, resp.status_code)
        resp_status = client.get("/api/refresh/not-a-real-target/status")
        check("⑨status查询接口同样拒绝非法target", resp_status.status_code == 404)
    with_temp_app_env(_run)


def test_import_app_does_not_write_real_database():
    """K节修复的显式回归测试（此前只隐藏在with_temp_app_env()每次调用末尾
    的mtime检查里，这里单独拎出来做一次明确、独立、专门针对这一个问题的
    测试）。修复的关键在db.py的_ensure_schema()懒初始化：无论"设置
    db.DB_PATH"和"import app"谁先谁后，只要在真正触发数据库I/O之前
    db.DB_PATH已经指向临时文件，就不会污染真实data/blog.db。
    """
    def _run(tmp, db, app_module):
        status = db.get_target_status("mirror", 300)
        check("临时数据库确实被正确初始化（能正常查询refresh_targets/refresh_locks）",
              status["state"] == "idle" and status["last_result"] is None, status)
    with_temp_app_env(_run)
    # 真实data/blog.db未被修改的断言在with_temp_app_env()内部自动执行。


def test_credentials_not_leaked_in_response():
    def _run(tmp, db, app_module):
        secret_token = "ghp_SUPER_SECRET_VALUE_SHOULD_NEVER_LEAK"
        app_module.GITHUB_TOKEN = secret_token
        app_module.git_publish.commit_and_push = lambda *a, **kw: {
            "pushed": True, "commit_sha": "deadbeef", "changed_file_count": 1,
        }
        app_module.github_actions.trigger_and_wait = lambda *a, **kw: {
            "outcome": "success", "run_id": 1, "run_html_url": "https://github.com/x/y/actions/runs/1",
        }
        client = app_module.app.test_client()
        resp = client.post("/api/refresh/github")
        body_text = resp.get_data(as_text=True)
        check("github成功流程的响应体不包含真实token", secret_token not in body_text)

        app_module.GITHUB_TOKEN = ""
        resp2 = client.post("/api/refresh/cf")
        body_text2 = resp2.get_data(as_text=True)
        check("凭据缺失分支的响应体也不包含token", secret_token not in body_text2)
        check("凭据缺失分支明确报告credentials_missing，不是伪装成功",
              resp2.get_json()["status"] == "failure"
              and resp2.get_json()["error_category"] == "credentials_missing")
    with_temp_app_env(_run)


def test_github_full_success_flow_mocked():
    """github完整两阶段流程：content_fetch(真stub子进程) -> git_publish
    (mock) -> workflow_dispatch+轮询(mock)，全程不碰真实git/GitHub。
    """
    def _run(tmp, db, app_module):
        app_module.GITHUB_TOKEN = "fake-token-for-test"
        app_module.git_publish.commit_and_push = lambda *a, **kw: {
            "pushed": True, "commit_sha": "abc123def456", "changed_file_count": 3,
        }
        app_module.github_actions.trigger_and_wait = lambda *a, **kw: {
            "outcome": "success", "run_id": 555, "run_html_url": "https://github.com/x/y/actions/runs/555",
        }
        client = app_module.app.test_client()
        resp = client.post("/api/refresh/github")
        data = resp.get_json()
        check("github完整成功流程返回200", resp.status_code == 200)
        check("status=success", data["status"] == "success", data)
        check("commit字段是真实commit sha", data["commit"] == "abc123def456", data)
        check("run_id/run_html_url正确透传", data.get("run_id") == 555, data)
    with_temp_app_env(_run)


def test_cf_full_success_flow_mocked():
    def _run(tmp, db, app_module):
        app_module.GITHUB_TOKEN = "fake-token-for-test"
        app_module.git_publish.commit_and_push = lambda *a, **kw: {
            "pushed": True, "commit_sha": "cf789xyz", "changed_file_count": 2,
        }
        client = app_module.app.test_client()
        resp = client.post("/api/refresh/cf")
        data = resp.get_json()
        check("cf完整成功流程返回200", resp.status_code == 200)
        check("status=success", data["status"] == "success", data)
        check("detail明确只声称push成功，不声称Cloudflare部署完成",
              "Cloudflare Pages" in data["detail"] and "push successful" in data["detail"], data)
        check("cf响应里没有run_id/run_html_url（不涉及GitHub Actions）",
              "run_id" not in data, data)
    with_temp_app_env(_run)


def test_no_html_changes_skips_publish_and_dispatch():
    """O-2：content_fetch完成后html/没有任何实际变化时，不commit/不push/
    github不workflow_dispatch/cf不重新部署——这里验证workflow_dispatch
    确实完全没有被调用到（不只是"最终没生效"）。
    """
    def _run(tmp, db, app_module):
        app_module.GITHUB_TOKEN = "fake-token-for-test"
        app_module.git_publish.commit_and_push = lambda *a, **kw: {
            "pushed": True, "commit_sha": "unchanged-sha", "changed_file_count": 0,
        }
        dispatch_calls = []

        def recording_dispatch(*a, **kw):
            dispatch_calls.append((a, kw))
            return {"outcome": "success", "run_id": 1, "run_html_url": "x"}
        app_module.github_actions.trigger_and_wait = recording_dispatch

        client = app_module.app.test_client()
        resp = client.post("/api/refresh/github")
        data = resp.get_json()
        check("内容无变化时仍然是真实success（不是错误）", data["status"] == "success", data)
        check("detail明确说明无变化", "无变化" in data["detail"], data)
        check("changed_file_count=0", data["changed_file_count"] == 0, data)
        check("没有触发workflow_dispatch", len(dispatch_calls) == 0, dispatch_calls)
    with_temp_app_env(_run)


def test_github_cf_refresh_does_not_touch_mirror_backup_target_state():
    """github/cf刷新只最终发布github/cf自己，不能因为它们内部复用了
    content_fetch这个共享步骤，就顺便把mirror/backup的cooldown/结果记录
    也当作"被刷新过"——target语义必须保持清晰。
    """
    def _run(tmp, db, app_module):
        app_module.GITHUB_TOKEN = "fake-token-for-test"
        app_module.git_publish.commit_and_push = lambda *a, **kw: {
            "pushed": True, "commit_sha": "sha1", "changed_file_count": 2,
        }
        app_module.github_actions.trigger_and_wait = lambda *a, **kw: {
            "outcome": "success", "run_id": 1, "run_html_url": "x",
        }
        client = app_module.app.test_client()
        resp = client.post("/api/refresh/github")
        check("github刷新本身成功", resp.status_code == 200 and resp.get_json()["status"] == "success")

        mirror_status = db.get_target_status("mirror", 300)
        backup_status = db.get_target_status("backup", 300)
        check("github刷新不会顺便触发mirror的cooldown/结果记录",
              mirror_status["state"] == "idle" and mirror_status["last_result"] is None, mirror_status)
        check("github刷新不会顺便触发backup的cooldown/结果记录",
              backup_status["state"] == "idle" and backup_status["last_result"] is None, backup_status)
    with_temp_app_env(_run)


def test_github_actions_failure_reported_as_real_failure():
    def _run(tmp, db, app_module):
        app_module.GITHUB_TOKEN = "fake-token-for-test"
        app_module.git_publish.commit_and_push = lambda *a, **kw: {
            "pushed": True, "commit_sha": "sha-fail", "changed_file_count": 1,
        }
        app_module.github_actions.trigger_and_wait = lambda *a, **kw: {
            "outcome": "failure", "run_id": 42, "run_html_url": "x", "conclusion": "failure",
        }
        client = app_module.app.test_client()
        resp = client.post("/api/refresh/github")
        data = resp.get_json()
        check("Actions真实conclusion=failure时，target状态也是failure，不伪装成success",
              data["status"] == "failure", data)
        check("error_category=actions_run_failed", data["error_category"] == "actions_run_failed", data)
        check("cooldown_applied=True（即使失败，冷却依然生效）", data["cooldown_applied"] is True, data)
    with_temp_app_env(_run)


def test_github_actions_timeout_returns_202_not_fake_success():
    """202/running不是"函数结束、没人再管"——超时之后app.py会启动一个
    后台线程继续跟踪（见_watch_github_run_in_background()），这里必须
    也mock掉poll_until_conclusion，否则会在后台线程里真的发起网络请求
    打GitHub API（哪怕主测试线程已经拿到202断言完就退出，后台线程依然
    会真的执行）——这正是这次审查专门要求排除的情况。完整的"后台最终
    写回真实结果"生命周期由test_github_timeout_spawns_background_watcher_*
    专门验证，这里只确认202响应本身诚实、且不会意外触网。
    """
    def _run(tmp, db, app_module):
        app_module.GITHUB_TOKEN = "fake-token-for-test"
        app_module.git_publish.commit_and_push = lambda *a, **kw: {
            "pushed": True, "commit_sha": "sha-timeout", "changed_file_count": 1,
        }
        app_module.github_actions.trigger_and_wait = lambda *a, **kw: {
            "outcome": "timeout", "run_id": 77, "run_html_url": "https://github.com/x/y/actions/runs/77",
        }
        background_calls = []

        def stub_poll_until_conclusion(repo, run_id, run_html_url, token, max_wait_seconds, **kw):
            background_calls.append(run_id)
            return {"outcome": "success", "run_id": run_id, "run_html_url": run_html_url}
        app_module.github_actions.poll_until_conclusion = stub_poll_until_conclusion

        client = app_module.app.test_client()
        resp = client.post("/api/refresh/github")
        data = resp.get_json()
        check("有界等待到期时返回202，不是200/不假装success", resp.status_code == 202)
        check("status=running", data["status"] == "running", data)
        check("真实run_id/URL被保留", data["run_id"] == 77 and "77" in data["run_html_url"], data)

        deadline = time.time() + 3
        while not background_calls and time.time() < deadline:
            time.sleep(0.02)
        check("后台线程确实被启动，用的是mock而不是真实网络请求", background_calls == [77], background_calls)
    with_temp_app_env(_run)


def test_github_timeout_spawns_background_watcher_that_eventually_records_real_result():
    """严格验证202/running之后的完整生命周期，直接回答"返回202后是否
    只是函数结束、没人再管"这个问题：有界等待到期返回202后，必须有一个
    后台线程继续跟踪，最终把真实conclusion写回refresh_targets，让
    GET /api/refresh/github/status最终能反映真实结果——不是
    "dispatch->poll 90s->return 202->释放锁->函数结束"就此了结。
    """
    def _run(tmp, db, app_module):
        app_module.GITHUB_TOKEN = "fake-token-for-test"
        app_module.git_publish.commit_and_push = lambda *a, **kw: {
            "pushed": True, "commit_sha": "watch-me-sha", "changed_file_count": 1,
        }
        app_module.github_actions.trigger_and_wait = lambda *a, **kw: {
            "outcome": "timeout", "run_id": 999, "run_html_url": "https://github.com/x/y/actions/runs/999",
        }

        call_count = {"n": 0}

        def fake_poll_until_conclusion(repo, run_id, run_html_url, token, max_wait_seconds, **kw):
            call_count["n"] += 1
            # 特意sleep一下，验证的是后台线程真的自己继续跑、不是靠瞬间
            # 完成制造出来的假象。
            time.sleep(0.3)
            return {"outcome": "success", "run_id": run_id, "run_html_url": run_html_url}
        app_module.github_actions.poll_until_conclusion = fake_poll_until_conclusion

        client = app_module.app.test_client()
        resp = client.post("/api/refresh/github")
        data = resp.get_json()
        check("有界等待到期时返回202", resp.status_code == 202)
        check("202响应带着真实run_id", data["run_id"] == 999, data)

        state_immediately = db.get_target_status("github", 300)
        check("202返回的瞬间，后台还没来得及写入最终结果（last_result为None）",
              state_immediately["last_result"] is None, state_immediately)

        deadline = time.time() + 3
        final_state = None
        while time.time() < deadline:
            final_state = db.get_target_status("github", 300)
            if final_state["last_result"] is not None:
                break
            time.sleep(0.05)

        check("后台watcher最终把真实success结果写回refresh_targets",
              final_state is not None and final_state["last_result"] is not None
              and final_state["last_result"]["status"] == "success"
              and final_state["last_result"]["commit"] == "watch-me-sha", final_state)
        check("后台watcher确实只被调用了一次", call_count["n"] == 1)
    with_temp_app_env(_run)


def test_stale_background_watcher_does_not_clobber_newer_attempt_result():
    """如果后台watcher A还没等到结论，同一个target又发起了新一轮刷新
    （比如冷却过期后用户又点了一次），新一轮自己的结果必须优先——旧
    watcher迟到的结果不能覆盖新一轮已经写好的状态（record_target_result()
    的expected_generation fencing，直接对应D-2 fencing token同一个思路）。
    """
    def _run(tmp, db, app_module):
        app_module.GITHUB_TOKEN = "fake-token-for-test"

        acquire1 = db.try_acquire_lock("content_fetch", 420, target_key="github",
                                        cooldown_seconds=0, triggered_by="github")
        old_generation = acquire1["target_generation"]
        db.release_lock("content_fetch", acquire1["generation"], "ok", "old run")

        acquire2 = db.try_acquire_lock("content_fetch", 420, target_key="github",
                                        cooldown_seconds=0, triggered_by="github")
        new_generation = acquire2["target_generation"]
        db.release_lock("content_fetch", acquire2["generation"], "ok", "new run")
        check("两轮target_generation严格递增、确实不同",
              new_generation == old_generation + 1, (old_generation, new_generation))

        db.record_target_result("github", "success", "", commit_sha="new-sha",
                                 expected_generation=new_generation)

        events = []

        def slow_poll(repo, run_id, run_html_url, token, max_wait_seconds, **kw):
            events.append("old_watcher_resolved")
            return {"outcome": "success", "run_id": run_id, "run_html_url": run_html_url}
        app_module.github_actions.poll_until_conclusion = slow_poll

        app_module._watch_github_run_in_background(
            "github", 111, "https://github.com/x/y/actions/runs/111", "old-sha", old_generation,
        )

        deadline = time.time() + 3
        while "old_watcher_resolved" not in events and time.time() < deadline:
            time.sleep(0.02)
        time.sleep(0.1)  # 再给record_target_result()一点时间落盘

        state = db.get_target_status("github", 300)
        check("旧watcher迟到的结果没有覆盖新一轮已经写好的状态",
              state["last_result"]["commit"] == "new-sha", state)
    with_temp_app_env(_run)


# ---------------------------------------------------------------------------
# 3. 真正的多进程并发（模拟gunicorn -w 2）
# ---------------------------------------------------------------------------

def _find_two_free_ports():
    ports = []
    for _ in range(2):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(("127.0.0.1", 0))
        ports.append(s.getsockname()[1])
        s.close()
    return ports


def _wait_for_server_ready(port, timeout=10):
    """用原始TCP连接探测端口是否已经在监听，而不是发一次真实的业务请求
    ——这样readiness探测本身不会依赖任何具体路由的行为是否正常，跟"服务器
    进程是否已经起来"这个问题解耦（这台Windows测试机的/api/health在磁盘
    占用超阈值时会因为一个跟刷新锁机制无关的既有小bug而500，见此前完成
    报告，不在这里重复触发）。
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


def test_two_os_processes_gunicorn_like_concurrency():
    """本阶段最重要的测试：启动两个真正独立的OS进程，各自import app.py跑
    Flask自带开发服务器（Windows没有gunicorn，这是本机能达到的最接近
    "gunicorn -w 2两个worker各自独立进程"的验证方式），指向同一个临时
    sqlite文件，模拟真实生产部署形态，用两个真正的HTTP请求分别打
    /api/refresh/mirror和/api/refresh/backup，验证只有一个能真正执行
    fetch，另一个被明确拒绝（409），不是两个都成功、也不是两个都被静默吞掉。
    """
    def _run(tmp, db):
        db_path = db.DB_PATH
        stub = tmp / "stub_fetch.py"
        stub.write_text(
            "import os, sys, time\n"
            "time.sleep(float(os.environ.get('STUB_SLEEP_SECONDS', '1')))\n"
            "print('stub fetch ran')\n"
            "sys.exit(0)\n",
            encoding="utf-8",
        )

        port1, port2 = _find_two_free_ports()
        boot_script = tmp / "server_boot.py"
        boot_code = (
            "import sys, logging\n"
            "from pathlib import Path\n"
            f"sys.path.insert(0, {str(BASE_DIR)!r})\n"
            "import db\n"
            f"db.DB_PATH = Path({str(db_path)!r})\n"
            "import app as app_module\n"
            f"app_module.FETCH_SCRIPT = {str(stub)!r}\n"
            "db.init_db()\n"
            "logging.getLogger('werkzeug').setLevel(logging.ERROR)\n"
            "port = int(sys.argv[1])\n"
            "app_module.app.run(host='127.0.0.1', port=port, debug=False, "
            "use_reloader=False, threaded=False)\n"
        )
        boot_script.write_text(boot_code, encoding="utf-8")

        env = dict(os.environ)
        env["STUB_SLEEP_SECONDS"] = "1.5"
        proc1 = subprocess.Popen([sys.executable, str(boot_script), str(port1)],
                                  cwd=str(BASE_DIR), env=env,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        proc2 = subprocess.Popen([sys.executable, str(boot_script), str(port2)],
                                  cwd=str(BASE_DIR), env=env,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            _wait_for_server_ready(port1)
            _wait_for_server_ready(port2)

            results = {}

            def _post(port, target, key):
                req = urllib.request.Request(f"http://127.0.0.1:{port}/api/refresh/{target}", method="POST")
                try:
                    with urllib.request.urlopen(req, timeout=15) as resp:
                        results[key] = (resp.status, json.loads(resp.read().decode()))
                except urllib.error.HTTPError as e:
                    results[key] = (e.code, json.loads(e.read().decode()))

            t1 = threading.Thread(target=_post, args=(port1, "mirror", "mirror"))
            t2 = threading.Thread(target=_post, args=(port2, "backup", "backup"))
            t1.start()
            t2.start()
            t1.join(timeout=20)
            t2.join(timeout=20)

            status_mirror, data_mirror = results["mirror"]
            status_backup, data_backup = results["backup"]
            succeeded_mirror = data_mirror.get("status") == "success"
            succeeded_backup = data_backup.get("status") == "success"

            check("两个真正独立OS进程(模拟gunicorn -w 2两个worker)同时请求，"
                  f"只有一个成功启动fetch: mirror success={succeeded_mirror}(HTTP{status_mirror}), "
                  f"backup success={succeeded_backup}(HTTP{status_backup})",
                  succeeded_mirror != succeeded_backup)

            loser_status = status_backup if succeeded_mirror else status_mirror
            check("未成功的一方收到明确的409(busy)拒绝，不是两者都成功、也不是静默失败",
                  loser_status == 409)
        finally:
            for p in (proc1, proc2):
                p.terminate()
            for p in (proc1, proc2):
                try:
                    p.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    p.kill()
    with_temp_db(_run)


def main():
    tests = [
        test_first_acquire_succeeds,
        test_same_target_cooldown_rejects_immediate_retry,
        test_different_target_not_blocked_by_other_targets_cooldown,
        test_shared_lock_blocks_second_target_while_running,
        test_release_then_different_target_can_acquire_shared_lock,
        test_failure_result_recorded_and_lock_released,
        test_stale_running_lock_is_recovered_based_on_age_not_guessed,
        test_content_fetch_blocks_git_publish,
        test_git_publish_blocks_content_fetch,
        test_fencing_token_prevents_stale_release_from_clobbering_new_holder,
        test_record_target_result_fencing_rejects_stale_expected_generation,
        test_process_level_race_only_one_winner,
        test_scenario_1_first_mirror_refresh_succeeds,
        test_scenario_2_second_mirror_refresh_within_5min_rejected_with_remaining,
        test_scenario_3_mirror_cooldown_does_not_block_backup,
        test_scenario_6_fetch_failure_recorded_and_not_stuck,
        test_scenario_7_fetch_timeout_recorded_and_not_stuck,
        test_scenario_5_running_rejection_returns_409,
        test_illegal_target_rejected,
        test_import_app_does_not_write_real_database,
        test_credentials_not_leaked_in_response,
        test_github_full_success_flow_mocked,
        test_cf_full_success_flow_mocked,
        test_no_html_changes_skips_publish_and_dispatch,
        test_github_cf_refresh_does_not_touch_mirror_backup_target_state,
        test_github_actions_failure_reported_as_real_failure,
        test_github_actions_timeout_returns_202_not_fake_success,
        test_github_timeout_spawns_background_watcher_that_eventually_records_real_result,
        test_stale_background_watcher_does_not_clobber_newer_attempt_result,
        test_two_os_processes_gunicorn_like_concurrency,
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
