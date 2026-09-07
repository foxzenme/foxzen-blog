#!/usr/bin/env python3
"""generate_status_page.py新增的--publish/publish_update_page()测试："改TXT
->一条命令发布"这个工作流的落地部分。

不测试announcements.txt本身的解析/排序/HTML转义逻辑——那些是既有代码，
已经在test_update_page.py里覆盖。这里只测这次新增的、把已有git_publish.py
接上来的发布逻辑：
- 只commit static_status/这一个子目录，不碰data/announcements.txt本身、
  不碰仓库里其它任何文件（对应任务要求的"git diff只包含预期公开内容"/
  "不会误提交其它文件"）；
- 生成物/commit里不包含推送用的token；
- 没有GITHUB_TOKEN时明确报错，不会静默失败或抛出未分类异常；
- --publish参数正确接到publish_update_page()上。

跟test_git_publish.py同样的约定：全部对着一次性临时git仓库(work repo + 本地
bare remote)操作，push走本地文件系统路径，不碰网络、不碰真实GitHub。

用法: python3 test_update_publish.py
"""
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import generate_status_page

BASE_DIR = Path(__file__).parent
BOT_NAME = generate_status_page.GIT_BOT_NAME
BOT_EMAIL = generate_status_page.GIT_BOT_EMAIL

failures = []


def check(name, condition, detail=""):
    if condition:
        print(f"  [PASS] {name}")
    else:
        print(f"  [FAIL] {name} {detail}")
        failures.append(name)


def _run(*args, cwd, env=None, check_ok=True):
    # 显式指定encoding="utf-8"：这次测试的commit message/announcements内容
    # 包含中文，Windows下subprocess.run(text=True)默认按当前控制台代码页
    # （GBK）解码，读到UTF-8字节会直接在后台读取线程里抛
    # UnicodeDecodeError（不会传播到这里的try/except，而是让result.stdout
    # 变成None）——test_git_publish.py的同名helper没有这个问题只是因为
    # 它的测试内容全是纯ASCII，不代表这个写法本身是安全的。
    result = subprocess.run(list(args), cwd=str(cwd), capture_output=True,
                             text=True, encoding="utf-8", env=env)
    if check_ok and result.returncode != 0:
        raise RuntimeError(f"{args} 失败: {result.stderr}")
    return result


def _env_with_identity(name, email):
    env = dict(os.environ)
    env.update({"GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email,
                "GIT_COMMITTER_NAME": name, "GIT_COMMITTER_EMAIL": email})
    return env


def with_temp_repo(fn):
    """跟test_git_publish.py::with_temp_repo()同一个约定，只是把占位子目录
    从"html"换成"static_status"（这次实际发布的subpath），另外预先放一份
    data/announcements.txt（不提交进git——模拟"这是GreenCloud本地文件，
    不是git发布内容"这个真实生产设计）。
    """
    tmp = Path(tempfile.mkdtemp(prefix="update_publish_test_"))
    try:
        remote_dir = tmp / "remote.git"
        work_dir = tmp / "work"
        _run("git", "init", "--bare", "-b", "master", str(remote_dir), cwd=tmp)
        _run("git", "init", "-b", "master", str(work_dir), cwd=tmp)

        (work_dir / "README.md").write_text("init\n", encoding="utf-8")
        _run("git", "add", "README.md", cwd=work_dir)
        _run("git", "commit", "-m", "init", cwd=work_dir,
             env=_env_with_identity("Setup", "setup@example.invalid"))

        # static_status/预先放一个已提交的占位文件，理由跟test_git_publish.py
        # 里html/.gitkeep一样：避免"整个目录从未被追踪过"导致git status把它
        # 折叠成一行`?? static_status/`，跟真实场景（已经有过至少一次发布）
        # 不是一回事。
        status_dir = work_dir / "static_status"
        status_dir.mkdir()
        (status_dir / ".gitkeep").write_text("", encoding="utf-8")
        _run("git", "add", "static_status/.gitkeep", cwd=work_dir)
        _run("git", "commit", "-m", "seed static_status/", cwd=work_dir,
             env=_env_with_identity("Setup", "setup@example.invalid"))

        _run("git", "remote", "add", "origin", str(remote_dir), cwd=work_dir)
        _run("git", "push", "-u", "origin", "master", cwd=work_dir)

        # data/announcements.txt故意不commit——这就是它在真实生产里的地位：
        # GreenCloud本地文件，从不作为git发布内容的一部分。
        data_dir = work_dir / "data"
        data_dir.mkdir()
        (data_dir / "announcements.txt").write_text(
            "2026-09-06 12:00|维护公告|测试用公告内容。\n", encoding="utf-8")

        fn(work_dir, remote_dir)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _last_commit_files(work_dir):
    result = _run("git", "show", "--name-only", "--format=", "HEAD", cwd=work_dir)
    return [line for line in result.stdout.splitlines() if line.strip()]


