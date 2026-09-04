#!/usr/bin/env python3
"""github_actions.py的独立测试：http_call全部替换成返回预设JSON的假函数，
不会真的打GitHub REST API、不会真的触发任何workflow_dispatch。

覆盖：
- workflow_dispatch成功后正确识别对应run（按event=workflow_dispatch+
  时间戳过滤，取dispatch之后最早出现的那个）
- run没有立刻出现在列表里时会重试，不会第一次查不到就判定失败
- 轮询到conclusion=success/failure两条路径
- 有界等待超时时返回outcome=timeout，不假装success
- dispatch请求本身失败时抛GitHubActionsError(error_category=workflow_dispatch_error)
- 一直定位不到run时抛GitHubActionsError(error_category=run_identification_error)

用法: python3 test_github_actions.py
"""
import sys
import traceback
import urllib.error
from datetime import datetime, timedelta, timezone

import github_actions

failures = []


def check(name, condition, detail=""):
    if condition:
        print(f"  [PASS] {name}")
    else:
        print(f"  [FAIL] {name} {detail}")
        failures.append(name)


def _run_fmt(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


class FakeGitHubAPI:
    """按调用顺序返回预设响应的假http_call。dispatch固定204；run列表/run详情
    分别从一个可预先塞好的队列里按顺序弹出，让测试精确控制"第几次轮询才
    看到run出现""第几次轮询才completed"这类时序。
    """

    def __init__(self):
        self.list_responses = []   # 每次GET .../runs?event=workflow_dispatch 依次返回
        self.detail_responses = []  # 每次GET .../runs/{id} 依次返回
        self.calls = []
        self.dispatch_status = 204

    def __call__(self, method, url, token, body=None, timeout=15):
        self.calls.append((method, url, token, body))
        if method == "POST" and "/dispatches" in url:
            return self.dispatch_status, None
        if method == "GET" and "/runs?event=workflow_dispatch" in url:
            return 200, self.list_responses.pop(0)
        if method == "GET" and "/actions/runs/" in url:
            return 200, self.detail_responses.pop(0)
        raise AssertionError(f"未预期的调用: {method} {url}")


def test_success_path_identifies_run_and_polls_to_success():
    api = FakeGitHubAPI()
    now = datetime.now(timezone.utc)
    run = {"id": 111, "html_url": "https://github.com/x/y/actions/runs/111",
           "created_at": _run_fmt(now), "event": "workflow_dispatch"}
    api.list_responses = [{"workflow_runs": [run]}]
    api.detail_responses = [
        {"status": "in_progress"},
        {"status": "completed", "conclusion": "success"},
    ]
    result = github_actions.trigger_and_wait(
        "x/y", "pages.yml", "fake-token", wait_seconds=10, poll_interval_seconds=0.01,
        http_call=api,
    )
    check("成功识别run_id", result["run_id"] == 111)
    check("outcome=success", result["outcome"] == "success", result)
    check("run_html_url正确透传", result["run_html_url"] == run["html_url"])


def test_run_not_immediately_visible_retries_then_succeeds():
    api = FakeGitHubAPI()
    now = datetime.now(timezone.utc)
    run = {"id": 222, "html_url": "https://github.com/x/y/actions/runs/222",
           "created_at": _run_fmt(now), "event": "workflow_dispatch"}
    # 前两次查询列表都是空的（模拟GitHub那边还没把新run写进列表的传播延迟），
    # 第三次才出现
    api.list_responses = [{"workflow_runs": []}, {"workflow_runs": []}, {"workflow_runs": [run]}]
    api.detail_responses = [{"status": "completed", "conclusion": "success"}]

    real_sleep = github_actions.time.sleep
    github_actions.time.sleep = lambda s: real_sleep(0.001)
    try:
        result = github_actions.trigger_and_wait(
            "x/y", "pages.yml", "fake-token", wait_seconds=10, poll_interval_seconds=0.01,
            http_call=api,
        )
    finally:
        github_actions.time.sleep = real_sleep
    check("重试几次后仍能正确识别run", result["run_id"] == 222, result)
    check("outcome=success", result["outcome"] == "success")


def test_conclusion_failure_reported_honestly():
    api = FakeGitHubAPI()
    now = datetime.now(timezone.utc)
    run = {"id": 333, "html_url": "https://github.com/x/y/actions/runs/333",
           "created_at": _run_fmt(now), "event": "workflow_dispatch"}
    api.list_responses = [{"workflow_runs": [run]}]
    api.detail_responses = [{"status": "completed", "conclusion": "failure"}]

    result = github_actions.trigger_and_wait(
        "x/y", "pages.yml", "fake-token", wait_seconds=10, poll_interval_seconds=0.01,
        http_call=api,
    )
    check("真实conclusion=failure时outcome=failure，不伪装success",
          result["outcome"] == "failure" and result["conclusion"] == "failure", result)


def test_bounded_wait_timeout_does_not_claim_success():
    api = FakeGitHubAPI()
    now = datetime.now(timezone.utc)
    run = {"id": 444, "html_url": "https://github.com/x/y/actions/runs/444",
           "created_at": _run_fmt(now), "event": "workflow_dispatch"}
    api.list_responses = [{"workflow_runs": [run]}]
    # 一直是in_progress，轮询到有界等待自然到期为止
    api.detail_responses = [{"status": "in_progress"}] * 1000

    result = github_actions.trigger_and_wait(
        "x/y", "pages.yml", "fake-token", wait_seconds=0.2, poll_interval_seconds=0.05,
        http_call=api,
    )
    check("有界等待到期后outcome=timeout，不是success也不是failure",
          result["outcome"] == "timeout", result)
    check("timeout结果仍然带有真实run_id/URL，供客户端自行确认",
          result["run_id"] == 444 and result["run_html_url"] == run["html_url"], result)


def test_dispatch_itself_failing_raises_typed_error():
    api = FakeGitHubAPI()
    api.dispatch_status = 404

    try:
        github_actions.trigger_and_wait("x/y", "pages.yml", "fake-token", http_call=api)
        check("dispatch返回非204时应该抛异常", False)
    except github_actions.GitHubActionsError as e:
        check("异常分类为workflow_dispatch_error", e.error_category == "workflow_dispatch_error", e.error_category)


def test_dispatch_http_error_raises_typed_error():
    def raising_http_call(method, url, token, body=None, timeout=15):
        if method == "POST":
            raise urllib.error.HTTPError(url, 401, "Unauthorized", {}, None)
        raise AssertionError("不应该走到这里")

    try:
        github_actions.trigger_and_wait("x/y", "pages.yml", "bad-token", http_call=raising_http_call)
        check("dispatch请求异常时应该抛异常", False)
    except github_actions.GitHubActionsError as e:
        check("异常分类为workflow_dispatch_error（HTTP层错误）",
              e.error_category == "workflow_dispatch_error", e.error_category)


def test_run_never_appears_raises_identification_error():
    api = FakeGitHubAPI()
    api.list_responses = [{"workflow_runs": []}] * 20  # 一直找不到

    real_sleep = github_actions.time.sleep
    github_actions.time.sleep = lambda s: real_sleep(0.001)
    try:
        try:
            github_actions.trigger_and_wait("x/y", "pages.yml", "fake-token", http_call=api)
            check("一直定位不到run时应该抛异常", False)
        except github_actions.GitHubActionsError as e:
            check("异常分类为run_identification_error", e.error_category == "run_identification_error",
                  e.error_category)
    finally:
        github_actions.time.sleep = real_sleep


def test_stale_unrelated_run_not_mistaken_for_ours():
    """dispatch之前就已经存在的旧run（比如别人很久以前手动点过一次）不应该
    被误认成这次dispatch触发的run——必须按created_at晚于dispatch时刻过滤。
    """
    api = FakeGitHubAPI()
    now = datetime.now(timezone.utc)
    old_run = {"id": 999, "html_url": "https://github.com/x/y/actions/runs/999",
               "created_at": _run_fmt(now - timedelta(hours=1)), "event": "workflow_dispatch"}
    new_run = {"id": 1000, "html_url": "https://github.com/x/y/actions/runs/1000",
               "created_at": _run_fmt(now), "event": "workflow_dispatch"}
    api.list_responses = [{"workflow_runs": [old_run, new_run]}]
    api.detail_responses = [{"status": "completed", "conclusion": "success"}]

    result = github_actions.trigger_and_wait(
        "x/y", "pages.yml", "fake-token", wait_seconds=10, poll_interval_seconds=0.01,
        http_call=api,
    )
    check("正确选中dispatch之后的新run，没有误认dispatch之前的旧run",
          result["run_id"] == 1000, result)


def main():
    tests = [
        test_success_path_identifies_run_and_polls_to_success,
        test_run_not_immediately_visible_retries_then_succeeds,
        test_conclusion_failure_reported_honestly,
        test_bounded_wait_timeout_does_not_claim_success,
        test_dispatch_itself_failing_raises_typed_error,
        test_dispatch_http_error_raises_typed_error,
        test_run_never_appears_raises_identification_error,
        test_stale_unrelated_run_not_mistaken_for_ours,
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
