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
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import mock

import git_publish

BASE_DIR = Path(__file__).parent
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

        # html/预先放一个已经commit过的占位文件，不能让它保持"整个目录从未
        # 被追踪过"的状态：git status对完全未追踪的目录会把它折叠成一行
        # `?? html/`（不逐个列出内部文件），跟生产环境的html/（早就有几千个
        # 已提交文件）完全不是一回事，会让"新增文件是否被正确检测"这类
        # 测试的字符串匹配产生误导性的失败/通过。这个占位commit必须在下面
        # `git push -u`之前完成，让它跟着一起推送出去——否则work_dir的HEAD
        # 会天然领先origin/master一个commit，任何期望"起始状态HEAD与远程
        # 完全同步"的测试(比如真正的no-op场景)都会被这个多出来的本地
        # 占位commit污染。
        html_dir = work_dir / "html"
        html_dir.mkdir()
        (html_dir / ".gitkeep").write_text("", encoding="utf-8")
        _run("git", "add", "html/.gitkeep", cwd=work_dir)
        _run("git", "commit", "-m", "seed html/", cwd=work_dir,
             env=_env_with_identity("Setup", "setup@example.invalid"))

        _run("git", "remote", "add", "origin", str(remote_dir), cwd=work_dir)
        _run("git", "push", "-u", "origin", "master", cwd=work_dir)

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


def test_deleted_file_under_subpath_is_committed_and_pushed():
    """Blogger删除文章同步（fetch_blog.py的sync_deleted_posts()）依赖这一条：
    html/下的文件删除必须能被现有的`git add -- subpath`（不带-A/-u）正确
    检测并提交，不需要为删除场景另写git逻辑。这里用两轮commit_and_push()
    验证：第一轮先提交一个文件让它进入一次真实的历史提交，第二轮删掉它，
    确认删除被正确staged、commit、push到远端，且没有把它误判为"无变化"。
    """
    def _run_case(work_dir, remote_dir):
        post_file = work_dir / "html" / "posts" / "will-be-deleted.html"
        post_file.parent.mkdir(parents=True)
        post_file.write_text("<p>this post will be deleted</p>", encoding="utf-8")
        first = git_publish.commit_and_push(work_dir, "html", BOT_NAME, BOT_EMAIL,
                                             "add post", "fake-token", 30)
        check("第一轮：新增文件被正常commit+push", first["pushed"] and first["changed_file_count"] == 1)

        post_file.unlink()
        second = git_publish.commit_and_push(work_dir, "html", BOT_NAME, BOT_EMAIL,
                                              "delete post", "fake-token", 30)
        check("第二轮：删除文件也被识别为一次变化", second["pushed"] and second["changed_file_count"] == 1, second)
        check("第二轮确实产生了新commit（sha跟第一轮不同）",
              second["commit_sha"] != first["commit_sha"], (first["commit_sha"], second["commit_sha"]))

        tracked = _run("git", "ls-tree", "-r", "--name-only", "HEAD", "--", "html", cwd=work_dir).stdout
        check("删除的文件不再出现在HEAD的工作树里",
              "html/posts/will-be-deleted.html" not in tracked.splitlines(), tracked)

        remote_head = _run("git", "rev-parse", "master", cwd=remote_dir).stdout.strip()
        check("远程(bare repo)HEAD跟这次删除commit一致", remote_head == second["commit_sha"])
        remote_tracked = _run("git", "ls-tree", "-r", "--name-only", remote_head, "--", "html", cwd=remote_dir).stdout
        check("远程仓库里也确认删除已经生效，不是只有本地work_dir看起来删了",
              "html/posts/will-be-deleted.html" not in remote_tracked.splitlines(), remote_tracked)
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


