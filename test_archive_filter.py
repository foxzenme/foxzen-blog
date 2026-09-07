#!/usr/bin/env python3
"""mirror.foxzen.me"按年份/月份筛选文章"功能回归测试。

范围说明（跟此前已完成的任务明确分开，不重复测试）：
- 年/月统计（db.get_archive_index()）、/api/archive、/api/search的year/month
  参数、_resolve_scope()的year分支，这些全部是commit b83acb2（P2）里已经写好
  但从未有过正式测试文件的既有后端代码——这里补上测试，不是重新实现。
- 这次任务本身新增的代码只有两处：
  1. app.py::_selected_zip_filename()——按年/月筛选下载时用"2026-08.zip"这种
     名字，跟已有的整站下载/勾选下载文件名区分开。
  2. static/index.js里的年份/月份下拉框、URL状态同步、双语文案、"下载筛选结果"
     按钮——首页此前完全没有UI去调用已经存在的/api/archive和年月筛选参数。
- 不覆盖：refresh四目标管道、status/update页面、Wayback校验、文章阅读体验
  （标题层级/drop-cap/TOC/I18N_BLOCK）、整站ZIP缓存(zip_cache.py)本身的构建/
  锁/过期逻辑——那些各自有自己的测试文件，这里只做"没有被这次改动波及"的
  轻量回归验证。

用法: python3 test_archive_filter.py
"""
import json
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

BASE_DIR = Path(__file__).parent
REAL_DB = BASE_DIR / "data" / "blog.db"
INDEX_JS = (BASE_DIR / "static" / "index.js").read_text(encoding="utf-8")

failures = []


def check(name, condition, detail=""):
    if condition:
        print(f"  [PASS] {name}")
    else:
        print(f"  [FAIL] {name} {detail}")
        failures.append(name)


def with_temp_env(fn):
    """db.DB_PATH + app.HTML_DIR/POSTS_DIR全部指向临时目录，跑完自动还原/清理，
    绝不读写真实的data/blog.db或html/posts/。跟test_internal_links.py::
    with_temp_env()、test_canonical_static.py::with_temp_html_dir()同一个约定：
    db.DB_PATH必须先于`import app`重定向。
    """
    import db

    tmp = Path(tempfile.mkdtemp(prefix="archive_filter_test_"))
    orig_db_path = db.DB_PATH
    real_db_mtime = REAL_DB.stat().st_mtime if REAL_DB.exists() else None

    db.DB_PATH = tmp / "test.db"

    import app as app_module
    orig_html_dir = app_module.HTML_DIR
    orig_posts_dir = app_module.POSTS_DIR
    app_module.HTML_DIR = tmp / "html"
    app_module.POSTS_DIR = app_module.HTML_DIR / "posts"
    app_module.POSTS_DIR.mkdir(parents=True)
    app_module.app.config["TESTING"] = True

    try:
        db.init_db()
        fn(tmp, db, app_module)
    finally:
        db.DB_PATH = orig_db_path
        app_module.HTML_DIR = orig_html_dir
        app_module.POSTS_DIR = orig_posts_dir
        shutil.rmtree(tmp, ignore_errors=True)

    if real_db_mtime is not None:
        check("测试过程未修改真实data/blog.db（mtime不变）",
              REAL_DB.stat().st_mtime == real_db_mtime)


def _seed_post(db, app_module, post_id, title, published, tags=None,
                canonical_path=None, with_media=False):
    """写入一篇文章：db记录 + html/posts/<id>/index.html（供zip打包读取）。
    published是"YYYY-MM-DD"，故意不给published_ts（模拟published_ts为空的
    历史文章）——get_archive_index()/搜索/筛选按published分组不依赖published_ts，
    published_ts只用于同一天内的排序tie-break，跟这次筛选逻辑本身无关。
    """
    db.upsert_post(post_id, title, f'<div class="content">{title}的正文</div>',
                    tags or [], published, published + "T00:00:00Z",
                    f"hash-{post_id}", canonical_path=canonical_path,
                    source_url=f"https://foxzen.blogspot.com/{post_id}.html")
    post_dir = app_module.POSTS_DIR / post_id
    post_dir.mkdir(parents=True)
    (post_dir / "index.html").write_text(
        f'<html><body><div class="content">{title}的正文</div></body></html>',
        encoding="utf-8")
    if with_media:
        media_dir = post_dir / "media"
        media_dir.mkdir()
        (media_dir / "pic.png").write_bytes(b"\x89PNG\r\n fake image bytes")


