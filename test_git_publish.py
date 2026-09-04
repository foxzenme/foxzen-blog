#!/usr/bin/env python3
"""git_publish.py的独立测试：全部对着一次性临时git仓库(work repo + 本地bare
remote)操作，绝不会碰这个项目自己的仓库、绝不会打真实网络/真实GitHub。

覆盖：
- git add范围严格限制在指定subpath内，subpath之外的脏文件不会被误提交
  （对应你要求的测试项15）
- html/没有变化时不commit、不push（对应测试项16）
- commit使用固定机器人身份，不是当前机器的全局git identity
- push失败（远程不存在/分叉）被正确分类为git_push_error
- push超时被正确分类为git_push_error且不抛异常
- 所有subprocess调用都是参数列表、不经过shell（对应测试项10 command injection的一部分）

用法: python3 test_git_publish.py
"""
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import mock

import git_publish

BOT_NAME = "Foxzen Refresh Bot"
BOT_EMAIL = "foxzen-refresh-bot@users.noreply.github.com"

failures = []


def check(name, condition, detail=""):
    if condition:
        print(f"  [PASS] {name}")
    else:
        print(f"  [FAIL] {name} {detail}")
        failures.append(name)


def _run(*args, cwd, env=None, check_ok=True):
    result = subprocess.run(list(args), cwd=str(cwd), capture_output=True, text=True, env=env)
    if check_ok and result.returncode != 0:
        raise RuntimeError(f"{args} 失败: {result.stderr}")
    return result


def with_temp_repo(fn):
    """建一个work repo + 本地bare remote的组合：push走本地文件系统路径，
    真的会成功/失败，但完全不碰网络、不碰真实GitHub。跑完整个删除。
    """
    tmp = Path(tempfile.mkdtemp(prefix="git_publish_test_"))
    try:
        remote_dir = tmp / "remote.git"
        work_dir = tmp / "work"
        _run("git", "init", "--bare", "-b", "master", str(remote_dir), cwd=tmp)
        _run("git", "init", "-b", "master", str(work_dir), cwd=tmp)

        (work_dir / "README.md").write_text("init\n", encoding="utf-8")
        _run("git", "add", "README.md", cwd=work_dir)
        _run("git", "commit", "-m", "init", cwd=work_dir,
             env=_env_with_identity("Setup", "setup@example.invalid"))
        _run("git", "remote", "add", "origin", str(remote_dir), cwd=work_dir)
        _run("git", "push", "-u", "origin", "master", cwd=work_dir)

        (work_dir / "html").mkdir()
        fn(work_dir, remote_dir)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _env_with_identity(name, email):
    import os
    env = dict(os.environ)
    env.update({"GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email,
                "GIT_COMMITTER_NAME": name, "GIT_COMMITTER_EMAIL": email})
    return env


def _last_commit_author(work_dir):
    result = _run("git", "log", "-1", "--format=%an <%ae>", cwd=work_dir)
    return result.stdout.strip()


def _last_commit_files(work_dir):
    result = _run("git", "show", "--name-only", "--format=", "HEAD", cwd=work_dir)
    return [line for line in result.stdout.splitlines() if line.strip()]


# ---------------------------------------------------------------------------

def test_no_changes_no_commit_no_push():
    def _run_case(work_dir, remote_dir):
        head_before = _run("git", "rev-parse", "HEAD", cwd=work_dir).stdout.strip()
        result = git_publish.commit_and_push(work_dir, "html", BOT_NAME, BOT_EMAIL,
                                              "test commit", "fake-token", 30)
        check("html/无变化时pushed=True但changed_file_count=0", result["pushed"] and result["changed_file_count"] == 0)
        check("html/无变化时commit_sha是当前HEAD（没有产生新提交）", result["commit_sha"] == head_before)
        head_after = _run("git", "rev-parse", "HEAD", cwd=work_dir).stdout.strip()
        check("HEAD确实没有变化", head_after == head_before)
    with_temp_repo(_run_case)


def test_changes_committed_and_pushed_with_bot_identity():
    def _run_case(work_dir, remote_dir):
        (work_dir / "html" / "post1.html").write_text("<p>hello</p>", encoding="utf-8")
        result = git_publish.commit_and_push(work_dir, "html", BOT_NAME, BOT_EMAIL,
                                              "test commit", "fake-token", 30)
        check("有变化时pushed=True", result["pushed"])
        check("changed_file_count=1", result["changed_file_count"] == 1, result)
        check("commit_sha非空", bool(result["commit_sha"]))

        author = _last_commit_author(work_dir)
        check(f"commit作者是固定机器人身份，不是当前机器的git identity: {author!r}",
              author == f"{BOT_NAME} <{BOT_EMAIL}>", author)

        remote_head = _run("git", "rev-parse", "master", cwd=remote_dir).stdout.strip()
        check("远程(bare repo)真的收到了这次push", remote_head == result["commit_sha"])
    with_temp_repo(_run_case)


