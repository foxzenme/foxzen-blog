#!/usr/bin/env python3
"""cron专用：把mirror的整点内容抓取，从"直接跑fetch_blog.py"改成"调用本机
已经在跑的/api/refresh/mirror"（P0修复：cron与手动刷新按钮必须共用同一套
并发保护）。

背景：production cron此前是`0 * * * * ... fetch_blog.py`，直接把
fetch_blog.py当子进程跑，完全绕开了app.py::_run_content_fetch()里的
CONTENT_FETCH_LOCK（同一时刻只能有一个content_fetch在跑的互斥锁）。如果
cron整点触发的同时有人手动点了"刷新mirror/backup/github/cf"里任意一个
按钮，就会有两个fetch_blog.py进程同时读写同一个data/blog.db和html/工作树
——谁也不知道对方存在，是真正的数据竞争，不是体验层面的小问题。

这个脚本本身不做任何抓取，只发一个POST请求给本机已经在跑的Flask API：
    POST http://172.17.0.1:5000/api/refresh/mirror
这跟浏览器上"刷新mirror"按钮点击后发出的请求，命中的是完全相同的匿名公开
端点——同一个CONTENT_FETCH_LOCK、同一个5分钟冷却(REFRESH_COOLDOWN_SECONDS)、
同一套generation fencing，cron不再有任何绕过这些保护的特殊通道。

地址说明：172.17.0.1是gunicorn实际监听的地址（见
systemd/blog-mirror-api.service的`-b 172.17.0.1:5000`），是Docker bridge
网关地址，不是127.0.0.1——宿主机上直接跑的这个脚本可以直接连到这个地址，
不经过Nginx、不经过公网/Cloudflare、不需要任何认证凭据（这个端点本身设计
成公开匿名，见app.py::refresh_target()的文档字符串"公开匿名刷新"）。

退出码约定（cron/日志判断用）：
    0 = 请求得到了明确、符合预期的响应——success/cooldown/busy三种都算，
        cooldown/busy说明CONTENT_FETCH_LOCK正在正确地工作（可能是刚被手动
        刷新占用，也可能是上一轮还没到5分钟冷却），这是这套保护机制生效的
        表现，不是这个脚本的故障，不应该被当成异常处理。
    1 = 真正值得注意的情况：content_fetch本身失败（fetch_blog.py出错/超时，
        表现为HTTP 200但status=failure）、连不上API、或者收到未预期的HTTP
        状态码。

不发Telegram通知：/api/refresh/mirror本身对mirror/backup这两个target从不
发送Telegram通知（app.py::refresh_target()里没有任何notify()调用，全文
唯一的notify()调用点是磁盘用量告警，跟刷新流程无关），这里保持同样的
"安静"约定——cron每小时跑一次，如果cooldown/busy这种符合预期的正常情况都
发消息，很快就会从"有效信息"变成噪音。真正的失败(exit 1)体现在这个脚本
自己的stderr输出里，跟以前fetch_blog.py直接失败时一样，沿用cron现有的
`>> fetch.log 2>&1`记录方式，不新增任何通知渠道。

用法（cron，见cron/root.crontab）：
    0 * * * * cd /root/blog-mirror && /root/blog-mirror/venv/bin/python3 cron_refresh_mirror.py >> /root/blog-mirror/fetch.log 2>&1

也可以手动跑一次确认：python3 cron_refresh_mirror.py
"""
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime

# 默认指向gunicorn的真实监听地址（见上面docstring）。允许用环境变量覆盖，
# 只是为了让test_cron_refresh_mirror.py能把它指向测试用的临时端口，不需要
# 因此新增任何命令行参数解析——生产环境完全不设置这个环境变量，走默认值。
DEFAULT_API_URL = "http://172.17.0.1:5000/api/refresh/mirror"
API_URL = os.environ.get("MIRROR_REFRESH_API_URL", DEFAULT_API_URL)

# 略高于app.py::FETCH_SUBPROCESS_TIMEOUT_SECONDS(300s)：这个请求要等服务端
# 同步跑完fetch_blog.py子进程才会返回，网络本身是本机环回、延迟可以忽略，
# 这里只是在300s之上留一点余量，避免服务端刚好跑到临界值时客户端先一步断开。
REQUEST_TIMEOUT_SECONDS = 320


def _post_refresh(api_url=API_URL, timeout=REQUEST_TIMEOUT_SECONDS):
    """发起一次刷新请求，把HTTP层面的各种结果归一化成一个结构化结果，不在
    这里直接打印/决定退出码——方便脱离main()单独测试分类逻辑本身。

    返回 {"outcome": "success"|"cooldown"|"busy"|"content_fetch_failed"|
                      "http_error"|"network_error",
          "http_status": int | None, "body": dict, "message": str}
    """
    req = urllib.request.Request(api_url, data=b"", method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read().decode("utf-8"))
        except Exception:
            body = {}
        if e.code == 429:
            return {"outcome": "cooldown", "http_status": 429, "body": body,
                     "message": f"冷却中，剩余{body.get('cooldown_remaining_seconds', '?')}秒"}
        if e.code == 409:
            return {"outcome": "busy", "http_status": 409, "body": body,
                     "message": body.get("detail", "另一个刷新任务正忙")}
        return {"outcome": "http_error", "http_status": e.code, "body": body,
                 "message": f"未预期的HTTP {e.code}: {body}"}
    except Exception as e:
        return {"outcome": "network_error", "http_status": None, "body": {},
                 "message": f"请求失败（网络/连接问题）: {e}"}

    if body.get("status") == "success":
        return {"outcome": "success", "http_status": 200, "body": body,
                 "message": f"post_count={body.get('post_count')}"}
    return {"outcome": "content_fetch_failed", "http_status": 200, "body": body,
             "message": body.get("detail", "content_fetch状态为failure")}


# 这三种结果都说明CONTENT_FETCH_LOCK/冷却机制在正常工作，不是脚本故障，
# 不需要用非零退出码提醒任何人。
_QUIET_OUTCOMES = {"success", "cooldown", "busy"}


def main():
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    result = _post_refresh()
    line = (f"[{now}] mirror refresh via API: outcome={result['outcome']} "
            f"http_status={result['http_status']} - {result['message']}")
    if result["outcome"] in _QUIET_OUTCOMES:
        print(line)
        return 0
    print(line, file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
