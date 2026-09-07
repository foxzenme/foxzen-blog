#!/usr/bin/env python3
"""把GreenCloud本地某个子目录（生产环境固定是html/）的实际变化commit+push到
GitHub master的最小实现，只被app.py里github/cf两个refresh target调用。

安全边界（本模块存在的核心原因，不是随手抽取的公共函数）：
- 只会对`git add`传固定的目录参数（生产环境固定是"html"），从不是
  `git add .`/`git add -A`——绝不会碰这个目录之外的任何working tree改动，
  包括这个仓库里此刻可能还躺着的、别人正在进行的其它未提交工作；commit
  本身也用`git commit --only -- <subpath>`而不是裸`git commit`（B1修复：
  裸commit会把整个index一起提交，即使index里当时已经staged了跟这次发布
  无关的其它文件也会被一起带走——`--only`+pathspec保证commit真正只取
  subpath这一部分，index里其它已经staged的内容原封不动地留在index里，
  既不会被提交，也不会被这次操作丢弃）。
- 发布前强制校验仓库处于正常状态（当前分支必须是master、不能是detached
  HEAD、不能存在进行中的merge/rebase/cherry-pick）——B3修复：commit不能
  静默地落在错误的分支上，之后`git push origin master`如果master本身没
  变化会报告"Everything up-to-date"而被误判为发布成功，实际上这次内容
  根本没有真正发布出去。
- push永远走fast-forward-only的默认行为，绝不加`--force`：本地HEAD与
  origin/master一旦分叉，push会被git自身拒绝，这里只是原样把这个事实
  分类成一个明确的remote_diverged错误报给调用方，不做任何自动合并/变基/
  强推（B2修复的一部分）。
- 无论这一轮subpath内容是否有变化都会尝试push一次：commit成功但push
  失败会在本地留下一个从未真正发布出去的commit，如果只在"检测到变化"
  时才push，下一次调用会因为工作树已经干净而误判"无变化=已经是最新"，
  这个遗留commit会被永远卡住、却一直被报告成功（B2的核心场景）。
- 所有subprocess调用都是固定的参数列表，不经过shell（不传shell=True），
  不把任何外部输入（target名、文章标题、文件名……）拼接进命令本身——
  target/repo_dir/subpath全部来自调用方在Python层面已经校验过的固定值，
  从不是直接来自HTTP请求体的原始字符串。
- push认证只通过GIT_ASKPASS+环境变量传递（见git_askpass_helper.py），
  token不出现在argv、不写入remote URL、不写入.git/config、不出现在
  这个函数返回值以外的任何地方。每次真正push前都会显式补一次这个helper
  脚本的可执行位（B5修复：这个仓库在Windows上开发/提交，Windows没有Unix
  可执行位的概念，检出到Linux VPS时的实际权限取决于Git对象库里记录的
  文件mode，不能假设一定可执行——execvp在没有执行权限时会直接EACCES，
  GIT_TERMINAL_PROMPT=0又切断了交互式回退，最终表现为push彻底失败）。

repo_dir/subpath都是显式参数（不是硬编码的常量），方便测试指向一次性的
临时git仓库——这个模块本身不知道、也不关心BASE_DIR是什么。
"""
import os
import subprocess
from pathlib import Path

import safe_errors

GIT_ASKPASS_SCRIPT = Path(__file__).parent / "git_askpass_helper.py"

_PUBLISH_BRANCH = "master"
_IN_PROGRESS_OP_MARKERS = (
    "MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-merge", "rebase-apply", "BISECT_LOG",
)


class GitPublishError(Exception):
    def __init__(self, error_category: str, detail: str):
        self.error_category = error_category
        self.detail = detail
        super().__init__(detail)


def detect_changed_paths(repo_dir: Path, subpath: str) -> list[str]:
    """返回subpath目录下相对repo_dir实际有变化（新增/修改/删除）的路径列表。
    空列表代表没有任何变化，调用方据此跳过commit（不产生空commit）。
    """
    result = subprocess.run(
        ["git", "status", "--porcelain", "--", subpath],
        cwd=str(repo_dir), capture_output=True, text=True, encoding="utf-8", timeout=30,
    )
    if result.returncode != 0:
        raise GitPublishError("git_commit_error", f"git status失败: {result.stderr[-500:]}")
    return [line[3:].strip() for line in result.stdout.splitlines() if line.strip()]


