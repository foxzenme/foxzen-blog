#!/usr/bin/env python3
"""
Telegram告警。TOKEN/CHAT_ID从环境变量读取，不在代码里硬编码凭证。

部署前需要：
1. 找 @BotFather 创建一个bot，拿到token
2. 找 @userinfobot 或给你的bot发消息后查 getUpdates 拿到你的chat_id
3. 在服务器上设置环境变量（写入 ~/.bashrc 或 systemd service 的 Environment=）：
   export TG_BOT_TOKEN="123456:ABC-your-token"
   export TG_CHAT_ID="your_chat_id"
"""
import os
import urllib.request
import urllib.parse
import json

TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "")
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "")


def notify(message: str) -> bool:
    """发送Telegram消息，失败时打印到stderr但不抛异常（告警本身不该造成主流程崩溃）。"""
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        print(f"[telegram_notify] 未配置TG_BOT_TOKEN/TG_CHAT_ID，跳过发送: {message}")
        return False

    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id": TG_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
    }).encode()

    try:
        req = urllib.request.Request(url, data=data, method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read().decode())
            if not result.get("ok"):
                print(f"[telegram_notify] 发送失败: {result}")
                return False
            return True
    except Exception as e:
        print(f"[telegram_notify] 发送异常: {e}")
        return False


if __name__ == "__main__":
    ok = notify("测试消息：blog-mirror telegram_notify.py 自检")
    print("发送成功" if ok else "发送失败，检查TG_BOT_TOKEN/TG_CHAT_ID是否配置")