def test_staged_external_file_never_enters_commit():
    """B1的核心回归测试：git add -- html本身没问题，但如果调用commit_and_push
    之前，工作区里已经有一个跟这次刷新无关的文件被开发者/运维手动git add
    过（比如正在VPS上编辑一个还没提交的配置文件），旧的`git commit -m msg`
    (不带pathspec)会把整个index一起提交，包括这个已经staged的外部文件
    ——这才是B1真正的漏洞场景（跟上面test_add_scope_strictly_limited_to_subpath
    覆盖的"未staged的脏文件"是两种不同的情况）。修复后用
    `git commit --only -m msg -- html`，index里html/之外已经staged的内容
    必须原封不动地留在index里，既不会被提交，也不会被丢弃。
    """
    def _run_case(work_dir, remote_dir):
        (work_dir / "html" / "post1.html").write_text("<p>hello</p>", encoding="utf-8")
        (work_dir / "STAGED_SECRET.conf").write_text("token=super-secret", encoding="utf-8")
        _run("git", "add", "STAGED_SECRET.conf", cwd=work_dir)

        result = git_publish.commit_and_push(work_dir, "html", BOT_NAME, BOT_EMAIL,
                                              "test commit", "fake-token", 30)
        check("html/有变化时正常pushed", result["pushed"] and result["changed_file_count"] == 1, result)

        committed_files = _last_commit_files(work_dir)
        check("已经staged的外部文件STAGED_SECRET.conf绝不能进入commit",
              "STAGED_SECRET.conf" not in committed_files, committed_files)
        check("commit里只包含html/下的文件",
              all(f.startswith("html/") for f in committed_files), committed_files)

        status = _run("git", "status", "--porcelain", cwd=work_dir).stdout
        check("STAGED_SECRET.conf仍然保持原来staged的状态，没有被丢弃也没有被提交",
              "A  STAGED_SECRET.conf" in status, status)
    with_temp_repo(_run_case)


def test_true_noop_when_head_already_matches_remote():
    """B2情形A：html/无变化且HEAD已经等于origin/master，才是真正的no-op，
    push_state必须是noop。
    """
    def _run_case(work_dir, remote_dir):
        result = git_publish.commit_and_push(work_dir, "html", BOT_NAME, BOT_EMAIL,
                                              "test commit", "fake-token", 30)
        check("真正no-op时pushed=True changed_file_count=0",
              result["pushed"] and result["changed_file_count"] == 0, result)
        check("push_state=noop", result.get("push_state") == "noop", result)
    with_temp_repo(_run_case)


def test_pending_commit_from_previous_failed_push_is_retried():
    """B2情形B：模拟"上一轮commit成功但push失败"留下的本地遗留commit——
    这一轮内容本身没有任何新变化（html/已经是干净的工作树），但HEAD领先
    于origin/master，必须继续尝试push，不能因为"html/无变化"就直接报告
    no-op success而放弃。
    """
    def _run_case(work_dir, remote_dir):
        (work_dir / "html" / "post1.html").write_text("<p>leftover</p>", encoding="utf-8")
        _run("git", "add", "--", "html", cwd=work_dir)
        _run("git", "commit", "--only", "-m", "leftover commit from failed push", "--", "html",
             cwd=work_dir, env=_env_with_identity(BOT_NAME, BOT_EMAIL))
        # 此时故意不push，模拟"commit成功但push失败"后的状态：本地HEAD已经
        # 领先origin/master，但工作树对html/而言已经干净(没有未commit的变化)。

        head_before = _run("git", "rev-parse", "HEAD", cwd=work_dir).stdout.strip()
        remote_before = _run("git", "rev-parse", "master", cwd=remote_dir).stdout.strip()
        check("准备场景：本地HEAD领先于远程", head_before != remote_before)

        result = git_publish.commit_and_push(work_dir, "html", BOT_NAME, BOT_EMAIL,
                                              "test commit", "fake-token", 30)
        check("这一轮changed_file_count=0（html/本身确实没有新变化）",
              result["pushed"] and result["changed_file_count"] == 0, result)
        check("push_state=pushed（不是noop）：遗留的commit被真正推送出去了",
              result.get("push_state") == "pushed", result)

        remote_after = _run("git", "rev-parse", "master", cwd=remote_dir).stdout.strip()
        check("远程现在确实收到了遗留的commit", remote_after == head_before, (remote_after, head_before))
    with_temp_repo(_run_case)


