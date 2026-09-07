#!/usr/bin/env python3
"""
Flask后端。本版起，nginx不再直接serve文章页/首页/短号——这几类请求都改成proxy到这里，
由Flask读取磁盘上已经渲染好的静态文件、计数、再返回内容。图片/CSS等真静态资源
仍由nginx直接serve（见default.conf），不走这里，性能不受影响。

职责：
- GET  /                              首页，计数
- GET  /<int:number>/                 短号，302跳转到当前canonical路径，不在此处计数
                                       （计数在跳转后的落地页发生，避免重复计数）
- GET  /posts/<post_id>/              旧版直链，301跳转到canonical路径（保持旧链接不失效）
- GET  /<year>/<month>/<slug>.html    文章页（canonical路径），计数并返回内容
- POST /api/refresh/<target>          公开匿名刷新（target: mirror/backup/github/cf），限流
- GET  /api/refresh/<target>/status   查询某个target的当前状态/上次结果
- GET  /api/search                    全文搜索
- GET  /api/download/all              打包全站zip，计数(scope=site)
- POST /api/download/selected         打包勾选文章zip，逐篇计数(scope=article)
- POST /api/export/base64             导出自包含离线版，按canonical_path命名，
                                       顶部附Blogger原始地址+最后修改时间
- GET  /api/health                    健康检查
"""
import base64
import calendar
import html as html_lib
import io
import json
import mimetypes
import os
import random
import re
import shutil
import subprocess
import sys
import threading
import time
import zipfile
from pathlib import Path

from flask import Flask, request, jsonify, send_file, Response, abort, redirect

import db
import git_publish
import github_actions
import safe_errors
import zip_cache
from internal_links import rewrite_internal_links
from telegram_notify import notify

app = Flask(__name__, static_folder="static", static_url_path="/static")
zip_cache.start_cleanup_thread()
# 数据库schema的建立/迁移不在这里做（不再有模块顶层的db.init_db()调用）。
# 之前这里无条件跑db.init_db()是为了让gunicorn直接import这个模块时（不会
# 走文件末尾"if __name__=='__main__'"）也能保证refresh_locks/refresh_targets
# 这两张表就绪——但代价是任何`import app`（包括测试、包括临时排查用的
# `python -c "import app"`）都会立刻对当时db.DB_PATH指向的文件生效，这正是
# 真实data/blog.db被测试意外污染出两张空表的根因。现在改成db.py内部的懒
# 初始化（见db._ensure_schema()）：首次真正访问数据库时才建表，无论是生产
# 环境的gunicorn worker收到第一个真实请求，还是测试先把db.DB_PATH指向
# 临时文件——import这个模块本身不再触发任何数据库I/O。

BASE_DIR = Path(__file__).parent
HTML_DIR = BASE_DIR / "html"
POSTS_DIR = HTML_DIR / "posts"
FETCH_SCRIPT = BASE_DIR / "fetch_blog.py"

REFRESH_COOLDOWN_SECONDS = 5 * 60
DISK_ALERT_THRESHOLD = 0.80

# 公开匿名刷新系统：mirror/backup/github/cf四个target共用的常量。
# data/last_refresh.json不再被读写——冷却计时基准和锁状态全部落在SQLite
# （db.py的refresh_locks/refresh_targets表），不保留旧文件的兼容读取：
# 旧文件里最坏情况下残留的冷却时间戳窗口只有5分钟，忽略它不会造成安全
# 问题，继续读它反而会形成"SQLite一份、JSON一份"两个可能不一致的状态
# 来源。旧文件本身不会被这里的代码删除。
CONTENT_FETCH_LOCK = "content_fetch"          # 4个target都要先跑一次fetch_blog.py，共享这把锁
GIT_PUBLISH_LOCK = "git_publish"              # github/cf共享："把html/变化commit+push"这把锁
FETCH_SUBPROCESS_TIMEOUT_SECONDS = 300         # 与下面subprocess.run(fetch_blog.py)的timeout保持一致
CONTENT_FETCH_STALE_SECONDS = FETCH_SUBPROCESS_TIMEOUT_SECONDS + 120   # 420，判定死锁年龄阈值，留2分钟余量
GIT_PUSH_TIMEOUT_SECONDS = 60                  # git push本身的subprocess超时
# S2修复：这个值必须严格大于git_publish临界区自身可能达到的最长真实执行
# 时间，否则一个正常进行中、尚未超时的git_publish会被误判为"进程已经死了"
# 而被stale-recovery机制强行抢占——这不是理论风险，是简单的算术错误
# （旧值120本身就小于当时git_publish.py内部subprocess timeout总和）。
# 逐项计算git_publish.commit_and_push()按最坏情况顺序执行完的subprocess
# timeout之和（见git_publish.py）：
#   _check_publish_preconditions: rev-parse --abbrev-ref HEAD(10) + rev-parse --git-dir(10) = 20
#   detect_changed_paths: git status --porcelain(30)                                        = 30
#   git add -- html(30)                                                                      = 30
#   git commit --only(30)                                                                    = 30
#   _push: git push(GIT_PUSH_TIMEOUT_SECONDS=60)                                             = 60
# 合计170秒。留130秒余量（覆盖磁盘/CPU繁忙时的额外调度延迟，以及SQLite
# BEGIN IMMEDIATE本身等待写锁的时间），取整300秒。
GIT_PUBLISH_STALE_SECONDS = 300
GITHUB_ACTIONS_WAIT_SECONDS = 90               # 同步HTTP请求里有界轮询Actions conclusion的上限，超过就返回202/running
# 90秒有界等待到期只是这次HTTP请求不再继续占用gunicorn worker等下去，不代表
# 没人关心结果——超时后会启动一个后台daemon线程继续跟踪，这是它的独立、
# 更长的上限（不占用HTTP worker，只占用一个后台线程，可以给得比前台等待
# 宽松很多）。这台VPS上pages.yml工作流只是"跑几个纯Python测试脚本+
# 生成静态文件+上传"，正常预期在几分钟内结束，20分钟是留了充分余量的
# "确实异常了就别再等"上限，不是精确计算出来的值，如果之后发现工作流
# 经常需要更久，直接调这一个数字即可。
GITHUB_ACTIONS_BACKGROUND_WAIT_SECONDS = 1200

GITHUB_REPO = "foxzenme/foxzen-blog"
GITHUB_PAGES_WORKFLOW_FILE = "pages.yml"
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")   # 由VPS systemd Environment=注入，见systemd/blog-mirror-api.service
# S8修复：app.py本身从不调用Cloudflare API（fetch_blog.py内部的
# _purge_cloudflare_cache()才会用到这个token，那是content_fetch子进程
# 自己读取的环境变量），这里单独读一份只是为了能在_run_content_fetch()
# 里把子进程stdout/stderr里可能意外出现的这个token值也redact掉——两个
# 进程共享同一份环境变量（subprocess.run没有传独立的env=，默认继承当前
# 进程环境），值一定相同，读一份用来对照redact不会跟子进程实际用的值不一致。
CF_API_TOKEN = os.environ.get("CF_API_TOKEN", "")
GIT_BOT_NAME = "Foxzen Refresh Bot"
GIT_BOT_EMAIL = "foxzen-refresh-bot@users.noreply.github.com"

