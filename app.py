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
- POST /api/refresh                   手动刷新，限流
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
import random
import re
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path
from threading import Lock

from flask import Flask, request, jsonify, send_file, Response, abort, redirect

import db
import zip_cache
from internal_links import rewrite_internal_links
from telegram_notify import notify

app = Flask(__name__, static_folder="static", static_url_path="/static")
zip_cache.start_cleanup_thread()

BASE_DIR = Path(__file__).parent
HTML_DIR = BASE_DIR / "html"
POSTS_DIR = HTML_DIR / "posts"
FETCH_SCRIPT = BASE_DIR / "fetch_blog.py"

REFRESH_COOLDOWN_SECONDS = 5 * 60
DISK_ALERT_THRESHOLD = 0.80

REFRESH_STATE_FILE = BASE_DIR / "data" / "last_refresh.json"
_refresh_lock = Lock()


def _read_refresh_state():
    try:
        return json.loads(REFRESH_STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"last_refresh_ts": 0.0, "last_result": {"status": "unknown", "detail": "尚未执行过刷新"}}


def _write_refresh_state(state):
    REFRESH_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    REFRESH_STATE_FILE.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")


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

@app.route("/api/refresh", methods=["POST"])
def refresh():
    with _refresh_lock:
        state = _read_refresh_state()
        now = time.time()
        elapsed = now - state.get("last_refresh_ts", 0.0)
        if elapsed < REFRESH_COOLDOWN_SECONDS:
            remaining = int(REFRESH_COOLDOWN_SECONDS - elapsed)
            return jsonify({
                "executed": False,
                "reason": f"距离上次刷新不足{REFRESH_COOLDOWN_SECONDS//60}分钟，请{remaining}秒后再试",
                "last_result": state.get("last_result"),
            }), 429

        try:
            result = subprocess.run(
                [sys.executable, str(FETCH_SCRIPT)],
                cwd=str(BASE_DIR), capture_output=True, text=True, timeout=300,
            )
            ok = result.returncode == 0
            last_result = {
                "status": "ok" if ok else "error",
                "detail": (result.stdout[-500:] if ok else result.stderr[-500:]),
            }
        except subprocess.TimeoutExpired:
            last_result = {"status": "error", "detail": "抓取超时(>300s)"}
        except Exception as e:
            last_result = {"status": "error", "detail": str(e)}

        _write_refresh_state({"last_refresh_ts": now, "last_result": last_result})
        return jsonify({"executed": True, "result": last_result})


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
    """zip内部按真实 年/月/slug.html 文件夹结构命名。"""
    canonical = db.get_canonical_path(post_id)
    if canonical:
        name = canonical + ".html"
    else:
        name = _safe_filename(_get_title(post_id)) + ".html"
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
    db.init_db()
    app.run(host="127.0.0.1", port=5000)