def _seed_multi_year_fixture(db, app_module):
    """2025年8月/12月各1篇，2026年8月2篇+2026年7月1篇——覆盖"同月不同年"
    (2025-08 vs 2026-08)、"同年多月"、"每月数量不同"三种情况，不需要伪造
    真实文章之外再引入假的日期字段/假的数据库表。
    """
    _seed_post(db, app_module, "p-2025-08", "2025年8月的文章", "2025-08-15")
    _seed_post(db, app_module, "p-2025-12", "2025年12月的文章", "2025-12-01")
    _seed_post(db, app_module, "p-2026-08-a", "2026年8月文章A", "2026-08-01", with_media=True)
    _seed_post(db, app_module, "p-2026-08-b", "2026年8月文章B", "2026-08-20")
    _seed_post(db, app_module, "p-2026-07", "2026年7月的文章", "2026-07-10")


# ============================================================
# 一、/api/archive：年/月统计（db.get_archive_index()，已有代码补测试）
# ============================================================

def test_archive_endpoint_lists_years_and_months_from_published_field():
    def _run(tmp, db, app_module):
        _seed_multi_year_fixture(db, app_module)
        client = app_module.app.test_client()
        resp = client.get("/api/archive")
        check("GET /api/archive 返回200", resp.status_code == 200, resp.status_code)
        data = resp.get_json()
        years = {y["year"]: y for y in data["years"]}
        check("年份列表包含2026和2025", set(years) == {2026, 2025}, years.keys())
        check("2026年总数=3", years[2026]["count"] == 3, years[2026])
        check("2025年总数=2", years[2025]["count"] == 2, years[2025])
        check("年份倒序排列（2026在前）", data["years"][0]["year"] == 2026, data["years"])

        months_2026 = {m["month"]: m["count"] for m in years[2026]["months"]}
        check("2026年8月=2篇", months_2026.get(8) == 2, months_2026)
        check("2026年7月=1篇", months_2026.get(7) == 1, months_2026)
        months_2025 = {m["month"]: m["count"] for m in years[2025]["months"]}
        check("2025年8月=1篇，与2026年8月互不影响", months_2025.get(8) == 1, months_2025)
        check("2025年12月=1篇", months_2025.get(12) == 1, months_2025)

    with_temp_env(_run)


def test_archive_uses_published_not_mtime_or_post_id():
    """故意反向写入：post_id按字母序在前的文章，published日期却更晚，
    确认分组/排序依据的是published字段本身，不是post_id或写入顺序/文件mtime。
    """
    def _run(tmp, db, app_module):
        _seed_post(db, app_module, "aaa-post", "字母序靠前但published更晚", "2026-12-01")
        _seed_post(db, app_module, "zzz-post", "字母序靠后但published更早", "2026-01-01")
        idx = db.get_archive_index()
        months = {m["month"]: m["count"] for y in idx for m in y["months"] if y["year"] == 2026}
        check("按published分组：12月和1月各1篇，不受post_id字母序影响",
              months.get(12) == 1 and months.get(1) == 1, months)
    with_temp_env(_run)


# ============================================================
# 二、/api/search：year/month筛选（已有代码补测试）
# ============================================================

def test_search_filters_by_year_only():
    def _run(tmp, db, app_module):
        _seed_multi_year_fixture(db, app_module)
        client = app_module.app.test_client()
        resp = client.get("/api/search?year=2026&page_size=20")
        data = resp.get_json()
        check("year=2026只返回2026年的3篇", data["total"] == 3, data)
        ids = {r["post_id"] for r in data["results"]}
        check("不包含2025年的文章", "p-2025-08" not in ids and "p-2025-12" not in ids, ids)
    with_temp_env(_run)


def test_search_filters_by_year_and_month():
    def _run(tmp, db, app_module):
        _seed_multi_year_fixture(db, app_module)
        client = app_module.app.test_client()
        resp = client.get("/api/search?year=2026&month=8&page_size=20")
        data = resp.get_json()
        check("year=2026&month=8只返回2篇", data["total"] == 2, data)
        ids = {r["post_id"] for r in data["results"]}
        check("是2026年8月的两篇", ids == {"p-2026-08-a", "p-2026-08-b"}, ids)

        resp2 = client.get("/api/search?year=2025&month=8&page_size=20")
        data2 = resp2.get_json()
        check("year=2025&month=8（同月不同年）只返回2025年8月那1篇，不会跟2026-08混淆",
              data2["total"] == 1 and data2["results"][0]["post_id"] == "p-2025-08", data2)
    with_temp_env(_run)


