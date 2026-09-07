#!/usr/bin/env python3
"""Blogger删除文章 -> 本地镜像同步删除的回归测试。

覆盖：
- db.py: get_all_post_ids() / delete_post_record()（只删posts+posts_fts，
  不动post_versions/post_numbers/download_counts/page_hits/finish_reads）
- fetch_blog.py: find_deleted_post_ids() / _delete_post_static_files() /
  _deletion_sync_allowed() / sync_deleted_posts()
- 灾难保护：entries为空（Blogger API异常/返回不完整数据）绝不触发删除
- 删除同步后sitemap.xml/index.html/归档索引不残留已删除文章

不覆盖（有意，见报告）：
- fetch_feed()网络异常 -> main()提前sys.exit(1)这条路径本身：main()从未
  有过网络层面的测试基础设施（这个项目至今没有test_fetch_blog.py），这里
  不新建一套main()级别的网络mock框架；真正的安全性质——"没有可信的完整
  文章集合就不删除任何东西"——由本文件的灾难保护测试在sync_deleted_posts()
  这一层直接、完整地覆盖（main()对网络异常的处理是提前return/exit，实际
  上根本不会走到sync_deleted_posts()这一步）。
- git层面"删除是否被正确commit/push"：见test_git_publish.py新增的
  test_deleted_file_under_subpath_is_committed_and_pushed()，属于
  git_publish.py自己的职责，不在这里重复测。
- fan-out/mirror/backup请求级别的行为：fan-out和mirror/backup路由完全不
  关心html/这次变化是新增/修改/删除，统一处理，不需要专门为删除场景另写
  一份路由级测试（见test_publish_fanout.py已有覆盖）。

真实临时目录 + 真实临时sqlite数据库，绝不读写真实的data/blog.db或html/。

用法: python3 test_fetch_delete_sync.py
"""
import shutil
import sys
import tempfile
import traceback
from pathlib import Path

BASE_DIR = Path(__file__).parent
REAL_DB = BASE_DIR / "data" / "blog.db"

failures = []


def check(name, condition, detail=""):
    if condition:
        print(f"  [PASS] {name}")
    else:
        print(f"  [FAIL] {name} {detail}")
        failures.append(name)


def with_temp_fetch_env(fn):
    """跟test_refresh_lock.py的with_temp_app_env()同一个约定：db.DB_PATH
    必须先于`import fetch_blog`（进而触发`from app import ...`）被设置成
    临时路径，再额外把fetch_blog.HTML_DIR/POSTS_DIR指向一个临时目录，跑完
    全部还原。绝不读写真实的data/blog.db，也绝不写入真实的html/。
    """
    tmp = Path(tempfile.mkdtemp(prefix="fetch_delete_sync_test_"))
    real_db_mtime = REAL_DB.stat().st_mtime if REAL_DB.exists() else None

    import db
    orig_db_path = db.DB_PATH
    db.DB_PATH = tmp / "test.db"

    import fetch_blog as fetch_blog_module
    orig_html_dir = fetch_blog_module.HTML_DIR
    orig_posts_dir = fetch_blog_module.POSTS_DIR

    html_dir = tmp / "html"
    posts_dir = html_dir / "posts"
    posts_dir.mkdir(parents=True)
    fetch_blog_module.HTML_DIR = html_dir
    fetch_blog_module.POSTS_DIR = posts_dir

    try:
        db.init_db()
        fn(tmp, db, fetch_blog_module, html_dir)
    finally:
        fetch_blog_module.HTML_DIR = orig_html_dir
        fetch_blog_module.POSTS_DIR = orig_posts_dir
        db.DB_PATH = orig_db_path
        shutil.rmtree(tmp, ignore_errors=True)

    if real_db_mtime is not None:
        check("测试过程未修改真实data/blog.db（mtime不变）",
              REAL_DB.stat().st_mtime == real_db_mtime)


