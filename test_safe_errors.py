#!/usr/bin/env python3
"""safe_errors.py的独立测试：纯函数，不涉及数据库/网络/子进程，只验证
两条规则本身——S8的完整回归测试（真实HTTP response/status端点）见
test_refresh_lock.py，这里只测这个模块自己的核心逻辑。

用法: python3 test_safe_errors.py
"""
import sys
import traceback

import safe_errors

failures = []


def check(name, condition, detail=""):
    if condition:
        print(f"  [PASS] {name}")
    else:
        print(f"  [FAIL] {name} {detail}")
        failures.append(name)


# 跟app.py/git_publish.py/github_actions.py里实际会产生的error_category
# 保持一致——这张表本身就是防止"新增了一个error_category但忘了在
# safe_errors.py里补一条模板"这类疏漏的回归清单。
_ALL_KNOWN_ERROR_CATEGORIES = [
    "content_fetch_error", "git_commit_error", "git_push_error", "wrong_branch",
    "repository_busy", "repository_state_error", "remote_diverged", "credentials_missing",
    "workflow_dispatch_error", "run_identification_error", "actions_poll_error",
    "actions_run_failed", "actions_run_unresolved", "internal_error",
]


def test_every_known_error_category_has_a_fixed_safe_template():
    for category in _ALL_KNOWN_ERROR_CATEGORIES:
        result = safe_errors.safe_public_detail("failure", category)
        check(f"error_category={category}有固定模板，不是通用兜底文案",
              result != "任务失败", result)
        check(f"error_category={category}的模板本身不是空字符串",
              bool(result), result)


def test_unknown_error_category_falls_back_to_generic_failure_not_raw_text():
    result = safe_errors.safe_public_detail("failure", "some_future_category_not_yet_mapped")
    check("未登记的error_category退回通用失败文案，不是None/异常/原始文本",
          result == "任务失败", result)

    result_none = safe_errors.safe_public_detail("failure", None)
    check("error_category=None时同样安全退回通用失败文案",
          result_none == "任务失败", result_none)


def test_success_status_always_returns_fixed_generic_text_regardless_of_category():
    result = safe_errors.safe_public_detail("success", None)
    check("status=success时返回固定通用成功文案", result == "任务成功", result)

    # error_category在success时本来就不该有意义，但即使误传了也不能被
    # 拿去做任何查表/拼接，必须仍然是同一个固定文案。
    result2 = safe_errors.safe_public_detail("success", "git_push_error")
    check("success时即使意外带了error_category也不影响返回固定成功文案",
          result2 == "任务成功", result2)


def test_unexpected_status_value_falls_back_safely_without_raising():
    result = safe_errors.safe_public_detail("running", None)
    check("未预期的status值不抛异常，安全退回", isinstance(result, str) and bool(result), result)


def test_redact_known_secrets_replaces_exact_value_only():
    text = "fatal: Authorization failed for token ghp_REALSECRET123, remote rejected"
    redacted = safe_errors.redact_known_secrets(text, "ghp_REALSECRET123")
    check("精确匹配的secret值被替换", "ghp_REALSECRET123" not in redacted, redacted)
    check("其它诊断文本原样保留，不是整段被吞掉",
          "Authorization failed" in redacted and "remote rejected" in redacted, redacted)


def test_redact_known_secrets_does_not_touch_unrelated_lookalike_text():
    """精确匹配已知具体值，不是"看起来像token就替换"——这是设计上刻意的
    取舍（不会因为凭据格式变化而漏判已知值，但也不会误伤跟已知secret值
    不同的普通文本，即使它长得也像一个token）。
    """
    text = "error mentions ghp_SOME_OTHER_UNRELATED_STRING but not our real token"
    redacted = safe_errors.redact_known_secrets(text, "ghp_REALSECRET123")
    check("跟已知secret值不同的普通文本不受影响", redacted == text, redacted)


def test_redact_known_secrets_handles_multiple_secrets_and_empty_values():
    text = "token1=AAA token2=BBB unrelated=CCC"
    redacted = safe_errors.redact_known_secrets(text, "AAA", "", None, "BBB")
    check("多个已知secret值都被替换，空/None值被安全忽略",
          "AAA" not in redacted and "BBB" not in redacted and "CCC" in redacted, redacted)


def test_redact_known_secrets_handles_empty_or_none_text():
    check("text为空字符串时原样返回，不抛异常", safe_errors.redact_known_secrets("", "secret") == "")
    check("text为None时原样返回，不抛异常", safe_errors.redact_known_secrets(None, "secret") is None)


def main():
    tests = [
        test_every_known_error_category_has_a_fixed_safe_template,
        test_unknown_error_category_falls_back_to_generic_failure_not_raw_text,
        test_success_status_always_returns_fixed_generic_text_regardless_of_category,
        test_unexpected_status_value_falls_back_safely_without_raising,
        test_redact_known_secrets_replaces_exact_value_only,
        test_redact_known_secrets_does_not_touch_unrelated_lookalike_text,
        test_redact_known_secrets_handles_multiple_secrets_and_empty_values,
        test_redact_known_secrets_handles_empty_or_none_text,
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