def test_search_without_year_month_returns_all():
    def _run(tmp, db, app_module):
        _seed_multi_year_fixture(db, app_module)
        client = app_module.app.test_client()
        resp = client.get("/api/search?page_size=20")
        data = resp.get_json()
        check("不带year/month参数时返回全部5篇（全部文章恢复正确）",
              data["total"] == 5, data)
    with_temp_env(_run)


def test_search_year_month_zero_results():
    def _run(tmp, db, app_module):
        _seed_multi_year_fixture(db, app_module)
        client = app_module.app.test_client()
        resp = client.get("/api/search?year=2023&month=1")
        check("请求成功（不是错误响应）", resp.status_code == 200, resp.status_code)
        data = resp.get_json()
        check("0篇结果时total=0", data["total"] == 0, data)
        check("0篇结果时results为空列表", data["results"] == [], data["results"])
    with_temp_env(_run)


def test_search_existing_tag_and_query_filters_unaffected():
    """确认这次改动没有影响已有的按标签/按关键词搜索——不带year/month时
    应该跟改动之前完全一样。"""
    def _run(tmp, db, app_module):
        _seed_post(db, app_module, "p-tagged", "带标签的文章", "2026-05-01", tags=["随笔"])
        _seed_post(db, app_module, "p-untagged", "不带标签的文章", "2026-05-02")
        client = app_module.app.test_client()

        resp = client.get("/api/search?tag=随笔")
        data = resp.get_json()
        check("按标签筛选仍然只返回带该标签的文章",
              data["total"] == 1 and data["results"][0]["post_id"] == "p-tagged", data)

        resp2 = client.get("/api/search?q=不带标签")
        data2 = resp2.get_json()
        check("按关键词搜索仍然正常工作",
              any(r["post_id"] == "p-untagged" for r in data2["results"]), data2)
    with_temp_env(_run)


def test_search_pagination_unaffected():
    def _run(tmp, db, app_module):
        for i in range(3):
            _seed_post(db, app_module, f"p-page-{i}", f"分页测试{i}", f"2026-03-0{i + 1}")
        client = app_module.app.test_client()
        resp = client.get("/api/search?page=1&page_size=2")
        data = resp.get_json()
        check("分页：第1页2篇，total=3，total_pages=2",
              len(data["results"]) == 2 and data["total"] == 3 and data["total_pages"] == 2, data)
        resp2 = client.get("/api/search?page=2&page_size=2")
        data2 = resp2.get_json()
        check("分页：第2页剩下1篇", len(data2["results"]) == 1, data2)
    with_temp_env(_run)


def test_search_canonical_path_passthrough_unaffected():
    def _run(tmp, db, app_module):
        _seed_post(db, app_module, "p-canon", "带canonical的文章", "2026-02-01",
                   canonical_path="2026/02/p-canon")
        client = app_module.app.test_client()
        resp = client.get("/api/search?year=2026&month=2")
        data = resp.get_json()
        check("筛选结果里canonical_path原样透出，未被改写",
              data["results"][0]["canonical_path"] == "2026/02/p-canon", data["results"])
    with_temp_env(_run)


def test_year_month_range_rejects_malformed_input():
    """_year_month_range()是已有代码（未改动），这里锁定其现有行为：
    月份非法/年份非法时返回(None, None)，调用方据此当作"没有筛选"处理。
    真正防止用户带着非法URL参数误以为筛选生效的是前端的校验（见下面
    test_index_js_validates_year_month_before_using_url_params）。
    """
    def _run(tmp, db, app_module):
        check("month=13非法", app_module._year_month_range(2026, 13) == (None, None))
        check("month='abc'非法", app_module._year_month_range(2026, "abc") == (None, None))
        check("year='abc'非法", app_module._year_month_range("abc", 8) == (None, None))
        check("year=2026,month=None合法（整年范围）",
              app_module._year_month_range(2026, None) == ("2026-01-01", "2026-12-31"))
        check("year=2026,month=8合法", app_module._year_month_range(2026, 8) == ("2026-08-01", "2026-08-31"))
    with_temp_env(_run)


# ============================================================
# 三、POST /api/download/selected：按年/月筛选下载（新增_selected_zip_filename()）
# ============================================================

