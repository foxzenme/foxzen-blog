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
import sqlite3
import json
from pathlib import Path
from datetime import datetime, date, timedelta

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
"""


def get_conn():
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

    conn.commit()
    conn.close()


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