def test_diverged_remote_reported_as_failure_never_force_pushed():
    """B2情形C：本地HEAD与origin/master已经分叉（比如有人直接在另一个
    clone上push过与本地历史不相容的提交），绝不能自动force push或者
    静默覆盖，必须原样报告为一个明确、可识别的failure类别。
    """
    def _run_case(work_dir, remote_dir):
        other_clone = work_dir.parent / "other_clone"
        _run("git", "clone", str(remote_dir), str(other_clone), cwd=work_dir.parent)
        (other_clone / "from_elsewhere.txt").write_text("x", encoding="utf-8")
        _run("git", "add", "from_elsewhere.txt", cwd=other_clone)
        _run("git", "commit", "-m", "diverging commit from elsewhere", cwd=other_clone,
             env=_env_with_identity("Someone Else", "else@example.invalid"))
        _run("git", "push", "origin", "master", cwd=other_clone)

        (work_dir / "html" / "post1.html").write_text("<p>local change</p>", encoding="utf-8")
        _run("git", "add", "--", "html", cwd=work_dir)
        _run("git", "commit", "--only", "-m", "local change", "--", "html",
             cwd=work_dir, env=_env_with_identity(BOT_NAME, BOT_EMAIL))

        remote_before = _run("git", "rev-parse", "master", cwd=remote_dir).stdout.strip()

        result = git_publish.commit_and_push(work_dir, "html", BOT_NAME, BOT_EMAIL,
                                              "test commit", "fake-token", 30)
        check("分叉时pushed=False", result["pushed"] is False, result)
        check("error_category=remote_diverged（不是笼统的git_push_error）",
              result.get("error_category") == "remote_diverged", result)

        remote_after = _run("git", "rev-parse", "master", cwd=remote_dir).stdout.strip()
        check("远程完全没有被改变（绝没有发生force push覆盖）",
              remote_after == remote_before, (remote_before, remote_after))
    with_temp_repo(_run_case)


def test_wrong_branch_refuses_publish():
    def _run_case(work_dir, remote_dir):
        _run("git", "checkout", "-b", "feature", cwd=work_dir)
        (work_dir / "html" / "post1.html").write_text("<p>x</p>", encoding="utf-8")

        result = git_publish.commit_and_push(work_dir, "html", BOT_NAME, BOT_EMAIL,
                                              "test commit", "fake-token", 30)
        check("非master分支时拒绝发布，pushed=False", result["pushed"] is False, result)
        check("error_category=wrong_branch", result.get("error_category") == "wrong_branch", result)

        status = _run("git", "status", "--porcelain", cwd=work_dir).stdout
        check("html/post1.html完全没有被add/commit（预检查在最前面拦下）",
              "post1.html" in status and status.strip().startswith("??"), status)
    with_temp_repo(_run_case)


def test_detached_head_refuses_publish():
    def _run_case(work_dir, remote_dir):
        head_sha = _run("git", "rev-parse", "HEAD", cwd=work_dir).stdout.strip()
        _run("git", "checkout", head_sha, cwd=work_dir)
        (work_dir / "html" / "post1.html").write_text("<p>x</p>", encoding="utf-8")

        result = git_publish.commit_and_push(work_dir, "html", BOT_NAME, BOT_EMAIL,
                                              "test commit", "fake-token", 30)
        check("detached HEAD时拒绝发布", result["pushed"] is False, result)
        check("error_category=wrong_branch", result.get("error_category") == "wrong_branch", result)
    with_temp_repo(_run_case)


def test_mid_merge_state_refuses_publish():
    """模拟"仓库正处于未完成的merge"这种异常状态：手动放一个MERGE_HEAD
    文件（比真的走出一次merge conflict更简单、更聚焦——这个测试只关心
    _check_publish_preconditions()是否识别出这个标记文件并拒绝）。
    """
    def _run_case(work_dir, remote_dir):
        git_dir_result = _run("git", "rev-parse", "--git-dir", cwd=work_dir)
        git_dir = work_dir / git_dir_result.stdout.strip()
        (git_dir / "MERGE_HEAD").write_text(
            _run("git", "rev-parse", "HEAD", cwd=work_dir).stdout, encoding="utf-8")
        (work_dir / "html" / "post1.html").write_text("<p>x</p>", encoding="utf-8")

        result = git_publish.commit_and_push(work_dir, "html", BOT_NAME, BOT_EMAIL,
                                              "test commit", "fake-token", 30)
        check("存在进行中的merge时拒绝发布", result["pushed"] is False, result)
        check("error_category=repository_busy", result.get("error_category") == "repository_busy", result)
    with_temp_repo(_run_case)


def test_askpass_helper_relays_token_exactly_and_nothing_else():
    """跨平台都能跑：直接验证git_askpass_helper.py这个脚本自身的逻辑——
    从FOXZEN_GIT_PUSH_TOKEN环境变量读token，原样写到stdout，不多输出
    任何东西（不能有多余的换行/提示语混进去，git会把stdout原样当密码），
    stderr必须为空（不能把token意外打到stderr——大多数终端/日志会同时
    采集两者，stderr多一个字符都是不必要的暴露面）。用假token，不使用
    真实凭据。
    """
    fake_token = "fake-test-token-not-a-real-credential-12345"
    env = dict(os.environ)
    env["FOXZEN_GIT_PUSH_TOKEN"] = fake_token
    result = subprocess.run(
        [sys.executable, str(git_publish.GIT_ASKPASS_SCRIPT), "Password for 'https://x@github.com': "],
        capture_output=True, text=True, env=env, timeout=10,
    )
    check("helper原样、完整地把token写到stdout", result.stdout == fake_token, repr(result.stdout))
    check("stderr完全为空，没有任何多余输出", result.stderr == "", repr(result.stderr))
    check("退出码为0", result.returncode == 0)


