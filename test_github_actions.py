#!/usr/bin/env python3
"""github_actions.py的独立测试：http_call全部替换成返回预设JSON的假函数，
不会真的打GitHub REST API、不会真的触发任何workflow_dispatch。

覆盖：
- workflow_dispatch成功后正确识别对应run（S5修复后：按run-name精确匹配
  "Pages refresh {refresh_id}"，不再仅靠"event=workflow_dispatch+时间戳"
  这种弱关联——人工同时手动触发"Run workflow"、VPS时钟漂移都不会导致
  误认）
- run没有立刻出现在列表里时会重试，不会第一次查不到就判定失败
- 轮询到conclusion=success/failure两条路径
- 有界等待超时时返回outcome=timeout，不假装success
- dispatch请求本身失败时抛GitHubActionsError(error_category=workflow_dispatch_error)
- 一直定位不到匹配run-name的run时抛GitHubActionsError(error_category=run_identification_error)
- S4：run识别/conclusion轮询阶段反复的瞬时网络异常被转成类型化异常，不会
  以裸异常冒泡给调用方
- S5：人工手动触发的run（没有refresh_id，run-name是默认值）即使时间上
  更早/更接近，也绝不会被误认成本次自动化dispatch对应的run

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

    S5修复后，run-name的实际值依赖trigger_and_wait()内部随机生成的
    refresh_id（测试没法提前预知这个值），所以list_responses里的元素除了
    可以是普通dict，也可以是一个可调用对象——它会在真正被消费的那一刻，
    用dispatch请求体里实际携带的inputs现场构造响应，贴近真实GitHub API的
    行为（run-name的渲染值本来就依赖那次dispatch实际传的inputs，不是
    测试可以提前写死的常量）。
    """

    def __init__(self):
        self.list_responses = []   # 每次GET .../runs?event=workflow_dispatch 依次返回
        self.detail_responses = []  # 每次GET .../runs/{id} 依次返回
        self.calls = []
        self.dispatch_status = 204
        self.last_dispatch_inputs = None

    def __call__(self, method, url, token, body=None, timeout=15):
        self.calls.append((method, url, token, body))
        if method == "POST" and "/dispatches" in url:
            self.last_dispatch_inputs = (body or {}).get("inputs") or {}
            return self.dispatch_status, None
        if method == "GET" and "/runs?event=workflow_dispatch" in url:
            response = self.list_responses.pop(0)
            if callable(response):
                response = response(self.last_dispatch_inputs)
            return 200, response
        if method == "GET" and "/actions/runs/" in url:
            return 200, self.detail_responses.pop(0)
        raise AssertionError(f"未预期的调用: {method} {url}")


def _our_run(run_id, html_url=None, created_at=None):
    """构造一个callable，在真正被FakeGitHubAPI消费时才用实际拿到的
    refresh_id现场生成"确实是我们这次dispatch对应的run"这条记录——
    run-name字段(name/display_title)精确等于trigger_and_wait()内部生成
    的那个随机refresh_id对应的值，贴近pages.yml里run-name表达式的真实
    渲染结果。
    """
    def _build(refresh_id):
        now = datetime.now(timezone.utc)
        return {
            "id": run_id,
            "html_url": html_url or f"https://github.com/x/y/actions/runs/{run_id}",
            "created_at": _run_fmt(created_at or now),
            "event": "workflow_dispatch",
            "name": f"Pages refresh {refresh_id}",
            "display_title": f"Pages refresh {refresh_id}",
        }
    return _build


def _manual_run(run_id, created_at=None):
    """人工在GitHub网页上手动点"Run workflow"触发的run：没有填refresh_id
    这个可选输入，run-name渲染成workflow文件里定义的默认值，不可能意外
    匹配上我们生成的随机refresh_id。
    """
    now = datetime.now(timezone.utc)
    return {
        "id": run_id,
        "html_url": f"https://github.com/x/y/actions/runs/{run_id}",
        "created_at": _run_fmt(created_at or now),
        "event": "workflow_dispatch",
        "name": "Deploy GitHub Pages",
        "display_title": "Deploy GitHub Pages",
    }