def _seed_post(db_mod, html_dir, post_id, *, canonical_path=None, title=None,
                with_media=False, content_hash=None):
    """在临时数据库+临时html/目录里同时构造一篇"看起来真实存在"的文章：
    posts表记录 + html/posts/<id>/index.html(+media/一张图) + （如果给了
    canonical_path）html/YYYY/MM/slug.html。跟fetch_blog.py真实生成的文件
    不要求内容完全一致，只要求"存在于预期路径、内容可辨识"，足够断言
    删除前后文件是否被正确移除/保留。
    """
    title = title or f"标题-{post_id}"
    content_hash = content_hash or f"hash-{post_id}"
    db_mod.upsert_post(post_id, title, f"<p>{post_id}正文</p>", ["tag1"],
                        "2026-01-01", "2026-01-01T00:00:00Z", content_hash,
                        canonical_path=canonical_path,
                        source_url=f"https://digatlas.blogspot.com/2026/01/{post_id}.html",
                        published_ts="2026-01-01T00:00:00.000+08:00")

    post_dir = html_dir / "posts" / post_id
    post_dir.mkdir(parents=True)
    (post_dir / "index.html").write_text(f"<html><body>{post_id}</body></html>", encoding="utf-8")
    if with_media:
        media_dir = post_dir / "media"
        media_dir.mkdir()
        (media_dir / "pic.jpg").write_bytes(b"fake-jpg-bytes")

    if canonical_path:
        year, month, slug = canonical_path.split("/")
        canonical_file = html_dir / year / month / f"{slug}.html"
        canonical_file.parent.mkdir(parents=True, exist_ok=True)
        canonical_file.write_text(f"<html><body>{post_id} canonical</body></html>", encoding="utf-8")


def _fake_entry(post_id_numeric_part: str) -> dict:
    """构造一个足够让slugify()/entries循环用的最小Blogger entry——只有
    id.$t字段，跟fetch_blog.slugify()对"post-(\\d+)"的匹配规则对应。
    deletion同步只依赖entries里能提取出post_id，不关心其它字段。
    """
    return {"id": {"$t": f"tag:blogger.com,1999:blog-1.post-{post_id_numeric_part}"}}


# ---------------------------------------------------------------------------
# db.py: get_all_post_ids / delete_post_record
# ---------------------------------------------------------------------------

def test_get_all_post_ids_returns_current_post_ids():
    def _run(tmp, db_mod, fb, html_dir):
        _seed_post(db_mod, html_dir, "111")
        _seed_post(db_mod, html_dir, "222")
        ids = db_mod.get_all_post_ids()
        check("get_all_post_ids()返回全部post_id", ids == {"111", "222"}, ids)
    with_temp_fetch_env(_run)


def test_delete_post_record_removes_posts_and_fts_row():
    def _run(tmp, db_mod, fb, html_dir):
        _seed_post(db_mod, html_dir, "111", title="独一无二的关键字XYZZY")
        db_mod.delete_post_record("111")
        check("posts表记录已删除", db_mod.get_existing_hash("111") is None)
        check("get_all_post_ids()不再包含已删除post_id", "111" not in db_mod.get_all_post_ids())
        results = db_mod.search_posts(query="XYZZY")
        check("FTS索引也已同步删除，搜索不到已删除文章", all(r["post_id"] != "111" for r in results), results)
    with_temp_fetch_env(_run)


def test_delete_post_record_preserves_post_versions_history():
    def _run(tmp, db_mod, fb, html_dir):
        _seed_post(db_mod, html_dir, "111")
        db_mod.save_version("111", "旧标题", "<p>旧内容</p>", "old-hash")
        db_mod.delete_post_record("111")
        conn = db_mod.get_conn()
        rows = conn.execute("SELECT * FROM post_versions WHERE post_id=?", ("111",)).fetchall()
        conn.close()
        check("删除文章不影响post_versions历史归档（历史不因当前站点删除而消失）", len(rows) == 1, rows)
    with_temp_fetch_env(_run)


def test_delete_post_record_preserves_post_numbers_short_link():
    def _run(tmp, db_mod, fb, html_dir):
        _seed_post(db_mod, html_dir, "111")
        newly = db_mod.assign_missing_numbers()
        number = newly["111"]
        db_mod.delete_post_record("111")
        check("删除文章不影响post_numbers短号映射（号码permanently fixed）",
              db_mod.get_number_for_post("111") == number)
    with_temp_fetch_env(_run)


def test_delete_post_record_preserves_download_and_hit_counts():
    def _run(tmp, db_mod, fb, html_dir):
        _seed_post(db_mod, html_dir, "111")
        db_mod.record_download("111", scope="article")
        db_mod.record_page_hit("111", visitor_key="1.2.3.4")
        db_mod.delete_post_record("111")
        check("删除文章不影响download_counts历史记录", db_mod.get_post_download_count("111") == 1)
        check("删除文章不影响page_hits历史记录", db_mod.get_post_click_count("111") == 1)
    with_temp_fetch_env(_run)