def _last_commit_author(work_dir):
    result = _run("git", "log", "-1", "--format=%an <%ae>", cwd=work_dir)
    return result.stdout.strip()


def _head_sha(work_dir):
    return _run("git", "rev-parse", "HEAD", cwd=work_dir).stdout.strip()


# ---------------------------------------------------------------------------

def test_publish_generates_and_pushes():
    def _case(work_dir, remote_dir):
        env = dict(os.environ)
        env["GITHUB_TOKEN"] = "dummy-token-for-local-push-not-checked"
        old_environ = dict(os.environ)
        os.environ["GITHUB_TOKEN"] = env["GITHUB_TOKEN"]
        try:
            result = generate_status_page.publish_update_page(
                repo_dir=work_dir,
                output_dir=work_dir / "static_status",
                announcements_file=work_dir / "data" / "announcements.txt",
            )
        finally:
            os.environ.clear()
            os.environ.update(old_environ)

        check("发布成功", result["pushed"], result)
        check("生成的公告条数为1", result["generated_entries"] == 1, result)
        check("push_state是pushed（确实推送了新内容）", result["push_state"] == "pushed", result)
        check("返回了commit_sha", bool(result.get("commit_sha")), result)

        check("index.html已生成", (work_dir / "static_status" / "index.html").exists())
        check("CNAME已生成", (work_dir / "static_status" / "CNAME").exists())

        files = _last_commit_files(work_dir)
        check("commit只包含static_status/下的文件", all(f.startswith("static_status/") for f in files), files)
        check("commit没有包含data/announcements.txt本身", "data/announcements.txt" not in files, files)
        check("commit没有包含README.md等无关文件", "README.md" not in files, files)

        author = _last_commit_author(work_dir)
        check("commit作者是固定机器人身份，不是当前机器全局git identity",
              author == f"{BOT_NAME} <{BOT_EMAIL}>", author)

        remote_head = _run("git", "rev-parse", "master", cwd=remote_dir).stdout.strip()
        check("远程master已经等于本地HEAD（确实push成功，不是只在本地commit）",
              remote_head == _head_sha(work_dir))
    with_temp_repo(_case)


def test_publish_missing_token_returns_error_and_does_not_commit():
    def _case(work_dir, remote_dir):
        head_before = _head_sha(work_dir)
        old_environ = dict(os.environ)
        os.environ.pop("GITHUB_TOKEN", None)
        try:
            result = generate_status_page.publish_update_page(
                repo_dir=work_dir,
                output_dir=work_dir / "static_status",
                announcements_file=work_dir / "data" / "announcements.txt",
            )
        finally:
            os.environ.clear()
            os.environ.update(old_environ)

        check("没有GITHUB_TOKEN时明确返回失败", result["pushed"] is False, result)
        check("错误分类是credentials_missing", result["error_category"] == "credentials_missing", result)
        check("即使没有token，页面本身仍然在本地生成了（只是不推送）",
              (work_dir / "static_status" / "index.html").exists())
        check("没有产生新的commit（HEAD不变）", _head_sha(work_dir) == head_before)
    with_temp_repo(_case)


def test_publish_no_secrets_in_generated_page_or_commit():
    def _case(work_dir, remote_dir):
        fake_token = "ghp_ThisShouldNeverAppearAnywhereInOutput1234"
        old_environ = dict(os.environ)
        os.environ["GITHUB_TOKEN"] = fake_token
        try:
            generate_status_page.publish_update_page(
                repo_dir=work_dir,
                output_dir=work_dir / "static_status",
                announcements_file=work_dir / "data" / "announcements.txt",
            )
        finally:
            os.environ.clear()
            os.environ.update(old_environ)

        page_content = (work_dir / "static_status" / "index.html").read_text(encoding="utf-8")
        check("生成的页面不包含推送用的token", fake_token not in page_content)

        show_result = _run("git", "show", "HEAD", cwd=work_dir)
        check("commit本身（diff+message）不包含推送用的token", fake_token not in show_result.stdout)
    with_temp_repo(_case)


def test_publish_second_call_same_content_is_noop_push():
    def _case(work_dir, remote_dir):
        old_environ = dict(os.environ)
        os.environ["GITHUB_TOKEN"] = "dummy-token"
        try:
            first = generate_status_page.publish_update_page(
                repo_dir=work_dir, output_dir=work_dir / "static_status",
                announcements_file=work_dir / "data" / "announcements.txt")
            second = generate_status_page.publish_update_page(
                repo_dir=work_dir, output_dir=work_dir / "static_status",
                announcements_file=work_dir / "data" / "announcements.txt")
        finally:
            os.environ.clear()
            os.environ.update(old_environ)

        check("第一次发布成功", first["pushed"], first)
        check("内容不变时第二次调用也不报错", second["pushed"], second)
        check("内容完全相同时第二次是noop（没有产生空commit）",
              second["push_state"] == "noop", second)
    with_temp_repo(_case)