def test_download_selected_by_year_month_scopes_content_and_names_zip():
    def _run(tmp, db, app_module):
        _seed_multi_year_fixture(db, app_module)
        client = app_module.app.test_client()
        resp = client.post("/api/download/selected", json={"year": 2026, "month": 8})
        check("请求成功", resp.status_code == 200, resp.status_code)

        disposition = resp.headers.get("Content-Disposition", "")
        check("文件名是2026-08.zip（跟整站/勾选下载区分开）",
              'filename="2026-08.zip"' in disposition or "filename=2026-08.zip" in disposition,
              disposition)

        buf_path = tmp / "downloaded.zip"
        buf_path.write_bytes(resp.data)
        with zipfile.ZipFile(buf_path) as zf:
            names = zf.namelist()
            check("包含2026年8月文章A", any("p-2026-08-a" in n for n in names), names)
            check("包含2026年8月文章B", any("p-2026-08-b" in n for n in names), names)
            check("不包含2026年7月的文章", not any("p-2026-07" in n for n in names), names)
            check("不包含2025年的文章", not any("p-2025" in n for n in names), names)
            check("图片随文章一起进入ZIP", any(n.endswith("media/pic.png") for n in names), names)
            check("ZIP里没有.db文件", not any(n.endswith(".db") for n in names), names)
            check("ZIP里没有.py源码文件", not any(n.endswith(".py") for n in names), names)
    with_temp_env(_run)


def test_download_selected_by_year_only_uses_year_filename():
    def _run(tmp, db, app_module):
        _seed_multi_year_fixture(db, app_module)
        client = app_module.app.test_client()
        resp = client.post("/api/download/selected", json={"year": 2026})
        disposition = resp.headers.get("Content-Disposition", "")
        check('只筛年份时文件名是2026.zip', "2026.zip" in disposition, disposition)
        check('不是"2026-.zip"或其它畸形名字', "2026-.zip" not in disposition, disposition)

        buf_path = tmp / "downloaded_year.zip"
        buf_path.write_bytes(resp.data)
        with zipfile.ZipFile(buf_path) as zf:
            names = zf.namelist()
            check("整年下载包含7月和8月的文章",
                  any("p-2026-07" in n for n in names) and any("p-2026-08-a" in n for n in names), names)
    with_temp_env(_run)


def test_download_selected_manual_post_ids_filename_unchanged():
    """回归：手动勾选下载（已有功能）文件名必须还是blog-mirror-selected.zip，
    不能被这次新增的年/月命名逻辑影响到。"""
    def _run(tmp, db, app_module):
        _seed_post(db, app_module, "p-manual", "手动勾选的文章", "2026-06-01")
        client = app_module.app.test_client()
        resp = client.post("/api/download/selected", json={"post_ids": ["p-manual"]})
        disposition = resp.headers.get("Content-Disposition", "")
        check("手动post_ids下载文件名仍是blog-mirror-selected.zip",
              "blog-mirror-selected.zip" in disposition, disposition)
    with_temp_env(_run)


def test_download_selected_tag_scope_filename_unchanged():
    def _run(tmp, db, app_module):
        _seed_post(db, app_module, "p-tag-dl", "按标签下载", "2026-06-01", tags=["测试标签"])
        client = app_module.app.test_client()
        resp = client.post("/api/download/selected", json={"tag": "测试标签"})
        disposition = resp.headers.get("Content-Disposition", "")
        check("按标签下载文件名仍是blog-mirror-selected.zip（不是年/月命名）",
              "blog-mirror-selected.zip" in disposition, disposition)
    with_temp_env(_run)


def test_download_selected_empty_year_month_scope_returns_400():
    def _run(tmp, db, app_module):
        _seed_multi_year_fixture(db, app_module)
        client = app_module.app.test_client()
        resp = client.post("/api/download/selected", json={"year": 2023, "month": 1})
        check("0篇结果的年/月不生成空zip，返回400", resp.status_code == 400, resp.status_code)
        data = resp.get_json()
        check("错误信息提示筛选条件未匹配到文章", "筛选条件" in data.get("error", ""), data)
    with_temp_env(_run)