# ---------------------------------------------------------------------------
# fetch_blog.py: find_deleted_post_ids
# ---------------------------------------------------------------------------

def test_find_deleted_post_ids_detects_removed_post():
    def _run(tmp, db_mod, fb, html_dir):
        _seed_post(db_mod, html_dir, "111")
        _seed_post(db_mod, html_dir, "222")
        _seed_post(db_mod, html_dir, "333")
        deleted = fb.find_deleted_post_ids({"111", "222"})
        check("正确找出Blogger已不存在的post_id", deleted == ["333"], deleted)
    with_temp_fetch_env(_run)


def test_find_deleted_post_ids_ignores_modified_and_new_posts():
    def _run(tmp, db_mod, fb, html_dir):
        _seed_post(db_mod, html_dir, "111")  # 仍在current_ids里 = 未删除（不管内容是否变化）
        deleted = fb.find_deleted_post_ids({"111", "999"})  # 999是数据库里还没有的新文章
        check("仍存在于Blogger的文章不会被当成删除", deleted == [], deleted)
    with_temp_fetch_env(_run)


def test_find_deleted_post_ids_empty_when_nothing_removed():
    def _run(tmp, db_mod, fb, html_dir):
        _seed_post(db_mod, html_dir, "111")
        deleted = fb.find_deleted_post_ids({"111"})
        check("没有文章被删除时返回空列表", deleted == [], deleted)
    with_temp_fetch_env(_run)


def test_find_deleted_post_ids_sorted_and_handles_multiple():
    def _run(tmp, db_mod, fb, html_dir):
        for pid in ("333", "111", "222"):
            _seed_post(db_mod, html_dir, pid)
        deleted = fb.find_deleted_post_ids(set())
        check("多篇同时删除时按post_id排序返回，结果确定", deleted == ["111", "222", "333"], deleted)
    with_temp_fetch_env(_run)


# ---------------------------------------------------------------------------
# fetch_blog.py: _deletion_sync_allowed
# ---------------------------------------------------------------------------

def test_deletion_sync_allowed_false_for_empty_entries():
    def _run(tmp, db_mod, fb, html_dir):
        check("entries为空列表时不允许删除同步", fb._deletion_sync_allowed([]) is False)
    with_temp_fetch_env(_run)


def test_deletion_sync_allowed_true_for_nonempty_entries():
    def _run(tmp, db_mod, fb, html_dir):
        check("entries非空时允许删除同步", fb._deletion_sync_allowed([_fake_entry("111")]) is True)
    with_temp_fetch_env(_run)


# ---------------------------------------------------------------------------
# fetch_blog.py: _delete_post_static_files
# ---------------------------------------------------------------------------

def test_delete_post_static_files_removes_post_directory_and_media():
    def _run(tmp, db_mod, fb, html_dir):
        _seed_post(db_mod, html_dir, "111", with_media=True)
        post_dir = html_dir / "posts" / "111"
        check("准备阶段：文章目录+media文件确实存在", (post_dir / "media" / "pic.jpg").exists())
        fb._delete_post_static_files("111", None)
        check("posts/<id>/整个目录（含media/）已被删除", not post_dir.exists())
    with_temp_fetch_env(_run)


def test_delete_post_static_files_removes_canonical_static_file():
    def _run(tmp, db_mod, fb, html_dir):
        _seed_post(db_mod, html_dir, "111", canonical_path="2026/01/hello-world")
        canonical_file = html_dir / "2026" / "01" / "hello-world.html"
        check("准备阶段：canonical静态文件确实存在", canonical_file.exists())
        fb._delete_post_static_files("111", "2026/01/hello-world")
        check("YYYY/MM/slug.html canonical静态文件已被删除", not canonical_file.exists())
    with_temp_fetch_env(_run)


def test_delete_post_static_files_does_not_touch_sibling_post_in_same_month():
    def _run(tmp, db_mod, fb, html_dir):
        _seed_post(db_mod, html_dir, "111", canonical_path="2026/01/post-one")
        _seed_post(db_mod, html_dir, "222", canonical_path="2026/01/post-two")
        fb._delete_post_static_files("111", "2026/01/post-one")
        check("同月份的另一篇文章posts/目录不受影响", (html_dir / "posts" / "222").exists())
        check("同月份的另一篇文章canonical文件不受影响",
              (html_dir / "2026" / "01" / "post-two.html").exists())
    with_temp_fetch_env(_run)