def test_build_update_page_alone_does_not_touch_git():
    """不带--publish（直接调用build_update_page()）时只在本地生成文件，
    绝不触碰git——确认"生成"和"发布"这两步是严格分离的两个动作。"""
    def _case(work_dir, remote_dir):
        head_before = _head_sha(work_dir)
        generate_status_page.build_update_page(
            output_dir=work_dir / "static_status",
            announcements_file=work_dir / "data" / "announcements.txt",
        )
        check("只生成不发布时HEAD完全不变", _head_sha(work_dir) == head_before)
        status = _run("git", "status", "--porcelain", cwd=work_dir)
        check("static_status/出现本地未提交的修改（生成了文件但没有commit）",
              "static_status/" in status.stdout, status.stdout)
    with_temp_repo(_case)


def test_main_publish_flag_invokes_publish_update_page():
    """静态/结构验证：--publish参数确实接到了publish_update_page()上，
    不需要真的建git仓库/真的push——用一个记录调用的桩函数替换真实实现。
    """
    calls = []

    def fake_publish(*args, **kwargs):
        calls.append((args, kwargs))
        return {"pushed": True, "commit_sha": "deadbeef", "changed_file_count": 1,
                "push_state": "pushed", "generated_entries": 0}

    orig_publish = generate_status_page.publish_update_page
    orig_argv = sys.argv
    generate_status_page.publish_update_page = fake_publish
    sys.argv = ["generate_status_page.py", "--publish"]
    try:
        generate_status_page.main()
        check("main()在--publish时调用了publish_update_page()", len(calls) == 1, calls)
    finally:
        generate_status_page.publish_update_page = orig_publish
        sys.argv = orig_argv


def test_main_publish_flag_exits_nonzero_on_failure():
    def fake_publish(*args, **kwargs):
        return {"pushed": False, "error_category": "git_push_error", "detail": "模拟失败",
                "generated_entries": 0}

    orig_publish = generate_status_page.publish_update_page
    orig_argv = sys.argv
    generate_status_page.publish_update_page = fake_publish
    sys.argv = ["generate_status_page.py", "--publish"]
    try:
        exited = False
        code = None
        try:
            generate_status_page.main()
        except SystemExit as e:
            exited = True
            code = e.code
        check("发布失败时main()以非0退出码结束（不能静默当成功处理）", exited and code == 1, code)
    finally:
        generate_status_page.publish_update_page = orig_publish
        sys.argv = orig_argv


def test_main_without_publish_flag_only_builds_locally():
    """不带--publish时main()走build_update_page()分支，不调用
    publish_update_page()——同时把build_update_page()也换成桩函数，
    因为它不带参数调用时默认写向这个项目真实的static_status/目录
    （build_update_page()的默认参数在模块导入时就绑定好了，测试运行期间
    重新赋值generate_status_page.OUTPUT_DIR对它不起作用），必须避免真的
    写这个仓库自己的文件。
    """
    publish_calls = []
    build_calls = []

    def fake_publish(*args, **kwargs):
        publish_calls.append((args, kwargs))
        return {"pushed": True}

    def fake_build(*args, **kwargs):
        build_calls.append((args, kwargs))
        return generate_status_page.OUTPUT_DIR, []

    orig_publish = generate_status_page.publish_update_page
    orig_build = generate_status_page.build_update_page
    orig_argv = sys.argv
    generate_status_page.publish_update_page = fake_publish
    generate_status_page.build_update_page = fake_build
    sys.argv = ["generate_status_page.py"]
    try:
        generate_status_page.main()
        check("不带--publish时不会调用publish_update_page()（只在本地生成）", publish_calls == [])
        check("不带--publish时调用了build_update_page()", len(build_calls) == 1, build_calls)
    finally:
        generate_status_page.publish_update_page = orig_publish
        generate_status_page.build_update_page = orig_build
        sys.argv = orig_argv


def main():
    tests = [
        test_publish_generates_and_pushes,
        test_publish_missing_token_returns_error_and_does_not_commit,
        test_publish_no_secrets_in_generated_page_or_commit,
        test_publish_second_call_same_content_is_noop_push,
        test_build_update_page_alone_does_not_touch_git,
        test_main_publish_flag_invokes_publish_update_page,
        test_main_publish_flag_exits_nonzero_on_failure,
        test_main_without_publish_flag_only_builds_locally,
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
