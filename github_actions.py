#!/usr/bin/env python3
"""封装GitHub Actions workflow_dispatch触发 + run识别 + conclusion轮询，只被
app.py里的github这一个refresh target调用。

已知的GitHub API限制（不是需要猜测的行为，是文档化的接口行为）：
POST .../workflows/{id}/dispatches 只返回204 No Content，响应体里没有run id。
这里的规避方式（S5修复后）：每次dispatch生成一个随机refresh_id，通过
.github/workflows/pages.yml里的run-name表达式把它写进这次run的显示名称，
之后按这个近乎不可能碰撞的字符串精确匹配对应的run——不再仅依赖"dispatch
时间戳+几秒容差窗口"这种跟VPS本地时钟精度绑定、容易被时钟漂移或人工
同时手动触发干扰的弱关联方式（旧实现的已知缺陷：人工在同一个窗口内手动
点"Run workflow"，或者VPS时钟有几秒漂移，都可能导致误认）。时间戳过滤
仍然保留作为第一层粗筛，减少需要精确比较run-name的候选数量，但真正决定
"是不是这次dispatch对应的run"的判定标准是run-name精确匹配。

http_call做成可替换参数（默认是_default_http_call，真的打GitHub REST API），
测试传入一个返回预设JSON的假函数即可完全离线验证轮询/识别逻辑，不会在
测试里真的触发GitHub Actions。

S4修复：_find_dispatched_run/_poll_until内部对http_call的调用现在都有
try/except包裹——单次瞬时网络/API异常（超时、502之类）会被当作"这一次
没查到，按原有的重试节奏再试一次"处理，不会让一个偶发的瞬时故障直接
以裸异常冒泡到调用方（这里是Flask路由处理函数），导致push已经成功却
没有任何target结果被记录。重试多次仍然失败，才会转成统一的
GitHubActionsError抛出，调用方据此走正常的failure记录路径。
"""
import json
import secrets
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

import safe_errors

_CLOCK_SKEW_MARGIN = timedelta(minutes=10)


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


def _find_dispatched_run(repo, workflow_file, token, dispatched_at, refresh_id, http_call,
                          max_attempts=6, retry_interval_seconds=2.0):
    """dispatch到run出现在列表里之间有几秒传播延迟，需要重试几次、短退避，
    不能只查一次就判定失败。找不到时返回None（调用方转成run_identification_error）。

    S5修复：候选run必须run-name精确匹配"Pages refresh {refresh_id}"才会被
    接受——created_at>=dispatched_at只是第一层粗筛（减少候选数量），不是
    判定依据本身，人工手动触发的run(run-name是workflow文件里run-name表达式
    在没有refresh_id输入时渲染出的默认值)不可能意外匹配上这个随机字符串。

    S4修复：http_call本身抛出异常时不立即放弃，按已有的重试节奏当作
    "这一次没查到"处理，多次重试后仍然失败才转成GitHubActionsError抛出。

    复核阶段进一步钉死的三点：

    A. run-name渲染值实际落在哪个字段：GitHub REST API的workflow run
    对象里，`name`是这个workflow*文件本身*的名字（对应pages.yml顶部
    `name: Deploy GitHub Pages`那一行，同一个workflow文件不管这次run是
    谁触发的、run-name表达式渲染成什么，这个字段的值都固定不变，根本
    不可能是我们随机生成的"Pages refresh {refresh_id}"），真正随每次run
    动态变化、承载run-name表达式渲染结果的字段是`display_title`——这是
    GitHub官方文档给run-name功能举例时明确对应的字段。也就是说
    `r.get("name") == expected_name`这个分支在实践中几乎不可能为真：
    不是因为它错，而是因为它多余——继续保留它不会引入误判风险
    （expected_name是"Pages refresh <64bit随机hex>"，不可能巧合等于
    工作流固定的静态名字"Deploy GitHub Pages"），纯粹是"万一未来GitHub
    某个API版本行为有出入"的零成本兜底。这一条无法用真实GitHub API
    现场验证（任务边界不允许真实调用），是基于GitHub官方文档对这两个
    字段的定义得出，如果以后真的观测到不一致，应该以实际API返回为准
    调整，而不是继续假设。

    B/C. 更可靠的关联策略：不再只看run-name一个维度，追加两层过滤，
    三者都满足才接受为候选：
      1) event必须精确等于'workflow_dispatch'——服务端URL的
         `?event=workflow_dispatch`查询参数已经做过一次过滤，这里在
         客户端拿到响应后再显式核对一次同一个字段，双重保险，不单纯
         信任查询参数一定生效；这一层也是为pages.yml注释里写明的未来
         计划（"确认整个流程稳定之后，可以再单独加一个push触发器"）
         预留的防线——届时一次恰好同一时刻发生的push触发的run，即使
         display_title意外撞上，也会被event这一层挡在外面。
      2) run-name精确匹配（不变，核心判定依据）。
      3) workflow本身的范围已经由请求URL路径里的{workflow_file}保证
         （这次GET请求本身只会返回这一个workflow文件下的run列表），
         不需要也没办法在返回的run对象里再单独核对一次workflow_id——
         除非专门再发一次"查这个workflow文件对应的workflow_id具体是
         多少"的请求，那样反而多引入一个可能出错的环节，却不会带来
         任何额外的确定性（URL本身已经是权威的范围限定）。

    如果同时有2个或以上的run都精确满足上述全部条件（refresh_id具有
    secrets.token_hex(8)=64bit随机性，理论上不可能真的撞上，只有API
    异常或其它不可预见的边界情况才会出现），不再像旧实现那样静默挑
    "最早的那个"当正确答案——立即判定为run_identification_error，绝不猜。
    """
    expected_name = f"Pages refresh {refresh_id}"
    last_error = None
    for _ in range(max_attempts):
        try:
            _status, data = http_call(
                "GET",
                f"https://api.github.com/repos/{repo}/actions/workflows/{workflow_file}/runs"
                f"?event=workflow_dispatch&per_page=10",
                token,
            )
        except Exception as e:
            last_error = e
            time.sleep(retry_interval_seconds)
            continue

        last_error = None
        candidates = [
            r for r in (data or {}).get("workflow_runs", [])
            if r.get("event") == "workflow_dispatch"
            and _parse_gh_time(r["created_at"]) >= dispatched_at - _CLOCK_SKEW_MARGIN
            and (r.get("display_title") == expected_name or r.get("name") == expected_name)
        ]
        if len(candidates) > 1:
            raise GitHubActionsError(
                "run_identification_error",
                f"有{len(candidates)}个run同时精确匹配run-name'{expected_name}'"
                f"(run_id={[r.get('id') for r in candidates]})，无法唯一确定，拒绝猜测其中一个。",
            )
        if candidates:
            return candidates[0]
        time.sleep(retry_interval_seconds)

    if last_error is not None:
        raise GitHubActionsError(
            "run_identification_error",
            f"查询workflow run列表时反复网络/API异常，最后一次: "
            f"{safe_errors.redact_known_secrets(str(last_error), token)}",
        )
    return None


