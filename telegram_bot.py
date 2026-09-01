#!/usr/bin/env python3
"""
Telegram长轮询bot，独立进程运行（不依赖Flask是否启动）。
目前只支持一个指令: /status —— 返回抓取状态/文章数/磁盘/内存/最近版本变更。

用法: python3 telegram_bot.py  （建议用systemd常驻，见README_DEPLOY.md）
"""
import json
import os
import shutil
import subprocess
import time
import urllib.request
import urllib.parse
from pathlib import Path
from datetime import datetime

import db

TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "")
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "")  # 只回复这个chat_id发来的指令，防止陌生人操控你的bot
BASE_DIR = Path(__file__).parent
API_BASE = f"https://api.telegram.org/bot{TG_BOT_TOKEN}"


def tg_get(method, params=None, timeout=35):
    url = f"{API_BASE}/{method}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def send_message(text):
    data = urllib.parse.urlencode({"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "HTML"}).encode()
    req = urllib.request.Request(f"{API_BASE}/sendMessage", data=data, method="POST")
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode())


def disk_usage_ratio(path=BASE_DIR):
    total, used, free = shutil.disk_usage(path)
    return used / total, used, total


def get_recent_version_changes(limit=5):
    conn = db.get_conn()
    rows = conn.execute("""
        SELECT post_id, title, saved_at FROM post_versions
        ORDER BY saved_at DESC LIMIT ?
    """, (limit,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def check_systemd_service(name):
    """查询指定systemd服务是否active，失败返回'未知'而不是崩溃。"""
    try:
        result = subprocess.run(["systemctl", "is-active", name], capture_output=True, text=True, timeout=5)
        return result.stdout.strip()
    except Exception:
        return "未知"


def build_status_report():
    last_fetch = db.get_last_fetch_status()
    all_posts = db.get_all_posts()
    ratio, used, total = disk_usage_ratio()
    recent_changes = get_recent_version_changes()

    api_status = check_systemd_service("blog-mirror-api")

    lines = ["<b>blog-mirror 服务器状态</b>", f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", ""]

    lines.append("<b>抓取状态</b>")
    if last_fetch:
        lines.append(f"最近一次: {last_fetch['status']} @ {last_fetch['started_at']}")
        lines.append(f"完成于: {last_fetch.get('finished_at') or '未完成/进行中'}")
        if last_fetch.get("detail"):
            lines.append(f"详情: {last_fetch['detail'][:200]}")
    else:
        lines.append("尚无抓取记录")

    lines.append("")
    lines.append(f"<b>文章总数</b>: {len(all_posts)}")

    lines.append("")
    lines.append("<b>磁盘</b>")
    lines.append(f"使用率: {ratio*100:.1f}% ({used//(1024**3)}GB / {total//(1024**3)}GB)")
    if ratio >= 0.80:
        lines.append("⚠️ 已超过80%告警阈值")

    lines.append("")
    lines.append(f"<b>Flask API服务</b>: {api_status}")

    if recent_changes:
        lines.append("")
        lines.append("<b>最近历史版本变更</b>")
        for c in recent_changes:
            lines.append(f"· {c['title']} @ {c['saved_at']}")

    return "\n".join(lines)


def handle_update(update):
    msg = update.get("message", {})
    chat_id = str(msg.get("chat", {}).get("id", ""))
    text = msg.get("text", "").strip()

    if chat_id != TG_CHAT_ID:
        # 非本人发的指令一律忽略，不回复（避免陌生人探测你的bot存在并交互）
        return

    if text == "/status":
        report = build_status_report()
        send_message(report)
    elif text == "/help":
        send_message("可用指令:\n/status - 查看服务器与镜像站当前状态")


def main():
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        print("未配置TG_BOT_TOKEN/TG_CHAT_ID，无法启动")
        return

    print("telegram_bot 已启动，长轮询中...")
    offset = 0
    while True:
        try:
            result = tg_get("getUpdates", {"offset": offset, "timeout": 30})
            for update in result.get("result", []):
                offset = update["update_id"] + 1
                handle_update(update)
        except Exception as e:
            print(f"轮询异常: {e}")
            time.sleep(5)


if __name__ == "__main__":
    main()
