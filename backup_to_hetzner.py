#!/usr/bin/env python3
"""
每日备份脚本：检查数据库里是否有比上次备份更新的内容（新文章或历史版本变更），
有变化才生成全站Base64自包含版本并推送到Hetzner Storage Box；没变化则跳过，不做无谓的抓取/上传。

用法: python3 backup_to_hetzner.py
建议cron每天跑一次，例如凌晨3点（避开每小时抓取的整点，避免和fetch_blog.py抢锁）：
    0 3 * * * cd /root/blog-mirror && /root/blog-mirror/venv/bin/python3 backup_to_hetzner.py >> /root/blog-mirror/backup.log 2>&1

依赖：需要服务器上已配置好 rclone remote，名字为 "hetzner"（rclone listremotes 应显示 hetzner:）
"""
import json
import subprocess
import sys
from pathlib import Path
from datetime import datetime

import db
from telegram_notify import notify

BASE_DIR = Path(__file__).parent
STATE_FILE = BASE_DIR / "data" / "last_backup.json"
TEMP_EXPORT_DIR = BASE_DIR / "data" / "backup_tmp"
RCLONE_REMOTE = "hetzner:blog-mirror-standalone-backup"

sys.path.insert(0, str(BASE_DIR))
from app import _inline_post_as_base64, _safe_filename, _get_title  # noqa: E402


def read_last_backup_state():
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"last_backup_at": None}


def write_last_backup_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")


def get_latest_content_timestamp():
    conn = db.get_conn()
    row1 = conn.execute("SELECT MAX(fetched_at) AS t FROM posts").fetchone()
    row2 = conn.execute("SELECT MAX(saved_at) AS t FROM post_versions").fetchone()
    conn.close()
    candidates = [t["t"] for t in (row1, row2) if t and t["t"]]
    return max(candidates) if candidates else None


def has_changes_since(last_backup_at):
    latest = get_latest_content_timestamp()
    if latest is None:
        return False
    if last_backup_at is None:
        return True
    return latest > last_backup_at


def export_all_to_dir(target_dir: Path):
    target_dir.mkdir(parents=True, exist_ok=True)
    all_posts = db.get_all_posts()
    used_names = set()
    count = 0
    for p in all_posts:
        html = _inline_post_as_base64(p["post_id"])
        if html is None:
            print(f"  [跳过] {p['title']} 找不到本地HTML产物")
            continue
        base_name = _safe_filename(_get_title(p["post_id"]))
        name = f"{base_name}.html"
        if name in used_names:
            name = f"{base_name}-{p['post_id']}.html"
        used_names.add(name)
        (target_dir / name).write_text(html, encoding="utf-8")
        count += 1
    return count


def rclone_push(local_dir: Path, remote_subdir: str) -> bool:
    remote_path = f"{RCLONE_REMOTE}/{remote_subdir}"
    result = subprocess.run(
        ["rclone", "copy", str(local_dir), remote_path, "--progress"],
        capture_output=True, text=True, timeout=1800,
    )
    if result.returncode != 0:
        print(f"  [rclone错误] {result.stderr[-1000:]}")
        return False
    return True


def main():
    state = read_last_backup_state()
    last_backup_at = state.get("last_backup_at")

    if not has_changes_since(last_backup_at):
        print(f"无内容变化（上次备份: {last_backup_at}），跳过本次备份。")
        return

    print(f"检测到内容变化（上次备份: {last_backup_at}），开始生成Base64导出...")
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    export_dir = TEMP_EXPORT_DIR / timestamp

    try:
        count = export_all_to_dir(export_dir)
        print(f"已生成 {count} 篇文章的Base64自包含版本，本地目录: {export_dir}")

        ok = rclone_push(export_dir, timestamp)
        if not ok:
            notify(f"⚠️ blog-mirror Hetzner备份失败：rclone推送出错，时间戳{timestamp}")
            sys.exit(1)

        print(f"已推送到 hetzner:blog-mirror-standalone-backup/{timestamp}/")
        write_last_backup_state({"last_backup_at": datetime.now().isoformat(timespec="seconds")})
        notify(f"✅ blog-mirror 已备份到Hetzner: {timestamp}，共{count}篇文章")

    finally:
        import shutil
        if export_dir.exists():
            shutil.rmtree(export_dir)


if __name__ == "__main__":
    main()
