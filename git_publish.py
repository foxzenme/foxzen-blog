#!/usr/bin/env python3
"""把GreenCloud本地某个子目录（生产环境固定是html/）的实际变化commit+push到
GitHub master的最小实现，只被app.py里github/cf两个refresh target调用。

安全边界（本模块存在的核心原因，不是随手抽取的公共函数）：
- 只会对`git add`传固定的目录参数（生产环境固定是"html"），从不是
  `git add .`/`git add -A`——绝不会碰这个目录之外的任何working tree改动，
  包括这个仓库里此刻可能还躺着的、别人正在进行的其它未提交工作。
- 所有subprocess调用都是固定的参数列表，不经过shell（不传shell=True），
  不把任何外部输入（target名、文章标题、文件名……）拼接进命令本身——
  target/repo_dir/subpath全部来自调用方在Python层面已经校验过的固定值，
  从不是直接来自HTTP请求体的原始字符串。
- push认证只通过GIT_ASKPASS+环境变量传递（见git_askpass_helper.py），
  token不出现在argv、不写入remote URL、不写入.git/config、不出现在
  这个函数返回值以外的任何地方。

repo_dir/subpath都是显式参数（不是硬编码的常量），方便测试指向一次性的
临时git仓库——这个模块本身不知道、也不关心BASE_DIR是什么。
"""
import os
import subprocess
from pathlib import Path

GIT_ASKPASS_SCRIPT = Path(__file__).parent / "git_askpass_helper.py"


class GitPublishError(Exception):
    def __init__(self, error_category: str, detail: str):
        self.error_category = error_category
        self.detail = detail
        super().__init__(detail)


def detect_changed_paths(repo_dir: Path, subpath: str) -> list[str]:
    """返回subpath目录下相对repo_dir实际有变化（新增/修改/删除）的路径列表。
    空列表代表没有任何变化，调用方应据此跳过commit（不产生空commit）。
    """
    result = subprocess.run(
        ["git", "status", "--porcelain", "--", subpath],
        cwd=str(repo_dir), capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        raise GitPublishError("git_commit_error", f"git status失败: {result.stderr[-500:]}")
    return [line[3:].strip() for line in result.stdout.splitlines() if line.strip()]


def current_head_sha(repo_dir: Path) -> str | None:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(repo_dir), capture_output=True, text=True, timeout=10,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def commit_and_push(repo_dir: Path, subpath: str, author_name: str, author_email: str,
                     commit_message: str, push_token: str, push_timeout_seconds: int) -> dict:
    """检测变化 -> (有变化才)add+commit -> (有变化才)push。

    返回（永远不抛异常，所有失败都体现在返回值里，方便调用方统一处理）：
      {"pushed": True, "commit_sha": "<sha>", "changed_file_count": N>0}   # 有变化，已提交并推送
      {"pushed": True, "commit_sha": "<当前HEAD sha>", "changed_file_count": 0}  # 无变化，未commit/push
      {"pushed": False, "error_category": "git_commit_error"|"git_push_error", "detail": "..."}
    """
    try:
        changed = detect_changed_paths(repo_dir, subpath)
    except GitPublishError as e:
        return {"pushed": False, "error_category": e.error_category, "detail": e.detail}

    if not changed:
        return {"pushed": True, "commit_sha": current_head_sha(repo_dir), "changed_file_count": 0}

    add_result = subprocess.run(
        ["git", "add", "--", subpath], cwd=str(repo_dir), capture_output=True, text=True, timeout=30,
    )
    if add_result.returncode != 0:
        return {"pushed": False, "error_category": "git_commit_error",
                "detail": f"git add失败: {add_result.stderr[-500:]}"}

    commit_env = dict(os.environ)
    commit_env.update({
        "GIT_AUTHOR_NAME": author_name, "GIT_AUTHOR_EMAIL": author_email,
        "GIT_COMMITTER_NAME": author_name, "GIT_COMMITTER_EMAIL": author_email,
    })
    commit_result = subprocess.run(
        ["git", "commit", "-m", commit_message],
        cwd=str(repo_dir), capture_output=True, text=True, timeout=30, env=commit_env,
    )
    if commit_result.returncode != 0:
        return {"pushed": False, "error_category": "git_commit_error",
                "detail": f"git commit失败: {commit_result.stderr[-500:]}"}

    commit_sha = current_head_sha(repo_dir)

    push_env = dict(os.environ)
    push_env["GIT_ASKPASS"] = str(GIT_ASKPASS_SCRIPT)
    push_env["FOXZEN_GIT_PUSH_TOKEN"] = push_token
    push_env["GIT_TERMINAL_PROMPT"] = "0"  # 没有GIT_ASKPASS可用时绝不退回交互式终端提示

    try:
        push_result = subprocess.run(
            ["git", "push", "origin", "master"],
            cwd=str(repo_dir), capture_output=True, text=True,
            timeout=push_timeout_seconds, env=push_env,
        )
    except subprocess.TimeoutExpired:
        return {"pushed": False, "error_category": "git_push_error",
                "detail": f"git push超时(>{push_timeout_seconds}s)，未继续占用锁"}

    if push_result.returncode != 0:
        return {"pushed": False, "error_category": "git_push_error",
                "detail": f"git push失败: {push_result.stderr[-500:]}"}

    return {"pushed": True, "commit_sha": commit_sha, "changed_file_count": len(changed)}
