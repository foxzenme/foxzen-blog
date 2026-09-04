#!/usr/bin/env python3
"""封装GitHub Actions workflow_dispatch触发 + run识别 + conclusion轮询，只被
app.py里的github这一个refresh target调用。

已知的GitHub API限制（不是需要猜测的行为，是文档化的接口行为）：
POST .../workflows/{id}/dispatches 只返回204 No Content，响应体里没有run id。
这里用标准的规避方式：dispatch前记录时间戳，dispatch后轮询run列表，找
"dispatch之后第一个出现的、event=workflow_dispatch的run"，视为对应本次调用
（pages.yml目前只有workflow_dispatch一种触发方式，误认的唯一场景是人工
和自动化在同一个窗口内同时触发，概率很低但不是零，见完成报告的风险说明）。

http_call做成可替换参数（默认是_default_http_call，真的打GitHub REST API），
测试传入一个返回预设JSON的假函数即可完全离线验证轮询/识别逻辑，不会在
测试里真的触发GitHub Actions。
"""
import json
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

_CLOCK_SKEW_MARGIN = timedelta(seconds=2)


class GitHubActionsError(Exception):
    def __init__(self, error_category: str, detail: str):
        self.error_category = error_category
        self.detail = detail
        super().__init__(detail)


def _default_http_call(method: str, url: str, token: str, body: dict = None, timeout: int = 15):
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        return resp.status, (json.loads(raw.decode("utf-8")) if raw else None)


def _parse_gh_time(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def _find_dispatched_run(repo, workflow_file, token, dispatched_at, http_call,
                          max_attempts=6, retry_interval_seconds=2.0):
    """dispatch到run出现在列表里之间有几秒传播延迟，需要重试几次、短退避，
    不能只查一次就判定失败。找不到时返回None（调用方转成run_identification_error）。
    """
    for _ in range(max_attempts):
        _status, data = http_call(
            "GET",
            f"https://api.github.com/repos/{repo}/actions/workflows/{workflow_file}/runs"
            f"?event=workflow_dispatch&per_page=5",
            token,
        )
        candidates = [
            r for r in (data or {}).get("workflow_runs", [])
            if _parse_gh_time(r["created_at"]) >= dispatched_at - _CLOCK_SKEW_MARGIN
        ]
        if candidates:
            return min(candidates, key=lambda r: r["created_at"])
        time.sleep(retry_interval_seconds)
    return None


def _poll_until(repo, run_id, run_html_url, token, wait_seconds, poll_interval_seconds, http_call):
    """真正的轮询循环，trigger_and_wait()的有界等待和poll_until_conclusion()
    的后台长等待共用同一份逻辑，只是wait_seconds不同——避免两处各写一份、
    以后改了轮询细节只改了一边。

    返回：
      {"outcome": "success", "run_id": int, "run_html_url": str}
      {"outcome": "failure", "run_id": int, "run_html_url": str, "conclusion": str}
      {"outcome": "timeout", "run_id": int, "run_html_url": str}
    """
    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        _status, run_detail = http_call(
            "GET", f"https://api.github.com/repos/{repo}/actions/runs/{run_id}", token,
        )
        if (run_detail or {}).get("status") == "completed":
            conclusion = run_detail.get("conclusion")
            if conclusion == "success":
                return {"outcome": "success", "run_id": run_id, "run_html_url": run_html_url}
            return {"outcome": "failure", "run_id": run_id, "run_html_url": run_html_url,
                    "conclusion": conclusion}
        time.sleep(poll_interval_seconds)
    return {"outcome": "timeout", "run_id": run_id, "run_html_url": run_html_url}


def trigger_and_wait(repo: str, workflow_file: str, token: str, *,
                      ref: str = "master", wait_seconds: int = 90,
                      poll_interval_seconds: float = 3.0,
                      http_call=_default_http_call) -> dict:
    """触发workflow_dispatch，定位对应run，在有界时间内轮询到conclusion。

    返回值/outcome含义同_poll_until()。**"outcome": "timeout"不代表没人
    再关心这个run了**——调用方（app.py的refresh_target()）在这种情况下
    会另外启动一个后台线程调用poll_until_conclusion()继续跟踪，见那边的
    调用点和注释；这个函数本身只负责"这次同步HTTP请求最多等多久"。

    抛出：
      GitHubActionsError(error_category, detail) —— dispatch请求本身失败，
          或dispatch成功但无法可靠定位到对应run。
    """
    dispatched_at = datetime.now(timezone.utc)

    try:
        status, _ = http_call(
            "POST",
            f"https://api.github.com/repos/{repo}/actions/workflows/{workflow_file}/dispatches",
            token, body={"ref": ref},
        )
    except urllib.error.HTTPError as e:
        raise GitHubActionsError("workflow_dispatch_error", f"dispatch请求被拒绝: HTTP {e.code}")
    except Exception as e:
        raise GitHubActionsError("workflow_dispatch_error", f"dispatch请求失败: {e}")
    if status != 204:
        raise GitHubActionsError("workflow_dispatch_error", f"dispatch返回非预期状态码: {status}")

    run = _find_dispatched_run(repo, workflow_file, token, dispatched_at, http_call)
    if run is None:
        raise GitHubActionsError("run_identification_error", "dispatch后未能定位到对应的workflow run")

    return _poll_until(repo, run["id"], run["html_url"], token, wait_seconds, poll_interval_seconds, http_call)


def poll_until_conclusion(repo: str, run_id: int, run_html_url: str, token: str, *,
                           max_wait_seconds: int, poll_interval_seconds: float = 5.0,
                           http_call=_default_http_call) -> dict:
    """供后台watcher使用：已经知道具体run_id（trigger_and_wait()返回timeout
    时带出来的那个），不需要重新dispatch、不需要重新做run识别，只管继续
    轮询这一个run直到它真正completed，或者等到max_wait_seconds这个更长的
    上限也到期为止（这里的上限应该明显比trigger_and_wait()的有界等待长，
    因为这次不占用HTTP worker，只占用一个后台线程）。

    返回值/outcome含义同_poll_until()——"timeout"在这里的意思是"连这次
    更长的后台等待也放弃了"，调用方(app.py)据此把结果记为一个明确的
    "长时间未产出结论"的failure，而不是无限期挂起或者假装成功。
    """
    return _poll_until(repo, run_id, run_html_url, token, max_wait_seconds, poll_interval_seconds, http_call)