def _poll_until(repo, run_id, run_html_url, token, wait_seconds, poll_interval_seconds, http_call):
    """真正的轮询循环，trigger_and_wait()的有界等待和poll_until_conclusion()
    的后台长等待共用同一份逻辑，只是wait_seconds不同——避免两处各写一份、
    以后改了轮询细节只改了一边。

    返回：
      {"outcome": "success", "run_id": int, "run_html_url": str}
      {"outcome": "failure", "run_id": int, "run_html_url": str, "conclusion": str}
      {"outcome": "timeout", "run_id": int, "run_html_url": str}

    S4修复：单次http_call异常不立即放弃整个轮询——记下来，按原有轮询间隔
    继续尝试，只要在wait_seconds到期之前有一次成功拿到明确的completed
    状态，就正常返回；如果直到到期前的最后一次尝试仍然是异常（而不是
    正常的"还没跑完"），才转成GitHubActionsError抛出，不让裸异常冒泡到
    Flask路由处理函数——那样会导致push已经真的成功、却没有任何target
    结果被记录下来。
    """
    deadline = time.monotonic() + wait_seconds
    last_error = None
    while time.monotonic() < deadline:
        try:
            _status, run_detail = http_call(
                "GET", f"https://api.github.com/repos/{repo}/actions/runs/{run_id}", token,
            )
        except Exception as e:
            last_error = e
            time.sleep(poll_interval_seconds)
            continue

        last_error = None
        if (run_detail or {}).get("status") == "completed":
            conclusion = run_detail.get("conclusion")
            if conclusion == "success":
                return {"outcome": "success", "run_id": run_id, "run_html_url": run_html_url}
            return {"outcome": "failure", "run_id": run_id, "run_html_url": run_html_url,
                    "conclusion": conclusion}
        time.sleep(poll_interval_seconds)

    if last_error is not None:
        raise GitHubActionsError(
            "actions_poll_error",
            f"轮询run conclusion时反复网络/API异常，最后一次: "
            f"{safe_errors.redact_known_secrets(str(last_error), token)}",
        )
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

    S5修复：随机生成refresh_id并作为workflow_dispatch的inputs一并传出去
    ——pages.yml的run-name表达式会用它渲染这次run的显示名称，
    _find_dispatched_run()据此精确匹配，不再依赖时间戳弱关联。

    抛出：
      GitHubActionsError(error_category, detail) —— dispatch请求本身失败，
          或dispatch成功但无法可靠定位到对应run，或轮询阶段反复网络异常。
    """
    dispatched_at = datetime.now(timezone.utc)
    refresh_id = secrets.token_hex(8)

    try:
        status, _ = http_call(
            "POST",
            f"https://api.github.com/repos/{repo}/actions/workflows/{workflow_file}/dispatches",
            token, body={"ref": ref, "inputs": {"refresh_id": refresh_id}},
        )
    except urllib.error.HTTPError as e:
        raise GitHubActionsError("workflow_dispatch_error", f"dispatch请求被拒绝: HTTP {e.code}")
    except Exception as e:
        raise GitHubActionsError("workflow_dispatch_error",
                                  f"dispatch请求失败: {safe_errors.redact_known_secrets(str(e), token)}")
    if status != 204:
        raise GitHubActionsError("workflow_dispatch_error", f"dispatch返回非预期状态码: {status}")

    run = _find_dispatched_run(repo, workflow_file, token, dispatched_at, refresh_id, http_call)
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

    抛出：GitHubActionsError("actions_poll_error", ...) —— 反复网络/API
        异常（S4修复）；app.py的_watch_github_run_in_background()本身
        也有一层兜底的except Exception，这里抛类型化异常是为了让那层
        兜底记录的错误分类更准确，不是依赖这里一定要抛出才能保证安全。
    """
    return _poll_until(repo, run_id, run_html_url, token, max_wait_seconds, poll_interval_seconds, http_call)
