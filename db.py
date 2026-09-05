#!/usr/bin/env python3
"""
数据库层：posts（当前版本）+ post_versions（历史版本）+ FTS5全文索引
       + page_hits（页面访问计数，按天聚合）+ download_counts（下载计数）
所有抓取/查询/搜索共用这一份schema，避免逻辑分裂。

本版新增：
- posts.canonical_path 字段：Blogger原始permalink对应的 年/月/slug 路径
  （用于镜像站友好URL、下载文件命名。permalink变了这个字段会跟着更新，
  但已生成的旧路径html文件不会被删除——沿用历史版本不设上限的取舍）
- page_hits: 记录首页(post_id=NULL)和每篇文章的访问次数，按天聚合，
  用于算今日/本周/本月/今年/累计访问量
- download_counts: 记录下载次数，scope='article'为单篇，'site'为全站打包

迁移说明：ALTER TABLE ADD COLUMN对已有数据库是安全的增量操作，
不会影响现有posts数据；新表用CREATE TABLE IF NOT EXISTS，同样安全。
"""
import math
import sqlite3
import json
from pathlib import Path
from datetime import datetime, date, timedelta

import safe_errors

DB_PATH = Path(__file__).parent / "data" / "blog.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS posts (
    post_id      TEXT PRIMARY KEY,
    title        TEXT NOT NULL,
    content_html TEXT NOT NULL,   -- 已本地化媒体路径的正文
    tags         TEXT NOT NULL DEFAULT '[]',  -- JSON数组
    published    TEXT NOT NULL,   -- ISO日期 YYYY-MM-DD
    updated      TEXT NOT NULL,   -- Blogger的updated字段
    fetched_at   TEXT NOT NULL,   -- 本地最近一次抓取写入时间
    content_hash TEXT NOT NULL    -- content_html的md5，用于判断是否变化
);

CREATE TABLE IF NOT EXISTS post_versions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id      TEXT NOT NULL,
    title        TEXT NOT NULL,
    content_html TEXT NOT NULL,
    saved_at     TEXT NOT NULL,   -- 该历史版本被存档的时间戳
    content_hash TEXT NOT NULL,
    FOREIGN KEY (post_id) REFERENCES posts(post_id)
);
CREATE INDEX IF NOT EXISTS idx_versions_post_id ON post_versions(post_id);

-- FTS5全文索引：标题+正文。
-- 用 trigram 分词器而非 unicode61/porter：后者按"词边界"切分，对中文（没有空格分词）
-- 会把整段中文当一个token，导致"狐狸"这种子串查询完全搜不到（已实测验证此问题）。
-- trigram按3字符一组切分，能正确支持中文子串搜索，代价是查询词短于3字符时匹配效果变差。
CREATE VIRTUAL TABLE IF NOT EXISTS posts_fts USING fts5(
    post_id UNINDEXED,
    title,
    content,
    tokenize = 'trigram'
);

CREATE TABLE IF NOT EXISTS fetch_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status     TEXT NOT NULL,   -- ok / error
    detail     TEXT,
    post_count INTEGER
);

-- 短号映射：/1/ /2/ 这类短链接。编号一旦分配永久固定，不因后续重排而改变，
-- 避免"以前分享出去的短链接"失效。分配顺序按发布时间从早到晚。
-- 注意：短号现在由Flask路由处理跳转（查canonical_path后302），不再是文件系统symlink，
-- 这样Blogger permalink变了，短号自动跳到新地址，不会失效。
CREATE TABLE IF NOT EXISTS post_numbers (
    post_id    TEXT PRIMARY KEY,
    number     INTEGER UNIQUE NOT NULL,
    assigned_at TEXT NOT NULL
);

-- 访问去重：同一天同一访客（按IP识别）对同一页面只计一次点击。
-- page_key用'__home__'代表首页，不用NULL——SQLite里UNIQUE/PRIMARY KEY对NULL的
-- 处理是"每个NULL都各不相同"，如果这里用NULL，去重判断永远不会命中，等于没做去重。
CREATE TABLE IF NOT EXISTS page_hit_dedup (
    page_key    TEXT NOT NULL,
    visitor_key TEXT NOT NULL,
    hit_date    TEXT NOT NULL,
    PRIMARY KEY (page_key, visitor_key, hit_date)
);

-- 完读计数：读者滚动到文章底部才算一次，跟page_hits一样按天聚合+IP去重
CREATE TABLE IF NOT EXISTS finish_reads (
    post_id  TEXT NOT NULL,
    hit_date TEXT NOT NULL,
    count    INTEGER NOT NULL DEFAULT 0,
    UNIQUE(post_id, hit_date)
);
CREATE TABLE IF NOT EXISTS finish_read_dedup (
    post_id     TEXT NOT NULL,
    visitor_key TEXT NOT NULL,
    hit_date    TEXT NOT NULL,
    PRIMARY KEY (post_id, visitor_key, hit_date)
);

-- 页面访问计数，按天聚合。post_id为NULL表示首页访问。
-- 之所以按天聚合而不是记每一条原始请求，是为了避免这张表无限增长
-- （一篇热门文章被访问几万次也只占几万分之一的行数，而不是几万行）。
CREATE TABLE IF NOT EXISTS page_hits (
    post_id  TEXT,
    hit_date TEXT NOT NULL,
    count    INTEGER NOT NULL DEFAULT 0,
    UNIQUE(post_id, hit_date)
);
CREATE INDEX IF NOT EXISTS idx_page_hits_date ON page_hits(hit_date);
CREATE INDEX IF NOT EXISTS idx_page_hits_post ON page_hits(post_id);

-- 下载计数。scope='article'对应某篇文章被下载（无论是单篇导出还是勾选批量导出
-- 里包含了这篇），scope='site'对应"全站打包下载"或"导出离线版(全部)"这类整站操作，
-- 此时post_id为NULL。
CREATE TABLE IF NOT EXISTS download_counts (
    post_id  TEXT,
    scope    TEXT NOT NULL,
    count    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (post_id, scope)
);

