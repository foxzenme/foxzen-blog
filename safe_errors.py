#!/usr/bin/env python3
"""S8修复：匿名公开的/api/refresh/*接口（POST的直接响应 + GET .../status）
绝不能把原始subprocess stderr、服务器绝对路径、内部Git远程URL、Blogger
feed URL、或任何形式的credential透传给调用方——这些接口对任何人都是匿名
可访问的，原始错误文本里实际会出现什么完全不受控制（取决于当时具体是
git/subprocess/urllib返回了什么）。

这个模块是"内部真实错误"到"对外安全摘要"之间唯一被允许穿越的窄缝：
app.py构造POST直接响应、db.get_target_status()构造GET .../status响应，
都必须调用safe_public_detail()，不允许各自维护一份、容易遗漏或不一致的
对外文案。

真正的诊断信息（原始stderr、异常文本、服务器路径、git远程URL）仍然完整
写入refresh_locks/refresh_targets两张表——那是运维通过SSH/sqlite3直接
查看的内部诊断通道，不是这个模块要挡的对象；这个模块只挡"这些内容绝不
能经由HTTP响应出去"。

redact_known_secrets()是另一道独立防线，用在git_publish.py/github_actions.py/
app.py构造detail文本、以及写入上面说的"内部诊断通道"之前：不是因为设计上
token应该出现在这些文本里（GIT_ASKPASS机制决定push token正常情况下根本
不会流入git的stderr，见git_askpass_helper.py；GitHub/Cloudflare token都
通过HTTP头传递，也不会流入urllib异常文本），而是防御性地保证"即使以后
某处改动意外引入了新的泄露路径"，credential也不会被写进任何存储（哪怕是
内部诊断表）——精确匹配已知的secret具体值再替换，而不是猜测"什么样的
字符串长得像token"，前者不会因为凭据格式变化（GitHub PAT前缀、换成别的
认证方式）而漏判，后者既容易漏判也容易误伤正常文本。
"""

# error_category -> 对外展示的固定中文摘要。只允许在这个字典里增删；任何
# 调用方都不应该在这之外自己为某个失败拼一条新的对外文案，否则又会退回
# "每个调用点各自决定该不该带敏感信息"的老问题。
_SAFE_FAILURE_DETAIL_BY_CATEGORY = {
    "content_fetch_error": "内容抓取失败",
    "git_commit_error": "Git 提交失败",
    "git_push_error": "Git 推送失败",
    "wrong_branch": "服务器仓库当前不在预期分支，已拒绝发布",
    "repository_busy": "服务器仓库正在进行其它 Git 操作，已拒绝发布",
    "repository_state_error": "无法确认服务器仓库状态，已拒绝发布",
    "remote_diverged": "本地与远端仓库历史已分叉，已拒绝自动覆盖，需人工介入",
    "credentials_missing": "服务器未配置必要凭据",
    "workflow_dispatch_error": "GitHub Actions 触发失败",
    "run_identification_error": "无法确认本次触发对应的 GitHub Actions 运行",
    "actions_poll_error": "查询 GitHub Actions 运行状态时反复出错",
    "actions_run_failed": "GitHub Actions 执行失败",
    "actions_run_unresolved": "任务超时",
    "internal_error": "服务器处理时发生未预期错误",
}
_GENERIC_FAILURE_DETAIL = "任务失败"
_GENERIC_SUCCESS_DETAIL = "任务成功"


def safe_public_detail(status: str, error_category: str = None) -> str:
    """匿名可访问的两个出口（POST直接响应、GET .../status）构造detail字段时
    必须调用这个函数，绝不能直接使用任何原始subprocess/异常文本。

    status="success"：固定返回通用成功摘要，不区分具体是哪种成功——同时
        返回的commit/run_id/changed_file_count等字段已经能提供足够的、
        本来就安全的上下文，detail不需要、也不应该再额外带任何自由文本。
    status="failure"：按error_category查表返回固定摘要；查不到（比如以后
        新增了某个error_category但忘了在上面那张表里补上——这是需要在代码
        review时发现并修的疏漏）时退回通用"任务失败"，绝不退回原始detail。
    其它status值（理论上不会真的发生，两个调用方目前只会传success/
        failure）同样退回通用失败摘要，不抛异常——这两个接口保持匿名公开，
        不能因为一个未预见的status取值直接500。
    """
    if status == "success":
        return _GENERIC_SUCCESS_DETAIL
    return _SAFE_FAILURE_DETAIL_BY_CATEGORY.get(error_category, _GENERIC_FAILURE_DETAIL)


def redact_known_secrets(text: str, *secrets: str) -> str:
    """把text里所有精确等于某个已知secret值的子串替换成占位符——精确匹配
    已知的具体值，而不是用正则猜测"什么样的字符串长得像凭据"，前者不会
    因为凭据格式变化而漏判。忽略空/None的secret（没配置凭据、或者这次
    调用跟凭据无关时，不必也不应该处理）。
    """
    if not text:
        return text
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[REDACTED]")
    return text