def test_push_stderr_never_leaks_the_real_push_token_even_if_git_echoed_it():
    """S8要求2的独立防线：正常情况下GIT_ASKPASS机制决定push_token根本不会
    被git打印到stderr（见git_askpass_helper.py只往stdout写、且上面那个
    测试已经验证了stderr确实为空），这里不依赖"git永远不会意外把它打出来"
    这个假设——直接mock subprocess.run，构造一个"假设git真的把token打印
    到了stderr"的极端场景，验证即使发生这种情况，_push()构造出的
    GitPublishError.detail里也不会包含这个真实token（已经被redact_known_secrets
    替换掉），同时确认其它诊断信息没有被连带一起吞掉。
    """
    real_token = "ghp_THIS_IS_THE_REAL_TOKEN_VALUE_FOR_THIS_TEST"
    leaky_stderr = f"fatal: Authorization failed, token was {real_token}, remote rejected"

    def fake_run(cmd, **kwargs):
        if cmd[:2] == ["git", "push"]:
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr=leaky_stderr)
        raise AssertionError(f"这个测试不应该调用到除git push之外的subprocess: {cmd}")

    with mock.patch.object(git_publish.subprocess, "run", side_effect=fake_run):
        try:
            git_publish._push(Path("."), real_token, 10)
            check("stderr里出现认证失败信息时应该抛GitPublishError", False)
        except git_publish.GitPublishError as e:
            check("即使(假设的)git stderr真的包含了真实token，构造出的错误详情里也不包含它",
                  real_token not in e.detail, e.detail)
            check("错误详情仍然保留了其它诊断信息，不是整段被吞掉",
                  "Authorization failed" in e.detail and "remote rejected" in e.detail, e.detail)


def test_git_publish_ensures_askpass_executable_before_push():
    """B5核心回归：git_publish.py不能假设git_askpass_helper.py检出到Linux
    VPS时天然带着可执行位（Windows开发环境不会保留这个位，实际权限取决于
    Git对象库里记录的文件mode）。这里在POSIX平台上把helper的可执行位先
    显式清掉，验证commit_and_push()内部的_ensure_askpass_executable()会
    在真正push之前把它补回来——这是一次真实的、对本地bare remote的push
    （不是mock），如果补权限没有生效，git会因为GIT_ASKPASS指向的文件
    EACCES而push失败，这个测试能直接暴露出来，不是靠检查一个内部函数
    被调用过就算数。
    """
    if os.name != "posix":
        print("  [SKIP] 仅在POSIX平台验证真实可执行位语义（Windows没有对应概念，"
              "os.chmod在Windows上是no-op，真正的验证在Linux CI/VPS上进行）")
        return

    def _run_case(work_dir, remote_dir):
        helper = git_publish.GIT_ASKPASS_SCRIPT
        original_mode = helper.stat().st_mode
        try:
            os.chmod(helper, 0o644)  # 显式去掉所有可执行位，模拟B5发现的问题
            check("准备场景：helper此刻确实不可执行", not os.access(helper, os.X_OK))

            (work_dir / "html" / "post1.html").write_text("<p>x</p>", encoding="utf-8")
            result = git_publish.commit_and_push(work_dir, "html", BOT_NAME, BOT_EMAIL,
                                                  "test commit", "fake-token", 30)
            check("即使helper起初不可执行，commit_and_push仍能成功push"
                  "（真正在push前主动补上了可执行位，不是依赖checkout自带）",
                  result["pushed"], result)
            check("_ensure_askpass_executable()调用之后，helper变为可执行",
                  os.access(helper, os.X_OK))
        finally:
            os.chmod(helper, original_mode)
    with_temp_repo(_run_case)