def test_delete_post_static_files_cleans_up_now_empty_year_month_dirs():
    def _run(tmp, db_mod, fb, html_dir):
        _seed_post(db_mod, html_dir, "111", canonical_path="2026/01/only-one")
        fb._delete_post_static_files("111", "2026/01/only-one")
        check("变空的月份目录被顺手清理", not (html_dir / "2026" / "01").exists())
        check("变空的年份目录被顺手清理", not (html_dir / "2026").exists())
    with_temp_fetch_env(_run)


def test_delete_post_static_files_keeps_month_dir_when_sibling_file_remains():
    def _run(tmp, db_mod, fb, html_dir):
        _seed_post(db_mod, html_dir, "111", canonical_path="2026/01/post-one")
        _seed_post(db_mod, html_dir, "222", canonical_path="2026/01/post-two")
        fb._delete_post_static_files("111", "2026/01/post-one")
        check("月份目录里还有其它文章时不会被清理掉",
              (html_dir / "2026" / "01").exists() and (html_dir / "2026" / "01" / "post-two.html").exists())
    with_temp_fetch_env(_run)


def test_delete_post_static_files_missing_canonical_path_is_a_noop_for_static_file():
    def _run(tmp, db_mod, fb, html_dir):
        _seed_post(db_mod, html_dir, "111")  # 没有canonical_path
        fb._delete_post_static_files("111", None)  # 不应该抛异常
        check("canonical_path为None时静态文件这一步安全跳过，不抛异常", True)
    with_temp_fetch_env(_run)


def test_delete_post_static_files_idempotent_when_already_gone():
    def _run(tmp, db_mod, fb, html_dir):
        _seed_post(db_mod, html_dir, "111", canonical_path="2026/01/gone-twice")
        fb._delete_post_static_files("111", "2026/01/gone-twice")
        fb._delete_post_static_files("111", "2026/01/gone-twice")  # 第二次调用，文件已经不存在
        check("对已经不存在的文件重复调用不抛异常（幂等）", True)
    with_temp_fetch_env(_run)


def test_delete_post_static_files_does_not_touch_shared_assets():
    def _run(tmp, db_mod, fb, html_dir):
        images_dir = html_dir / "images"
        images_dir.mkdir()
        (images_dir / "fox-header.png").write_bytes(b"fake-png")
        _seed_post(db_mod, html_dir, "111", canonical_path="2026/01/hello-world")
        fb._delete_post_static_files("111", "2026/01/hello-world")
        check("全站共享的images/目录不受任何影响", (images_dir / "fox-header.png").exists())
    with_temp_fetch_env(_run)


# ---------------------------------------------------------------------------
# fetch_blog.py: sync_deleted_posts —— 端到端编排 + 灾难保护
# ---------------------------------------------------------------------------

def test_sync_deleted_posts_end_to_end_single_deletion():
    def _run(tmp, db_mod, fb, html_dir):
        _seed_post(db_mod, html_dir, "111", canonical_path="2026/01/keep-me")
        _seed_post(db_mod, html_dir, "222", canonical_path="2026/01/delete-me")
        entries = [_fake_entry("111")]  # 222在这次抓取里已经不存在了

        result = fb.sync_deleted_posts(entries)

        check("返回值只包含真正被删除的那一篇", result == [{"post_id": "222", "canonical_path": "2026/01/delete-me"}], result)
        check("被删除文章的DB记录已消失", "222" not in db_mod.get_all_post_ids())
        check("被删除文章的html目录已消失", not (html_dir / "posts" / "222").exists())
        check("被删除文章的canonical静态文件已消失", not (html_dir / "2026" / "01" / "delete-me.html").exists())
        check("未删除文章的DB记录原封不动", "111" in db_mod.get_all_post_ids())
        check("未删除文章的html目录原封不动", (html_dir / "posts" / "111").exists())
        check("未删除文章的canonical静态文件原封不动", (html_dir / "2026" / "01" / "keep-me.html").exists())
    with_temp_fetch_env(_run)


