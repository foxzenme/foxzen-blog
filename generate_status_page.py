#!/usr/bin/env python3
"""
把 data/announcements.txt 渲染成一个自包含的静态HTML状态页，供以后的
update.foxzen.me 使用（见"发送给 Claude_Code_当前任务.txt"第十四节）。

为什么做成独立的静态生成脚本，而不是Flask里的一个路由：
update.foxzen.me 存在的意义就是"GreenCloud/Flask本身宕机时，读者仍然能看到
公告"。如果公告页面本身也是这台服务器上Flask动态渲染的，服务器一挂，公告页
跟着一起挂，起不到状态页该起的作用。所以这里只生成一份不依赖数据库、不依赖
Flask进程的纯静态HTML，后续可以推到GitHub Pages/Cloudflare Pages（第十六/
十七节的静态灾备），跟正常博客站点完全解耦。

用法：
    python3 generate_status_page.py
    （会读 data/announcements.txt，生成 static_status/index.html）

尚未完成、需要人工决定的部分（本次不擅自处理）：
    - update.foxzen.me 这个子域名的DNS记录、是否接入Cloudflare，需要在
      Cloudflare控制台手动添加；
    - static_status/ 目录推送到哪个GitHub仓库/Cloudflare Pages项目，
      需要你提供仓库地址和授权方式后再实现自动同步（第十六/十七节）；
    - 本脚本目前需要手动运行；要不要接到fetch_blog.py的cron里自动重新生成，
      等你确认再做。
"""
import html
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).parent
ANNOUNCEMENTS_FILE = BASE_DIR / "data" / "announcements.txt"
OUTPUT_DIR = BASE_DIR / "static_status"
OUTPUT_FILE = OUTPUT_DIR / "index.html"

PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>狐斋志异 - 站点状态与公告</title>
<style>
body {{ font-family: -apple-system, "Microsoft YaHei", sans-serif; max-width: 640px;
       margin: 40px auto; padding: 0 16px; line-height: 1.6; color: #222; }}
h1 {{ font-size: 1.4em; }}
.entry {{ border-bottom: 1px solid #ddd; padding: 12px 0; }}
.entry:last-child {{ border-bottom: none; }}
.time {{ color: #888; font-size: 0.85em; }}
.type {{ display: inline-block; background: #eee; border-radius: 4px; padding: 1px 8px;
        font-size: 0.8em; margin-left: 8px; }}
.empty {{ color: #888; }}
</style>
</head>
<body>
<h1>狐斋志异 - 站点状态与公告</h1>
<p class="empty" style="display:{empty_display}">目前没有公告。</p>
{entries_html}
</body>
</html>
"""

ENTRY_TEMPLATE = """<div class="entry">
  <span class="time">{time}</span><span class="type">{type}</span>
  <div>{message}</div>
</div>"""


# 允许的时间格式，按顺序尝试：可以不写秒，但年月日时分必须是合法的公历日期时间。
# Python的strptime对%m/%d/%H/%M本来就不要求严格零填充（"2026-9-2 9:30"能被
# %Y-%m-%d %H:%M解析），这里不额外强制零填充——反正排序用的是解析后的datetime
# 对象，不是原始字符串，零填不填不影响排序结果是否正确。
TIME_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M")


def _parse_time(time_str):
    """按TIME_FORMATS依次尝试解析，都失败返回None（调用方据此跳过整行并警告）。"""
    for fmt in TIME_FORMATS:
        try:
            return datetime.strptime(time_str, fmt)
        except ValueError:
            continue
    return None


def parse_announcements(text):
    """解析announcements.txt，跳过注释和空行、分段数不对的行、时间格式非法的行
    （不因为一行格式错误就让整个页面生成失败），返回按时间倒序排列的公告列表。

    排序用解析后的datetime对象而不是原始字符串——之前直接按字符串字典序排，
    如果时间格式不统一（比如混用零填充和不零填充、或者干脆用不同的日期格式），
    字典序和实际时间先后顺序会对不上；解析成datetime之后按真实时间值比较，
    不会有这个问题。
    """
    entries = []
    for lineno, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("|", 2)
        if len(parts) != 3:
            print(f"  [警告] announcements.txt 第{lineno}行格式不对（应为 时间|类型|内容），已跳过: {line!r}")
            continue
        time_str, type_str, message = (p.strip() for p in parts)
        parsed_time = _parse_time(time_str)
        if parsed_time is None:
            print(f"  [警告] announcements.txt 第{lineno}行时间格式不对"
                  f"（应为 YYYY-MM-DD HH:MM 或 YYYY-MM-DD HH:MM:SS），已跳过: {time_str!r}")
            continue
        entries.append({"time": time_str, "parsed_time": parsed_time, "type": type_str, "message": message})
    entries.sort(key=lambda e: e["parsed_time"], reverse=True)
    return entries


def render_html(entries):
    entries_html = "\n".join(
        ENTRY_TEMPLATE.format(
            time=html.escape(e["time"]),
            type=html.escape(e["type"]),
            message=html.escape(e["message"]),
        )
        for e in entries
    )
    return PAGE_TEMPLATE.format(
        entries_html=entries_html,
        empty_display="block" if not entries else "none",
    )


def main():
    text = ANNOUNCEMENTS_FILE.read_text(encoding="utf-8") if ANNOUNCEMENTS_FILE.exists() else ""
    entries = parse_announcements(text)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_FILE.write_text(render_html(entries), encoding="utf-8")
    print(f"已生成 {OUTPUT_FILE}，共{len(entries)}条公告。")


if __name__ == "__main__":
    main()