def _list_response_with(*run_builders):
    """把_our_run()返回的callable和/或_manual_run()返回的普通dict混合
    在一起，构造list_responses里的一个callable元素。
    """
    def _build(dispatch_inputs):
        refresh_id = (dispatch_inputs or {}).get("refresh_id", "")
        runs = [b(refresh_id) if callable(b) else b for b in run_builders]
        return {"workflow_runs": runs}
    return _build


def test_success_path_identifies_run_and_polls_to_success():
    api = FakeGitHubAPI()
    api.list_responses = [_list_response_with(_our_run(111))]
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
    check("run_html_url正确透传", result["run_html_url"] == "https://github.com/x/y/actions/runs/111")

    dispatch_call = next(c for c in api.calls if c[0] == "POST" and "/dispatches" in c[1])
    _method, _url, _token, body = dispatch_call
    check("dispatch请求体里真的包含inputs.refresh_id，且是非空随机值（S5机制的前提）",
          bool((body or {}).get("inputs", {}).get("refresh_id")), body)


def test_run_not_immediately_visible_retries_then_succeeds():
    api = FakeGitHubAPI()
    # 前两次查询列表都是空的（模拟GitHub那边还没把新run写进列表的传播延迟），
    # 第三次才出现
    api.list_responses = [{"workflow_runs": []}, {"workflow_runs": []},
                           _list_response_with(_our_run(222))]
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
    api.list_responses = [_list_response_with(_our_run(333))]
    api.detail_responses = [{"status": "completed", "conclusion": "failure"}]

    result = github_actions.trigger_and_wait(
        "x/y", "pages.yml", "fake-token", wait_seconds=10, poll_interval_seconds=0.01,
        http_call=api,
    )
    check("真实conclusion=failure时outcome=failure，不伪装success",
          result["outcome"] == "failure" and result["conclusion"] == "failure", result)