REFRESH_CORS_ALLOWED_ORIGINS = {
    "https://mirror.foxzen.me", "https://backup.foxzen.me",
    "https://github.foxzen.me", "https://cf.foxzen.me",
}

# status.foxzen.me（只读状态页，见build_status_page.py）只需要能读取
# GET-only的/api/health和/api/refresh/<target>/status，用来展示"最近一次
# 同步状态"，不应该获得触发/api/refresh/<target>这个POST写操作的任何新
# 能力——加不加CORS都不影响POST本身能不能被调用（匿名公开触发是既有设计，
# 见下面_apply_refresh_cors的说明），CORS只影响"哪些网页的JS能读到响应
# 内容"。这里特意用一个单独、更宽的集合，只给两个GET-only端点用，不给
# POST触发端点用，让"这次改动到底新增了什么"在代码里一眼可辨。
STATUS_PAGE_ORIGIN = "https://status.foxzen.me"
# GitHub Pages卫星仓库foxzen-status发布的页面使用默认github.io地址
# https://foxzenme.github.io/foxzen-status/（这一侧不绑定status.foxzen.me
# 自定义域名，见build_status_page.py::publish_status_page()的
# write_cname=False），页面本身跟status.foxzen.me是同一份PAGE_TEMPLATE、
# 发起完全相同的探测请求，只是浏览器发起请求时的Origin不同——不加这一条，
# 这个渠道打开时mirror/backup/github/cf/greencloud几行会因为CORS被挡住而
# 全部显示"无法访问"，即使GreenCloud本身完全正常，就不是一个真正独立、
# 功能对等的备用渠道了。GitHub Pages项目页面(<user>.github.io/<repo>/)的
# CORS Origin只到github.io这一级、不含仓库名路径，所以foxzen-status和
# foxzen-update两个卫星仓库共用这一个origin，不需要分别列出。
GITHUB_PAGES_STATUS_ORIGIN = "https://foxzenme.github.io"
STATUS_READ_CORS_ALLOWED_ORIGINS = REFRESH_CORS_ALLOWED_ORIGINS | {STATUS_PAGE_ORIGIN, GITHUB_PAGES_STATUS_ORIGIN}


# ---------------------------------------------------------------------------
# 页面路由（原来由nginx直接serve，现在经Flask计数后返回）
# ---------------------------------------------------------------------------

QUOTES_FILE = BASE_DIR / "data" / "quotes.txt"


def _random_quote() -> str:
    """从quotes.txt随机抽一行。每次请求都重新读文件（不缓存），
    这样用户直接编辑txt文件就立刻生效，不用重启服务。
    文件不存在或是空的时候返回空字符串，不报错，页面上就是没这行字，不影响其他内容。
    """
    try:
        lines = [ln.strip() for ln in QUOTES_FILE.read_text(encoding="utf-8").splitlines()]
        lines = [ln for ln in lines if ln]
        return random.choice(lines) if lines else ""
    except FileNotFoundError:
        return ""


def _visitor_ip() -> str:
    """取访客真实IP。mirror.foxzen.me走Cloudflare代理，Flask直接拿到的连接IP
    是Cloudflare的边缘节点IP，不是访客的——必须读CF-Connecting-IP这个头。
    没有这个头（比如以后哪天不走Cloudflare了）就退回nginx传的X-Real-IP，
    再退回原始连接IP，保证任何情况下都有个值可用。
    """
    return (request.headers.get("CF-Connecting-IP")
            or request.headers.get("X-Real-IP")
            or request.remote_addr
            or "unknown")


@app.route("/", methods=["GET"])
@app.route("/index.html", methods=["GET"])
def index_page():
    index_file = HTML_DIR / "index.html"
    if not index_file.exists():
        abort(404)
    db.record_page_hit(None, visitor_key=_visitor_ip())
    html = index_file.read_text(encoding="utf-8")
    html = html.replace("<!--QUOTE-->", html_lib.escape(_random_quote()))
    return Response(html, mimetype="text/html")


@app.route("/<int:number>/", methods=["GET"])
def short_link(number):
    conn = db.get_conn()
    row = conn.execute("SELECT post_id FROM post_numbers WHERE number = ?", (number,)).fetchone()
    conn.close()
    if not row:
        abort(404)
    canonical = db.get_canonical_path(row["post_id"])
    if canonical:
        return redirect(f"/{canonical}.html", code=302)
    # 极端兜底：这篇文章没能解析出canonical_path（permalink格式异常），
    # 退回旧的post_id路径，至少不404
    return redirect(f"/posts/{row['post_id']}/", code=302)


@app.route("/404/", methods=["GET"])
@app.route("/404/index.html", methods=["GET"])
def easter_egg_404():
    f = HTML_DIR / "404" / "index.html"
    if not f.exists():
        abort(404)
    html = f.read_text(encoding="utf-8")
    html = html.replace("<!--QUOTE-->", html_lib.escape(_random_quote()))
    return Response(html, mimetype="text/html")


def _serve_post_response(post_id: str, raw_html: str) -> Response:
    """mirror/backup共用的文章响应生成：把正文里"引用本站另一篇文章"的Blogger
    permalink改写成本站根相对地址，再记一次访问。改写只产出不带协议/域名的
    相对路径，不需要也不判断request.host——浏览器天然按当前访问的域名解析，
    mirror请求留在mirror，backup请求留在backup（见internal_links.py）。

    只用read_text()读到的文本做内存改写，磁盘上的html/posts/<post_id>/index.html
    本身不会被修改——_inline_post_as_base64()读到的仍是原始Blogger链接，离线
    下载/导出功能不受影响。
    """
    permalink_to_url = db.get_all_permalinks()
    own_permalink = db.get_source_url(post_id)
    html_text = rewrite_internal_links(raw_html, permalink_to_url, own_permalink)
    db.record_page_hit(post_id, visitor_key=_visitor_ip())
    return Response(html_text, mimetype="text/html")


@app.route("/posts/<post_id>/", methods=["GET"])
@app.route("/posts/<post_id>/index.html", methods=["GET"])
def legacy_post_link(post_id):
    canonical = db.get_canonical_path(post_id)
    if canonical:
        return redirect(f"/{canonical}.html", code=301)
    post_dir = POSTS_DIR / post_id
    index_file = post_dir / "index.html"
    if not index_file.exists():
        abort(404)
    return _serve_post_response(post_id, index_file.read_text(encoding="utf-8"))