-- 公开匿名刷新系统的两把共享执行锁：
--   content_fetch: mirror/backup/github/cf 四个target发起刷新时都要先跑
--                  一次fetch_blog.py，这把锁保证同一时刻只有一个进程在跑，
--                  跟下面refresh_targets的每入口冷却是两个独立概念。
--   git_publish:   github/cf 都要把 html/ 的变化commit+push到同一个本地
--                  git工作区，这把锁保证同一时刻只有一个进程在动这个工作区。
-- content_fetch与git_publish之间是双向互斥（见app.py的_run_content_fetch/
-- _run_git_publish）：任何一个在跑，另一个都不能开始，防止"content_fetch
-- 正在改写html/文件、git_publish同时在git add"这种撕裂读风险。
-- generation是fencing token：每次成功acquire（含stale超时后的自动恢复）
-- 都会+1，release时必须带上acquire时拿到的generation，不匹配就静默放弃
-- （说明锁已经被后来者接管），防止旧worker在stale超时恢复之后，还能
-- 把新worker正在跑的锁误释放掉——见try_acquire_lock()/release_lock()。
CREATE TABLE IF NOT EXISTS refresh_locks (
    lock_key        TEXT PRIMARY KEY,        -- 'content_fetch' / 'git_publish'
    status          TEXT NOT NULL DEFAULT 'idle',   -- idle / running
    generation      INTEGER NOT NULL DEFAULT 0,     -- fencing token
    started_at      TEXT,
    finished_at     TEXT,
    triggered_by    TEXT,                    -- 本次占用者：mirror/backup/github/cf
    last_status     TEXT,                    -- ok / error（这把锁本身最近一次执行结果，
                                              -- 跨target共享，仅供诊断，不是target结果的权威来源）
    last_detail     TEXT,
    last_post_count INTEGER,                 -- 仅content_fetch有意义
    last_commit_sha TEXT                     -- 仅git_publish有意义
);