def test_bounded_wait_timeout_does_not_claim_success():
    api = FakeGitHubAPI()
    api.list_responses = [_list_response_with(_our_run(444))]
    # 一直是in_progress，轮询到有界等待自然到期为止
    api.detail_responses = [{"status": "in_progress"}] * 1000

    result = github_actions.trigger_and_wait(
        "x/y", "pages.yml", "fake-token", wait_seconds=0.2, poll_interval_seconds=0.05,
        http_call=api,
    )
    check("有界等待到期后outcome=timeout，不是success也不是failure",
          result["outcome"] == "timeout", result)
    check("timeout结果仍然带有真实run_id/URL，供客户端自行确认",
          result["run_id"] == 444, result)


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
    被误认成这次dispatch触发的run——created_at晚于dispatch时刻的粗筛加上
    run-name精确匹配，双重保证不会选中它。
    """
    api = FakeGitHubAPI()
    now = datetime.now(timezone.utc)
    old_run = _manual_run(999, created_at=now - timedelta(hours=1))
    api.list_responses = [_list_response_with(old_run, _our_run(1000))]
    api.detail_responses = [{"status": "completed", "conclusion": "success"}]

    result = github_actions.trigger_and_wait(
        "x/y", "pages.yml", "fake-token", wait_seconds=10, poll_interval_seconds=0.01,
        http_call=api,
    )
    check("正确选中dispatch之后的新run，没有误认dispatch之前的旧run",
          result["run_id"] == 1000, result)


def test_run_name_correlation_correctly_identifies_our_run_among_others():
    """S5核心场景：本次自动化dispatch产生的run和一个几乎同一时刻出现的、
    人工手动触发的run同时出现在候选列表里——必须靠run-name精确匹配挑出
    真正对应本次dispatch的那一个，不能被"created_at最早"这类弱启发式
    误导（这里特意让manual_run的created_at比我们的更早，如果退回旧的
    纯时间戳启发式会选错）。
    """
    api = FakeGitHubAPI()
    now = datetime.now(timezone.utc)
    manual_run = _manual_run(5001, created_at=now)
    api.list_responses = [_list_response_with(manual_run, _our_run(5002, created_at=now))]
    api.detail_responses = [{"status": "completed", "conclusion": "success"}]

    result = github_actions.trigger_and_wait(
        "x/y", "pages.yml", "fake-token", wait_seconds=10, poll_interval_seconds=0.01, http_call=api,
    )
    check("即使人工手动run和自动化run同时出现、时间戳完全一样，也能靠run-name精确挑中我们自己的run",
          result["run_id"] == 5002, result)


def test_manual_run_with_earlier_timing_never_misidentified_as_ours():
    """人工手动触发的run(没有refresh_id，run-name是默认值)即使恰好created_at
    比我们的run更早、更符合旧启发式"取最早的那个"，也绝不会被误认。
    """
    api = FakeGitHubAPI()
    now = datetime.now(timezone.utc)
    manual_run = _manual_run(6001, created_at=now - timedelta(seconds=1))
    api.list_responses = [_list_response_with(manual_run, _our_run(6002, created_at=now))]
    api.detail_responses = [{"status": "completed", "conclusion": "success"}]

    result = github_actions.trigger_and_wait(
        "x/y", "pages.yml", "fake-token", wait_seconds=10, poll_interval_seconds=0.01, http_call=api,
    )
    check("人工手动run(创建时间更早，旧启发式会选它)没有被误认成我们的run",
          result["run_id"] == 6002, result)


def test_no_matching_run_name_raises_identification_error_not_wrong_guess():
    """列表里有run，但没有一个的run-name匹配我们这次的refresh_id（比如
    某种极端情况下workflow文件版本不一致、run-name没有正确渲染）：必须
    安全地报告run_identification_error，绝不能退回"随便选一个/选最早的
    那个"这种可能选错的旧行为。
    """
    api = FakeGitHubAPI()
    api.list_responses = [{"workflow_runs": [_manual_run(7001)]}] * 6

    real_sleep = github_actions.time.sleep
    github_actions.time.sleep = lambda s: real_sleep(0.001)
    try:
        try:
            github_actions.trigger_and_wait("x/y", "pages.yml", "fake-token", http_call=api)
            check("找不到run-name匹配的run时应该抛异常，不是随便选一个", False)
        except github_actions.GitHubActionsError as e:
            check("异常分类为run_identification_error",
                  e.error_category == "run_identification_error", e.error_category)
    finally:
        github_actions.time.sleep = real_sleep


def test_multiple_ambiguous_run_name_matches_raises_error_never_guesses():
    """复核阶段新增：如果(理论上因为refresh_id有64bit随机性而几乎不可能，
    但不能排除API异常等边界情况)同时有两个run的run-name都精确匹配本次
    refresh_id，绝不能像旧实现"挑最早的那个"一样悄悄猜一个——必须明确
    判定为run_identification_error，交给上层走失败路径，而不是可能选中
    错误的run却还报告成功。
    """
    api = FakeGitHubAPI()

    def _dup_response(dispatch_inputs):
        refresh_id = (dispatch_inputs or {}).get("refresh_id", "")
        return {"workflow_runs": [_our_run(8001)(refresh_id), _our_run(8002)(refresh_id)]}

    api.list_responses = [_dup_response]

    try:
        github_actions.trigger_and_wait("x/y", "pages.yml", "fake-token", http_call=api)
        check("出现多个run-name同时精确匹配的候选时应该抛异常，不是随便选一个", False)
    except github_actions.GitHubActionsError as e:
        check("异常分类为run_identification_error",
              e.error_category == "run_identification_error", e.error_category)
        check("异常详情里能看到两个候选run_id，方便人工排查",
              "8001" in e.detail and "8002" in e.detail, e.detail)


def test_matching_run_name_but_wrong_event_never_matched():
    """复核阶段新增：即使display_title字符串精确等于我们期望的run-name，
    如果event不是workflow_dispatch（比如以后pages.yml按注释里的计划加了
    push触发器之后，一次push恰好撞上同名run-name这种极端情况），也绝不能
    被当作我们这次dispatch对应的run——event必须同时满足，是run-name之外
    独立的第二层校验，不是单纯信任服务端URL的?event=查询参数一定生效。
    """
    api = FakeGitHubAPI()

    def _wrong_event_response(dispatch_inputs):
        refresh_id = (dispatch_inputs or {}).get("refresh_id", "")
        wrong_event_run = _our_run(8501)(refresh_id)
        wrong_event_run["event"] = "push"
        return {"workflow_runs": [wrong_event_run]}

    api.list_responses = [_wrong_event_response] * 6

    real_sleep = github_actions.time.sleep
    github_actions.time.sleep = lambda s: real_sleep(0.001)
    try:
        try:
            github_actions.trigger_and_wait("x/y", "pages.yml", "fake-token", http_call=api)
            check("run-name对了但event不对时不应该被当作我们的run，应该最终判定为找不到", False)
        except github_actions.GitHubActionsError as e:
            check("异常分类为run_identification_error",
                  e.error_category == "run_identification_error", e.error_category)
    finally:
        github_actions.time.sleep = real_sleep


def test_transient_http_error_during_run_identification_becomes_typed_failure():
    """S4：run识别阶段的http_call抛出普通异常(不是GitHubActionsError)时，
    不能原样冒泡成裸异常(在Flask里就是500)，必须被转成
    GitHubActionsError(run_identification_error)，让上层能正常记录failure。
    """
    call_count = {"n": 0}

    def flaky_http_call(method, url, token, body=None, timeout=15):
        if method == "POST":
            return 204, None
        if "/runs?event=workflow_dispatch" in url:
            call_count["n"] += 1
            raise ConnectionResetError("模拟一次瞬时网络异常")
        raise AssertionError("不应该走到这里")

    real_sleep = github_actions.time.sleep
    github_actions.time.sleep = lambda s: real_sleep(0.001)
    try:
        try:
            github_actions.trigger_and_wait("x/y", "pages.yml", "fake-token", http_call=flaky_http_call)
            check("run识别阶段反复网络异常时应该抛类型化异常，不是裸异常直接冒泡", False)
        except github_actions.GitHubActionsError as e:
            check("异常分类为run_identification_error（不是ConnectionResetError本身冒泡出去）",
                  e.error_category == "run_identification_error", e.error_category)
        except Exception as e:
            check(f"不应该冒泡出裸异常类型: {type(e).__name__}", False)
    finally:
        github_actions.time.sleep = real_sleep
    check("确实重试了多次而不是第一次异常就放弃", call_count["n"] > 1, call_count["n"])


def test_transient_http_error_during_conclusion_polling_becomes_typed_failure():
    """S4：conclusion轮询阶段(_poll_until，trigger_and_wait和
    poll_until_conclusion共用)遇到一次瞬时异常后如果后续恢复正常，应该能
    继续正常识别出真实conclusion，不是异常一次就整体放弃——只有反复失败
    才会转成GitHubActionsError抛出。
    """
    api = FakeGitHubAPI()
    api.list_responses = [_list_response_with(_our_run(9001))]
    api.detail_responses = [{"status": "completed", "conclusion": "success"}]

    poll_calls = {"n": 0}
    real_call = api.__call__

    def flaky_then_recovers(method, url, token, body=None, timeout=15):
        if method == "GET" and "/actions/runs/" in url:
            poll_calls["n"] += 1
            if poll_calls["n"] == 1:
                raise TimeoutError("模拟一次瞬时轮询异常")
        return real_call(method, url, token, body=body, timeout=timeout)

    result = github_actions.trigger_and_wait(
        "x/y", "pages.yml", "fake-token", wait_seconds=10, poll_interval_seconds=0.01,
        http_call=flaky_then_recovers,
    )
    check("轮询阶段一次瞬时异常之后能恢复并正确识别真实conclusion，不是直接冒泡异常",
          result["outcome"] == "success", result)
    check("确实先经历了一次异常才成功（不是巧合从没触发过异常分支）",
          poll_calls["n"] >= 2, poll_calls["n"])


def test_run_identification_transient_error_detail_never_contains_the_real_token():
    """S8要求2的独立防线：_find_dispatched_run()对http_call异常的处理正常
    情况下不会让token出现在异常文本里（token通过HTTP头传递，不会被
    urllib的异常字符串带出来），但这里不依赖这个假设——构造一个http_call，
    让它抛出的异常字符串里"假设"真的包含了这次调用实际使用的token，验证
    最终转成的GitHubActionsError.detail里这个真实token已经被redact掉。
    """
    real_token = "ghp_THIS_IS_THE_REAL_TOKEN_VALUE_FOR_THIS_TEST"

    def leaky_http_call(method, url, token, body=None, timeout=15):
        if method == "POST":
            return 204, None
        raise ConnectionResetError(f"connection reset, Authorization: Bearer {token}")

    real_sleep = github_actions.time.sleep
    github_actions.time.sleep = lambda s: real_sleep(0.001)
    try:
        try:
            github_actions.trigger_and_wait("x/y", "pages.yml", real_token, http_call=leaky_http_call)
            check("run识别阶段反复异常时应该抛异常", False)
        except github_actions.GitHubActionsError as e:
            check("异常分类为run_identification_error", e.error_category == "run_identification_error")
            check("即使(假设的)异常文本真的包含了真实token，detail里也不包含它",
                  real_token not in e.detail, e.detail)
            check("detail仍然保留了其它诊断信息，不是整段被吞掉",
                  "connection reset" in e.detail, e.detail)
    finally:
        github_actions.time.sleep = real_sleep


def test_conclusion_polling_transient_error_detail_never_contains_the_real_token():
    """同上，验证_poll_until()那一侧（trigger_and_wait/poll_until_conclusion
    共用）的同一道防线。"""
    real_token = "ghp_THIS_IS_THE_REAL_TOKEN_VALUE_FOR_THIS_TEST"
    api = FakeGitHubAPI()
    api.list_responses = [_list_response_with(_our_run(9101))]

    def leaky_detail_call(method, url, token, body=None, timeout=15):
        if method == "GET" and "/actions/runs/" in url:
            raise TimeoutError(f"poll timed out, Authorization: Bearer {token}")
        return api(method, url, token, body=body, timeout=timeout)

    real_sleep = github_actions.time.sleep
    github_actions.time.sleep = lambda s: real_sleep(0.001)
    try:
        try:
            github_actions.trigger_and_wait("x/y", "pages.yml", real_token,
                                             wait_seconds=0.05, poll_interval_seconds=0.01,
                                             http_call=leaky_detail_call)
            check("轮询阶段反复异常时应该抛异常", False)
        except github_actions.GitHubActionsError as e:
            check("异常分类为actions_poll_error", e.error_category == "actions_poll_error", e.error_category)
            check("即使(假设的)异常文本真的包含了真实token，detail里也不包含它",
                  real_token not in e.detail, e.detail)
    finally:
        github_actions.time.sleep = real_sleep


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
        test_run_name_correlation_correctly_identifies_our_run_among_others,
        test_manual_run_with_earlier_timing_never_misidentified_as_ours,
        test_no_matching_run_name_raises_identification_error_not_wrong_guess,
        test_multiple_ambiguous_run_name_matches_raises_error_never_guesses,
        test_matching_run_name_but_wrong_event_never_matched,
        test_run_identification_transient_error_detail_never_contains_the_real_token,
        test_conclusion_polling_transient_error_detail_never_contains_the_real_token,
        test_transient_http_error_during_run_identification_becomes_typed_failure,
        test_transient_http_error_during_conclusion_polling_becomes_typed_failure,
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
