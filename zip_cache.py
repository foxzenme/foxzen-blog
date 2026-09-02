#!/usr/bin/env python3
"""
完整博客ZIP的按需缓存层：给 app.py 的 /api/download/all 用。

设计目标（对应"发送给 Claude_Code_当前任务.txt"第十一/十二/十三节）：
- 第一次请求才生成，之后复用磁盘上的缓存文件，不用每次都重新压缩；
- 有文章新增/更新时，缓存自动判定过期，下次下载会重新生成整份ZIP；
- 生产环境是 gunicorn -w 2（两个独立进程，不是线程），所以"只生成一次"不能只靠
  threading.Lock，必须用跨进程都认的文件锁；
- 内容超过安全阈值时自动分卷，分卷粒度是"按文章分组"，不是拆单篇文章的ZIP字节流
  （Python zipfile标准库不支持真正的跨卷ZIP格式，也没必要为此手写）；
- 不引入Redis/Celery/数据库，只用标准库 + 文件系统。

版本号怎么算：不去改fetch_blog.py或数据库schema，直接用"文章数量 + 所有文章
index.html的最新mtime + 首页index.html的mtime"做签名。新增文章会改变数量，
内容更新fetch_blog.py会重写index.html从而改变mtime，两种情况都能被捕捉到。

删除旧版本ZIP为什么不用防"正在下载"的锁：
Linux下 unlink() 一个还有进程持有打开fd的文件是安全的——文件内容对那个fd
继续可读，只是新的open()再也找不到这个文件名了。所以"新版本生成后即可删除
旧版本文件"在生产的Linux环境下天然不会打断正在进行中的旧下载。
Windows不允许删除被打开的文件，本地开发/测试时可能会跳过删除、留到下一轮
清理再重试，这是本模块唯一的Windows/Linux行为差异，不影响生产正确性。
"""
import hashlib
import json
import os
import threading
import time
import zipfile
from pathlib import Path

BASE_DIR = Path(__file__).parent
HTML_DIR = BASE_DIR / "html"
POSTS_DIR = HTML_DIR / "posts"
CACHE_DIR = BASE_DIR / "data" / "zip_cache"
STATE_FILE = CACHE_DIR / "state.json"
LOCK_FILE = CACHE_DIR / "build.lock"

# 单个分卷的安全阈值：不设成刚好512MB（Cloudflare单文件缓存上限），留足安全余量。
MAX_PART_BYTES = 450 * 1024 * 1024

# 生成任务锁的最长等待时间：超过这个时间还没等到，说明构建大概率卡死了，
# 与其让请求无限挂起，不如明确报错，运维能第一时间在日志里看到。
LOCK_WAIT_TIMEOUT_SECONDS = 300
LOCK_POLL_INTERVAL_SECONDS = 0.5
# 锁文件存在超过这个时间还没被释放，视为上一次构建异常崩溃遗留，强行接管清除，
# 避免一次意外崩溃就永久卡死后续所有下载请求。
LOCK_STALE_SECONDS = 600

# 当前版本的ZIP如果超过这么久没有新的下载请求，允许清理释放磁盘，
# 下次请求会重新生成（此时版本号大概率不变，等于重新压缩一遍，这是刻意的取舍：
# 正确性和磁盘占用优先于"重新压缩浪费一点CPU"）。
IDLE_CLEANUP_SECONDS = 3600
# 后台清理线程的轮询间隔，不需要很及时，5分钟一次足够满足"约1小时"这种粒度的需求。
CLEANUP_LOOP_INTERVAL_SECONDS = 300

_thread_lock = threading.Lock()
_cleanup_thread_started = False
_cleanup_thread_lock = threading.Lock()


_FILE_RETRY_ATTEMPTS = 5
_FILE_RETRY_DELAY_SECONDS = 0.05