def test_download_all_whole_blog_unaffected():
    """整站下载(/api/download/all + zip_cache.py)这次任务完全没有改动，
    这里只做轻量冒烟测试，确认没有被意外波及。"""
    def _run(tmp, db, app_module):
        _seed_multi_year_fixture(db, app_module)
        client = app_module.app.test_client()
        resp = client.get("/api/download/all")
        check("整站下载仍然200", resp.status_code == 200, resp.status_code)
        check("整站下载Content-Type仍是zip", resp.mimetype == "application/zip", resp.mimetype)
        disposition = resp.headers.get("Content-Disposition", "")
        check("单卷时文件名仍是blog-mirror-full.zip（未被年/月命名逻辑影响）",
              "blog-mirror-full.zip" in disposition, disposition)
    with_temp_env(_run)


# ============================================================
# 四、static/index.js：前端筛选UI（纯源码结构校验，项目里没有浏览器自动化工具，
#    跟test_reading_experience.py对I18N_BLOCK的校验方式保持同一个约定）
# ============================================================

def test_index_js_reads_year_month_from_url_on_load():
    check("定义了URL参数解析函数", "function parseArchiveParamsFromLocation" in INDEX_JS)
    check("页面加载时立即调用它填充state（刷新页面筛选条件仍然存在）",
          "Object.assign(state, parseArchiveParamsFromLocation())" in INDEX_JS)
    check("从location.search读取，不是从别的地方拼URL",
          "new URLSearchParams(location.search)" in INDEX_JS)


def test_index_js_validates_year_month_before_using_url_params():
    check("年份要求4位数字格式校验", r"/^\d{4}$/" in INDEX_JS)
    check("月份要求1-12范围校验", "monthNum >= 1 && monthNum <= 12" in INDEX_JS)


def test_index_js_updates_url_and_supports_back_forward():
    check("筛选变化时用pushState写入新URL（支持前进/后退）",
          "history.pushState" in INDEX_JS)
    check("监听popstate处理浏览器前进/后退", 'addEventListener("popstate"' in INDEX_JS)
    check("popstate处理函数里重新解析URL并重新查询",
          "parseArchiveParamsFromLocation()" in INDEX_JS.split('addEventListener("popstate"')[1][:400])


def test_index_js_year_month_included_in_search_request():
    check("搜索参数构造函数把state.year传给/api/search",
          'params.set("year", state.year)' in INDEX_JS)
    check("搜索参数构造函数把state.month传给/api/search",
          'params.set("month", state.month)' in INDEX_JS)


def test_index_js_bilingual_strings_symmetric():
    check("包含中文筛选文案", '"按年份筛选"' in INDEX_JS and '"按月份筛选"' in INDEX_JS)
    check("包含英文筛选文案", '"Filter by year"' in INDEX_JS and '"Filter by month"' in INDEX_JS)
    check("包含中文空结果提示", "这个月份没有文章" in INDEX_JS)
    check("包含英文空结果提示", "No posts in this month" in INDEX_JS)

    import re
    zh_block = re.search(r"zh:\s*\{(.*?)\},\s*en:", INDEX_JS, re.DOTALL)
    en_block = re.search(r"en:\s*\{(.*?)\},\s*\};", INDEX_JS, re.DOTALL)
    check("能定位到ARCHIVE_STRINGS.zh/.en两个块", bool(zh_block and en_block))
    if zh_block and en_block:
        zh_keys = set(re.findall(r"(\w+):", zh_block.group(1)))
        en_keys = set(re.findall(r"(\w+):", en_block.group(1)))
        check("中英文翻译键集合完全一致（不会一边少了某个key）",
              zh_keys == en_keys, (zh_keys, en_keys))


def test_index_js_localstorage_key_matches_article_pages():
    check("使用跟fetch_blog.py::I18N_BLOCK同一个localStorage key(foxzen_lang)，"
          "文章页选过的语言回到首页筛选器文案也一致",
          'FOXZEN_LANG_KEY = "foxzen_lang"' in INDEX_JS)
    check("语言检测优先读localStorage，其次才是浏览器语言（手动选择优先）",
          INDEX_JS.index("localStorage.getItem(FOXZEN_LANG_KEY)") <
          INDEX_JS.index("return detectDefaultFoxzenLang();"))


def test_index_js_never_touches_article_content():
    """index.js只渲染文章列表(标题/日期/统计数字)，从不获取/渲染content_html，
    结构上就不可能因为语言切换而改变文章正文——这里断言它压根没有触碰过
    .content这个只在文章正文里出现的选择器/class。"""
    check("index.js全文不包含.content（不处理文章正文，只处理列表）",
          ".content" not in INDEX_JS)