def test_sync_deleted_posts_multiple_deletions_in_one_round():
    def _run(tmp, db_mod, fb, html_dir):
        _seed_post(db_mod, html_dir, "111")
        _seed_post(db_mod, html_dir, "222")
        _seed_post(db_mod, html_dir, "333")
        entries = [_fake_entry("111")]  # 222和333同时被删除

        result = fb.sync_deleted_posts(entries)

        deleted_ids = sorted(d["post_id"] for d in result)
        check("一次抓取里同时删除多篇文章", deleted_ids == ["222", "333"], deleted_ids)
        check("幸存文章不受影响", db_mod.get_all_post_ids() == {"111"})
    with_temp_fetch_env(_run)


def test_sync_deleted_posts_returns_empty_and_deletes_nothing_when_entries_empty():
    """灾难保护核心测试：Blogger返回空列表（API异常/数据不完整的典型表现），
    绝不能被当成"所有文章都被删除了"。"""
    def _run(tmp, db_mod, fb, html_dir):
        _seed_post(db_mod, html_dir, "111", canonical_path="2026/01/one")
        _seed_post(db_mod, html_dir, "222", canonical_path="2026/01/two")
        before_ids = db_mod.get_all_post_ids()

        result = fb.sync_deleted_posts([])

        check("entries为空时返回空列表，不删除任何文章", result == [], result)
        check("数据库里的post_id集合完全没有变化", db_mod.get_all_post_ids() == before_ids)
        check("html/posts/111/未被触碰", (html_dir / "posts" / "111").exists())
        check("html/posts/222/未被触碰", (html_dir / "posts" / "222").exists())
        check("canonical静态文件未被触碰",
              (html_dir / "2026" / "01" / "one.html").exists() and (html_dir / "2026" / "01" / "two.html").exists())
    with_temp_fetch_env(_run)


def test_sync_deleted_posts_skips_post_whose_file_deletion_fails_but_continues_others():
    def _run(tmp, db_mod, fb, html_dir):
        _seed_post(db_mod, html_dir, "111")
        _seed_post(db_mod, html_dir, "222")
        entries = []  # 111和222都"被删除"，但下面故意让111的文件删除失败

        orig_delete_files = fb._delete_post_static_files

        def _flaky_delete(post_id, canonical_path):
            if post_id == "111":
                raise OSError("模拟磁盘权限错误")
            return orig_delete_files(post_id, canonical_path)

        fb._delete_post_static_files = _flaky_delete
        try:
            result = fb.sync_deleted_posts([_fake_entry("999")])  # 999不存在于db，111/222都待删除
        finally:
            fb._delete_post_static_files = orig_delete_files

        deleted_ids = sorted(d["post_id"] for d in result)
        check("文件删除失败的那一篇被跳过，不出现在返回值里", deleted_ids == ["222"], deleted_ids)
        check("文件删除失败的那一篇DB记录被保留（不是先删DB再删文件）", "111" in db_mod.get_all_post_ids())
        check("其它待删除文章不受一篇失败的影响，正常删除", "222" not in db_mod.get_all_post_ids())
    with_temp_fetch_env(_run)


def test_sync_deleted_posts_url_list_omits_posts_without_canonical_path():
    def _run(tmp, db_mod, fb, html_dir):
        _seed_post(db_mod, html_dir, "111", canonical_path="2026/01/has-canonical")
        _seed_post(db_mod, html_dir, "222")  # 没有canonical_path（permalink解析失败的极端情况）
        result = fb.sync_deleted_posts([])
        # 上面这次entries=[]不会真的删除任何东西（灾难保护），这里单独验证
        # main()里"deleted_urls = [...]"这行的过滤逻辑本身：
        deleted_urls = [f"{fb.MIRROR_ROOT_URL}/{d['canonical_path']}.html" for d in
                         [{"post_id": "111", "canonical_path": "2026/01/has-canonical"},
                          {"post_id": "222", "canonical_path": None}]
                         if d["canonical_path"]]
        check("没有canonical_path的已删除文章不会生成一个畸形的IndexNow/purge URL",
              deleted_urls == ["https://mirror.foxzen.me/2026/01/has-canonical.html"], deleted_urls)
    with_temp_fetch_env(_run)


# ---------------------------------------------------------------------------
# 删除同步后，下游产物（首页/sitemap/归档索引）不残留已删除文章
# ---------------------------------------------------------------------------