def _read_state():
    """并发场景下（多个请求同时touch_last_used/构建完成后写state.json），
    Windows不允许在另一个进程/线程正持有文件句柄时rename/替换到目标路径，
    读取端可能瞬间撞上PermissionError——这纯粹是Windows的文件锁语义，
    Linux的rename是原子操作不会有这个问题。用短暂重试吸收这个时间窗口，
    而不是放大成用户可见的500错误。"""
    for attempt in range(_FILE_RETRY_ATTEMPTS):
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return {}
        except PermissionError:
            if attempt == _FILE_RETRY_ATTEMPTS - 1:
                return {}
            time.sleep(_FILE_RETRY_DELAY_SECONDS)
    return {}


def _write_state_atomic(state):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(f".tmp-{os.getpid()}-{threading.get_ident()}")
    tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    for attempt in range(_FILE_RETRY_ATTEMPTS):
        try:
            os.replace(tmp, STATE_FILE)
            return
        except PermissionError:
            if attempt == _FILE_RETRY_ATTEMPTS - 1:
                raise
            time.sleep(_FILE_RETRY_DELAY_SECONDS)


def compute_content_version():
    """见模块开头说明：数量+最大mtime拼出的短哈希，代表磁盘上镜像内容的当前版本。"""
    max_mtime = 0.0
    count = 0
    if POSTS_DIR.exists():
        for post_dir in POSTS_DIR.iterdir():
            if not post_dir.is_dir():
                continue
            idx = post_dir / "index.html"
            if idx.exists():
                count += 1
                mtime = idx.stat().st_mtime
                if mtime > max_mtime:
                    max_mtime = mtime
    index_file = HTML_DIR / "index.html"
    if index_file.exists():
        mtime = index_file.stat().st_mtime
        if mtime > max_mtime:
            max_mtime = mtime
    raw = f"{count}:{max_mtime:.3f}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def _parts_exist(state):
    parts = state.get("parts")
    if not parts:
        return False
    return all((CACHE_DIR / p["name"]).exists() for p in parts)


def _acquire_build_lock(timeout=LOCK_WAIT_TIMEOUT_SECONDS):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    deadline = time.time() + timeout
    while True:
        try:
            fd = os.open(str(LOCK_FILE), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode("utf-8"))
            os.close(fd)
            return True
        except FileExistsError:
            try:
                age = time.time() - LOCK_FILE.stat().st_mtime
            except FileNotFoundError:
                continue  # 锁刚好被上一个持有者释放，立刻重试
            if age > LOCK_STALE_SECONDS:
                try:
                    LOCK_FILE.unlink()
                except FileNotFoundError:
                    pass
                continue
            if time.time() > deadline:
                return False
            time.sleep(LOCK_POLL_INTERVAL_SECONDS)


def _release_build_lock():
    try:
        LOCK_FILE.unlink()
    except FileNotFoundError:
        pass


def _dir_size(path):
    total = 0
    for f in path.rglob("*"):
        if f.is_file():
            total += f.stat().st_size
    return total


def _plan_volumes(post_dirs):
    """把文章目录按大小贪心分组，每组累计大小不超过MAX_PART_BYTES。
    用未压缩的原始大小做估算，实际压缩后的ZIP只会更小，估算偏保守是安全的方向。
    单篇文章本身就超过阈值的极端情况，单独成一卷，不强行拆开一篇文章的内容。
    """
    sized = [(d, _dir_size(d)) for d in post_dirs]
    volumes = []
    current, current_size = [], 0
    for d, size in sized:
        if current and current_size + size > MAX_PART_BYTES:
            volumes.append(current)
            current, current_size = [], 0
        current.append(d)
        current_size += size
    if current:
        volumes.append(current)
    return volumes or [[]]