-- 四个UI入口(mirror/backup/github/cf)各自独立的冷却计时基准 + 各自最近
-- 一次完整pipeline的最终结果（这才是"这个target上次到底成没成功"的权威
-- 来源——refresh_locks是共享资源，可能被别的target的执行"顺路"更新，
-- 不能拿来回答"github上次刷新结果如何"这类问题）。
CREATE TABLE IF NOT EXISTS refresh_targets (
    target_key             TEXT PRIMARY KEY,  -- mirror / backup / github / cf
    last_started_at        TEXT,              -- cooldown基准
    generation              INTEGER NOT NULL DEFAULT 0,  -- fencing token，每次成功acquire自增1；
                                               -- last_started_at是秒级精度的时间戳，两次acquire
                                               -- 如果发生在同一秒内会完全相同、不能用来分辨
                                               -- "是不是同一轮"——不能假设两轮之间一定隔着完整的
                                               -- cooldown窗口（比如cooldown_seconds=0的场景，或者
                                               -- 未来cooldown数值调整），必须有一个独立于时间戳、
                                               -- 严格递增的值，跟refresh_locks.generation同一个思路
    last_finished_at       TEXT,
    last_status             TEXT,             -- success / failure
    last_error_category      TEXT,
    last_detail                TEXT,
    last_commit_sha              TEXT,        -- mirror/backup恒为NULL；github/cf是实际commit sha
    last_retry_recommended         INTEGER,   -- 0/1，仅failure时有意义
    last_result_generation          INTEGER  -- S6修复：last_status等字段实际对应的那一轮generation
                                               -- （record_target_result()写入时的generation值），
                                               -- 不一定等于当前generation列——如果之后又发起过新一轮
                                               -- 刷新(generation前进)但那一轮从未成功写回结果(比如
                                               -- git_publish被409拒绝、进程被杀、后台watcher因worker
                                               -- 重启丢失)，两者就会不相等，get_target_status()据此
                                               -- 判断last_result是否还能代表"最近一次尝试"，见
                                               -- get_target_status()文档字符串
);
"""


def get_conn():
    _ensure_schema()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")  # 并发读写更稳，Flask+cron同时访问需要
    return conn


def _column_exists(conn, table, column):
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(r["name"] == column for r in rows)


def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = get_conn()
    conn.executescript(SCHEMA)

    # 迁移：给已存在的posts表加canonical_path字段（新库不受影响，
    # CREATE TABLE已经没有这个字段的历史包袱，但线上库是旧schema建的，
    # 必须用ALTER TABLE补，且要判断是否已存在，避免重复运行时报错）
    if not _column_exists(conn, "posts", "canonical_path"):
        conn.execute("ALTER TABLE posts ADD COLUMN canonical_path TEXT")
        print("[迁移] posts表已添加 canonical_path 字段")

    if not _column_exists(conn, "posts", "source_url"):
        conn.execute("ALTER TABLE posts ADD COLUMN source_url TEXT")
        print("[迁移] posts表已添加 source_url 字段")

    if not _column_exists(conn, "posts", "export_size_bytes"):
        conn.execute("ALTER TABLE posts ADD COLUMN export_size_bytes INTEGER DEFAULT 0")
        print("[迁移] posts表已添加 export_size_bytes 字段")

    if not _column_exists(conn, "posts", "published_ts"):
        conn.execute("ALTER TABLE posts ADD COLUMN published_ts TEXT")
        print("[迁移] posts表已添加 published_ts 字段（完整时间戳，专门用于排序，"
              "不影响published字段原有的显示/筛选逻辑）")

    if not _column_exists(conn, "refresh_locks", "generation"):
        conn.execute("ALTER TABLE refresh_locks ADD COLUMN generation INTEGER NOT NULL DEFAULT 0")
        print("[迁移] refresh_locks表已添加 generation 字段（fencing token）")
    if not _column_exists(conn, "refresh_locks", "last_commit_sha"):
        conn.execute("ALTER TABLE refresh_locks ADD COLUMN last_commit_sha TEXT")
        print("[迁移] refresh_locks表已添加 last_commit_sha 字段")

    for col, coltype in (
        ("generation", "INTEGER NOT NULL DEFAULT 0"),
        ("last_finished_at", "TEXT"), ("last_status", "TEXT"),
        ("last_error_category", "TEXT"), ("last_detail", "TEXT"),
        ("last_commit_sha", "TEXT"), ("last_retry_recommended", "INTEGER"),
        ("last_result_generation", "INTEGER"),
    ):
        if not _column_exists(conn, "refresh_targets", col):
            conn.execute(f"ALTER TABLE refresh_targets ADD COLUMN {col} {coltype}")
            print(f"[迁移] refresh_targets表已添加 {col} 字段")

    conn.commit()
    conn.close()


_schema_ready_paths = set()


def _ensure_schema():
    """懒初始化：只在真正要访问数据库时才建表/迁移，绝不在模块import时执行。

    之前db.init_db()被无条件挂在app.py模块顶层，任何`import app`（不管是不是
    测试、不管有没有先改db.DB_PATH）都会立刻对当时db.DB_PATH指向的文件执行一次
    schema操作——这正是真实data/blog.db被测试意外污染出refresh_locks/
    refresh_targets两张空表的根因。这里改成"首次真正用到数据库时才做"，
    无论调用方是生产环境的gunicorn worker，还是测试里先把db.DB_PATH指向
    临时文件再触发任何数据库操作，_ensure_schema()执行的时刻，DB_PATH早已经
    是调用方真正想要的那个路径，不会再有"import的时候路径还没来得及被覆盖"
    这种时序问题。

    复核阶段修复：标记"是否已初始化"必须按DB_PATH本身区分，不能是单个
    进程级布尔值——同一个Python进程完全可能在生命周期内把db.DB_PATH从A
    改指向B（测试就是这么做的：每个测试用例各自的临时数据库），旧实现里
    只要进程内曾经对任意一个路径完成过一次懒初始化，这个布尔值就永久变成
    True，之后哪怕换成一个从未初始化过的全新路径B，也会被误判成"已经
    ready"而直接短路跳过——B的数据库文件会被sqlite3.connect()静默自动
    创建成一个没有任何表的空文件，第一条真实SQL就会因为"no such table"
    报错，或者更隐蔽地，如果B恰好是一个已存在但schema陈旧的文件，还会
    连带跳过本该执行的ALTER TABLE迁移。改成一个集合，按DB_PATH.resolve()
    记录每个路径各自是否已经初始化——用resolve()而不是直接用Path对象或
    原始字符串做key，是为了让"同一个物理文件，只是一次用相对路径、一次用
    绝对路径写"这种表面不同、实际相同的Path不会被误判成两个不同的数据库；
    resolve()不要求文件已经存在也能正常工作（不抛异常），对懒初始化里
    "路径对应的文件还没被创建"这种最常见的场景同样安全。生产环境gunicorn
    worker只会用同一个固定DB_PATH，行为跟修复前完全一致：进程生命周期内
    第一次真实数据库访问触发一次init_db()，此后所有访问都命中缓存直接
    返回，不会重复执行schema操作。

    先把这个路径记进_schema_ready_paths再调用init_db()：init_db()内部会调
    get_conn()，get_conn()又会调_ensure_schema()——不先记录会无限递归；
    记录之后同一路径再重入直接短路返回。
    """
    resolved_path = DB_PATH.resolve()
    if resolved_path in _schema_ready_paths:
        return
    _schema_ready_paths.add(resolved_path)
    init_db()


# ---------------------------------------------------------------------------
# 公开匿名刷新系统：content_fetch / git_publish 两把共享锁的原子化
# 冷却 + 并发 + fencing 控制
#
# get_conn()默认isolation_level=""（sqlite3模块在第一条DML前隐式开事务），
# 这里换成isolation_level=None（autocommit）+显式BEGIN IMMEDIATE，是因为
# "检查target冷却 -> 检查其它lock_key是否空闲 -> 检查自己是否空闲 -> 都通过
# 才占用"必须是读+写一整块不可分割的操作：BEGIN IMMEDIATE在语句执行前就
# 立刻抢占SQLite的写锁，同一时刻只有一个连接能进入这段临界区，另一个连接
# 的BEGIN IMMEDIATE会阻塞到前者COMMIT/ROLLBACK为止（最多等timeout秒），
# 而不是各自基于自己读到的旧状态各自做判断——这就避免了"先SELECT、Python
# 判断、再UPDATE"这种在两个gunicorn worker下会出现两边都判断"可以执行"的
# 经典竞态。
#
# 由于SQLite的BEGIN IMMEDIATE抢的是整个数据库文件级别的RESERVED锁（不是
# "某一行"的锁），任意时刻全库只能有一个连接处于这类事务中——这意味着
# content_fetch和git_publish两个lock_key的获取请求，本质上都要先排队拿到
# 这个全库唯一的写事务名额，而"排队等待"不会持有任何东西去等别的资源，
# 所以两把锁之间不可能出现锁顺序反转式的死锁：这不是靠调用顺序小心避免的，
# 是SQLite这个机制本身决定的。
# ---------------------------------------------------------------------------

def try_acquire_lock(lock_key: str, stale_after_seconds: int, *,
                      target_key: str = None, cooldown_seconds: int = None,
                      cross_check_idle: tuple = (), triggered_by: str = None) -> dict:
    """原子地尝试获取lock_key（'content_fetch' 或 'git_publish'）。

    target_key/cooldown_seconds: 同时提供时，会在同一个事务里先检查+设置
        target_key（mirror/backup/github/cf之一）在refresh_targets里的冷却
        基准——用于4个target各自第一阶段（获取content_fetch）的调用。留空
        （None）则跳过冷却检查，只做纯资源互斥——用于github/cf第二阶段
        （获取git_publish）：这个target的冷却已经在第一阶段设置过，不重复设置。

    cross_check_idle: [(其它lock_key, 那个lock_key自己的stale_after_seconds), ...]
        实现content_fetch/git_publish双向互斥：对方处于running且未超过它
        自己的stale阈值时，本次获取原子拒绝（reason="busy_<对方lock_key>"）；
        对方处于running但已超过它自己的stale阈值时，视为可穿透（不阻塞本次
        获取），但不会代为恢复对方那一行——恢复动作只应该发生在真正有人去
        acquire它自己的时候，这里只是"不被一个早就死掉的资源挡住"。

    stale_after_seconds: 判定"锁疑似因进程异常终止而没被释放"的年龄阈值，
        必须大于任务本身允许的最长执行时间，否则会把一个真实还在跑的任务
        误判为死锁——这不是"猜"，是有明确证据支撑的判断：正常情况下任务
        不可能跑得比它自己的超时时间还长，如果锁的年龄超过了这个上限还没
        释放，唯一合理的解释就是持锁的那个进程本身已经不在了。

    triggered_by: 记录本次实际占用者（mirror/backup/github/cf），供
        get_target_status()判断"当前正在running的是不是这个target自己"用。

    返回：
        {"acquired": True, "generation": int} —— 拿到了，generation是这次
            acquire的fencing token，调用方必须原样传给release_lock()，
            且无论成功/失败/超时/异常，最终都必须调用一次release_lock()，
            否则锁会永久停在running。
        {"acquired": False, "reason": "cooldown", "cooldown_remaining_seconds": int}
        {"acquired": False, "reason": "busy_<lock_key>"}
    """
    _ensure_schema()
    conn = sqlite3.connect(DB_PATH, isolation_level=None, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
        try:
            conn.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError:
            # 连SQLite写锁本身都抢不到（极端高并发/磁盘异常繁忙），保守
            # 处理成"当前繁忙"，绝不能让请求方误以为可以执行。
            return {"acquired": False, "reason": f"busy_{lock_key}"}

        try:
            now = datetime.now()
            now_iso = now.isoformat(timespec="seconds")

            if target_key is not None and cooldown_seconds is not None:
                target_row = conn.execute(
                    "SELECT last_started_at FROM refresh_targets WHERE target_key = ?", (target_key,)
                ).fetchone()
                if target_row and target_row["last_started_at"]:
                    elapsed = (now - datetime.fromisoformat(target_row["last_started_at"])).total_seconds()
                    if elapsed < cooldown_seconds:
                        conn.execute("ROLLBACK")
                        return {"acquired": False, "reason": "cooldown",
                                "cooldown_remaining_seconds": math.ceil(cooldown_seconds - elapsed)}

            for other_key, other_stale_seconds in cross_check_idle:
                other_row = conn.execute(
                    "SELECT status, started_at FROM refresh_locks WHERE lock_key = ?", (other_key,)
                ).fetchone()
                if other_row and other_row["status"] == "running":
                    other_age = (now - datetime.fromisoformat(other_row["started_at"])).total_seconds()
                    if other_age <= other_stale_seconds:
                        conn.execute("ROLLBACK")
                        return {"acquired": False, "reason": f"busy_{other_key}"}
                    # 对方已超过它自己的stale阈值：不阻塞本次获取，但也不
                    # 代为恢复它那一行，留给它自己下次被acquire时处理。

            lock_row = conn.execute(
                "SELECT status, started_at FROM refresh_locks WHERE lock_key = ?", (lock_key,)
            ).fetchone()
            if lock_row and lock_row["status"] == "running":
                lock_age = (now - datetime.fromisoformat(lock_row["started_at"])).total_seconds()
                if lock_age <= stale_after_seconds:
                    conn.execute("ROLLBACK")
                    return {"acquired": False, "reason": f"busy_{lock_key}"}
                # 锁的年龄超过了任务本身可能的最长执行时间，判定为异常终止
                # 遗留的死锁，本次请求负责原子地把它恢复成idle——具体的
                # "据为己有"发生在下面的INSERT...ON CONFLICT里，
                # generation会在那一步自增，让旧worker手里的旧generation
                # 值永远对不上，release时天然失效，不会误释放新锁。

            target_generation = None
            if target_key is not None and cooldown_seconds is not None:
                conn.execute("""
                    INSERT INTO refresh_targets (target_key, last_started_at, generation) VALUES (?, ?, 1)
                    ON CONFLICT(target_key) DO UPDATE SET
                        last_started_at = excluded.last_started_at, generation = generation + 1
                """, (target_key, now_iso))
                target_generation = conn.execute(
                    "SELECT generation FROM refresh_targets WHERE target_key = ?", (target_key,)
                ).fetchone()["generation"]

            conn.execute("""
                INSERT INTO refresh_locks (lock_key, status, generation, started_at, finished_at, triggered_by)
                VALUES (?, 'running', 1, ?, NULL, ?)
                ON CONFLICT(lock_key) DO UPDATE SET
                    status='running', generation=generation + 1, started_at=excluded.started_at,
                    finished_at=NULL, triggered_by=excluded.triggered_by
            """, (lock_key, now_iso, triggered_by))
            new_generation = conn.execute(
                "SELECT generation FROM refresh_locks WHERE lock_key = ?", (lock_key,)
            ).fetchone()["generation"]
            conn.execute("COMMIT")
            # target_generation一并返回：target_key/cooldown_seconds都提供的
            # 那次调用（4个target各自的第一阶段）会把这个值同时写进
            # refresh_targets.generation——调用方把它原样存起来，后续所有
            # record_target_result()调用都带上，作为"这次写入是否还对应
            # 当前这一轮刷新"的fencing依据（见record_target_result()的
            # expected_generation参数）。不用last_started_at本身当fencing值
            # ——那是秒级精度的时间戳，两次acquire如果发生在同一秒内会完全
            # 相同，不能可靠地分辨"是不是同一轮"（这不是假设性的边界情况，
            # 是被cooldown_seconds=0场景的真实测试直接触发过的问题）。
            return {"acquired": True, "generation": new_generation, "started_at": now_iso,
                    "target_generation": target_generation}
        except Exception:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()


def release_lock(lock_key: str, generation: int, status: str, detail: str = "",
                  post_count=None, commit_sha=None) -> bool:
    """任务结束（成功/失败/超时）后必须调用一次，把锁释放回idle并记录本次
    结果。调用方必须用try/finally包住整个任务执行过程，保证无论走哪条路径
    都会执行到这里——否则锁会永久停留在running，后续请求都会被误判为
    "正在运行"而拒绝。

    generation必须是对应try_acquire_lock()返回的那个值——fencing校验：
    `WHERE lock_key=? AND generation=?`，如果这次release发生的时候锁已经
    被后来者（stale恢复或者是同一个lock_key更早一次的旧worker）重新acquire
    过（generation已经前进），条件不匹配，UPDATE影响0行，本次release静默
    作废、不覆盖对方状态——返回False。调用方一般不需要对False做特殊处理
    （最多记一条诊断日志），因为这恰恰是"防止旧worker释放新worker的锁"
    这个安全属性生效的表现，不是错误。
    """
    _ensure_schema()
    conn = sqlite3.connect(DB_PATH, isolation_level=None, timeout=10)
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("BEGIN IMMEDIATE")
        try:
            cur = conn.execute("""
                UPDATE refresh_locks
                SET status='idle', finished_at=?, last_status=?, last_detail=?,
                    last_post_count=COALESCE(?, last_post_count),
                    last_commit_sha=COALESCE(?, last_commit_sha)
                WHERE lock_key=? AND generation=?
            """, (datetime.now().isoformat(timespec="seconds"), status, (detail or "")[:500],
                  post_count, commit_sha, lock_key, generation))
            released = cur.rowcount > 0
            conn.execute("COMMIT")
            return released
        except Exception:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()


def record_target_result(target_key: str, status: str, detail: str = "",
                          error_category: str = None, retry_recommended: bool = None,
                          commit_sha: str = None, expected_generation: int = None) -> bool:
    """一个target（mirror/backup/github/cf）的整条pipeline全部完成（或提前
    因某一步失败而终止）后，写这个target自己的最终结果——这是
    get_target_status()回答"这个target上次到底成没成功"的权威数据源，
    跟refresh_locks（共享资源，可能被别的target"顺路"更新）分开。

    只在成功走完至少一次try_acquire_lock()之后调用（refresh_targets的行
    在那一步已经由INSERT...ON CONFLICT创建），所以这里用UPDATE不用upsert。

    expected_generation: 提供时，UPDATE额外带上
    `WHERE ... AND generation=expected_generation`——只有"这次要写的结果，
    仍然对应refresh_targets当前记录的那一轮刷新"才会真正生效，返回True；
    如果这期间同一个target又发起过新一轮刷新（generation已经前进），条件
    不匹配，写入静默作废，返回False，不会用旧结果覆盖新一轮的状态。这是
    给GitHub Actions有界等待超时后的后台watcher用的：90秒有界等待到期
    只是"这次同步HTTP请求不再继续等"，不代表没人关心结果——后台线程会
    继续跟踪真实conclusion，晚些时候才调用这个函数把结果写回来，这时候
    必须确认自己跟踪的还是"最新的那一轮"，不是被同一个target后来的新
    请求已经取代的旧一轮（同一思路的实现见release_lock()的generation
    fencing，这里保护的是refresh_targets这一行）。

    刻意不用last_started_at本身当fencing值——那是秒级精度的时间戳，两次
    acquire如果发生在同一秒内会完全相同，不能可靠地分辨"是不是同一轮"
    （这不是假设性的边界情况：cooldown_seconds=0或者未来cooldown数值调整
    都可能让两次acquire落在同一秒内，已经被真实测试直接触发过）。
    refresh_targets.generation是独立于时间戳、严格递增的整数，不受这个
    问题影响。

    留空（None，默认）保持原有的无条件写入行为，用于所有同步路径
    （mirror/backup/cf/github的即时success/failure/dispatch失败分支）——
    这些路径本来就运行在"当前就是最新一轮"这个前提成立的调用栈里，不需要
    额外校验。
    """
    conn = get_conn()
    if expected_generation is not None:
        cur = conn.execute("""
            UPDATE refresh_targets
            SET last_finished_at=?, last_status=?, last_error_category=?, last_detail=?,
                last_commit_sha=?, last_retry_recommended=?, last_result_generation=generation
            WHERE target_key=? AND generation=?
        """, (datetime.now().isoformat(timespec="seconds"), status, error_category, (detail or "")[:500],
              commit_sha, (None if retry_recommended is None else int(bool(retry_recommended))),
              target_key, expected_generation))
    else:
        # last_result_generation=generation是同一行内的列自引用（UPDATE
        # 单条语句内原子读写同一行，没有额外的读-改-写竞态）：没有提供
        # expected_generation时，直接记录"写入这一刻这一行实际的generation
        # 是多少"，跟带fencing的分支使用完全一样的字段含义，get_target_status()
        # 不需要关心结果到底来自哪个分支。
        cur = conn.execute("""
            UPDATE refresh_targets
            SET last_finished_at=?, last_status=?, last_error_category=?, last_detail=?,
                last_commit_sha=?, last_retry_recommended=?, last_result_generation=generation
            WHERE target_key=?
        """, (datetime.now().isoformat(timespec="seconds"), status, error_category, (detail or "")[:500],
              commit_sha, (None if retry_recommended is None else int(bool(retry_recommended))),
              target_key))
    written = cur.rowcount > 0
    conn.commit()
    conn.close()
    return written


def get_target_status(target_key: str, cooldown_seconds: int, lock_stale_seconds: dict = None) -> dict:
    """GET /api/refresh/<target>/status 的数据来源。

    state: 'running'（refresh_locks里有一行status=running且triggered_by=
        这个target自己，且年龄未超过它自己的stale阈值）/ 'stale'（S3修复：
        同样是那一行status=running，但年龄已经超过lock_stale_seconds里
        对应lock_key的阈值——holder大概率已经异常终止，不应该再被当作
        "确实在执行中"展示给用户，即使出于fencing安全性的考虑、这里的
        只读查询本身并不会去真正回收这一行；真正的回收仍然只在下一次
        有人acquire这把锁时才发生，见try_acquire_lock()）/ 'cooldown'
        （不在running/stale，但还在自己的冷却窗口内）/ 'idle'（都不是）。
        lock_stale_seconds形如{"content_fetch": 420, "git_publish": 300}，
        留空(None)时不做stale判断，永远只会是running（保持旧行为，供
        不关心这个区分的调用方使用）。

    last_result_is_current（S6修复）: True——last_result实际写入时对应的
        generation(refresh_targets.last_result_generation)跟这个target
        当前的generation一致，last_result确实就是"最近一次尝试"的真实
        结果；False——generation已经比last_result写入时更新（说明之后
        至少又发起过一次新的尝试：acquire会让generation前进），但那次
        更新的尝试从未成功调用record_target_result()写回结果（可能是
        被busy_git_publish拒绝、进程被杀、后台watcher因worker重启丢失
        ……），此时last_result展示的实际上是更早一轮的陈旧结果，不能被
        误当作反映了最近这次尝试；None——从来没有任何一轮真正写完过
        结果（last_result本身就是None，这个字段不适用）。
    last_result: 上一次真正写完的结果，无论state是什么、无论
        last_result_is_current是True还是False，都不会被清空——调用方
        始终能看到"历史上最近一次的结果"，只是需要结合last_result_is_current
        自己判断这份结果是否还能代表"最近一次尝试"。
    last_result.detail（S8修复）: 永远是safe_errors.safe_public_detail()按
        status/error_category生成的固定模板摘要，不是数据库里last_detail
        列存的原始文本——这个函数是匿名公开的GET端点，last_detail列本身
        允许保留原始subprocess stderr/异常文本供运维内部排查，但绝不能
        经这里透传给调用方。
    """
    lock_stale_seconds = lock_stale_seconds or {}
    conn = get_conn()
    target_row = conn.execute(
        "SELECT * FROM refresh_targets WHERE target_key = ?", (target_key,)
    ).fetchone()
    running_row = conn.execute(
        "SELECT lock_key, started_at FROM refresh_locks WHERE status='running' AND triggered_by=?",
        (target_key,),
    ).fetchone()
    conn.close()

    if running_row:
        stale_threshold = lock_stale_seconds.get(running_row["lock_key"])
        age = (datetime.now() - datetime.fromisoformat(running_row["started_at"])).total_seconds()
        state = "stale" if (stale_threshold is not None and age > stale_threshold) else "running"
    elif target_row and target_row["last_started_at"]:
        elapsed = (datetime.now() - datetime.fromisoformat(target_row["last_started_at"])).total_seconds()
        state = "cooldown" if elapsed < cooldown_seconds else "idle"
    else:
        state = "idle"

    last_result = None
    last_result_is_current = None
    if target_row and target_row["last_status"]:
        last_result = {
            "status": target_row["last_status"],
            "error_category": target_row["last_error_category"],
            # S8修复：last_detail这一列本身允许保留原始诊断文本（供运维
            # 通过sqlite3直接排查），但这个GET端点是匿名公开的，对外的
            # detail永远只能是safe_errors按status/error_category查出的固定
            # 模板摘要，绝不能把这一列的原始内容直接透传出去。
            "detail": safe_errors.safe_public_detail(target_row["last_status"],
                                                       target_row["last_error_category"]),
            "commit": target_row["last_commit_sha"],
            "finished_at": target_row["last_finished_at"],
            "retry_recommended": (None if target_row["last_retry_recommended"] is None
                                   else bool(target_row["last_retry_recommended"])),
        }
        if target_row["last_result_generation"] is not None:
            last_result_is_current = (target_row["last_result_generation"] == target_row["generation"])

    return {"target": target_key, "state": state,
            "last_result_is_current": last_result_is_current, "last_result": last_result}


def strip_html_for_fts(html: str) -> str:
    """去标签，只留纯文本给FTS索引，避免HTML标签污染搜索结果和排序权重。"""
    import re
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def upsert_post(post_id, title, content_html, tags, published, updated, content_hash,
                 canonical_path=None, source_url=None, published_ts=None):
    """写入/更新当前版本。如果content_hash变化，调用方需自行先调用 save_version 存档旧版本。
    source_url是Blogger当次抓取返回的真实完整地址（不是拼出来的），用于下载文件里的
    来源标注——这样博客换域名，重新抓一次就自动更新，不用改代码里任何硬编码域名。
    published_ts是完整时间戳（含时分秒），专门用于排序——published字段本身只存年月日
    （给显示和日期范围筛选用），同一天发布的多篇文章光靠published字段分不出先后，
    需要published_ts来正确排序。
    """
    conn = get_conn()
    now = datetime.now().isoformat(timespec="seconds")

    conn.execute("""
        INSERT INTO posts (post_id, title, content_html, tags, published, updated, fetched_at, content_hash, canonical_path, source_url, published_ts)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(post_id) DO UPDATE SET
            title=excluded.title, content_html=excluded.content_html, tags=excluded.tags,
            published=excluded.published, updated=excluded.updated,
            fetched_at=excluded.fetched_at, content_hash=excluded.content_hash,
            canonical_path=excluded.canonical_path, source_url=excluded.source_url,
            published_ts=excluded.published_ts
    """, (post_id, title, content_html, json.dumps(tags, ensure_ascii=False), published, updated, now, content_hash, canonical_path, source_url, published_ts))

    # 同步FTS索引：先删后插（FTS5没有原生upsert）
    conn.execute("DELETE FROM posts_fts WHERE post_id = ?", (post_id,))
    conn.execute("INSERT INTO posts_fts (post_id, title, content) VALUES (?, ?, ?)",
                 (post_id, title, strip_html_for_fts(content_html)))
    conn.commit()
    conn.close()


def get_existing_hash(post_id):
    conn = get_conn()
    row = conn.execute("SELECT content_hash FROM posts WHERE post_id = ?", (post_id,)).fetchone()
    conn.close()
    return row["content_hash"] if row else None


def get_canonical_path(post_id):
    conn = get_conn()
    row = conn.execute("SELECT canonical_path FROM posts WHERE post_id = ?", (post_id,)).fetchone()
    conn.close()
    return row["canonical_path"] if row else None


def set_export_size(post_id, size_bytes):
    """存这篇文章导出成Base64离线版之后的字节数，fetch_blog.py每次抓取后算一次，
    不在每次页面访问/搜索时现算（现算要重新读所有图片编码一遍，没必要每次都做）。
    """
    conn = get_conn()
    conn.execute("UPDATE posts SET export_size_bytes = ? WHERE post_id = ?", (size_bytes, post_id))
    conn.commit()
    conn.close()


def get_all_export_sizes():
    conn = get_conn()
    rows = conn.execute("SELECT post_id, export_size_bytes FROM posts").fetchall()
    conn.close()
    return {r["post_id"]: (r["export_size_bytes"] or 0) for r in rows}


def get_total_export_size():
    conn = get_conn()
    row = conn.execute("SELECT COALESCE(SUM(export_size_bytes), 0) AS s FROM posts").fetchone()
    conn.close()
    return row["s"]


def get_source_url(post_id):
    conn = get_conn()
    row = conn.execute("SELECT source_url FROM posts WHERE post_id = ?", (post_id,)).fetchone()
    conn.close()
    return row["source_url"] if row else None


def get_post_by_canonical_path(canonical_path):
    """按 年/月/slug 路径反查文章，Flask渲染文章页路由用。"""
    conn = get_conn()
    row = conn.execute("SELECT * FROM posts WHERE canonical_path = ?", (canonical_path,)).fetchone()
    conn.close()
    return dict(row) if row else None


def save_version(post_id, title, content_html, content_hash):
    """存档一个历史版本，不设数量上限（用户明确要求）。"""
    conn = get_conn()
    now = datetime.now().isoformat(timespec="seconds")
    conn.execute("""
        INSERT INTO post_versions (post_id, title, content_html, saved_at, content_hash)
        VALUES (?, ?, ?, ?, ?)
    """, (post_id, title, content_html, now, content_hash))
    conn.commit()
    conn.close()


def search_posts(query="", tag=None, date_from=None, date_to=None, limit=50, offset=0):
    """
    全文检索：标题+正文，BM25加权排序（FTS5内置bm25()函数，分数越小越相关）。
    query为空时退化为按发布日期倒序列出全部（供筛选/浏览用）。

    标签过滤现在放在SQL里做（用LIKE匹配JSON文本），不再是查完limit条之后
    用Python代码二次过滤——旧写法会导致"符合标签的文章其实还有很多，但SQL那层
    已经按limit截断了，看起来结果比实际少"，分页功能需要准确的数字，这个必须先修。
    """
    conn = get_conn()
    q = query.strip()
    if q:
        if len(q) < 3:
            sql = """
                SELECT p.post_id, p.title, p.published, p.tags, p.canonical_path, n.number, 0 AS rank
                FROM posts p
                LEFT JOIN post_numbers n ON n.post_id = p.post_id
                WHERE (p.title LIKE ? OR p.content_html LIKE ?)
            """
            like_q = f"%{q}%"
            params = [like_q, like_q]
            if tag:
                sql += " AND p.tags LIKE ?"
                params.append(f'%"{tag}"%')
            if date_from:
                sql += " AND p.published >= ?"
                params.append(date_from)
            if date_to:
                sql += " AND p.published <= ?"
                params.append(date_to)
            sql += " ORDER BY COALESCE(p.published_ts, p.published) DESC LIMIT ? OFFSET ?"
            params.extend([limit, offset])
            rows = conn.execute(sql, params).fetchall()
        else:
            sql = """
                SELECT p.post_id, p.title, p.published, p.tags, p.canonical_path, n.number,
                       bm25(posts_fts) AS rank
                FROM posts_fts
                JOIN posts p ON p.post_id = posts_fts.post_id
                LEFT JOIN post_numbers n ON n.post_id = p.post_id
                WHERE posts_fts MATCH ?
            """
            safe_q = '"' + q.replace('"', '""') + '"'
            params = [safe_q]
            if tag:
                sql += " AND p.tags LIKE ?"
                params.append(f'%"{tag}"%')
            if date_from:
                sql += " AND p.published >= ?"
                params.append(date_from)
            if date_to:
                sql += " AND p.published <= ?"
                params.append(date_to)
            sql += " ORDER BY rank LIMIT ? OFFSET ?"
            params.extend([limit, offset])
            rows = conn.execute(sql, params).fetchall()
    else:
        sql = """
            SELECT p.post_id, p.title, p.published, p.tags, p.canonical_path, n.number
            FROM posts p
            LEFT JOIN post_numbers n ON n.post_id = p.post_id
            WHERE 1=1
        """
        params = []
        if tag:
            sql += " AND p.tags LIKE ?"
            params.append(f'%"{tag}"%')
        if date_from:
            sql += " AND p.published >= ?"
            params.append(date_from)
        if date_to:
            sql += " AND p.published <= ?"
            params.append(date_to)
        sql += " ORDER BY COALESCE(p.published_ts, p.published) DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        rows = conn.execute(sql, params).fetchall()
    conn.close()

    results = []
    for r in rows:
        results.append({
            "post_id": r["post_id"],
            "title": r["title"],
            "published": r["published"],
            "tags": json.loads(r["tags"]),
            "number": r["number"],
            "canonical_path": r["canonical_path"],
        })
    return results


def count_posts(query="", tag=None, date_from=None, date_to=None):
    """跟search_posts用一样的筛选条件，只返回总数，不返回内容——分页UI要用这个
    数字算总页数。条件必须跟search_posts完全一致，不然分页会算错，所以特意
    保持跟上面同样的分支结构、同样的筛选逻辑，方便对照检查两边有没有写歪。
    """
    conn = get_conn()
    q = query.strip()
    if q:
        if len(q) < 3:
            sql = "SELECT COUNT(*) AS c FROM posts p WHERE (p.title LIKE ? OR p.content_html LIKE ?)"
            like_q = f"%{q}%"
            params = [like_q, like_q]
        else:
            sql = """
                SELECT COUNT(*) AS c FROM posts_fts
                JOIN posts p ON p.post_id = posts_fts.post_id
                WHERE posts_fts MATCH ?
            """
            params = ['"' + q.replace('"', '""') + '"']
    else:
        sql = "SELECT COUNT(*) AS c FROM posts p WHERE 1=1"
        params = []

    if tag:
        sql += " AND p.tags LIKE ?"
        params.append(f'%"{tag}"%')
    if date_from:
        sql += " AND p.published >= ?"
        params.append(date_from)
    if date_to:
        sql += " AND p.published <= ?"
        params.append(date_to)

    row = conn.execute(sql, params).fetchone()
    conn.close()
    return row["c"]


def get_all_posts():
    conn = get_conn()
    rows = conn.execute("""
        SELECT p.post_id, p.title, p.published, p.tags, p.canonical_path, n.number
        FROM posts p
        LEFT JOIN post_numbers n ON n.post_id = p.post_id
        ORDER BY COALESCE(p.published_ts, p.published) DESC
    """).fetchall()
    conn.close()
    return [{"post_id": r["post_id"], "title": r["title"], "published": r["published"],
              "tags": json.loads(r["tags"]), "number": r["number"],
              "canonical_path": r["canonical_path"]} for r in rows]


def get_all_permalinks():
    """返回 {Blogger permalink: 本站当前应该用的根相对地址} 映射，只给Flask
    响应层改写文章正文里"引用本站另一篇文章"的Blogger链接用（见internal_links.py）。
    地址优先级跟fetch_blog.py的_href_for()保持一致：canonical_path > 短号 > post_id，
    避免两处出现不一致的判断逻辑。这个映射只在内存里用一次，不写回任何文件。
    """
    conn = get_conn()
    rows = conn.execute("""
        SELECT p.post_id, p.source_url, p.canonical_path, n.number
        FROM posts p
        LEFT JOIN post_numbers n ON n.post_id = p.post_id
    """).fetchall()
    conn.close()
    result = {}
    for r in rows:
        if not r["source_url"]:
            continue
        if r["canonical_path"]:
            url = f"/{r['canonical_path']}.html"
        elif r["number"]:
            url = f"/{r['number']}/"
        else:
            url = f"/posts/{r['post_id']}/"
        result[r["source_url"]] = url
    return result


def get_archive_index():
    """按年/月统计文章数，供前端做年份/月份筛选下拉框用。
    直接用SQL的substr(published, 1, 4)/substr(published, 6, 2)分组，published是
    fetch_blog.py写入的ISO格式日期字符串（年月日都是零填充定长），substr切片安全。
    """
    conn = get_conn()
    rows = conn.execute("""
        SELECT substr(p.published, 1, 4) AS year,
               substr(p.published, 6, 2) AS month,
               COUNT(*) AS c
        FROM posts p
        WHERE p.published IS NOT NULL AND p.published != ''
        GROUP BY year, month
        ORDER BY year DESC, month DESC
    """).fetchall()
    conn.close()

    years = {}
    for r in rows:
        y, m, c = r["year"], r["month"], r["c"]
        if not y or not m:
            continue
        entry = years.setdefault(y, {"year": int(y), "count": 0, "months": []})
        entry["count"] += c
        entry["months"].append({"month": int(m), "count": c})

    return sorted(years.values(), key=lambda e: e["year"], reverse=True)


def log_fetch_start():
    conn = get_conn()
    now = datetime.now().isoformat(timespec="seconds")
    cur = conn.execute("INSERT INTO fetch_log (started_at, status) VALUES (?, 'running')", (now,))
    conn.commit()
    log_id = cur.lastrowid
    conn.close()
    return log_id


def log_fetch_end(log_id, status, detail="", post_count=0):
    conn = get_conn()
    now = datetime.now().isoformat(timespec="seconds")
    conn.execute("""
        UPDATE fetch_log SET finished_at=?, status=?, detail=?, post_count=?
        WHERE id=?
    """, (now, status, detail, post_count, log_id))
    conn.commit()
    conn.close()


def get_last_fetch_status():
    conn = get_conn()
    row = conn.execute("SELECT * FROM fetch_log ORDER BY id DESC LIMIT 1").fetchone()
    conn.close()
    return dict(row) if row else None

def assign_missing_numbers():
    conn = get_conn()
    numbered_ids = {r["post_id"] for r in conn.execute("SELECT post_id FROM post_numbers").fetchall()}
    unnumbered = conn.execute("""
        SELECT post_id, published FROM posts
        WHERE post_id NOT IN (SELECT post_id FROM post_numbers)
        ORDER BY published ASC, post_id ASC
    """).fetchall()

    max_row = conn.execute("SELECT MAX(number) AS m FROM post_numbers").fetchone()
    next_number = (max_row["m"] or 0) + 1

    now = datetime.now().isoformat(timespec="seconds")
    newly_assigned = {}
    for row in unnumbered:
        conn.execute(
            "INSERT INTO post_numbers (post_id, number, assigned_at) VALUES (?, ?, ?)",
            (row["post_id"], next_number, now),
        )
        newly_assigned[row["post_id"]] = next_number
        next_number += 1

    conn.commit()
    conn.close()
    return newly_assigned


def get_all_numbers():
    conn = get_conn()
    rows = conn.execute("SELECT post_id, number FROM post_numbers").fetchall()
    conn.close()
    return {r["post_id"]: r["number"] for r in rows}


def get_number_for_post(post_id):
    conn = get_conn()
    row = conn.execute("SELECT number FROM post_numbers WHERE post_id = ?", (post_id,)).fetchone()
    conn.close()
    return row["number"] if row else None


# ---------------------------------------------------------------------------
# 访问计数（page_hits）
# ---------------------------------------------------------------------------

def record_finish_read(post_id, visitor_key=None):
    """记一次'读完'——前端检测到读者滚动到文章底部才会调用这个。
    同一天同一访客对同一篇文章只算一次，逻辑跟record_page_hit完全一样。
    """
    today = date.today().isoformat()
    conn = get_conn()
    if visitor_key:
        try:
            conn.execute(
                "INSERT INTO finish_read_dedup (post_id, visitor_key, hit_date) VALUES (?, ?, ?)",
                (post_id, visitor_key, today),
            )
        except sqlite3.IntegrityError:
            conn.close()
            return
    conn.execute("""
        INSERT INTO finish_reads (post_id, hit_date, count) VALUES (?, ?, 1)
        ON CONFLICT(post_id, hit_date) DO UPDATE SET count = count + 1
    """, (post_id, today))
    conn.commit()
    conn.close()


def get_post_finish_read_count(post_id):
    conn = get_conn()
    row = conn.execute(
        "SELECT COALESCE(SUM(count), 0) AS s FROM finish_reads WHERE post_id = ?", (post_id,)
    ).fetchone()
    conn.close()
    return row["s"]


def get_all_finish_read_counts():
    conn = get_conn()
    rows = conn.execute(
        "SELECT post_id, SUM(count) AS s FROM finish_reads GROUP BY post_id"
    ).fetchall()
    conn.close()
    return {r["post_id"]: r["s"] for r in rows}


def record_page_hit(post_id=None, visitor_key=None):
    """记一次访问。post_id=None表示首页。

    传了visitor_key（访客IP）时会去重：同一天同一访客对同一页面只计第一次，
    重复刷新/多次点击不会重复计数。visitor_key传None时不去重（兼容旧调用/
    没拿到IP的极端情况），会照记不误——宁可多算也不要因为拿不到IP直接不计数。
    """
    today = date.today().isoformat()
    page_key = post_id or "__home__"
    conn = get_conn()

    if visitor_key:
        try:
            conn.execute(
                "INSERT INTO page_hit_dedup (page_key, visitor_key, hit_date) VALUES (?, ?, ?)",
                (page_key, visitor_key, today),
            )
        except sqlite3.IntegrityError:
            # 今天这个访客已经计过这个页面了，不重复+1
            conn.close()
            return

    conn.execute("""
        INSERT INTO page_hits (post_id, hit_date, count) VALUES (?, ?, 1)
        ON CONFLICT(post_id, hit_date) DO UPDATE SET count = count + 1
    """, (post_id, today))
    conn.commit()
    conn.close()


def get_visit_stats():
    """返回 {today, week, month, year, total} 五个访问量数字（首页+所有文章之和）。"""
    today = date.today()
    week_start = (today - timedelta(days=today.weekday())).isoformat()  # 本周一
    month_start = today.replace(day=1).isoformat()
    year_start = today.replace(month=1, day=1).isoformat()
    today_str = today.isoformat()

    conn = get_conn()

    def sum_since(since):
        row = conn.execute(
            "SELECT COALESCE(SUM(count), 0) AS s FROM page_hits WHERE hit_date >= ?", (since,)
        ).fetchone()
        return row["s"]

    stats = {
        "today": sum_since(today_str),
        "week": sum_since(week_start),
        "month": sum_since(month_start),
        "year": sum_since(year_start),
        "total": conn.execute("SELECT COALESCE(SUM(count), 0) AS s FROM page_hits").fetchone()["s"],
    }
    conn.close()
    return stats


def get_post_click_count(post_id):
    conn = get_conn()
    row = conn.execute(
        "SELECT COALESCE(SUM(count), 0) AS s FROM page_hits WHERE post_id = ?", (post_id,)
    ).fetchone()
    conn.close()
    return row["s"]


def get_all_post_click_counts():
    """返回 {post_id: 总点击数}，用于渲染index时批量取值，避免逐篇查询。"""
    conn = get_conn()
    rows = conn.execute(
        "SELECT post_id, SUM(count) AS s FROM page_hits WHERE post_id IS NOT NULL GROUP BY post_id"
    ).fetchall()
    conn.close()
    return {r["post_id"]: r["s"] for r in rows}


def get_top_clicked(limit=10):
    """点击排行榜：[{post_id, title, count}, ...]，按总点击数降序。"""
    conn = get_conn()
    rows = conn.execute("""
        SELECT h.post_id AS post_id, p.title AS title, SUM(h.count) AS count
        FROM page_hits h
        JOIN posts p ON p.post_id = h.post_id
        WHERE h.post_id IS NOT NULL
        GROUP BY h.post_id
        ORDER BY count DESC
        LIMIT ?
    """, (limit,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# 下载计数（download_counts）
# ---------------------------------------------------------------------------

def record_download(post_id, scope="article"):
    """记一次下载。scope='article'时post_id必填；scope='site'时post_id应为None
    （表示一次全站打包/全部导出操作）。"""
    conn = get_conn()
    conn.execute("""
        INSERT INTO download_counts (post_id, scope, count) VALUES (?, ?, 1)
        ON CONFLICT(post_id, scope) DO UPDATE SET count = count + 1
    """, (post_id, scope))
    conn.commit()
    conn.close()


def get_post_download_count(post_id):
    conn = get_conn()
    row = conn.execute(
        "SELECT COALESCE(count, 0) AS c FROM download_counts WHERE post_id = ? AND scope = 'article'",
        (post_id,)
    ).fetchone()
    conn.close()
    return row["c"] if row else 0


def get_all_post_download_counts():
    conn = get_conn()
    rows = conn.execute(
        "SELECT post_id, count FROM download_counts WHERE scope = 'article'"
    ).fetchall()
    conn.close()
    return {r["post_id"]: r["count"] for r in rows}


def get_site_download_count():
    """全站打包/全部导出被点击的总次数。"""
    conn = get_conn()
    row = conn.execute(
        "SELECT COALESCE(SUM(count), 0) AS s FROM download_counts WHERE scope = 'site'"
    ).fetchone()
    conn.close()
    return row["s"]


def get_top_downloaded(limit=10):
    """下载排行榜：[{post_id, title, count}, ...]，按下载数降序。"""
    conn = get_conn()
    rows = conn.execute("""
        SELECT d.post_id AS post_id, p.title AS title, d.count AS count
        FROM download_counts d
        JOIN posts p ON p.post_id = d.post_id
        WHERE d.scope = 'article'
        ORDER BY d.count DESC
        LIMIT ?
    """, (limit,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


if __name__ == "__main__":
    init_db()
    print(f"数据库已初始化/迁移完成: {DB_PATH}")
