#!/usr/bin/env python3
"""
把 data/announcements.txt 渲染成一个自包含的静态HTML公告页，即
update.foxzen.me（"最近发生了什么"——跟status.foxzen.me"现在怎么样"明确
区分，见build_status_page.py）。这个文件名字里虽然带"status"，但内容和
用途一直是公告/announcement页，不是本次新增的平台运行状态页，注意区分。

为什么做成独立的静态生成脚本，而不是Flask里的一个路由：
update.foxzen.me 存在的意义就是"GreenCloud/Flask本身宕机时，读者仍然能看到
公告"。如果公告页面本身也是这台服务器上Flask动态渲染的，服务器一挂，公告页
跟着一起挂，起不到公告页该起的作用。所以这里只生成一份不依赖数据库、不依赖
Flask进程的纯静态HTML，可以推到GitHub Pages/Cloudflare Pages（参考
publish_build.py/build_status_page.py现有的发布约定），跟正常博客站点完全
解耦。

用法：
    python3 generate_status_page.py
    （只在本地生成 static_status/index.html + static_status/CNAME，不做任何git操作）

    python3 generate_status_page.py --publish
    （生成后立即复用git_publish.py，commit+push static_status/这一个目录到
    origin/master——即"改TXT -> 一条命令发布"，需要当前目录是一个能push的
    git仓库、且环境变量GITHUB_TOKEN已设置；只会commit/push static_status/，
    不会碰data/announcements.txt本身或仓库里其它任何改动）

尚未完成、需要人工决定的部分（本次不擅自处理）：
    - update.foxzen.me 这个子域名的DNS记录当前仍代理到GreenCloud的IP，
      还没有切到foxzen-update.pages.dev（本轮不修改DNS）；
    - 生产服务器上暂时还没有一个"既是git仓库、又有GITHUB_TOKEN可用"的
      /root/blog-mirror目录来实际跑`--publish`——现状是/root/blog-mirror
      本身不是git仓库，候选目录/root/blog-mirror-new才是（细节见这次任务
      的报告），--publish要在生产上真正可用，需要先完成cutover，这不是
      本轮范围；
    - cron/自动任务检测announcements.txt变化并自动发布，本轮按要求没有引入，
      `--publish`目前需要人工在改完TXT后手动执行；
    - data/announcements.txt 目前没有任何真实公告内容，是否发布第一条
      公告、写什么，由你决定，这里不代为编造。
"""
import argparse
import html
import os
import sys
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).parent
ANNOUNCEMENTS_FILE = BASE_DIR / "data" / "announcements.txt"
OUTPUT_DIR = BASE_DIR / "static_status"
OUTPUT_FILE = OUTPUT_DIR / "index.html"
HOST = "update.foxzen.me"

# 发布static_status/到GitHub（Cloudflare Pages再从GitHub自动构建，见
# publish_update_page()文档字符串）用的subpath/提交身份。跟app.py里refresh
# 管道推送html/用的GIT_BOT_NAME/GIT_BOT_EMAIL取相同的值（保持提交作者身份
# 统一），这里单独定义常量而不是`from app import ...`——原因跟
# publish_build.py::BRAND_HEADING一致：这个脚本本来就设计成不依赖Flask/db
# （见文件头docstring"GreenCloud/Flask本身宕机时，读者仍然能看到公告"），
# import app就会连带引入Flask/数据库依赖，跟这个设计目标矛盾。
PUBLISH_SUBPATH = "static_status"
GIT_BOT_NAME = "Foxzen Refresh Bot"
GIT_BOT_EMAIL = "foxzen-refresh-bot@users.noreply.github.com"
GIT_PUSH_TIMEOUT_SECONDS = 60

PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>狐斋志异 · 站点公告</title>
<style>
body {{ font-family: -apple-system, "Microsoft YaHei", sans-serif; max-width: 640px;
       margin: 40px auto; padding: 0 16px; line-height: 1.6; color: #222; }}
h1 {{ font-size: 1.4em; margin-bottom: 4px; }}
.subtitle {{ color: #666; font-size: 0.9em; margin-bottom: 8px; }}
.cross-link {{ margin: 4px 0 24px; font-size: 0.9em; }}
.entry {{ border-bottom: 1px solid #ddd; padding: 12px 0; }}
.entry:last-child {{ border-bottom: none; }}
.time {{ color: #888; font-size: 0.85em; }}
.type {{ display: inline-block; background: #eee; color: #555; border-radius: 4px; padding: 1px 8px;
        font-size: 0.8em; margin-left: 8px; }}
.type.type-incident {{ background: #fbe6e6; color: #b3261e; }}
.type.type-maintenance {{ background: #fff4e0; color: #8a5a00; }}
.type.type-recovery {{ background: #e3f6e9; color: #1a7f3c; }}
.empty {{ color: #888; }}
.lang-toggle {{
  position: fixed; top: 12px; right: 12px; z-index: 100;
  display: inline-block; font-size: 0.85em;
  background: rgba(255,255,255,0.92); padding: 4px 10px; border-radius: 14px;
  box-shadow: 0 1px 4px rgba(0,0,0,0.15);
}}
.lang-toggle button {{
  background: none; border: none; padding: 2px 4px; cursor: pointer;
  color: #999; font-size: 1em; font-family: inherit;
}}
.lang-toggle button.active {{ color: #1a73e8; font-weight: bold; }}
@media (max-width: 480px) {{
  .lang-toggle {{ top: 8px; right: 8px; font-size: 0.75em; padding: 3px 8px; }}
}}
</style>
</head>
<body>
<div class="lang-toggle">
  <button type="button" data-lang-btn="zh" aria-label="切换到中文">中</button> / <button type="button" data-lang-btn="en" aria-label="Switch to English">EN</button>
</div>
<h1>狐斋志异 · 站点公告</h1>
<div class="subtitle">update.foxzen.me &mdash; <span data-i18n="update_subtitle">最近发生了什么</span></div>
<p class="cross-link"><a href="https://status.foxzen.me/" data-i18n="view_status_link">查看当前平台状态 →</a></p>
<p class="empty" style="display:{empty_display}" data-i18n="no_announcements">目前没有公告。</p>
{entries_html}
<script>
(function () {{
  "use strict";
  // 全站UI国际化：跟fetch_blog.py::I18N_BLOCK等其它几份独立实现同一套
  // localStorage key/检测算法/data-i18n约定，这是第七份独立实现（这个
  // 页面是generate_status_page.py单独生成的自包含文件，不依赖build_status_
  // page.py或任何前端JS文件）。只翻译页面UI标签(标题/副标题/链接/空状态
  // 提示)，{entries_html}里的实际公告内容(time/type/message，来自GreenCloud
  // 服务器上手工维护的data/announcements.txt)永远不套用data-i18n、永远
  // 保持原文——这是这个页面最核心的一条边界，任何"看起来像公告"的文字
  // 都不会被这段脚本触碰，因为它只认data-i18n属性，公告内容本身从来
  // 没有也不会被打上这个属性。
  var UPDATE_STRINGS = {{
    zh: {{ update_subtitle: "最近发生了什么", view_status_link: "查看当前平台状态 →", no_announcements: "目前没有公告。" }},
    en: {{ update_subtitle: "what's happened recently", view_status_link: "View current platform status →", no_announcements: "No announcements at this time." }},
  }};
  var STORAGE_KEY = "foxzen_lang";

  function detectDefaultLang() {{
    var langs = (navigator.languages && navigator.languages.length) ? navigator.languages : [navigator.language || ""];
    for (var i = 0; i < langs.length; i++) {{
      if (/^zh/i.test(langs[i])) return "zh";
    }}
    return "en";
  }}

  function getLang() {{
    try {{
      var saved = localStorage.getItem(STORAGE_KEY);
      if (saved === "zh" || saved === "en") return saved;
    }} catch (e) {{}}
    return detectDefaultLang();
  }}

  function applyLang(lang) {{
    var dict = UPDATE_STRINGS[lang] || UPDATE_STRINGS.en;
    var nodes = document.querySelectorAll("[data-i18n]");
    for (var i = 0; i < nodes.length; i++) {{
      var key = nodes[i].getAttribute("data-i18n");
      if (dict[key] !== undefined) nodes[i].textContent = dict[key];
    }}
    document.documentElement.setAttribute("lang", lang === "zh" ? "zh-CN" : "en");
    var btns = document.querySelectorAll("[data-lang-btn]");
    for (var j = 0; j < btns.length; j++) {{
      if (btns[j].getAttribute("data-lang-btn") === lang) btns[j].classList.add("active");
      else btns[j].classList.remove("active");
    }}
  }}

  function setLang(lang) {{
    try {{ localStorage.setItem(STORAGE_KEY, lang); }} catch (e) {{}}
    applyLang(lang);
  }}

  var toggleBtns = document.querySelectorAll("[data-lang-btn]");
  for (var k = 0; k < toggleBtns.length; k++) {{
    toggleBtns[k].addEventListener("click", function (e) {{
      setLang(e.currentTarget.getAttribute("data-lang-btn"));
    }});
  }}

  applyLang(getLang());
}})();
</script>
</body>
</html>
"""

ENTRY_TEMPLATE = """<div class="entry">
  <span class="time">{time}</span><span class="type {type_class}">{type}</span>
  <div>{message}</div>
</div>"""

# 类型字段是自由文本，不是固定枚举——announcements.txt文件头部的"功能更新/
# 网站宕机/恢复/维护/重要技术变更"只是举例，不是强制要求站长必须使用这几个
# 词。这里只做尽力而为的关键词匹配来决定视觉样式，任何匹配不上的文字都安全
# 退回中性样式（type-notice级别，即不加任何额外class，沿用.type的默认灰色），
# 不会因为遇到没见过的类型文字就出错或显示误导性的颜色。
_TYPE_STYLE_RULES = (
    (("故障", "宕机", "incident", "outage"), "type-incident"),
    (("维护", "maintenance"), "type-maintenance"),
    (("恢复", "recovery"), "type-recovery"),
)


def _type_css_class(type_str):
    lowered = type_str.lower()
    for keywords, css_class in _TYPE_STYLE_RULES:
        if any(kw in lowered for kw in keywords):
            return css_class
    return ""


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
            type_class=_type_css_class(e["type"]),
            message=html.escape(e["message"]),
        )
        for e in entries
    )
    return PAGE_TEMPLATE.format(
        entries_html=entries_html,
        empty_display="block" if not entries else "none",
    )


def build_update_page(output_dir: Path = OUTPUT_DIR, announcements_file: Path = ANNOUNCEMENTS_FILE):
    """解析announcements_file、渲染HTML，连同CNAME一起写入output_dir，
    返回(output_dir, entries)——entries一并返回给调用方（含main()自己的
    打印、测试）复用，不需要重新解析一遍文件。
    """
    text = announcements_file.read_text(encoding="utf-8") if announcements_file.exists() else ""
    entries = parse_announcements(text)
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "index.html").write_text(render_html(entries), encoding="utf-8")
    (output_dir / "CNAME").write_text(HOST + "\n", encoding="utf-8")
    return output_dir, entries


def publish_update_page(repo_dir: Path = BASE_DIR, output_dir: Path = OUTPUT_DIR,
                         announcements_file: Path = ANNOUNCEMENTS_FILE,
                         subpath: str = PUBLISH_SUBPATH) -> dict:
    """"改TXT -> 一条命令发布"的落地实现：先重新生成static_status/，再复用
    已有的git_publish.commit_and_push()把这个目录commit+push——这跟app.py里
    github/cf两个refresh target推送html/用的是同一个函数，不是重新写一遍git
    操作。默认只对PUBLISH_SUBPATH("static_status")这一个目录做commit（git_publish
    内部用`git add -- static_status`+`git commit --only -- static_status`），
    不会碰data/announcements.txt本身、不会碰仓库里其它任何未提交的改动。

    repo_dir必须是一个已经clone好、能fast-forward push到origin/master的git
    仓库（生产环境规划是cutover后的候选目录，例如/root/blog-mirror-new）——
    这个函数不负责clone/初始化仓库，只负责"仓库已经就绪"之后的commit+push
    这一步，跟git_publish.py本身"repo_dir是显式参数，不关心BASE_DIR是什么"
    的设计原则一致。

    subpath默认是PUBLISH_SUBPATH，对应"static_status/是foxzen-blog仓库内部
    一个子目录，Cloudflare Pages从这个子目录构建"这个现有用法。传"."用于
    发布到一个独立的卫星仓库（比如GitHub Pages专用的foxzen-update仓库）——
    这种场景repo_dir/output_dir都指向那个卫星仓库的checkout路径本身，整个
    仓库根目录就是这个页面，不再是foxzen-blog内部的一个子目录。

    push认证读环境变量GITHUB_TOKEN（跟app.py同一个约定），不在这里假设它
    来自哪个具体文件——本地是否用`export`还是从某个受保护权限的文件里读出来
    再传进这个进程，是运维层面的选择，不是这个函数需要关心的事。

    返回git_publish.commit_and_push()的原始返回值，外加"generated_entries"
    字段（这一轮实际生成了多少条公告，方便调用方打印/测试断言），不吞掉
    任何git_publish已经分类好的错误信息。
    """
    output_dir, entries = build_update_page(output_dir, announcements_file)

    push_token = os.environ.get("GITHUB_TOKEN", "")
    if not push_token:
        return {"pushed": False, "error_category": "credentials_missing",
                "detail": "环境变量GITHUB_TOKEN未设置，无法推送。",
                "generated_entries": len(entries)}

    import git_publish
    commit_message = f"更新公告页面 ({datetime.now().strftime('%Y-%m-%d %H:%M:%S')})"
    result = git_publish.commit_and_push(
        repo_dir, subpath, GIT_BOT_NAME, GIT_BOT_EMAIL,
        commit_message, push_token, GIT_PUSH_TIMEOUT_SECONDS,
    )
    result["generated_entries"] = len(entries)
    return result


def main():
    parser = argparse.ArgumentParser(description="生成（可选：发布）update.foxzen.me的公告静态页")
    parser.add_argument(
        "--publish", action="store_true",
        help="生成后立即commit+push（复用git_publish.py），需要环境变量GITHUB_TOKEN。"
             "不加这个参数时只在本地生成static_status/，不做任何git操作。",
    )
    parser.add_argument(
        "--repo-dir", type=Path, default=None,
        help="发布到一个独立的卫星仓库根目录(比如GitHub Pages专用的"
             "foxzen-update仓库的本地checkout路径)，而不是默认推到"
             "foxzen-blog本身的static_status/子目录。只在--publish时有意义；"
             "不传时保持原有行为(生成/推送到BASE_DIR下的static_status/)。",
    )
    args = parser.parse_args()

    if not args.publish:
        output_dir = args.repo_dir if args.repo_dir else OUTPUT_DIR
        output_dir, entries = build_update_page(output_dir)
        print(f"已生成 {output_dir / 'index.html'}，共{len(entries)}条公告。")
        return

    if args.repo_dir:
        result = publish_update_page(repo_dir=args.repo_dir, output_dir=args.repo_dir, subpath=".")
    else:
        result = publish_update_page()
    if result["pushed"]:
        print(f"已生成并推送，共{result['generated_entries']}条公告，"
              f"commit={result['commit_sha']}，push_state={result['push_state']}。")
    else:
        print(f"生成成功但推送失败（{result['error_category']}）: {result['detail']}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