@app.route("/<int:year>/<int:month>/<slug>.html", methods=["GET"])
def canonical_post_page(year, month, slug):
    canonical_path = f"{year}/{month:02d}/{slug}"
    post = db.get_post_by_canonical_path(canonical_path)
    if not post:
        abort(404)
    index_file = POSTS_DIR / post["post_id"] / "index.html"
    if not index_file.exists():
        abort(404)
    return _serve_post_response(post["post_id"], index_file.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

REFRESH_TARGET_CONVERTER = "mirror,backup,github,cf"

# B2/B3修复引入的git_publish错误类别里，哪些值得建议客户端重试：
# credentials_missing/wrong_branch/repository_busy/repository_state_error/
# remote_diverged都指向"需要人工介入的仓库/配置状态问题"，不是网络/超时
# 这类会自愈的瞬时故障——直接重试同一个请求会在同样的地方再次失败，
# 白白消耗一次5分钟冷却窗口，还可能对github多触发一次不必要的
# workflow_dispatch。其余错误类别（比如git_commit_error/git_push_error，
# 通常对应网络抖动/超时）保持retry_recommended=True。
_NON_RETRYABLE_GIT_ERROR_CATEGORIES = {
    "credentials_missing", "wrong_branch", "repository_busy",
    "repository_state_error", "remote_diverged",
}


def _run_content_fetch(target_key: str) -> dict:
    """4个target共用的第一阶段：原子检查target_key自己的冷却 + content_fetch/
    git_publish双向互斥 + 跑一次fetch_blog.py。

    双向互斥用cross_check_idle实现：content_fetch的获取会检查git_publish
    是否idle（防止content_fetch正在改写html/的同时git_publish在git add，
    产生撕裂读）；反过来_run_git_publish()获取git_publish时也会检查
    content_fetch是否idle——两边都不为了"少一点409"而破坏这个互斥，这是
    你明确要求的取舍。

    返回：
      {"acquired": False, "reason": "cooldown", "cooldown_remaining_seconds": int}
      {"acquired": False, "reason": "busy_content_fetch" | "busy_git_publish"}
      {"acquired": True, "status": "ok" | "error", "detail": str, "post_count": None, "target_generation": int}
    """
    acquire = db.try_acquire_lock(
        CONTENT_FETCH_LOCK, CONTENT_FETCH_STALE_SECONDS,
        target_key=target_key, cooldown_seconds=REFRESH_COOLDOWN_SECONDS,
        cross_check_idle=((GIT_PUBLISH_LOCK, GIT_PUBLISH_STALE_SECONDS),),
        triggered_by=target_key,
    )
    if not acquire["acquired"]:
        return acquire

    status, detail, post_count = "error", "未知错误", None
    try:
        result = subprocess.run(
            [sys.executable, str(FETCH_SCRIPT)],
            cwd=str(BASE_DIR), capture_output=True, text=True, timeout=FETCH_SUBPROCESS_TIMEOUT_SECONDS,
        )
        ok = result.returncode == 0
        status = "ok" if ok else "error"
        detail = result.stdout[-500:] if ok else result.stderr[-500:]
    except subprocess.TimeoutExpired:
        status, detail = "error", f"抓取超时(>{FETCH_SUBPROCESS_TIMEOUT_SECONDS}s)"
    except Exception as e:
        status, detail = "error", str(e)
    finally:
        # S8修复：fetch_blog.py子进程的stdout/stderr内容不受这里控制——
        # 失败时可能是原始异常堆栈/文件路径，成功时的stdout里也可能包含
        # _purge_cloudflare_cache()打印的诊断信息，理论上可能意外带出
        # GITHUB_TOKEN/CF_API_TOKEN（后者是子进程自己从环境变量读到的）。
        # 这里精确redact这两个已知的真实secret值，再写入下面的内部存储——
        # 这一步不是"外部不可见"的唯一防线（外部response另有safe_errors.
        # safe_public_detail()按status/error_category生成固定模板，根本
        # 不会用到这个detail原始文本），而是保证即使是内部诊断记录（供
        # 运维sqlite3直接查看）也不会意外留下真实凭据。
        detail = safe_errors.redact_known_secrets(detail, GITHUB_TOKEN, CF_API_TOKEN)
        # 无论成功/失败/超时/未预期异常，都必须释放锁，否则content_fetch
        # 会永久停在running，后续所有刷新请求都会被误判为运行中拒绝掉。
        db.release_lock(CONTENT_FETCH_LOCK, acquire["generation"], status, detail, post_count=post_count)

    return {"acquired": True, "status": status, "detail": detail, "post_count": post_count,
            "target_generation": acquire["target_generation"]}


def _run_git_publish(target_key: str) -> dict:
    """github/cf共用的第二阶段：原子获取git_publish锁（同样检查content_fetch
    是否idle）-> 校验分支/仓库状态(B3) -> 检测html/实际变化 -> 只有真的有
    变化才commit（O-2：没有变化绝不产生空commit）-> 无论本轮是否有变化都
    尝试push一次（B2：不能因为本轮html/无变化就跳过push——上一轮如果
    commit成功但push失败，会在本地留下一个从未真正推送的commit，必须
    在下一次调用时继续补上，不能被静默当作"无变化"而永远遗漏）。

    返回：
      {"acquired": False, "reason": "busy_content_fetch" | "busy_git_publish"}
      {"acquired": True, "pushed": True, "commit_sha": str | None,
       "changed_file_count": int, "push_state": "noop" | "pushed"}
      {"acquired": True, "pushed": False, "error_category": str, "detail": str}
        错误分类详见git_publish.commit_and_push()的文档字符串（B1/B2/B3
        引入了wrong_branch/repository_busy/repository_state_error/
        remote_diverged几种新类别）。
    """
    if not GITHUB_TOKEN:
        return {"acquired": True, "pushed": False, "error_category": "credentials_missing",
                "detail": "服务器未配置GITHUB_TOKEN，无法推送"}

    acquire = db.try_acquire_lock(
        GIT_PUBLISH_LOCK, GIT_PUBLISH_STALE_SECONDS,
        cross_check_idle=((CONTENT_FETCH_LOCK, CONTENT_FETCH_STALE_SECONDS),),
        triggered_by=target_key,
    )
    if not acquire["acquired"]:
        return acquire

    commit_message = f"content sync via {target_key} refresh"
    status, detail = "error", "未知错误"
    push_result = {"pushed": False, "error_category": "git_commit_error", "detail": detail}
    try:
        push_result = git_publish.commit_and_push(
            BASE_DIR, "html", GIT_BOT_NAME, GIT_BOT_EMAIL, commit_message,
            GITHUB_TOKEN, GIT_PUSH_TIMEOUT_SECONDS,
        )
        status = "ok" if push_result["pushed"] else "error"
        detail = "" if push_result["pushed"] else push_result["detail"]
    except Exception as e:
        # S8修复：git_publish.commit_and_push()正常情况下不会让token出现在
        # 任何异常文本里（见git_publish.py自己的redact），这里是这个函数
        # 完全未预料到的异常类型（比如它本身的bug）时的最后一道防线。
        scrubbed_detail = safe_errors.redact_known_secrets(str(e), GITHUB_TOKEN)
        push_result = {"pushed": False, "error_category": "git_commit_error", "detail": scrubbed_detail}
        status, detail = "error", scrubbed_detail
    finally:
        db.release_lock(
            GIT_PUBLISH_LOCK, acquire["generation"], status, detail,
            commit_sha=(push_result.get("commit_sha") if push_result.get("pushed") else None),
        )

    return {"acquired": True, **push_result}


def _rejection_response(target: str, outcome: dict):
    reason = outcome["reason"]
    if reason == "cooldown":
        return jsonify({
            "target": target, "status": "cooldown",
            "cooldown_remaining_seconds": outcome["cooldown_remaining_seconds"],
        }), 429
    # busy_content_fetch / busy_git_publish：明确告诉调用方是哪一个内部
    # 资源正忙，而不是笼统的"running"——避免用户以为是自己这个target卡住了。
    busy_label = "内容抓取（content_fetch）" if reason == "busy_content_fetch" else "Git 发布（git_publish）"
    return jsonify({
        "target": target, "status": "busy", "reason": reason,
        "detail": f"{busy_label} 正在被另一个刷新任务占用，请稍后重试",
    }), 409


def _watch_github_run_in_background(target_key, run_id, run_html_url, commit_sha, expected_generation):
    """90秒有界等待到期、已经给客户端返回202之后，用一个独立的后台daemon
    线程继续跟踪这个run真正的conclusion——git_publish锁在这之前已经正常
    释放（它的职责到"push完成"为止，不延伸到"等Actions跑完"），这个线程
    不持有、也不需要持有任何锁，只是单纯地继续问GitHub"这个run跑完了没"，
    跑完之后把真实结果写回refresh_targets，让GET /api/refresh/github/status
    最终能看到真实的success/failure，而不是永远停留在"上一次更早的结果"。

    expected_generation：派发这次github刷新时refresh_targets.generation
    的值。写回结果前，record_target_result()会重新核对这一行是否还是这个
    值——如果在等待期间同一个target又发起了新一轮刷新（cooldown过期后
    被再次点击，generation已经前进），说明这个后台线程跟踪的已经是过时
    的一轮，写入会被静默拒绝，不会用旧run的迟到结果覆盖新一轮的状态
    （跟release_lock()的generation fencing是同一个思路，这里保护的是
    refresh_targets这一行）。

    已知的、如实披露的局限：这是进程内的daemon线程，不是能扛住进程重启
    的持久化任务队列。如果gunicorn worker在这个线程跑完之前被回收/重启，
    这次跟踪会跟着丢失（refresh_targets就停留在没有最终结论的状态，用户
    需要自己去GitHub Actions页面确认，或者等5分钟冷却过后重新点一次）。
    这是"不引入Redis/Celery，只用项目里已有的线程机制（同zip_cache.py的
    start_cleanup_thread()）"这个明确取舍下的已知代价，不是被忽略的问题。
    """
    def _watch():
        try:
            result = github_actions.poll_until_conclusion(
                GITHUB_REPO, run_id, run_html_url, GITHUB_TOKEN,
                max_wait_seconds=GITHUB_ACTIONS_BACKGROUND_WAIT_SECONDS,
            )
        except Exception as e:
            db.record_target_result(
                target_key, "failure",
                safe_errors.redact_known_secrets(f"后台跟踪Actions结论时异常: {e}", GITHUB_TOKEN),
                error_category="actions_run_failed", retry_recommended=True,
                commit_sha=commit_sha, expected_generation=expected_generation)
            return

        if result["outcome"] == "success":
            db.record_target_result(target_key, "success", "", commit_sha=commit_sha,
                                     expected_generation=expected_generation)
        elif result["outcome"] == "failure":
            db.record_target_result(
                target_key, "failure", f"GitHub Actions run 结论为 {result.get('conclusion')}",
                error_category="actions_run_failed", retry_recommended=True,
                commit_sha=commit_sha, expected_generation=expected_generation,
            )
        else:  # outcome == "timeout"：连后台这次更长的等待也放弃了
            db.record_target_result(
                target_key, "failure",
                f"GitHub Actions run 长时间（>{GITHUB_ACTIONS_BACKGROUND_WAIT_SECONDS}s）未产出结论，"
                f"已放弃跟踪，请手动查看Actions页面确认",
                error_category="actions_run_unresolved", retry_recommended=True,
                commit_sha=commit_sha, expected_generation=expected_generation,
            )

    threading.Thread(target=_watch, daemon=True, name=f"github-actions-watch-{run_id}").start()


@app.route(f"/api/refresh/<any({REFRESH_TARGET_CONVERTER}):target>", methods=["POST"])
def refresh_target(target):
    fetch_outcome = _run_content_fetch(target)
    if not fetch_outcome["acquired"]:
        return _rejection_response(target, fetch_outcome)

    # 派发这一轮刷新时target自己的fencing token，后续所有record_target_result()
    # 调用都带上它，作为"这次写入是否仍对应当前这一轮"的依据（见
    # db.record_target_result()的expected_generation参数说明——用严格
    # 递增的整数而不是时间戳，因为秒级精度的时间戳在cooldown_seconds=0
    # 等场景下可能同一秒内重复，不能可靠地分辨"是不是同一轮"）。
    expected_generation = fetch_outcome["target_generation"]

    if target in ("mirror", "backup"):
        status = "success" if fetch_outcome["status"] == "ok" else "failure"
        error_category = None if status == "success" else "content_fetch_error"
        db.record_target_result(target, status, fetch_outcome["detail"], error_category=error_category,
                                 retry_recommended=(True if status == "failure" else None),
                                 expected_generation=expected_generation)
        # S8修复：fetch_outcome["detail"]是fetch_blog.py子进程的原始stdout/
        # stderr（供上面record_target_result()内部存档诊断用），这个接口
        # 匿名公开，对外detail必须换成safe_errors的固定模板，不能把原始
        # 输出直接返回。
        return jsonify({
            "target": target, "status": status,
            "detail": safe_errors.safe_public_detail(status, error_category),
            "post_count": fetch_outcome["post_count"], "commit": None,
        }), 200

    # target in ("github", "cf")：两阶段。content_fetch失败/超时直接结束，
    # 不进入git_publish阶段——没有新内容，不应该去发布。
    if fetch_outcome["status"] != "ok":
        db.record_target_result(target, "failure", fetch_outcome["detail"],
                                 error_category="content_fetch_error", retry_recommended=True,
                                 expected_generation=expected_generation)
        return jsonify({
            "target": target, "status": "failure", "error_category": "content_fetch_error",
            "detail": safe_errors.safe_public_detail("failure", "content_fetch_error"),
            "retry_recommended": True, "cooldown_applied": True,
        }), 200

    publish_outcome = _run_git_publish(target)
    if not publish_outcome["acquired"]:
        return _rejection_response(target, publish_outcome)
    if not publish_outcome["pushed"]:
        retry = publish_outcome["error_category"] not in _NON_RETRYABLE_GIT_ERROR_CATEGORIES
        db.record_target_result(target, "failure", publish_outcome["detail"],
                                 error_category=publish_outcome["error_category"], retry_recommended=retry,
                                 expected_generation=expected_generation)
        return jsonify({
            "target": target, "status": "failure", "error_category": publish_outcome["error_category"],
            "detail": safe_errors.safe_public_detail("failure", publish_outcome["error_category"]),
            "retry_recommended": retry, "cooldown_applied": True,
        }), 200

    commit_sha = publish_outcome["commit_sha"]
    changed = publish_outcome["changed_file_count"]
    push_state = publish_outcome["push_state"]

    # B2修复：判断"是否跳过发布"必须看push_state是不是真正的noop，不能只看
    # changed_file_count是否为0——changed==0但push_state=="pushed"意味着
    # 这一轮html/本身确实没有新变化，但补上了之前某次push失败遗留的本地
    # commit（这个commit此刻才真正推送到了remote）。这种情况下github/cf
    # 都必须继续走正常发布流程（github要重新dispatch，cf要如实报告push
    # 成功），不能因为changed==0就跳过——那正是B2要修的"push失败被永久
    # 静默掩盖成成功"问题的另一面：如果只看changed就跳过，这次真正发生的
    # push会被完全隐瞒，github也不会为这批终于推送出去的内容触发部署。
    if push_state == "noop":
        detail = "内容无变化，未产生新提交，未触发重新部署"
        db.record_target_result(target, "success", detail, commit_sha=commit_sha,
                                 expected_generation=expected_generation)
        return jsonify({
            "target": target, "status": "success", "commit": commit_sha,
            "changed_file_count": 0, "detail": detail,
        }), 200

    if target == "cf":
        # 没有真正查询Cloudflare部署状态，success只能代表push成功、已经
        # 移交给Cloudflare Pages的Git Integration，不能声称部署已完成。
        detail = "git push successful; handed off to Cloudflare Pages（未查询实际部署状态）"
        db.record_target_result(target, "success", detail, commit_sha=commit_sha,
                                 expected_generation=expected_generation)
        return jsonify({
            "target": target, "status": "success", "commit": commit_sha,
            "changed_file_count": changed, "detail": detail,
        }), 200

    # target == "github"：workflow_dispatch + 有界轮询真实conclusion
    if not GITHUB_TOKEN:
        db.record_target_result(target, "failure", "服务器未配置GITHUB_TOKEN",
                                 error_category="credentials_missing", retry_recommended=False,
                                 expected_generation=expected_generation)
        return jsonify({
            "target": target, "status": "failure", "error_category": "credentials_missing",
            "detail": "服务器未配置GitHub凭据", "retry_recommended": False, "cooldown_applied": True,
        }), 200

    try:
        gh_result = github_actions.trigger_and_wait(
            GITHUB_REPO, GITHUB_PAGES_WORKFLOW_FILE, GITHUB_TOKEN,
            wait_seconds=GITHUB_ACTIONS_WAIT_SECONDS,
        )

        if gh_result["outcome"] == "success":
            db.record_target_result(target, "success", "", commit_sha=commit_sha,
                                     expected_generation=expected_generation)
            return jsonify({
                "target": target, "status": "success", "commit": commit_sha, "changed_file_count": changed,
                "run_id": gh_result["run_id"], "run_html_url": gh_result["run_html_url"], "detail": "",
            }), 200

        if gh_result["outcome"] == "timeout":
            # 有界等待到期，conclusion尚未产出：不是"没人关心了"，启动后台
            # 线程继续跟踪真实结论（见_watch_github_run_in_background()），
            # HTTP响应本身如实返回running+真实run_id/URL，绝不假装success。
            _watch_github_run_in_background(target, gh_result["run_id"], gh_result["run_html_url"],
                                             commit_sha, expected_generation)
            return jsonify({
                "target": target, "status": "running", "commit": commit_sha,
                "run_id": gh_result["run_id"], "run_html_url": gh_result["run_html_url"],
                "detail": "内容已推送，Actions已触发，结论尚未产出，已转入后台继续跟踪，"
                          "请稍后查询状态或直接查看Actions页面",
            }), 202

        # outcome == "failure"：真实conclusion。conclusion本身取值是GitHub
        # 文档化的固定小枚举（success/failure/cancelled/timed_out/...），
        # 不是任意文本，但S8要求对外detail一律走固定模板——真实conclusion
        # 仍然完整写进下面record_target_result()的内部存档，需要区分具体
        # 是哪种conclusion时，运维可以直接查refresh_targets这一行。
        db.record_target_result(target, "failure", f"GitHub Actions run 结论为 {gh_result['conclusion']}",
                                 error_category="actions_run_failed", retry_recommended=True,
                                 commit_sha=commit_sha, expected_generation=expected_generation)
        return jsonify({
            "target": target, "status": "failure", "error_category": "actions_run_failed",
            "detail": safe_errors.safe_public_detail("failure", "actions_run_failed"),
            "run_id": gh_result["run_id"], "run_html_url": gh_result["run_html_url"],
            "retry_recommended": True, "cooldown_applied": True,
        }), 200

    except github_actions.GitHubActionsError as e:
        # e.detail可能包含反复重试后最后一次的原始异常文本（github_actions.py
        # 内部已经redact过token，但可能还带着其它诊断细节，比如URL片段），
        # 内部存档保留原样，对外detail同样必须走safe_errors的固定模板。
        db.record_target_result(target, "failure", e.detail, error_category=e.error_category,
                                 retry_recommended=True, commit_sha=commit_sha,
                                 expected_generation=expected_generation)
        return jsonify({
            "target": target, "status": "failure", "error_category": e.error_category,
            "detail": safe_errors.safe_public_detail("failure", e.error_category),
            "retry_recommended": True, "cooldown_applied": True,
        }), 200
    except Exception as e:
        # S4防御性兜底：github_actions.py内部已经把已知的HTTP/网络异常都
        # 转成了上面这个专门分支能处理的GitHubActionsError，这里只是防止
        # 任何未预料到的异常类型（不是刻意假设一定会发生，而是"万一发生"）
        # 导致这次请求在git push已经成功之后，却因为一个未捕获异常而
        # 完全不记录任何target结果——那样用户看到的是一个通用的500，且
        # refresh_targets会永远停留在上一轮的旧状态，无从得知这次push
        # 其实已经成功。
        db.record_target_result(
            target, "failure",
            safe_errors.redact_known_secrets(f"处理GitHub Actions结果时发生未预期异常: {e}", GITHUB_TOKEN),
            error_category="internal_error", retry_recommended=True,
            commit_sha=commit_sha, expected_generation=expected_generation)
        return jsonify({
            "target": target, "status": "failure", "error_category": "internal_error",
            "detail": f"处理GitHub Actions结果时发生未预期异常，内容已经push成功(commit={commit_sha})，"
                      f"请查看Actions页面或稍后重试",
            "retry_recommended": True, "cooldown_applied": True,
        }), 200


_LOCK_STALE_SECONDS_BY_KEY = {
    CONTENT_FETCH_LOCK: CONTENT_FETCH_STALE_SECONDS,
    GIT_PUBLISH_LOCK: GIT_PUBLISH_STALE_SECONDS,
}


@app.route(f"/api/refresh/<any({REFRESH_TARGET_CONVERTER}):target>/status", methods=["GET"])
def refresh_target_status(target):
    return jsonify(db.get_target_status(target, REFRESH_COOLDOWN_SECONDS, _LOCK_STALE_SECONDS_BY_KEY))


@app.route(f"/api/refresh/<any({REFRESH_TARGET_CONVERTER}):target>", methods=["OPTIONS"])
@app.route(f"/api/refresh/<any({REFRESH_TARGET_CONVERTER}):target>/status", methods=["OPTIONS"])
def refresh_target_options(target):
    # 实际CORS响应头由_apply_refresh_cors()这个after_request钩子统一加，
    # 这里只需要针对预检请求返回一个空的成功响应。
    return Response(status=204)


@app.after_request
def _apply_refresh_cors(response):
    """只对/api/refresh*路径和/api/health生效，不是全局CORS。精确匹配
    请求方自己的Origin（不是拼通配符），不允许的origin不加这个响应头——
    浏览器会因此拒绝跨域读取响应内容，等同拒绝。绝不设置
    Access-Control-Allow-Credentials（本来也不需要携带cookie）。

    两档白名单：
    - /api/refresh/<target>（POST触发本身 + 对应OPTIONS预检）：只允许
      mirror/backup/github/cf——这四个是唯一会在页面上放"刷新"按钮、
      需要读取触发结果的站点，范围维持原样不扩大。
    - /api/refresh/<target>/status（GET查询 + OPTIONS）和/api/health
      （GET）：额外允许status.foxzen.me和foxzenme.github.io（同一份
      status页面的GreenCloud/GitHub Pages两个独立渠道）——它们只读展示
      这些端点本来就公开的数据，从不调用POST（见test_status_page.py的
      验证），加这个白名单不代表获得了任何新的写权限。
    """
    path = request.path
    if path == "/api/health":
        allowed_origins = STATUS_READ_CORS_ALLOWED_ORIGINS
    elif path.startswith("/api/refresh"):
        allowed_origins = STATUS_READ_CORS_ALLOWED_ORIGINS if path.endswith("/status") else REFRESH_CORS_ALLOWED_ORIGINS
    else:
        return response

    origin = request.headers.get("Origin")
    if origin in allowed_origins:
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Vary"] = "Origin"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return response


@app.route("/api/finish-read/<post_id>", methods=["POST"])
def finish_read(post_id):
    """读者滚动到文章底部时前端调用这个，记一次"读完"。
    跟点击计数一样按天+IP去重，同一天同一人重复触发不会重复计数。
    """
    conn = db.get_conn()
    row = conn.execute("SELECT 1 FROM posts WHERE post_id = ?", (post_id,)).fetchone()
    conn.close()
    if not row:
        return jsonify({"error": "文章不存在"}), 404
    db.record_finish_read(post_id, visitor_key=_visitor_ip())
    return jsonify({"ok": True, "count": db.get_post_finish_read_count(post_id)})


def _year_month_range(year, month=None):
    """年份/月份筛选转成date_from/date_to，复用search_posts已有的日期范围过滤，
    不新增查询分支。year/month非法（不是数字、月份超出1-12）时返回(None, None)，
    调用方据此当作"没有筛选"处理，不因为一个坏参数就让整个接口报错。
    """
    try:
        year = int(year)
    except (TypeError, ValueError):
        return None, None
    if month is None or month == "":
        return f"{year:04d}-01-01", f"{year:04d}-12-31"
    try:
        month = int(month)
    except (TypeError, ValueError):
        return None, None
    if not 1 <= month <= 12:
        return None, None
    last_day = calendar.monthrange(year, month)[1]
    return f"{year:04d}-{month:02d}-01", f"{year:04d}-{month:02d}-{last_day:02d}"


@app.route("/api/archive", methods=["GET"])
def archive_index():
    """按年/月列出文章数量，供前端渲染年份/月份筛选下拉框。"""
    return jsonify({"years": db.get_archive_index()})


@app.route("/api/search", methods=["GET"])
def search():
    query = request.args.get("q", "")
    tag = request.args.get("tag") or None
    date_from = request.args.get("from") or None
    date_to = request.args.get("to") or None

    year = request.args.get("year") or None
    month = request.args.get("month") or None
    if year and not (date_from or date_to):
        date_from, date_to = _year_month_range(year, month)

    # 每页篇数：读者可以自己选，但服务端强制不超过20篇，不信任前端传来的数字
    try:
        page_size = int(request.args.get("page_size", 10))
    except (TypeError, ValueError):
        page_size = 10
    page_size = max(1, min(page_size, 20))

    try:
        page = int(request.args.get("page", 1))
    except (TypeError, ValueError):
        page = 1
    page = max(1, page)

    try:
        total = db.count_posts(query=query, tag=tag, date_from=date_from, date_to=date_to)
        results = db.search_posts(query=query, tag=tag, date_from=date_from, date_to=date_to,
                                   limit=page_size, offset=(page - 1) * page_size)
        click_counts = db.get_all_post_click_counts()
        download_counts = db.get_all_post_download_counts()
        export_sizes = db.get_all_export_sizes()
        finish_counts = db.get_all_finish_read_counts()
        for r in results:
            r["click_count"] = click_counts.get(r["post_id"], 0)
            r["download_count"] = download_counts.get(r["post_id"], 0)
            r["export_size_bytes"] = export_sizes.get(r["post_id"], 0)
            r["finish_read_count"] = finish_counts.get(r["post_id"], 0)
        total_pages = max(1, (total + page_size - 1) // page_size)
        return jsonify({
            "count": len(results), "results": results,
            "total": total, "page": page, "page_size": page_size, "total_pages": total_pages,
        })
    except Exception as e:
        return jsonify({"count": 0, "results": [], "error": f"查询语法有误: {e}"}), 400


def _zip_posts(post_ids):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for pid in post_ids:
            post_dir = POSTS_DIR / pid
            if not post_dir.exists():
                continue
            for f in post_dir.rglob("*"):
                if f.is_file():
                    zf.write(f, arcname=f"posts/{pid}/{f.relative_to(post_dir)}")
        index_file = HTML_DIR / "index.html"
        if index_file.exists():
            zf.write(index_file, arcname="index.html")
    buf.seek(0)
    return buf


def _send_cached_zip(path, download_name):
    """path是resolve_part_path()前一刻确认存在的文件，但"确认存在"和这里真正
    open()之间有一个极小的时间窗口——文件可能恰好在这中间被闲置清理或版本切换
    清理掉（这些都是正常的后台行为，不是故障）。用try/except兜底，把它转成
    明确的503请客户端重试，而不是让FileNotFoundError冒泡成没有意义的裸500。
    """
    try:
        return send_file(str(path), mimetype="application/zip", as_attachment=True,
                          download_name=download_name)
    except FileNotFoundError:
        return jsonify({"error": "文件在准备下载时被清理，请重新请求一次"}), 503


@app.route("/api/download/all", methods=["GET"])
def download_all():
    """整站完整ZIP走磁盘缓存（zip_cache.py），不再每次请求都现场压缩。
    单卷时保持旧行为：直接把zip文件内容返回给浏览器，文件名不变，前端下载按钮
    不需要改。超过安全阈值需要分卷时，没有"一个URL对应完整博客"这回事了，
    改为返回manifest JSON，前端后续需要相应展示"分为N个压缩包"（本次未实现，
    见部署说明）。
    """
    try:
        state = zip_cache.get_or_build()
    except RuntimeError as e:
        # 覆盖排队超时(ZipBuildError子类RuntimeError)和真正构建失败两种情况，
        # str(e)只包含zip_cache.py里写好的通用提示，不带内部路径/原始异常文本。
        return jsonify({"error": str(e)}), 503
    zip_cache.touch_last_used()

    db.record_download(None, scope="site")
    for p in db.get_all_posts():
        db.record_download(p["post_id"], scope="article")

    parts = state["parts"]
    if len(parts) == 1:
        path = zip_cache.resolve_part_path(parts[0]["name"])
        if not path:
            return jsonify({"error": "缓存文件意外丢失，请重试"}), 503
        return _send_cached_zip(path, "blog-mirror-full.zip")

    return jsonify({
        "multipart": True,
        "version": state["version"],
        "message": f"完整博客分为 {len(parts)} 个压缩包",
        "parts": [{"name": p["name"], "size": p["size"], "url": f"/download/{p['name']}"}
                   for p in parts],
    })


@app.route("/api/download/manifest", methods=["GET"])
def download_manifest():
    """查询当前完整博客ZIP的缓存状态，纯只读，不触发构建、不算一次"使用"。

    之前的实现调用了get_or_build()——一个看起来像"查询"的接口，第一次访问却
    会触发真正的压缩构建，语义上自相矛盾（而且当时这个接口还没有任何前端在用，
    改动不存在兼容性负担）。现在manifest只描述"磁盘上现在已经有什么"：
    - 还没生成过 -> cached=false, version=null, parts=[]，前端可以先显示一个
      不带具体大小的"下载完整博客"按钮；
    - 已经生成过，但文章后来又更新了 -> cached=true, current=false，manifest
      报告的是"点下载能立刻拿到的旧缓存"，不代表最新内容；
    - 已经生成过且就是最新内容 -> cached=true, current=true。
    真正的"按最新内容生成/复用"逻辑只在/api/download/all（用户点了下载）时发生。
    """
    info = zip_cache.peek_state()
    parts = info["parts"]
    return jsonify({
        "version": info["version"],
        "cached": info["cached"],
        "current": info["current"],
        "multipart": len(parts) > 1,
        "generated_at": info["generated_at"],
        "parts": [{"name": p["name"], "size": p["size"],
                   "url": f"/download/{p['name']}" if len(parts) > 1 else "/api/download/all"}
                  for p in parts],
    })


@app.route("/download/<name>", methods=["GET"])
def download_zip_part(name):
    """版本化、可被Cloudflare按URL缓存的下载地址，例如 blog-v1a2b3c4d5e6.zip。
    只允许取当前state.json里登记过的文件名，防止路径穿越或拿到已清理的旧版本文件。
    """
    path = zip_cache.resolve_part_path(name)
    if not path:
        abort(404)
    zip_cache.touch_last_used()
    return _send_cached_zip(path, name)


@app.route("/api/download/selected", methods=["POST"])
def download_selected():
    body = request.get_json(silent=True) or {}
    post_ids = body.get("post_ids") or _resolve_scope(body)
    if not post_ids:
        return jsonify({"error": "post_ids不能为空，或year/month/tag筛选条件未匹配到文章"}), 400
    buf = _zip_posts(post_ids)
    for pid in post_ids:
        db.record_download(pid, scope="article")
    return send_file(buf, mimetype="application/zip", as_attachment=True,
                      download_name="blog-mirror-selected.zip")


SAFE_FILENAME_RE = re.compile(r'[\\/:*?"<>|]')


def _safe_filename(title: str, max_len: int = 80) -> str:
    name = SAFE_FILENAME_RE.sub("_", title).strip()
    name = re.sub(r"\s+", " ", name)
    return name[:max_len] if name else "untitled"


def _download_filename_for(post_id: str) -> str:
    """按canonical_path命名下载文件：2026-07-slug.html。
    文件名不能带斜杠，年/月用短横线连接。解析不出canonical_path时退回标题命名（兜底，
    不应该发生，如果发生说明fetch_blog.py那边有文章permalink格式异常，需要去查日志）。
    """
    canonical = db.get_canonical_path(post_id)
    if canonical:
        return canonical.replace("/", "-") + ".html"
    return _safe_filename(_get_title(post_id)) + ".html"


def _zip_arcname_for(post_id: str, used_names: set) -> str:
    """离线版(base64内联)归档内的条目路径：年/月/<安全标题>.html。

    年/月来自这篇文章的发布日期(posts.published，"YYYY-MM-DD")，文件名来自
    标题经_safe_filename()清洗——不再用canonical_path/Blogger slug拼路径。
    "网页canonical URL存不存在"和"下载归档内部文件名应该是什么"是两个独立
    概念：canonical_path只影响/YYYY/MM/slug.html这个网页地址本身，跟用户
    下载到本地后看到的归档文件名无关，即便某篇文章解析不出canonical_path
    （permalink格式异常），归档命名依然按发布日期+标题稳定生成，不受影响。
    """
    published = _get_published(post_id)
    safe_title = _safe_filename(_get_title(post_id))
    if len(published) >= 7 and published[4] == "-":
        name = f"{published[:4]}/{published[5:7]}/{safe_title}.html"
    else:
        # 发布日期缺失/格式异常的兜底：理论上不该发生（published在入库时
        # 就已经是"YYYY-MM-DD"），发生了也不能让整个导出失败，退回不带
        # 年/月的纯标题命名。
        name = f"{safe_title}.html"
    if name in used_names:
        base, ext = name.rsplit(".", 1)
        name = f"{base}-{post_id}.{ext}"
    used_names.add(name)
    return name


def _source_note_html(post_id: str) -> str:
    """下载文件顶部的来源信息条：直接读数据库里存的source_url（Blogger当次抓取
    返回的真实完整地址），不再靠硬编码域名拼——博客换域名，重新抓一次就自动更新。
    """
    conn = db.get_conn()
    row = conn.execute("SELECT source_url, updated FROM posts WHERE post_id = ?", (post_id,)).fetchone()
    conn.close()
    updated = (row["updated"] if row else "") or "未知"
    source_url = row["source_url"] if row else None

    if source_url:
        source_line = f'本文镜像自 <a href="{source_url}">{source_url}</a>'
    else:
        source_line = "本文镜像自主站（原始地址暂缺，请在主站搜索标题核对）"

    return (
        '<div style="border-bottom:1px solid #ddd;padding-bottom:12px;margin-bottom:20px;'
        'font-size:0.85em;color:#666;">'
        f'{source_line}<br>最后修改时间：{updated}'
        '</div>'
    )


GA_BLOCK_RE = re.compile(r"<!-- GA_START -->.*?<!-- GA_END -->\s*", re.DOTALL)
FINISH_READ_BLOCK_RE = re.compile(r"<!-- FINISH_READ_START -->.*?<!-- FINISH_READ_END -->\s*", re.DOTALL)


def _inline_post_as_base64(post_id: str) -> str | None:
    post_dir = POSTS_DIR / post_id
    index_file = post_dir / "index.html"
    if not index_file.exists():
        return None

    html = index_file.read_text(encoding="utf-8")
    # 离线归档文件不需要联网追踪脚本，去掉
    html = GA_BLOCK_RE.sub("", html)
    html = FINISH_READ_BLOCK_RE.sub("", html)

    # 兼容两种历史格式：旧的相对路径 media/xxx，和新的绝对路径 /posts/{id}/media/xxx
    img_src_re = re.compile(rf'src="(?:/posts/{re.escape(post_id)}/)?media/([^"]+)"')

    missing = []

    def replace_one(m):
        filename = m.group(1)
        file_path = post_dir / "media" / filename
        if not file_path.exists():
            missing.append(filename)
            return m.group(0)
        mime, _ = mimetypes.guess_type(filename)
        mime = mime or "application/octet-stream"
        data = base64.b64encode(file_path.read_bytes()).decode("ascii")
        return f'src="data:{mime};base64,{data}"'

    html = img_src_re.sub(replace_one, html)

    if missing:
        # 之前这里是完全静默的——留着相对/绝对路径链接指向本地磁盘，下载到本地后
        # 打开自然是空图。现在至少打印出来，方便你从gunicorn日志里查是哪几篇。
        print(f"  [警告] {post_id} 导出base64时有{len(missing)}张图片在磁盘上找不到: {missing}")

    html = html.replace('<a class="back" href="/" onclick="if (history.length > 1) { history.back(); return false; }">&larr; 返回目录</a>', "")

    # 插入来源信息条：放在 <div class="content"> 之前
    note = _source_note_html(post_id)
    if '<div class="content">' in html:
        html = html.replace('<div class="content">', note + '<div class="content">', 1)
    else:
        html = note + html

    return html


def _resolve_scope(body: dict):
    if body.get("post_ids"):
        return list(body["post_ids"])
    if body.get("year"):
        date_from, date_to = _year_month_range(body.get("year"), body.get("month"))
        if date_from is None:
            return []
        return [p["post_id"] for p in db.search_posts(tag=body.get("tag"), date_from=date_from,
                                                        date_to=date_to, limit=10000)]
    if body.get("tag"):
        return [p["post_id"] for p in db.search_posts(tag=body["tag"], limit=10000)]
    if body.get("all"):
        return [p["post_id"] for p in db.get_all_posts()]
    return []


def _get_title(post_id: str) -> str:
    conn = db.get_conn()
    row = conn.execute("SELECT title FROM posts WHERE post_id = ?", (post_id,)).fetchone()
    conn.close()
    return row["title"] if row else post_id


def _get_published(post_id: str) -> str:
    """posts.published是"YYYY-MM-DD"（只存年月日，完整时间戳排序另有
    published_ts字段，见db.py注释），_zip_arcname_for()用它推导归档路径
    里的年/月子目录。"""
    conn = db.get_conn()
    row = conn.execute("SELECT published FROM posts WHERE post_id = ?", (post_id,)).fetchone()
    conn.close()
    return (row["published"] if row else "") or ""


@app.route("/api/export/base64", methods=["POST"])
def export_base64():
    body = request.get_json(silent=True) or {}
    post_ids = _resolve_scope(body)
    if not post_ids:
        return jsonify({"error": "未指定导出范围，需提供post_ids/tag/all之一"}), 400

    is_site_scope = bool(body.get("all"))

    if len(post_ids) == 1:
        pid = post_ids[0]
        html = _inline_post_as_base64(pid)
        if html is None:
            return jsonify({"error": f"文章不存在: {pid}"}), 404
        db.record_download(pid, scope="article")
        filename = _download_filename_for(pid)
        buf = io.BytesIO(html.encode("utf-8"))
        return send_file(buf, mimetype="text/html", as_attachment=True,
                          download_name=filename)

    buf = io.BytesIO()
    used_names = set()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for pid in post_ids:
            html = _inline_post_as_base64(pid)
            if html is None:
                continue
            name = _zip_arcname_for(pid, used_names)
            zf.writestr(name, html)
            db.record_download(pid, scope="article")
    buf.seek(0)

    if is_site_scope:
        db.record_download(None, scope="site")

    return send_file(buf, mimetype="application/zip", as_attachment=True,
                      download_name="blog-mirror-standalone.zip")


def _disk_usage_ratio(path=BASE_DIR):
    total, used, free = shutil.disk_usage(path)
    return used / total


_last_disk_alert_ts = 0.0
DISK_ALERT_COOLDOWN = 6 * 3600


@app.route("/api/health", methods=["GET"])
def health():
    global _last_disk_alert_ts
    last_fetch = db.get_last_fetch_status()
    all_posts = db.get_all_posts()
    disk_ratio = _disk_usage_ratio()

    if disk_ratio >= DISK_ALERT_THRESHOLD:
        now = time.time()
        if now - _last_disk_alert_ts > DISK_ALERT_COOLDOWN:
            notify(f"⚠️ blog-mirror 磁盘使用率已达 {disk_ratio*100:.1f}%，超过{int(DISK_ALERT_THRESHOLD*100)}%阈值")
            _last_disk_alert_ts = now

    return jsonify({
        "post_count": len(all_posts),
        "last_fetch": last_fetch,
        "disk_usage_ratio": round(disk_ratio, 4),
        "disk_alert": disk_ratio >= DISK_ALERT_THRESHOLD,
        "visit_stats": db.get_visit_stats(),
    })


if __name__ == "__main__":
    # 不需要在这里显式调用db.init_db()：db.py的_ensure_schema()会在第一次
    # 真正访问数据库时（比如下面app.run()收到第一个请求）懒初始化。
    app.run(host="127.0.0.1", port=5000)