def _write_zip(dest_path, post_dirs, include_index):
    tmp = dest_path.with_suffix(f".tmp-{os.getpid()}-{int(time.time()*1000)}")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
        for post_dir in post_dirs:
            for f in post_dir.rglob("*"):
                if f.is_file():
                    zf.write(f, arcname=f"posts/{post_dir.name}/{f.relative_to(post_dir)}")
        if include_index:
            index_file = HTML_DIR / "index.html"
            if index_file.exists():
                zf.write(index_file, arcname="index.html")
    for attempt in range(_FILE_RETRY_ATTEMPTS):
        try:
            os.replace(tmp, dest_path)
            break
        except PermissionError:
            if attempt == _FILE_RETRY_ATTEMPTS - 1:
                raise
            time.sleep(_FILE_RETRY_DELAY_SECONDS)
    return dest_path.stat().st_size


def _sweep_orphan_files(current_part_names):
    """删除不属于当前版本的旧zip文件。Linux下即使旧文件正被下载也不会中断
    （见模块开头说明）；Windows本地测试时删除被占用的文件会报错，这里吞掉，
    留给下一轮清理重试，不影响功能正确性。"""
    if not CACHE_DIR.exists():
        return
    for f in CACHE_DIR.glob("blog-v*.zip"):
        if f.name not in current_part_names:
            try:
                f.unlink()
            except OSError:
                pass


def _build(version):
    post_dirs = sorted(
        (p for p in POSTS_DIR.iterdir() if p.is_dir()),
        key=lambda p: p.name,
    ) if POSTS_DIR.exists() else []

    volumes = _plan_volumes(post_dirs)
    multipart = len(volumes) > 1
    parts = []
    for i, vol in enumerate(volumes, start=1):
        name = f"blog-v{version}.zip" if not multipart else f"blog-v{version}-part{i:02d}.zip"
        size = _write_zip(CACHE_DIR / name, vol, include_index=(i == 1))
        parts.append({"name": name, "size": size})

    now = time.time()
    state = {
        "version": version,
        "parts": parts,
        "generated_at": now,
        "last_used_at": now,
    }
    _write_state_atomic(state)
    _sweep_orphan_files({p["name"] for p in parts})
    return state


def get_or_build():
    """返回当前可用的缓存清单（含version/parts/...），必要时才真正生成。"""
    version = compute_content_version()
    state = _read_state()
    if state.get("version") == version and _parts_exist(state):
        return state

    with _thread_lock:
        state = _read_state()
        if state.get("version") == version and _parts_exist(state):
            return state
        if not _acquire_build_lock():
            raise RuntimeError("生成完整博客ZIP超时，请稍后重试")
        try:
            state = _read_state()
            if state.get("version") == version and _parts_exist(state):
                return state
            return _build(version)
        finally:
            _release_build_lock()


def touch_last_used():
    state = _read_state()
    if not state:
        return
    state["last_used_at"] = time.time()
    try:
        _write_state_atomic(state)
    except OSError:
        pass


def resolve_part_path(name):
    """按文件名取一个已缓存分卷的路径，只允许取当前state.json里登记过的文件名，
    防止路径穿越或者拿到已经被清理的历史版本文件。"""
    state = _read_state()
    for p in state.get("parts", []):
        if p["name"] == name:
            path = CACHE_DIR / name
            if path.exists():
                return path
    return None


def _cleanup_once():
    state = _read_state()
    if not state:
        return
    last_used = state.get("last_used_at", 0.0)
    if time.time() - last_used > IDLE_CLEANUP_SECONDS:
        for p in state.get("parts", []):
            try:
                (CACHE_DIR / p["name"]).unlink()
            except OSError:
                pass
        try:
            STATE_FILE.unlink()
        except FileNotFoundError:
            pass


def _cleanup_loop():
    while True:
        time.sleep(CLEANUP_LOOP_INTERVAL_SECONDS)
        try:
            _cleanup_once()
        except Exception:
            pass  # 后台清理线程不能因为一次异常就整个退出，下一轮再试


def start_cleanup_thread():
    global _cleanup_thread_started
    with _cleanup_thread_lock:
        if _cleanup_thread_started:
            return
        threading.Thread(target=_cleanup_loop, daemon=True).start()
        _cleanup_thread_started = True