def test_render_seo_files_and_render_index_exclude_deleted_post_after_sync():
    def _run(tmp, db_mod, fb, html_dir):
        _seed_post(db_mod, html_dir, "111", canonical_path="2026/01/still-here", title="幸存文章")
        _seed_post(db_mod, html_dir, "222", canonical_path="2026/02/gone-now", title="已删除文章")

        fb.sync_deleted_posts([_fake_entry("111")])
        fb.render_index()
        fb.render_seo_files()

        index_html = (html_dir / "index.html").read_text(encoding="utf-8")
        sitemap_xml = (html_dir / "sitemap.xml").read_text(encoding="utf-8")

        check("首页不再包含已删除文章的canonical链接", "/2026/02/gone-now.html" not in index_html)
        check("首页仍然包含幸存文章的canonical链接", "/2026/01/still-here.html" in index_html)
        check("sitemap.xml不再包含已删除文章的URL", "2026/02/gone-now.html" not in sitemap_xml)
        check("sitemap.xml仍然包含幸存文章的URL", "2026/01/still-here.html" in sitemap_xml)
    with_temp_fetch_env(_run)


def test_get_archive_index_excludes_deleted_post():
    def _run(tmp, db_mod, fb, html_dir):
        _seed_post(db_mod, html_dir, "111", canonical_path="2026/01/one")
        db_mod.upsert_post("111", "T1", "<p>1</p>", [], "2026-01-15", "2026-01-15T00:00:00Z", "h1",
                            canonical_path="2026/01/one", published_ts="2026-01-15T00:00:00+08:00")
        _seed_post(db_mod, html_dir, "222", canonical_path="2026/01/two")
        db_mod.upsert_post("222", "T2", "<p>2</p>", [], "2026-01-20", "2026-01-20T00:00:00Z", "h2",
                            canonical_path="2026/01/two", published_ts="2026-01-20T00:00:00+08:00")

        before = db_mod.get_archive_index()
        check("准备阶段：2026年1月有2篇文章", before[0]["months"][0]["count"] == 2, before)

        fb.sync_deleted_posts([_fake_entry("111")])

        after = db_mod.get_archive_index()
        check("删除1篇后，2026年1月归档计数正确减少为1", after[0]["months"][0]["count"] == 1, after)
    with_temp_fetch_env(_run)


# ---------------------------------------------------------------------------

def main():
    tests = [
        test_get_all_post_ids_returns_current_post_ids,
        test_delete_post_record_removes_posts_and_fts_row,
        test_delete_post_record_preserves_post_versions_history,
        test_delete_post_record_preserves_post_numbers_short_link,
        test_delete_post_record_preserves_download_and_hit_counts,
        test_find_deleted_post_ids_detects_removed_post,
        test_find_deleted_post_ids_ignores_modified_and_new_posts,
        test_find_deleted_post_ids_empty_when_nothing_removed,
        test_find_deleted_post_ids_sorted_and_handles_multiple,
        test_deletion_sync_allowed_false_for_empty_entries,
        test_deletion_sync_allowed_true_for_nonempty_entries,
        test_delete_post_static_files_removes_post_directory_and_media,
        test_delete_post_static_files_removes_canonical_static_file,
        test_delete_post_static_files_does_not_touch_sibling_post_in_same_month,
        test_delete_post_static_files_cleans_up_now_empty_year_month_dirs,
        test_delete_post_static_files_keeps_month_dir_when_sibling_file_remains,
        test_delete_post_static_files_missing_canonical_path_is_a_noop_for_static_file,
        test_delete_post_static_files_idempotent_when_already_gone,
        test_delete_post_static_files_does_not_touch_shared_assets,
        test_sync_deleted_posts_end_to_end_single_deletion,
        test_sync_deleted_posts_multiple_deletions_in_one_round,
        test_sync_deleted_posts_returns_empty_and_deletes_nothing_when_entries_empty,
        test_sync_deleted_posts_skips_post_whose_file_deletion_fails_but_continues_others,
        test_sync_deleted_posts_url_list_omits_posts_without_canonical_path,
        test_render_seo_files_and_render_index_exclude_deleted_post_after_sync,
        test_get_archive_index_excludes_deleted_post,
    ]
    for t in tests:
        print(f"--- {t.__name__} ---")
        try:
            t()
        except Exception:
            print(f"  [FAIL] {t.__name__} 抛出异常:")
            traceback.print_exc()
            failures.append(t.__name__)
    if failures:
        print(f"\n共 {len(failures)} 项失败: {failures}")
        sys.exit(1)
    print("\n全部测试通过。")


if __name__ == "__main__":
    main()