def test_index_js_no_unexpected_write_endpoints():
    """新增代码只应该调用GET /api/archive、GET /api/search（都是已有的只读接口）
    和POST /api/download/selected（已有的下载接口，这次只加了文件名区分）——
    不能因为这个筛选功能顺带引入任何refresh/git push/部署相关的新请求。
    """
    import re
    new_block_start = INDEX_JS.index("// ===== 年份/月份筛选 =====")
    new_block_end = INDEX_JS.index("function buildToolbar()")
    new_block = INDEX_JS[new_block_start:new_block_end]
    fetch_calls = re.findall(r'fetch\(\s*["\']([^"\']+)["\']', new_block)
    check("筛选相关新代码块只请求了预期的只读/既有接口",
          set(fetch_calls) <= {"/api/archive", "/api/download/selected"}, fetch_calls)
    # doDownloadFiltered()里的/api/download/selected用的是变量拼接过的写法，
    # 单独确认一次，同时确认没有出现refresh/publish/deploy字样。
    check("下载筛选结果调用的是已有的/api/download/selected",
          '"/api/download/selected"' in new_block)
    for bad in ("/api/refresh", "git push", "github_actions", "/api/deploy"):
        check(f"筛选功能代码块不包含'{bad}'", bad not in new_block)


def test_index_js_no_db_write_or_account_concept():
    import re
    new_block_start = INDEX_JS.index("// ===== 年份/月份筛选 =====")
    new_block_end = INDEX_JS.index("function buildToolbar()")
    new_block = INDEX_JS[new_block_start:new_block_end]
    check("筛选功能代码块不包含login/account/password等账号概念",
          not re.search(r"login|password|account", new_block, re.IGNORECASE))


def test_index_js_download_button_hidden_on_zero_results():
    check("0篇结果时提前return，不渲染下载按钮",
          "if (total === 0) {\n      summary.appendChild(el(\"span\", { text: ARCHIVE_T.noPostsInMonth }));\n      return;"
          in INDEX_JS)
    check("下载按钮文案包含数量（下载这N篇文章/Download these N posts）",
          "downloadFiltered: (count)" in INDEX_JS)


def test_index_js_filtered_download_distinct_from_whole_blog_and_selected():
    check("整站下载按钮仍然打/api/download/all（未改动）",
          '"/api/download/all"' in INDEX_JS)
    check("已有的勾选下载仍然只发post_ids，不受筛选下载影响",
          "body: JSON.stringify({ post_ids: Array.from(state.selected) })" in INDEX_JS)
    check("新的筛选下载发送的是year/month，不是post_ids",
          "const body = { year: state.year };" in INDEX_JS)


def main():
    tests = [
        test_archive_endpoint_lists_years_and_months_from_published_field,
        test_archive_uses_published_not_mtime_or_post_id,
        test_search_filters_by_year_only,
        test_search_filters_by_year_and_month,
        test_search_without_year_month_returns_all,
        test_search_year_month_zero_results,
        test_search_existing_tag_and_query_filters_unaffected,
        test_search_pagination_unaffected,
        test_search_canonical_path_passthrough_unaffected,
        test_year_month_range_rejects_malformed_input,
        test_download_selected_by_year_month_scopes_content_and_names_zip,
        test_download_selected_by_year_only_uses_year_filename,
        test_download_selected_manual_post_ids_filename_unchanged,
        test_download_selected_tag_scope_filename_unchanged,
        test_download_selected_empty_year_month_scope_returns_400,
        test_download_all_whole_blog_unaffected,
        test_index_js_reads_year_month_from_url_on_load,
        test_index_js_validates_year_month_before_using_url_params,
        test_index_js_updates_url_and_supports_back_forward,
        test_index_js_year_month_included_in_search_request,
        test_index_js_bilingual_strings_symmetric,
        test_index_js_localstorage_key_matches_article_pages,
        test_index_js_never_touches_article_content,
        test_index_js_no_unexpected_write_endpoints,
        test_index_js_no_db_write_or_account_concept,
        test_index_js_download_button_hidden_on_zero_results,
        test_index_js_filtered_download_distinct_from_whole_blog_and_selected,
    ]
    for t in tests:
        print(f"--- {t.__name__} ---")
        try:
            t()
        except Exception as e:
            print(f"  [FAIL] {t.__name__} 抛出异常: {e!r}")
            failures.append(t.__name__)

    print()
    if failures:
        print(f"共{len(failures)}项失败: {failures}")
        sys.exit(1)
    print("全部通过")


if __name__ == "__main__":
    main()