def test_new_png_under_html_is_not_ignored_by_gitignore():
    """S1回归测试：博客文章目录html/posts/<id>/media/下新增的PNG图片必须能
    被git_publish正常提交，不能被仓库根目录那条给"杂项图片"用的全局*.png
    规则连带误伤——用的是这个仓库真实的.gitignore文件本身（复制进临时
    仓库），不是测试自己编的规则，这样.gitignore以后被意外改回全局吞掉
    png这类回归也能被这个测试抓到。
    """
    def _run_case(work_dir, remote_dir):
        real_gitignore = (BASE_DIR / ".gitignore").read_text(encoding="utf-8")
        (work_dir / ".gitignore").write_text(real_gitignore, encoding="utf-8")
        _run("git", "add", ".gitignore", cwd=work_dir)
        _run("git", "commit", "-m", "add real .gitignore", cwd=work_dir,
             env=_env_with_identity("Setup", "setup@example.invalid"))

        # 先提交这篇文章目录下已有的一张图片，模拟"这篇文章之前抓取过、
        # media/目录本身已经是被追踪的非空目录"——这样贴近生产环境的真实
        # 拓扑（html/posts/<id>/media/早就存在其它已提交文件），也避免了
        # git对"整个从未被追踪过的新目录"会折叠成一行`?? html/posts/<id>/`
        # 而不逐个列出内部文件这个特性干扰断言（这不是生产场景会遇到的
        # 情况：真实的html/posts/<id>/media/要么是老文章的已追踪目录，
        # 要么整篇文章都是新的，两种情况下.gitignore的行为都需要正确，
        # 这里只聚焦验证.gitignore规则本身，用"老文章追加新图片"这个更
        # 容易在git status层面精确断言的场景）。
        media_dir = work_dir / "html" / "posts" / "123456" / "media"
        media_dir.mkdir(parents=True)
        (media_dir / "existing_old_image.png").write_bytes(b"\x89PNG existing tracked image")
        _run("git", "add", "html/posts/123456/media/existing_old_image.png", cwd=work_dir)
        _run("git", "commit", "-m", "seed existing post media", cwd=work_dir,
             env=_env_with_identity("Setup", "setup@example.invalid"))
        _run("git", "push", "origin", "master", cwd=work_dir)

        (media_dir / "abcdef123456.png").write_bytes(b"\x89PNG fake bytes for test")

        changed = git_publish.detect_changed_paths(work_dir, "html")
        check("html/posts/.../media/下的新PNG被检测为变化，没有被.gitignore的*.png规则吞掉",
              any("media/abcdef123456.png" in c for c in changed), changed)

        result = git_publish.commit_and_push(work_dir, "html", BOT_NAME, BOT_EMAIL,
                                              "test commit", "fake-token", 30)
        check("这张新PNG真的被push出去了", result["pushed"], result)
        committed_files = _last_commit_files(work_dir)
        check("commit里确实包含这张PNG文件",
              any("media/abcdef123456.png" in f for f in committed_files), committed_files)
    with_temp_repo(_run_case)


def test_png_outside_html_still_ignored():
    """确认S1修复的范围严格限定在html/内——html/之外的杂项PNG（比如仓库
    根目录下的截图）仍然应该被.gitignore正常忽略，没有被这次修复意外
    扩大到任意目录。
    """
    def _run_case(work_dir, remote_dir):
        real_gitignore = (BASE_DIR / ".gitignore").read_text(encoding="utf-8")
        (work_dir / ".gitignore").write_text(real_gitignore, encoding="utf-8")
        _run("git", "add", ".gitignore", cwd=work_dir)
        _run("git", "commit", "-m", "add real .gitignore", cwd=work_dir,
             env=_env_with_identity("Setup", "setup@example.invalid"))

        (work_dir / "some_screenshot.png").write_bytes(b"\x89PNG fake bytes")
        status = _run("git", "status", "--porcelain", cwd=work_dir).stdout
        check("html/之外的PNG仍然被.gitignore正常忽略，没有被误报为待添加的变化",
              "some_screenshot.png" not in status, status)
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
        test_deleted_file_under_subpath_is_committed_and_pushed,
        test_add_scope_strictly_limited_to_subpath,
        test_staged_external_file_never_enters_commit,
        test_true_noop_when_head_already_matches_remote,
        test_pending_commit_from_previous_failed_push_is_retried,
        test_diverged_remote_reported_as_failure_never_force_pushed,
        test_wrong_branch_refuses_publish,
        test_detached_head_refuses_publish,
        test_mid_merge_state_refuses_publish,
        test_askpass_helper_relays_token_exactly_and_nothing_else,
        test_push_stderr_never_leaks_the_real_push_token_even_if_git_echoed_it,
        test_git_publish_ensures_askpass_executable_before_push,
        test_new_png_under_html_is_not_ignored_by_gitignore,
        test_png_outside_html_still_ignored,
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
