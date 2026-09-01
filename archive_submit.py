#!/usr/bin/env python3
"""
每天把镜像站+Blogger主站的所有页面推给Internet Archive的Save Page Now接口，
让Wayback Machine定期存快照，长期下来能看到每个页面随时间的变化历史。

重要限制（必须先知道，不是bug）：
- 快照时间戳永远是"这次接口调用的时刻"，没法伪造/补录成文章实际发布的历史时间点
- 只能从部署这个脚本开始的这一刻往后逐日累积，补不了过去的历史

用法: python3 archive_submit.py  （建议cron每天跑一次，跟fetch_blog.py错开时间）
    0 4 * * * cd /root/blog-mirror && /root/blog-mirror/venv/bin/python3 archive_submit.py >> /root/blog-mirror/archive.log 2>&1

依赖环境变量：
    IA_ACCESS_KEY / IA_SECRET_KEY —— 去 https://archive.org/account/s3.php 生成
"""
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

import db
from telegram_notify import notify

IA_ACCESS_KEY = os.environ.get("IA_ACCESS_KEY", "")
IA_SECRET_KEY = os.environ.get("IA_SECRET_KEY", "")

SAVE_URL = "https://web.archive.org/save"
MIRROR_ROOT_URL = "https://mirror.foxzen.me"

REQUEST_INTERVAL_SECONDS = 12   # 认证用户限速6次/分钟，间隔12秒留足安全余量
RETRY_WAIT_SECONDS = 60         # 被限速(429)时等多久再重试
MAX_RETRIES = 1                 # 只重试一次，不做无限死磕，明天这个cron自然会再来一次
RECENT_HOURS = 24                # 只提交过去这么多小时内有变化/新增的文章


def _is_recent(iso_str, hours=RECENT_HOURS):
    """判断一个ISO时间戳是不是落在最近N小时内。解析失败一律当作"不算最近"，
    不猜测、不冒险多提交不该提交的。
    """
    if not iso_str:
        return False
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt >= datetime.now(timezone.utc) - timedelta(hours=hours)
    except Exception:
        return False


def build_url_list():
    """镜像站首页（每天固定推）+ 最近24小时内新增/变化的文章，
    加上这些文章在Blogger主站的source_url。不再是"所有历史页面全量推"，
    避免每天都撞限速、也避免对没变化的旧文章做无意义的重复提交。
    """
    urls = [MIRROR_ROOT_URL + "/"]
    blog_root = None

    conn = db.get_conn()
    rows = conn.execute("SELECT canonical_path, source_url, updated FROM posts").fetchall()
    conn.close()

    for r in rows:
        if not _is_recent(r["updated"]):
            continue
        if r["canonical_path"]:
            urls.append(f"{MIRROR_ROOT_URL}/{r['canonical_path']}.html")
        if r["source_url"]:
            urls.append(r["source_url"])
            if blog_root is None:
                proto_end = r["source_url"].find("://") + 3
                domain_end = r["source_url"].find("/", proto_end)
                blog_root = r["source_url"][:domain_end] + "/"

    if blog_root:
        urls.append(blog_root)

    return urls


def submit_one(url):
    """提交单个URL，遇到限速(429)等一次再重试，其他错误直接放弃这条，不影响其他URL。"""
    body = "&".join([
        f"url={urllib.parse.quote(url, safe='')}",
        "capture_all=1",
        "capture_outlinks=1",
        "if_not_archived_within=86400",
    ]).encode("utf-8")

    req = urllib.request.Request(
        SAVE_URL,
        data=body,
        headers={
            "Accept": "application/json",
            "Authorization": f"LOW {IA_ACCESS_KEY}:{IA_SECRET_KEY}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )

    for attempt in range(MAX_RETRIES + 1):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                result = json.loads(resp.read().decode("utf-8"))
                return True, result.get("job_id", "未知job_id")
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < MAX_RETRIES:
                print(f"    限速，等{RETRY_WAIT_SECONDS}秒后重试: {url}")
                time.sleep(RETRY_WAIT_SECONDS)
                continue
            return False, f"HTTP {e.code}: {e.read().decode('utf-8', errors='ignore')[:200]}"
        except Exception as e:
            return False, str(e)
    return False, "重试后仍失败"


def main():
    if not IA_ACCESS_KEY or not IA_SECRET_KEY:
        print("未配置IA_ACCESS_KEY/IA_SECRET_KEY，跳过归档提交")
        return

    urls = build_url_list()
    print(f"共{len(urls)}个地址待提交给Internet Archive")

    ok_count, fail_count = 0, 0
    failures = []

    for i, url in enumerate(urls):
        ok, detail = submit_one(url)
        if ok:
            ok_count += 1
            print(f"  [{i+1}/{len(urls)}] OK {url} -> {detail}")
        else:
            fail_count += 1
            failures.append((url, detail))
            print(f"  [{i+1}/{len(urls)}] 失败 {url} -> {detail}")

        if i < len(urls) - 1:
            time.sleep(REQUEST_INTERVAL_SECONDS)

    print(f"完成：成功{ok_count}，失败{fail_count}")

    if fail_count > 0:
        detail_lines = "\n".join(f"· {u}: {d[:80]}" for u, d in failures[:5])
        notify(f"⚠️ Internet Archive归档：{ok_count}成功/{fail_count}失败\n{detail_lines}")


if __name__ == "__main__":
    main()