def current_head_sha(repo_dir: Path) -> str | None:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(repo_dir), capture_output=True, text=True, encoding="utf-8", timeout=10,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def _check_publish_preconditions(repo_dir: Path) -> None:
    """B3：只有确认当前repo处于"master分支、非detached HEAD、没有进行中的
    merge/rebase/cherry-pick"这个正常状态时才允许继续，否则commit可能落在
    错误的分支上，随后`git push origin master`会因为master本身没变化而
    报告"Everything up-to-date"，被误判为no-op success——即便是这样一个
    "退出码正常"的场景，也绝不能被当作已经完成了本次发布。这个检查必须
    在detect_changed_paths/git add之前就做，异常状态下不应该产生任何
    新的staging动作。

    抛出：GitPublishError("wrong_branch"|"repository_busy"|
        "repository_state_error", detail)
    """
    branch_result = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=str(repo_dir), capture_output=True, text=True, encoding="utf-8", timeout=10,
    )
    if branch_result.returncode != 0:
        raise GitPublishError("repository_state_error",
                               f"无法确定当前分支: {branch_result.stderr[-500:]}")
    branch = branch_result.stdout.strip()
    if branch != _PUBLISH_BRANCH:
        raise GitPublishError(
            "wrong_branch",
            f"当前不在{_PUBLISH_BRANCH}分支（实际: {branch or '无法确定，可能处于detached HEAD'}），拒绝发布",
        )

    git_dir_result = subprocess.run(
        ["git", "rev-parse", "--git-dir"],
        cwd=str(repo_dir), capture_output=True, text=True, encoding="utf-8", timeout=10,
    )
    if git_dir_result.returncode != 0:
        raise GitPublishError("repository_state_error",
                               f"无法确定.git目录位置: {git_dir_result.stderr[-500:]}")
    git_dir = Path(repo_dir) / git_dir_result.stdout.strip()

    for marker in _IN_PROGRESS_OP_MARKERS:
        if (git_dir / marker).exists():
            raise GitPublishError(
                "repository_busy",
                f"工作区存在进行中的Git操作({marker})，拒绝发布，需人工介入处理",
            )


def _ensure_askpass_executable() -> None:
    """B5：不依赖检出这一步就天然带着正确的可执行位——每次真正push之前
    都显式补一次。os.chmod在Windows上对这个位基本是no-op（Windows没有
    对应的Unix语义），在Linux/Mac上才真正生效，两边都不会抛异常，可以
    无条件调用，不需要按平台分支。
    """
    try:
        current_mode = GIT_ASKPASS_SCRIPT.stat().st_mode
        os.chmod(GIT_ASKPASS_SCRIPT, current_mode | 0o111)
    except OSError as e:
        # 不在这里抛异常掩盖真实的push失败原因：即使补权限失败，也应该让
        # 后续git push自然执行、自然报错，由_push()统一分类返回，而不是
        # 在这里提前短路成一个语义不明确的错误类别；但必须留痕，不能悄无
        # 声息（对应CLAUDE.md"绝不静默except: pass"的要求）。
        print(f"  [警告] 补充GIT_ASKPASS可执行权限失败(不影响后续push尝试自然报错): {e}")


def _push(repo_dir: Path, push_token: str, push_timeout_seconds: int) -> str:
    """总是被调用一次（不管这一轮subpath是否检测到变化），用git push自身
    fast-forward-only的默认行为区分三种情况，不需要额外的git fetch：

      返回"noop"    —— stderr出现"Everything up-to-date"：当前HEAD已经
                       和origin/master一致，真正的no-op（B2情形A）。
      返回"pushed"  —— exit 0且不是上面这种情况：确实推送了新内容，可能
                       是这一轮刚commit的，也可能是之前push失败遗留的
                       本地commit这次补上了（B2情形B——两者在git层面无法/
                       也无需区分，调用方看changed_file_count是否为0
                       即可分辨是哪一种）。
      抛出GitPublishError("remote_diverged", ...) —— exit非0且stderr是
                       标准的non-fast-forward/rejected提示：本地和远端
                       已经分叉，绝不自动force/rebase，原样报告分叉
                       （B2情形C）。
      抛出GitPublishError("git_push_error", ...) —— 其它push失败（认证/
                       网络/超时）。
    """
    _ensure_askpass_executable()
    push_env = dict(os.environ)
    push_env["GIT_ASKPASS"] = str(GIT_ASKPASS_SCRIPT)
    push_env["FOXZEN_GIT_PUSH_TOKEN"] = push_token
    push_env["GIT_TERMINAL_PROMPT"] = "0"  # 没有GIT_ASKPASS可用时绝不退回交互式终端提示

    try:
        result = subprocess.run(
            ["git", "push", "origin", _PUBLISH_BRANCH],
            cwd=str(repo_dir), capture_output=True, text=True, encoding="utf-8",
            timeout=push_timeout_seconds, env=push_env,
        )
    except subprocess.TimeoutExpired:
        raise GitPublishError("git_push_error", f"git push超时(>{push_timeout_seconds}s)，未继续占用锁")

    # S8修复：正常情况下GIT_ASKPASS机制决定push_token根本不会被git打印到
    # 自己的stderr里（见git_askpass_helper.py），这里仍然防御性地精确匹配
    # 这次调用实际使用的token值并替换——不依赖"git/日后的改动一定不会
    # 意外把它打印出来"这个假设，即使发生也不会流入下面任何一条错误详情
    # （包括最终写入refresh_locks/refresh_targets的内部诊断记录）。
    stderr = safe_errors.redact_known_secrets(result.stderr or "", push_token)
    if result.returncode == 0:
        if "up-to-date" in stderr or "up to date" in stderr:
            return "noop"
        return "pushed"

    # git对non-fast-forward拒绝的标准提示（长期稳定的核心措辞，不是本项目
    # 猜测的行为）：" ! [rejected] ... (non-fast-forward)"
    # + "error: failed to push some refs..."。只要出现这个组合，就是真正
    # 的分叉，不是认证/网络类的普通失败，需要单独归类，绝不能被当作可以
    # 简单重试解决的错误。
    if "[rejected]" in stderr and ("non-fast-forward" in stderr or "fetch first" in stderr):
        raise GitPublishError(
            "remote_diverged",
            f"本地HEAD与origin/{_PUBLISH_BRANCH}已分叉，拒绝自动覆盖，需人工介入: {stderr[-500:]}",
        )

    raise GitPublishError("git_push_error", f"git push失败: {stderr[-500:]}")