def test_add_scope_strictly_limited_to_subpath():
    """subpath之外的脏文件——包括这个仓库里可能还躺着的、别人正在进行的
    其它未提交工作——绝不能被git_publish误提交。这是"绝不用git add ./-A"
    这条安全要求的直接回归测试。
    """
    def _run_case(work_dir, remote_dir):
        (work_dir / "html" / "post1.html").write_text("<p>hello</p>", encoding="utf-8")
        (work_dir / "OTHER_WORK_IN_PROGRESS.txt").write_text("不应该被提交", encoding="utf-8")

        result = git_publish.commit_and_push(work_dir, "html", BOT_NAME, BOT_EMAIL,
                                              "test commit", "fake-token", 30)
        check("html/有变化时正常pushed", result["pushed"] and result["changed_file_count"] == 1)

        committed_files = _last_commit_files(work_dir)
        check("commit里只包含html/下的文件",
              all(f.startswith("html/") for f in committed_files), committed_files)
        check("OTHER_WORK_IN_PROGRESS.txt没有出现在commit里",
              "OTHER_WORK_IN_PROGRESS.txt" not in committed_files, committed_files)

        status = _run("git", "status", "--porcelain", cwd=work_dir).stdout
        check("OTHER_WORK_IN_PROGRESS.txt在working tree里依然是未提交状态（原封不动）",
              "OTHER_WORK_IN_PROGRESS.txt" in status, status)
    with_temp_repo(_run_case)


def test_push_failure_when_remote_missing():
    def _run_case(work_dir, remote_dir):
        _run("git", "remote", "remove", "origin", cwd=work_dir)
        _run("git", "remote", "add", "origin", str(work_dir / "does-not-exist.git"), cwd=work_dir)
        (work_dir / "html" / "post1.html").write_text("x", encoding="utf-8")

        result = git_publish.commit_and_push(work_dir, "html", BOT_NAME, BOT_EMAIL,
                                              "test commit", "fake-token", 30)
        check("远程不存在时pushed=False", result["pushed"] is False)
        check("error_category=git_push_error", result.get("error_category") == "git_push_error", result)
        check("commit本身仍然发生了（本地commit不依赖push是否成功）",
              _run("git", "log", "-1", "--format=%s", cwd=work_dir).stdout.strip() == "test commit")
    with_temp_repo(_run_case)


def test_push_timeout_reported_as_failure_not_exception():
    """真实网络超时很难在测试里稳定复现，这里直接对subprocess.run打桩模拟
    TimeoutExpired——只验证git_publish.py自己这一层"超时必须被分类为
    git_push_error、不能抛异常、不能假装成功"的处理是否正确。
    """
    def _run_case(work_dir, remote_dir):
        (work_dir / "html" / "post1.html").write_text("x", encoding="utf-8")

        real_run = subprocess.run

        def fake_run(cmd, *args, **kwargs):
            if cmd[:2] == ["git", "push"]:
                raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout"))
            return real_run(cmd, *args, **kwargs)

        with mock.patch("git_publish.subprocess.run", side_effect=fake_run):
            result = git_publish.commit_and_push(work_dir, "html", BOT_NAME, BOT_EMAIL,
                                                  "test commit", "fake-token", 1)
        check("push超时时pushed=False，不抛异常", result["pushed"] is False)
        check("error_category=git_push_error", result.get("error_category") == "git_push_error", result)
        check("detail里提到超时", "超时" in result["detail"], result["detail"])
    with_temp_repo(_run_case)


def test_all_subprocess_calls_are_argument_lists_never_shell():
    """command injection防线的静态验证：commit_and_push()内部所有subprocess.run
    调用都必须是参数列表（不是拼接的字符串），且从不传shell=True。
    """
    def _run_case(work_dir, remote_dir):
        (work_dir / "html" / "post1.html").write_text("x", encoding="utf-8")
        calls = []
        real_run = subprocess.run

        def recording_run(cmd, *args, **kwargs):
            calls.append((cmd, kwargs.get("shell", False)))
            return real_run(cmd, *args, **kwargs)

        with mock.patch("git_publish.subprocess.run", side_effect=recording_run):
            git_publish.commit_and_push(work_dir, "html", BOT_NAME, BOT_EMAIL,
                                         "test commit", "fake-token", 30)

        check(f"共记录到{len(calls)}次subprocess调用，全部是list且shell!=True",
              len(calls) > 0 and all(isinstance(c, list) and not shell for c, shell in calls),
              calls)
    with_temp_repo(_run_case)


def main():
    tests = [
        test_no_changes_no_commit_no_push,
        test_changes_committed_and_pushed_with_bot_identity,
        test_add_scope_strictly_limited_to_subpath,
        test_push_failure_when_remote_missing,
        test_push_timeout_reported_as_failure_not_exception,
        test_all_subprocess_calls_are_argument_lists_never_shell,
    ]
    import traceback
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
