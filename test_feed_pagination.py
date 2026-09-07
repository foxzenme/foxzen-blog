#!/usr/bin/env python3
"""Blogger feed分页/completeness safety回归测试。

背景：Blogger feed用GData协议分页（start-index从1开始+max-results），响应体
feed.openSearch$totalResults/startIndex/itemsPerPage是标准OpenSearch分页扩展
字段——这几个字段名/语义是用实际的digatlas.blogspot.com正式接口验证过的
（不是凭记忆假设），关键结论：openSearch$itemsPerPage回显的是请求参数本身，
不是这一页实际返回的条数，判断"是否最后一页"只能看这一页实际返回的entry
数量，不能用这个字段。

覆盖：
- fetch_all_entries()正确分页：单页/多页/首页/中间页/末页/整除边界/>=500篇
- completeness safety：分页请求失败、totalResults缺失/非法/中途变化、
  累计条数跟totalResults不一致、跨页id重复、单页条数超过max_results，
  全部fail closed（抛FeedPaginationError，不返回部分结果）
- 端到端：分页不完整时main()不会删除、不会upsert任何东西就直接退出；
  分页完整成功时main()正常处理

不覆盖（有意）：
- find_deleted_post_ids()/_delete_post_static_files()/sync_deleted_posts()
  本身的删除逻辑——这次改动完全没有碰这几个函数，见test_fetch_delete_sync.py，
  职责刻意分开，不在这个文件里重复测。

真实临时目录 + 真实临时sqlite数据库，绝不发真实网络请求（fetch_feed_page
在这个文件里全程被替换成假实现），绝不读写真实的data/blog.db或html/。

用法: python3 test_feed_pagination.py
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
    """跟test_fetch_delete_sync.py的with_temp_fetch_env()同一个约定：
    db.DB_PATH必须先于`import fetch_blog`被设置成临时路径，再把
    fetch_blog.HTML_DIR/POSTS_DIR指向一个临时目录，跑完全部还原。
    """
    tmp = Path(tempfile.mkdtemp(prefix="feed_pagination_test_"))
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


def _entry(post_id_numeric_part) -> dict:
    """最小entry，只有id字段——fetch_all_entries()的分页/completeness逻辑
    只读entry的id字段，不需要title/content这些完整字段。"""
    return {"id": {"$t": f"tag:blogger.com,1999:blog-1.post-{post_id_numeric_part}"}}


def _feed_response(total, entries) -> dict:
    """构造一页fetch_feed_page()应该返回的dict形状，total是这一页声称的
    openSearch$totalResults（调用方可以故意跟entries实际数量不一致，
    用于构造异常场景）。"""
    return {"feed": {"openSearch$totalResults": {"$t": str(total)}, "entry": entries}}


class _FakePageProvider:
    """记录每次被调用时的(start_index, max_results)参数，按调用顺序依次
    返回预先准备好的page（dict则返回，Exception实例则raise）。"""
    def __init__(self, pages):
        self.pages = list(pages)
        self.calls = []

    def __call__(self, start_index, max_results):
        self.calls.append((start_index, max_results))
        if not self.pages:
            raise AssertionError(f"fetch_feed_page被调用次数超过预期准备的页数，调用记录: {self.calls}")
        page = self.pages.pop(0)
        if isinstance(page, Exception):
            raise page
        return page


def _install_fake_pages(fb, pages, page_size=None):
    """把fb.fetch_feed_page换成假实现，可选把fb.FEED_PAGE_SIZE也换成更小
    的值方便构造测试数据。返回(provider, restore_fn)，调用方必须在finally
    里调用restore_fn()还原。"""
    orig_fetch = fb.fetch_feed_page
    orig_page_size = fb.FEED_PAGE_SIZE
    provider = _FakePageProvider(pages)
    fb.fetch_feed_page = provider
    if page_size is not None:
        fb.FEED_PAGE_SIZE = page_size

    def _restore():
        fb.fetch_feed_page = orig_fetch
        fb.FEED_PAGE_SIZE = orig_page_size

    return provider, _restore


# ---------------------------------------------------------------------------
# fetch_all_entries()：正常分页
# ---------------------------------------------------------------------------

def test_single_page_when_total_under_page_size():
    def _run(tmp, db_mod, fb, html_dir):
        entries = [_entry(i) for i in range(1, 19)]  # 18篇，默认FEED_PAGE_SIZE=500
        provider, restore = _install_fake_pages(fb, [_feed_response(18, entries)])
        try:
            result = fb.fetch_all_entries()
            check("总数低于一页大小时只请求1次（不浪费请求）", len(provider.calls) == 1, provider.calls)
            check("第一次请求start_index=1", provider.calls[0][0] == 1, provider.calls)
            check("返回全部18篇", len(result) == 18, len(result))
        finally:
            restore()
    with_temp_fetch_env(_run)


def test_first_page_requests_start_index_1():
    def _run(tmp, db_mod, fb, html_dir):
        provider, restore = _install_fake_pages(
            fb, [_feed_response(2, [_entry(1), _entry(2)])], page_size=10)
        try:
            fb.fetch_all_entries()
            check("首页请求start_index=1、max_results=当前FEED_PAGE_SIZE",
                  provider.calls[0] == (1, 10), provider.calls)
        finally:
            restore()
    with_temp_fetch_env(_run)


def test_middle_page_start_index_advances_by_actual_returned_count():
    def _run(tmp, db_mod, fb, html_dir):
        # page_size=2：第1页正好2条（继续翻页），第2页正好2条（继续），
        # 第3页1条（<2，结束）——验证start_index按"上一页实际返回条数"累加，
        # 不是简单按page_size累加，也不会漏掉/重复中间的条目。
        pages = [
            _feed_response(5, [_entry(1), _entry(2)]),
            _feed_response(5, [_entry(3), _entry(4)]),
            _feed_response(5, [_entry(5)]),
        ]
        provider, restore = _install_fake_pages(fb, pages, page_size=2)
        try:
            result = fb.fetch_all_entries()
            check("请求了3页", len(provider.calls) == 3, provider.calls)
            check("start_index序列正确推进：1, 3, 5",
                  [c[0] for c in provider.calls] == [1, 3, 5], provider.calls)
            ids = [e["id"]["$t"] for e in result]
            check("5篇文章按顺序全部拿到、无遗漏无重复",
                  ids == [_entry(i)["id"]["$t"] for i in (1, 2, 3, 4, 5)], ids)
        finally:
            restore()
    with_temp_fetch_env(_run)


def test_last_page_shorter_than_page_size_terminates_pagination():
    def _run(tmp, db_mod, fb, html_dir):
        pages = [
            _feed_response(3, [_entry(1), _entry(2)]),
            _feed_response(3, [_entry(3)]),  # 1 < page_size=2，最后一页
        ]
        provider, restore = _install_fake_pages(fb, pages, page_size=2)
        try:
            result = fb.fetch_all_entries()
            check("末页不足一页时正常结束，不会继续多请求", len(provider.calls) == 2, provider.calls)
            check("累计拿到3篇", len(result) == 3, len(result))
        finally:
            restore()
    with_temp_fetch_env(_run)


def test_exact_multiple_of_page_size_triggers_extra_empty_page_fetch():
    """总数刚好是page_size的整数倍时（比如4篇、page_size=2），最后一页
    返回的条数会等于page_size本身（不会小于page_size），必须多请求一次
    拿到0条才能确认真的到底了，不能把"这一页刚好满"误判成"这是最后一页"。
    """
    def _run(tmp, db_mod, fb, html_dir):
        pages = [
            _feed_response(4, [_entry(1), _entry(2)]),
            _feed_response(4, [_entry(3), _entry(4)]),
            _feed_response(4, []),  # 确认结束的空页
        ]
        provider, restore = _install_fake_pages(fb, pages, page_size=2)
        try:
            result = fb.fetch_all_entries()
            check("整除边界必须多请求一次确认空页", len(provider.calls) == 3, provider.calls)
            check("累计拿到4篇，无重复", len(result) == 4, len(result))
        finally:
            restore()
    with_temp_fetch_env(_run)


def test_over_500_entries_paginated_correctly():
    """需求7：>=500篇文章的场景，用mock分页验证——1037篇，FEED_PAGE_SIZE
    保持默认500，分3页（500+500+37）。"""
    def _run(tmp, db_mod, fb, html_dir):
        total = 1037
        page1 = [_entry(i) for i in range(1, 501)]
        page2 = [_entry(i) for i in range(501, 1001)]
        page3 = [_entry(i) for i in range(1001, 1038)]
        pages = [_feed_response(total, page1), _feed_response(total, page2), _feed_response(total, page3)]
        provider, restore = _install_fake_pages(fb, pages)  # 默认FEED_PAGE_SIZE=500，不改
        try:
            result = fb.fetch_all_entries()
            check("1037篇（超过500）正确分3页请求", len(provider.calls) == 3, provider.calls)
            check("start_index序列: 1, 501, 1001",
                  [c[0] for c in provider.calls] == [1, 501, 1001], provider.calls)
            check("累计拿到全部1037篇", len(result) == 1037, len(result))
            ids = {e["id"]["$t"] for e in result}
            check("1037篇id全部唯一（无重复无遗漏）", len(ids) == 1037, len(ids))
        finally:
            restore()
    with_temp_fetch_env(_run)


# ---------------------------------------------------------------------------
# fetch_all_entries()：completeness safety —— 一律fail closed
# ---------------------------------------------------------------------------

def test_page_request_failure_raises_and_returns_nothing():
    def _run(tmp, db_mod, fb, html_dir):
        pages = [_feed_response(4, [_entry(1), _entry(2)]), ConnectionError("模拟网络中断")]
        provider, restore = _install_fake_pages(fb, pages, page_size=2)
        try:
            raised = False
            try:
                fb.fetch_all_entries()
            except fb.FeedPaginationError as e:
                raised = True
                check("异常信息包含原始错误", "模拟网络中断" in str(e), str(e))
            check("第2页请求失败时抛FeedPaginationError，不静默返回部分结果", raised)
        finally:
            restore()
    with_temp_fetch_env(_run)


def test_missing_total_results_on_first_page_raises():
    def _run(tmp, db_mod, fb, html_dir):
        bad_page = {"feed": {"entry": [_entry(1)]}}  # 没有openSearch$totalResults
        provider, restore = _install_fake_pages(fb, [bad_page])
        try:
            raised = False
            try:
                fb.fetch_all_entries()
            except fb.FeedPaginationError:
                raised = True
            check("首页缺少openSearch$totalResults时fail closed", raised)
            check("只尝试了1次就失败，不继续翻页", len(provider.calls) == 1, provider.calls)
        finally:
            restore()
    with_temp_fetch_env(_run)


def test_non_numeric_total_results_raises():
    def _run(tmp, db_mod, fb, html_dir):
        bad_page = {"feed": {"openSearch$totalResults": {"$t": "not-a-number"}, "entry": [_entry(1)]}}
        provider, restore = _install_fake_pages(fb, [bad_page])
        try:
            raised = False
            try:
                fb.fetch_all_entries()
            except fb.FeedPaginationError:
                raised = True
            check("openSearch$totalResults不是合法数字时fail closed", raised)
        finally:
            restore()
    with_temp_fetch_env(_run)


def test_total_results_changes_mid_pagination_raises():
    def _run(tmp, db_mod, fb, html_dir):
        pages = [
            _feed_response(5, [_entry(1), _entry(2)]),
            _feed_response(6, [_entry(3), _entry(4)]),  # total从5变成6
        ]
        provider, restore = _install_fake_pages(fb, pages, page_size=2)
        try:
            raised = False
            try:
                fb.fetch_all_entries()
            except fb.FeedPaginationError as e:
                raised = True
                check("异常信息提到total变化", "5" in str(e) and "6" in str(e), str(e))
            check("分页过程中totalResults变化时fail closed（疑似翻页期间文章集合变动）", raised)
        finally:
            restore()
    with_temp_fetch_env(_run)


def test_duplicate_entry_id_across_pages_raises():
    def _run(tmp, db_mod, fb, html_dir):
        pages = [
            _feed_response(4, [_entry(1), _entry(2)]),
            _feed_response(4, [_entry(2), _entry(3)]),  # id=2重复出现
        ]
        provider, restore = _install_fake_pages(fb, pages, page_size=2)
        try:
            raised = False
            try:
                fb.fetch_all_entries()
            except fb.FeedPaginationError:
                raised = True
            check("跨页出现重复文章id时fail closed（疑似翻页期间文章集合变动）", raised)
        finally:
            restore()
    with_temp_fetch_env(_run)


def test_final_count_mismatch_with_total_results_raises():
    """totalResults在每一页里都保持一致（不触发"变化"检查），但分页自然
    结束后累计条数对不上——比如翻页期间有文章被删除，实际能拿到的比
    Blogger声称的总数少。"""
    def _run(tmp, db_mod, fb, html_dir):
        pages = [
            _feed_response(5, [_entry(1), _entry(2)]),  # 2条，继续翻页
            _feed_response(5, [_entry(3)]),               # 1条(<page_size)，自然结束
        ]
        provider, restore = _install_fake_pages(fb, pages, page_size=2)
        try:
            raised = False
            try:
                fb.fetch_all_entries()
            except fb.FeedPaginationError as e:
                raised = True
                check("异常信息提到累计数量与声明总数", "3" in str(e) and "5" in str(e), str(e))
            check("累计条数跟openSearch$totalResults不一致时fail closed", raised)
        finally:
            restore()
    with_temp_fetch_env(_run)


def test_page_returns_more_entries_than_requested_raises():
    def _run(tmp, db_mod, fb, html_dir):
        # page_size=2，但这一页"服务端异常"返回了3条
        bad_page = _feed_response(3, [_entry(1), _entry(2), _entry(3)])
        provider, restore = _install_fake_pages(fb, [bad_page], page_size=2)
        try:
            raised = False
            try:
                fb.fetch_all_entries()
            except fb.FeedPaginationError:
                raised = True
            check("单页返回条数超过请求的max_results时fail closed（服务端行为异常）", raised)
        finally:
            restore()
    with_temp_fetch_env(_run)


def test_entry_missing_id_field_raises():
    def _run(tmp, db_mod, fb, html_dir):
        bad_page = _feed_response(1, [{"id": {}}])  # 缺少$t
        provider, restore = _install_fake_pages(fb, [bad_page])
        try:
            raised = False
            try:
                fb.fetch_all_entries()
            except fb.FeedPaginationError:
                raised = True
            check("entry缺少id字段时fail closed", raised)
        finally:
            restore()
    with_temp_fetch_env(_run)


def test_feed_key_missing_from_response_raises():
    def _run(tmp, db_mod, fb, html_dir):
        provider, restore = _install_fake_pages(fb, [{"unexpected": "shape"}])
        try:
            raised = False
            try:
                fb.fetch_all_entries()
            except fb.FeedPaginationError:
                raised = True
            check("响应体缺少feed字段时fail closed", raised)
        finally:
            restore()
    with_temp_fetch_env(_run)


# ---------------------------------------------------------------------------
# 端到端：main() + fetch_all_entries()
# ---------------------------------------------------------------------------

def _seed_post(db_mod, html_dir, post_id, *, canonical_path=None, title=None):
    title = title or f"标题-{post_id}"
    db_mod.upsert_post(post_id, title, f"<p>{post_id}正文</p>", ["tag1"],
                        "2026-01-01", "2026-01-01T00:00:00Z", f"hash-{post_id}",
                        canonical_path=canonical_path,
                        source_url=f"https://digatlas.blogspot.com/2026/01/{post_id}.html",
                        published_ts="2026-01-01T00:00:00.000+08:00")
    post_dir = html_dir / "posts" / post_id
    post_dir.mkdir(parents=True)
    (post_dir / "index.html").write_text(f"<html><body>{post_id}</body></html>", encoding="utf-8")
    if canonical_path:
        year, month, slug = canonical_path.split("/")
        canonical_file = html_dir / year / month / f"{slug}.html"
        canonical_file.parent.mkdir(parents=True, exist_ok=True)
        canonical_file.write_text(f"<html><body>{post_id} canonical</body></html>", encoding="utf-8")


def test_main_aborts_and_deletes_nothing_when_pagination_incomplete():
    """需求6：feed不完整时main()绝对不能删除已有文章——这里直接跑真实的
    main()（只替换fetch_all_entries()和notify()两个外部边界，DB/html/
    全部走临时目录的真实文件系统/真实sqlite），验证端到端行为，不只是
    验证fetch_all_entries()单独抛异常这一件事。
    """
    def _run(tmp, db_mod, fb, html_dir):
        _seed_post(db_mod, html_dir, "111", canonical_path="2026/01/one")
        before_ids = db_mod.get_all_post_ids()

        def _raise_incomplete():
            raise fb.FeedPaginationError("模拟分页不完整（测试注入）")

        orig_fetch_all = fb.fetch_all_entries
        orig_notify = fb.notify
        fb.fetch_all_entries = _raise_incomplete
        fb.notify = lambda msg: None
        try:
            exited = False
            try:
                fb.main()
            except SystemExit as e:
                exited = True
                check("main()以非0退出码结束（不是静默吞掉异常）", e.code not in (0, None), e.code)
            check("main()在分页不完整时会sys.exit，不会往下继续跑", exited)
        finally:
            fb.fetch_all_entries = orig_fetch_all
            fb.notify = orig_notify

        check("已有文章的数据库post_id集合完全没有变化", db_mod.get_all_post_ids() == before_ids, db_mod.get_all_post_ids())
        check("已有文章的html/posts/目录完全没有变化", (html_dir / "posts" / "111").exists())
        check("已有文章的canonical静态文件完全没有变化", (html_dir / "2026" / "01" / "one.html").exists())
    with_temp_fetch_env(_run)


def test_main_succeeds_and_upserts_when_pagination_complete():
    """跟上面的失败路径对照：分页完整成功时main()应该正常处理新文章，
    确认这次改动没有把正常路径也搞坏。"""
    def _run(tmp, db_mod, fb, html_dir):
        fake_entries = [{
            "id": {"$t": "tag:blogger.com,1999:blog-1.post-222"},
            "title": {"$t": "测试文章"},
            "content": {"$t": "<p>没有图片/音频的纯文本正文</p>"},
            "published": {"$t": "2026-01-05T10:00:00.000+08:00"},
            "updated": {"$t": "2026-01-05T10:00:00.000+08:00"},
        }]

        def _fake_fetch_all():
            return fake_entries

        orig_fetch_all = fb.fetch_all_entries
        orig_notify = fb.notify
        fb.fetch_all_entries = _fake_fetch_all
        fb.notify = lambda msg: None
        try:
            fb.main()
        finally:
            fb.fetch_all_entries = orig_fetch_all
            fb.notify = orig_notify

        check("分页完整成功时，main()正常upsert新文章", "222" in db_mod.get_all_post_ids())
        check("文章静态页正常渲染", (html_dir / "posts" / "222" / "index.html").exists())
    with_temp_fetch_env(_run)


# ---------------------------------------------------------------------------

def main():
    tests = [
        test_single_page_when_total_under_page_size,
        test_first_page_requests_start_index_1,
        test_middle_page_start_index_advances_by_actual_returned_count,
        test_last_page_shorter_than_page_size_terminates_pagination,
        test_exact_multiple_of_page_size_triggers_extra_empty_page_fetch,
        test_over_500_entries_paginated_correctly,
        test_page_request_failure_raises_and_returns_nothing,
        test_missing_total_results_on_first_page_raises,
        test_non_numeric_total_results_raises,
        test_total_results_changes_mid_pagination_raises,
        test_duplicate_entry_id_across_pages_raises,
        test_final_count_mismatch_with_total_results_raises,
        test_page_returns_more_entries_than_requested_raises,
        test_entry_missing_id_field_raises,
        test_feed_key_missing_from_response_raises,
        test_main_aborts_and_deletes_nothing_when_pagination_incomplete,
        test_main_succeeds_and_upserts_when_pagination_complete,
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