def commit_and_push(repo_dir: Path, subpath: str, author_name: str, author_email: str,
                     commit_message: str, push_token: str, push_timeout_seconds: int) -> dict:
    """校验仓库状态 -> 检测变化 -> (有变化才)add+commit(仅限subpath) ->
    无论这一轮是否有变化都尝试push一次。

    返回（永远不抛异常，所有失败都体现在返回值里，方便调用方统一处理）：
      {"pushed": True, "commit_sha": "<sha>", "changed_file_count": N,
       "push_state": "noop"}    # html/本轮无变化，且HEAD已等于origin/master
      {"pushed": True, "commit_sha": "<sha>", "changed_file_count": N,
       "push_state": "pushed"}  # 确实推送了新内容（N可能是0——见_push()文档
                                 # 里B2情形B的说明：本轮无变化但补推了遗留commit）
      {"pushed": False, "error_category":
          "wrong_branch" | "repository_busy" | "repository_state_error" |
          "git_commit_error" | "remote_diverged" | "git_push_error",
       "detail": "..."}
    """
    try:
        _check_publish_preconditions(repo_dir)
    except GitPublishError as e:
        return {"pushed": False, "error_category": e.error_category, "detail": e.detail}

    try:
        changed = detect_changed_paths(repo_dir, subpath)
    except GitPublishError as e:
        return {"pushed": False, "error_category": e.error_category, "detail": e.detail}

    if changed:
        add_result = subprocess.run(
            ["git", "add", "--", subpath], cwd=str(repo_dir), capture_output=True, text=True,
            encoding="utf-8", timeout=30,
        )
        if add_result.returncode != 0:
            return {"pushed": False, "error_category": "git_commit_error",
                    "detail": f"git add失败: {add_result.stderr[-500:]}"}

        commit_env = dict(os.environ)
        commit_env.update({
            "GIT_AUTHOR_NAME": author_name, "GIT_AUTHOR_EMAIL": author_email,
            "GIT_COMMITTER_NAME": author_name, "GIT_COMMITTER_EMAIL": author_email,
        })
        # --only + pathspec（B1修复）：只取subpath这部分内容入库，index里
        # 任何跟这次发布无关、可能已经被别的进程/人工staged的其它路径
        # 原封不动地留在index里，既不会被这次提交带走，也不会被丢弃。
        commit_result = subprocess.run(
            ["git", "commit", "--only", "-m", commit_message, "--", subpath],
            cwd=str(repo_dir), capture_output=True, text=True, encoding="utf-8",
            timeout=30, env=commit_env,
        )
        if commit_result.returncode != 0:
            return {"pushed": False, "error_category": "git_commit_error",
                    "detail": f"git commit失败: {commit_result.stderr[-500:]}"}

    # 无论这一轮subpath是否有变化都尝试push：changed为空不代表"没有需要
    # push的东西"——上一轮如果commit成功但push失败，本地会遗留一个从未
    # 推送的commit，这一轮subpath自然检测不出变化（工作树已经是干净的），
    # 但这个遗留commit仍然需要被推送出去，绝不能因为"本轮没有新变化"就
    # 直接判定no-op success而放弃重试push（B2）。
    try:
        push_state = _push(repo_dir, push_token, push_timeout_seconds)
    except GitPublishError as e:
        return {"pushed": False, "error_category": e.error_category, "detail": e.detail}

    return {"pushed": True, "commit_sha": current_head_sha(repo_dir),
            "changed_file_count": len(changed), "push_state": push_state}
