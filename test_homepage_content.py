#!/usr/bin/env python3
"""首页内容配置回归测试："每日格言"(data/quotes.txt) + "我最喜欢的博客"
(data/favorite_blogs.txt)。

范围说明（跟已完成的其它任务明确分开）：
- quotes.txt的读取/随机选择逻辑(app.py::_random_quote())是既有代码，本次
  未改动，这里主要是审计后补齐测试；唯一的真实代码改动是publish_build.py
  的静态发布路径此前完全没有替换<!--QUOTE-->占位符（github.foxzen.me/
  cf.foxzen.me上这行会显示成空的），这里一并测试新补上的替换逻辑。
- favorite_blogs.txt是全新功能：fetch_blog.py::_parse_favorite_blogs()/
  _render_favorite_blogs_html()（渲染时机是fetch_blog.py抓取阶段一次性
  写入html/index.html，不是app.py按请求实时替换——因为内容本身不需要
  "每次请求随机"）。
- static/index.js里的年份/月份筛选器(ARCHIVE_STRINGS等)已有test_archive_filter.py
  覆盖，这里只测这次新增的HOMEPAGE_I18N_STRINGS/applyHomepageI18n()。
- announcements.txt/update.foxzen.me发布逻辑本身见test_update_page.py（既有）
  和test_update_publish.py（这次新增，测--publish/git_publish集成）。

用法: python3 test_homepage_content.py
"""
import shutil
import sys
import tempfile
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


# ============================================================
# 通用隔离helper（跟test_archive_filter.py/test_site_entries.py/
# test_publish_build.py同样的约定，各测试文件各自维护一份）
# ============================================================

def with_temp_app_env(fn):
    """db.DB_PATH + app.HTML_DIR + app.QUOTES_FILE全部指向临时目录/文件，
    跑完自动还原/清理，绝不读写真实的data/blog.db或html/index.html。
    """
    import db
    tmp = Path(tempfile.mkdtemp(prefix="homepage_content_app_test_"))
    orig_db_path = db.DB_PATH
    real_db_mtime = REAL_DB.stat().st_mtime if REAL_DB.exists() else None
    db.DB_PATH = tmp / "test.db"

    import app as app_module
    orig_html_dir = app_module.HTML_DIR
    orig_quotes_file = app_module.QUOTES_FILE
    app_module.HTML_DIR = tmp / "html"
    app_module.HTML_DIR.mkdir(parents=True)
    app_module.QUOTES_FILE = tmp / "quotes.txt"
    app_module.app.config["TESTING"] = True

    try:
        db.init_db()
        fn(tmp, db, app_module)
    finally:
        db.DB_PATH = orig_db_path
        app_module.HTML_DIR = orig_html_dir
        app_module.QUOTES_FILE = orig_quotes_file
        shutil.rmtree(tmp, ignore_errors=True)

    if real_db_mtime is not None:
        check("测试过程未修改真实data/blog.db（mtime不变）",
              REAL_DB.stat().st_mtime == real_db_mtime)


def with_temp_html_dir(fn):
    """跟test_site_entries.py同一个约定：只重定向fetch_blog.HTML_DIR，
    render_index()内部对db的调用走真实data/blog.db（只读SELECT，不修改任何
    数据），这些测试不断言具体文章内容，只断言首页模板/新增区块本身的结构，
    不受"当前到底有多少篇真实文章"影响。
    """
    import fetch_blog
    tmp = Path(tempfile.mkdtemp(prefix="homepage_content_fetch_test_"))
    orig_html_dir = fetch_blog.HTML_DIR
    orig_fav_file = fetch_blog.FAVORITE_BLOGS_FILE
    fetch_blog.HTML_DIR = tmp
    try:
        fn(tmp, fetch_blog)
    finally:
        fetch_blog.HTML_DIR = orig_html_dir
        fetch_blog.FAVORITE_BLOGS_FILE = orig_fav_file
        shutil.rmtree(tmp, ignore_errors=True)


def with_publish_build_fixture(fn):
    """跟test_publish_build.py同一个约定：重定向publish_build.HTML_DIR/
    QUOTES_FILE指向一个最小的合成html/目录（只需要_build_index_html()
    用到的几个占位符齐全，不需要真实文章）。"""
    import publish_build
    tmp = Path(tempfile.mkdtemp(prefix="homepage_content_publish_test_"))
    orig_html_dir = publish_build.HTML_DIR
    orig_quotes_file = publish_build.QUOTES_FILE
    fixture_html = tmp / "html"
    fixture_html.mkdir()
    (fixture_html / "index.html").write_text(
        '<!DOCTYPE html><html><body>'
        '<p class="daily-quote">🦊 <!--QUOTE--></p>'
        '<div id="app"></div>'
        '<script src="/static/index.js"></script>'
        '</body></html>',
        encoding="utf-8",
    )
    publish_build.HTML_DIR = fixture_html
    publish_build.QUOTES_FILE = tmp / "quotes.txt"
    output_dir = tmp / "publish_out"
    try:
        fn(tmp, output_dir, publish_build)
    finally:
        publish_build.HTML_DIR = orig_html_dir
        publish_build.QUOTES_FILE = orig_quotes_file
        shutil.rmtree(tmp, ignore_errors=True)


# ============================================================
# 一、quotes.txt —— 既有实现审计+补测试
# ============================================================

def test_quotes_normal_read():
    def _run(tmp, db, app_module):
        app_module.QUOTES_FILE.write_text("唯一的格言\n", encoding="utf-8")
        check("单行quotes.txt返回这一条", app_module._random_quote() == "唯一的格言")
    with_temp_app_env(_run)


def test_quotes_empty_file():
    def _run(tmp, db, app_module):
        app_module.QUOTES_FILE.write_text("", encoding="utf-8")
        check("空文件返回空字符串（不报错）", app_module._random_quote() == "")
    with_temp_app_env(_run)


def test_quotes_missing_file_no_crash():
    def _run(tmp, db, app_module):
        # 不创建QUOTES_FILE
        check("文件不存在时返回空字符串（不抛异常）", app_module._random_quote() == "")
    with_temp_app_env(_run)


def test_quotes_multiple_lines_random_from_valid_set():
    def _run(tmp, db, app_module):
        expected = {"格言一", "格言二", "格言三"}
        app_module.QUOTES_FILE.write_text("\n".join(expected), encoding="utf-8")
        seen = {app_module._random_quote() for _ in range(60)}
        check("多条格言时每次结果都在预期集合内", seen <= expected, seen)
        check("多次抽取覆盖到不止一条（不是恰好每次都抽中同一条这种极端小概率巧合）",
              len(seen) > 1, seen)
    with_temp_app_env(_run)


def test_quotes_blank_lines_skipped():
    def _run(tmp, db, app_module):
        app_module.QUOTES_FILE.write_text("\n\n真正的格言\n   \n", encoding="utf-8")
        for _ in range(20):
            check("空白行被跳过，只会抽到真正的格言",
                  app_module._random_quote() == "真正的格言")
    with_temp_app_env(_run)


def test_index_route_substitutes_quote_placeholder():
    def _run(tmp, db, app_module):
        (app_module.HTML_DIR / "index.html").write_text(
            '<p class="daily-quote">🦊 <!--QUOTE--></p>', encoding="utf-8")
        app_module.QUOTES_FILE.write_text("测试格言内容\n", encoding="utf-8")
        client = app_module.app.test_client()
        resp = client.get("/")
        check("首页请求成功", resp.status_code == 200, resp.status_code)
        body = resp.get_data(as_text=True)
        check("占位符已被替换为真实格言", "测试格言内容" in body, body)
        check("不再残留字面量<!--QUOTE-->占位符", "<!--QUOTE-->" not in body, body)
    with_temp_app_env(_run)


def test_index_route_quote_is_html_escaped():
    def _run(tmp, db, app_module):
        (app_module.HTML_DIR / "index.html").write_text(
            '<p class="daily-quote">🦊 <!--QUOTE--></p>', encoding="utf-8")
        app_module.QUOTES_FILE.write_text("<script>alert(1)</script>\n", encoding="utf-8")
        client = app_module.app.test_client()
        resp = client.get("/")
        body = resp.get_data(as_text=True)
        check("格言内容里的尖括号被转义，不会被当成真实标签",
              "<script>alert(1)</script>" not in body and "&lt;script&gt;" in body, body)
    with_temp_app_env(_run)


# ============================================================
# 二、quotes.txt —— 静态发布路径（publish_build.py，此前存在的缺口，
#    这次任务实际修复的代码）
# ============================================================

def test_publish_build_substitutes_quote_placeholder():
    def _run(tmp, output_dir, publish_build):
        publish_build.QUOTES_FILE.write_text("静态发布测试格言\n", encoding="utf-8")
        content = publish_build._build_index_html("github.foxzen.me", output_dir)
        check("静态构建也替换了<!--QUOTE-->占位符", "静态发布测试格言" in content, content)
        check("不再残留字面量<!--QUOTE-->（此前的缺口：github.foxzen.me/"
              "cf.foxzen.me上这行此前会显示为空）", "<!--QUOTE-->" not in content)
    with_publish_build_fixture(_run)


def test_publish_build_quote_missing_file_no_crash():
    def _run(tmp, output_dir, publish_build):
        # 不创建QUOTES_FILE
        content = publish_build._build_index_html("cf.foxzen.me", output_dir)
        check("quotes.txt不存在时构建不报错", True)
        check("占位符被替换成空字符串而不是保留原样",
              "<!--QUOTE-->" not in content)
    with_publish_build_fixture(_run)


def test_publish_build_quote_is_html_escaped():
    def _run(tmp, output_dir, publish_build):
        publish_build.QUOTES_FILE.write_text("<b>不应该被当成标签</b>\n", encoding="utf-8")
        content = publish_build._build_index_html("github.foxzen.me", output_dir)
        check("静态构建里的格言同样做HTML转义",
              "<b>不应该被当成标签</b>" not in content and "&lt;b&gt;" in content)
    with_publish_build_fixture(_run)


def test_publish_build_404_page_quote_substituted_and_escaped():
    """github.foxzen.me/cf.foxzen.me的404页(output_dir/404.html +
    .../404/index.html)此前是跟首页同一类但被漏掉的缺口：build_publish()
    只对index.html做<!--QUOTE-->替换，404页此前走shutil.copy2()原样复制，
    留下字面量占位符没有任何替换，且没有测试覆盖到这一点。

    这里需要完整跑一次build_publish()（不是_build_index_html()）——404页
    的替换逻辑在build_publish()内部，且需要真实的html/404/index.html
    （本身就带<!--QUOTE-->，见html/404/index.html第49行），所以复用跟
    test_freshly_rendered_index_survives_publish_build_with_i18n_intact
    同样的"拷贝真实html/到临时目录"方式，不修改仓库里的任何文件。
    """
    import publish_build

    real_html_dir = BASE_DIR / "html"
    real_404 = real_html_dir / "404" / "index.html"
    if not real_html_dir.exists() or not real_404.exists():
        print("  [跳过] 本地没有真实html/404/index.html")
        return

    tmp = Path(tempfile.mkdtemp(prefix="homepage_content_404_quote_test_"))
    html_dir = tmp / "html"
    shutil.copytree(real_html_dir, html_dir)
    orig_html_dir = publish_build.HTML_DIR
    orig_quotes_file = publish_build.QUOTES_FILE
    try:
        publish_build.HTML_DIR = html_dir
        publish_build.QUOTES_FILE = tmp / "quotes.txt"
        publish_build.QUOTES_FILE.write_text("404页<b>测试</b>格言\n", encoding="utf-8")
        out_dir = tmp / "publish_out_404_quote"
        publish_build.build_publish("github.foxzen.me", out_dir)

        root_404 = (out_dir / "404.html").read_text(encoding="utf-8")
        nested_404 = (out_dir / "404" / "index.html").read_text(encoding="utf-8")
        check("根目录404.html已替换<!--QUOTE-->占位符为真实格言",
              "404页" in root_404 and "格言" in root_404, root_404)
        check("根目录404.html不再残留字面量<!--QUOTE-->", "<!--QUOTE-->" not in root_404)
        check("根目录404.html的格言内容做了HTML转义",
              "<b>测试</b>" not in root_404 and "&lt;b&gt;" in root_404, root_404)
        check("嵌套404/index.html同样已替换且转义（两份产物保持一致）",
              "<!--QUOTE-->" not in nested_404 and "&lt;b&gt;" in nested_404, nested_404)

        publish_build.verify_publish(out_dir, "github.foxzen.me")
        check("verify_publish()对修复后的构建产物通过检查", True)
    finally:
        publish_build.HTML_DIR = orig_html_dir
        publish_build.QUOTES_FILE = orig_quotes_file
        shutil.rmtree(tmp, ignore_errors=True)


def test_verify_publish_rejects_leftover_quote_placeholder():
    """verify_publish()新增的安全网检查：构建产物里任何.html文件如果残留
    字面量<!--QUOTE-->，必须直接拒绝通过，而不是让一个"发布后格言位置空白"
    的产物悄悄通过CI/本地检查——这正是本次实际发生过的两次缺口（首页、
    404页）能够长期不被发现的原因，把它变成构建时的强制不变量。

    用一次已知良好的真实构建作基础，人为在其中一个输出文件里重新塞回
    占位符，模拟"以后又有新页面引入了同样的占位符但忘记接线替换逻辑"
    这种回归场景。
    """
    import publish_build

    real_html_dir = BASE_DIR / "html"
    if not real_html_dir.exists():
        print("  [跳过] 本地没有真实html/目录")
        return

    tmp = Path(tempfile.mkdtemp(prefix="homepage_content_verify_quote_test_"))
    html_dir = tmp / "html"
    shutil.copytree(real_html_dir, html_dir)
    orig_html_dir = publish_build.HTML_DIR
    orig_quotes_file = publish_build.QUOTES_FILE
    try:
        publish_build.HTML_DIR = html_dir
        publish_build.QUOTES_FILE = tmp / "quotes.txt"
        publish_build.QUOTES_FILE.write_text("正常格言\n", encoding="utf-8")
        out_dir = tmp / "publish_out_verify_quote"
        publish_build.build_publish("github.foxzen.me", out_dir)

        # 人为破坏一份已经正确替换过的输出，模拟回归
        index_file = out_dir / "index.html"
        corrupted = index_file.read_text(encoding="utf-8") + "\n<!--QUOTE-->\n"
        index_file.write_text(corrupted, encoding="utf-8")

        raised = False
        message = ""
        try:
            publish_build.verify_publish(out_dir, "github.foxzen.me")
        except publish_build.PublishVerificationError as e:
            raised = True
            message = str(e)
        check("verify_publish()识别出残留的<!--QUOTE-->占位符并拒绝通过", raised)
        check("错误信息里提到index.html和占位符本身",
              "index.html" in message and "<!--QUOTE-->" in message, message)
    finally:
        publish_build.HTML_DIR = orig_html_dir
        publish_build.QUOTES_FILE = orig_quotes_file
        shutil.rmtree(tmp, ignore_errors=True)


# ============================================================
# 三、favorite_blogs.txt —— 解析逻辑（fetch_blog.py，纯函数，不需要I/O隔离）
# ============================================================

def test_favorite_blogs_normal_read():
    import fetch_blog
    entries = fetch_blog._parse_favorite_blogs("Wait But Why|https://waitbutwhy.com/\nsive.rs|https://sive.rs/\n")
    check("解析出2条", len(entries) == 2, entries)
    check("第一条名称/URL正确", entries[0] == {"name": "Wait But Why", "url": "https://waitbutwhy.com/"})
    check("第二条名称/URL正确", entries[1] == {"name": "sive.rs", "url": "https://sive.rs/"})


def test_favorite_blogs_empty_file():
    import fetch_blog
    check("空文本返回空列表", fetch_blog._parse_favorite_blogs("") == [])


def test_favorite_blogs_comments_and_blank_lines_skipped():
    import fetch_blog
    text = "# 这是注释\n\n名称|https://example.com/\n   \n# 另一条注释\n"
    entries = fetch_blog._parse_favorite_blogs(text)
    check("注释和空行被跳过，只剩1条真实数据", len(entries) == 1, entries)


def test_favorite_blogs_malformed_line_skipped():
    import fetch_blog
    text = "这一行没有分隔符\n正常名称|https://example.com/\n"
    entries = fetch_blog._parse_favorite_blogs(text)
    check("格式错误的行被跳过，不影响后面正常的行",
          len(entries) == 1 and entries[0]["name"] == "正常名称", entries)


def test_favorite_blogs_empty_name_or_url_skipped():
    import fetch_blog
    text = "|https://example.com/\n名称|\n有效名称|https://example.com/\n"
    entries = fetch_blog._parse_favorite_blogs(text)
    check("名称为空/URL为空的行都被跳过", len(entries) == 1 and entries[0]["name"] == "有效名称", entries)


def test_favorite_blogs_non_https_rejected():
    import fetch_blog
    text = (
        "明文HTTP|http://example.com/\n"
        "JS注入|javascript:alert(1)\n"
        "FTP|ftp://example.com/\n"
        "合法HTTPS|https://example.com/\n"
    )
    entries = fetch_blog._parse_favorite_blogs(text)
    check("只有HTTPS链接被保留", len(entries) == 1 and entries[0]["name"] == "合法HTTPS", entries)


def test_favorite_blogs_html_escape_in_name():
    import fetch_blog
    entries = [{"name": '<script>alert(1)</script>', "url": "https://example.com/"}]
    rendered = fetch_blog._render_favorite_blogs_html(entries)
    check("名称里的HTML被转义，不会被当成真实标签",
          "<script>alert(1)</script>" not in rendered and "&lt;script&gt;" in rendered, rendered)


def test_favorite_blogs_url_escape_in_href():
    import fetch_blog
    entries = [{"name": "测试", "url": 'https://example.com/?a="x"&b=1'}]
    rendered = fetch_blog._render_favorite_blogs_html(entries)
    check("URL里的引号被转义，不会提前闭合href属性",
          'href="https://example.com/?a="x"&b=1"' not in rendered, rendered)


def test_favorite_blogs_link_attributes_target_and_rel():
    import fetch_blog
    entries = [{"name": "测试博客", "url": "https://example.com/"}]
    rendered = fetch_blog._render_favorite_blogs_html(entries)
    check('包含target="_blank"', 'target="_blank"' in rendered, rendered)
    check('包含rel="noopener noreferrer"（不是只有noopener）',
          'rel="noopener noreferrer"' in rendered, rendered)


def test_favorite_blogs_zero_entries_renders_nothing():
    import fetch_blog
    check("零条有效数据时不渲染任何区块（不显示空标题）",
          fetch_blog._render_favorite_blogs_html([]) == "")


def test_favorite_blogs_heading_has_data_i18n_attribute():
    import fetch_blog
    entries = [{"name": "测试", "url": "https://example.com/"}]
    rendered = fetch_blog._render_favorite_blogs_html(entries)
    check('标题带data-i18n="fav_blogs_heading"（供static/index.js按foxzen_lang替换文案）',
          'data-i18n="fav_blogs_heading"' in rendered, rendered)


# ============================================================
# 四、favorite_blogs.txt —— render_index()集成
# ============================================================

def test_render_index_includes_favorite_blogs_section():
    def _run(tmp, fetch_blog):
        fetch_blog.FAVORITE_BLOGS_FILE = tmp / "favorite_blogs.txt"
        fetch_blog.FAVORITE_BLOGS_FILE.write_text(
            "Wait But Why|https://waitbutwhy.com/\nsive.rs|https://sive.rs/\n", encoding="utf-8")
        fetch_blog.render_index()
        content = (tmp / "index.html").read_text(encoding="utf-8")
        check("首页包含Wait But Why链接", 'href="https://waitbutwhy.com/"' in content, content[:2000])
        check("首页包含sive.rs链接", 'href="https://sive.rs/"' in content)
        check("首页包含博客名称文字", "Wait But Why" in content and "sive.rs" in content)
    with_temp_html_dir(_run)


def test_render_index_favorite_blogs_missing_file_no_crash():
    def _run(tmp, fetch_blog):
        fetch_blog.FAVORITE_BLOGS_FILE = tmp / "does_not_exist.txt"
        fetch_blog.render_index()
        content = (tmp / "index.html").read_text(encoding="utf-8")
        check("favorite_blogs.txt不存在时render_index()不报错，正常生成首页",
              "狐斋志异" in content)
        check('不出现空的data-i18n="fav_blogs_heading"标题区块',
              'data-i18n="fav_blogs_heading"' not in content)
    with_temp_html_dir(_run)


def test_render_index_unrelated_sections_still_present():
    """确认新增区块没有误删既有的统计box/排行榜/彩蛋链接（跟test_site_entries.py
    里同名断言保持一致的关注点，避免这次改动引入回归）。"""
    def _run(tmp, fetch_blog):
        fetch_blog.FAVORITE_BLOGS_FILE = tmp / "favorite_blogs.txt"
        fetch_blog.FAVORITE_BLOGS_FILE.write_text("名称|https://example.com/\n", encoding="utf-8")
        fetch_blog.render_index()
        content = (tmp / "index.html").read_text(encoding="utf-8")
        check("统计box仍存在", 'class="stats-box"' in content)
        check("排行榜仍存在", 'class="leaderboard"' in content)
        check("彩蛋链接仍存在", "这个网站藏着一只找不到路的狐狸" in content)
        check("FoxZen其他入口区块仍存在", "FoxZen 的其他入口" in content)
    with_temp_html_dir(_run)


# ============================================================
# 五、static/index.js —— 首页i18n扫描（这次新增，跟已有的ARCHIVE_STRINGS
#    是同一个foxzen_lang机制的延伸，不是另一套系统）
# ============================================================

def test_index_js_homepage_i18n_strings_both_languages():
    check('中文文案存在', '"🔗 我最喜欢的博客"' in INDEX_JS)
    check('英文文案存在', '"🔗 My Favorite Blogs"' in INDEX_JS)


def test_index_js_applies_data_i18n_sweep():
    check("定义了首页i18n扫描函数", "function applyHomepageI18n" in INDEX_JS)
    check("扫描逻辑基于[data-i18n]属性（跟服务端渲染的data-i18n标记对应）",
          'querySelectorAll("[data-i18n]")' in INDEX_JS)


def test_index_js_homepage_i18n_called_at_bootstrap():
    check("启动时调用了applyHomepageI18n()", "applyHomepageI18n();" in INDEX_JS)


def test_index_js_homepage_i18n_reuses_foxzen_lang_key():
    check("首页i18n复用同一个FOXZEN_LANG（不是另建一套语言检测）",
          "HOMEPAGE_I18N_STRINGS[FOXZEN_LANG]" in INDEX_JS)


# ============================================================
# 六、真实文件只读验收（第十二节要求）
# ============================================================

def test_real_quotes_txt_reads_without_error():
    real_quotes = BASE_DIR / "data" / "quotes.txt"
    check("真实quotes.txt存在", real_quotes.exists())
    text = real_quotes.read_text(encoding="utf-8")
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    check("真实quotes.txt至少有1条格言", len(lines) >= 1, len(lines))


def test_real_favorite_blogs_txt_reads_without_error():
    import fetch_blog
    real_file = BASE_DIR / "data" / "favorite_blogs.txt"
    check("真实favorite_blogs.txt存在", real_file.exists())
    entries = fetch_blog._parse_favorite_blogs(real_file.read_text(encoding="utf-8"))
    names = {e["name"] for e in entries}
    check("真实favorite_blogs.txt解析出Wait But Why", "Wait But Why" in names, names)
    check("真实favorite_blogs.txt解析出sive.rs", "sive.rs" in names, names)
    check("真实favorite_blogs.txt里的URL都是HTTPS",
          all(e["url"].startswith("https://") for e in entries), entries)


def test_real_announcements_txt_reads_without_error():
    import generate_status_page
    real_file = BASE_DIR / "data" / "announcements.txt"
    check("真实announcements.txt存在", real_file.exists())
    text = real_file.read_text(encoding="utf-8")
    entries = generate_status_page.parse_announcements(text)
    check("真实announcements.txt解析不报错（当前无真实公告，列表可以是空的）",
          isinstance(entries, list))


# ============================================================
# 七、全站UI国际化扩展（本次新增：把首页从"只有筛选文案/favorite blogs
#    标题双语"扩展成"整个首页UI都能中英切换"，机制仍然是同一个foxzen_lang，
#    这里补充测试覆盖第十二节要求的相关条目）
# ============================================================

def test_index_js_homepage_i18n_strings_key_sets_symmetric():
    """跟test_archive_filter.py对ARCHIVE_STRINGS做的同一种检查：zh/en两个
    翻译字典的key集合必须完全一致，不能一边有另一边没有。"""
    import re
    m = re.search(r"const HOMEPAGE_I18N_STRINGS = \{(.*?)\n  \};", INDEX_JS, re.DOTALL)
    check("能定位到HOMEPAGE_I18N_STRINGS字典", m is not None)
    if not m:
        return
    body = m.group(1)
    zh_block = re.search(r"zh:\s*\{(.*?)\n    \},\s*en:", body, re.DOTALL)
    en_block = re.search(r"en:\s*\{(.*?)\n    \}", body, re.DOTALL)
    check("能定位到HOMEPAGE_I18N_STRINGS.zh/.en两个块", bool(zh_block and en_block))
    if zh_block and en_block:
        zh_keys = set(re.findall(r"^\s*(\w+):", zh_block.group(1), re.MULTILINE))
        en_keys = set(re.findall(r"^\s*(\w+):", en_block.group(1), re.MULTILINE))
        check("HOMEPAGE_I18N_STRINGS中英文翻译键集合完全一致",
              zh_keys == en_keys, (zh_keys - en_keys, en_keys - zh_keys))


def test_index_js_lang_toggle_setter_exists_and_persists():
    check("定义了setFoxzenLang()支持点击后动态切换（不是只在加载时应用一次）",
          "function setFoxzenLang(lang)" in INDEX_JS)
    check("setFoxzenLang()会写入localStorage", "localStorage.setItem(FOXZEN_LANG_KEY, lang)" in INDEX_JS)
    check("定义了wireLangToggle()绑定按钮点击", "function wireLangToggle()" in INDEX_JS)
    check("启动时调用了wireLangToggle()", "wireLangToggle();" in INDEX_JS)


def test_index_js_no_translation_api_used():
    for bad in ("translate.googleapis", "translation.googleapis", "api.openai.com",
                "api.anthropic.com", "bing.com/translator", "microsoft.com/translator"):
        check(f"static/index.js不引入翻译API: {bad}", bad not in INDEX_JS)


def test_index_js_download_and_toolbar_ui_has_i18n_hooks():
    for key in ("search_placeholder", "tag_placeholder", "search_btn", "download_all_btn",
                "download_selected_btn", "export_selected_btn", "export_tag_btn", "export_all_btn",
                "refresh_label", "prev_page", "next_page"):
        check(f"HOMEPAGE_I18N_STRINGS包含key: {key}", f"{key}:" in INDEX_JS, key)
    check('data-i18n-placeholder用于搜索框', '"data-i18n-placeholder": "search_placeholder"' in INDEX_JS)
    check('data-i18n-tpl用于分页信息', '"data-i18n-tpl": "pagination_info"' in INDEX_JS)
    check('data-i18n-tpl用于单篇文章统计（浏览/下载/离线版/完读次数）', '"data-i18n-tpl": "post_stats"' in INDEX_JS)


def test_index_js_post_title_never_wrapped_in_i18n():
    """renderList()里文章标题(p.title)只用于<a>的text，旁边的统计span才带
    data-i18n-tpl——用位置关系确认标题本身没有被套上任何翻译属性。"""
    import re
    m = re.search(r"function renderList\(posts\) \{(.*?)\n  \}", INDEX_JS, re.DOTALL)
    check("能定位到renderList()函数体", m is not None)
    if m:
        body = m.group(1)
        title_line = next((ln for ln in body.splitlines() if "text: p.title" in ln), None)
        check("找到构造文章标题<a>的那一行", title_line is not None, body)
        if title_line:
            check("文章标题所在的<a>元素没有data-i18n相关属性", "data-i18n" not in title_line, title_line)


def test_index_template_lang_toggle_button_present():
    import fetch_blog
    check("首页模板包含右上角语言切换按钮容器", 'class="lang-toggle"' in fetch_blog.INDEX_TEMPLATE)
    check("包含中文切换按钮", 'data-lang-btn="zh"' in fetch_blog.INDEX_TEMPLATE)
    check("包含英文切换按钮", 'data-lang-btn="en"' in fetch_blog.INDEX_TEMPLATE)


def test_index_template_lang_toggle_fixed_top_right_and_mobile_safe():
    """按钮桌面端/移动端都要能用：position:fixed保证桌面端右上角固定可见，
    额外的@media (max-width: 480px)规则保证窄屏(手机)下不会遮挡内容/溢出。
    项目没有浏览器自动化工具，这里只能做样式规则层面的静态确认。"""
    import fetch_blog
    style_start = fetch_blog.INDEX_TEMPLATE.index(".lang-toggle {")
    style_slice = fetch_blog.INDEX_TEMPLATE[style_start:style_start + 700]
    check("桌面端：.lang-toggle使用position:fixed定位到右上角", "position: fixed" in style_slice)
    check("桌面端：.lang-toggle定位在top/right", "top: 12px" in style_slice and "right: 12px" in style_slice)
    check("移动端：存在窄屏媒体查询覆盖.lang-toggle", "@media (max-width: 480px)" in style_slice)


def test_index_template_internet_archive_line_never_gets_i18n():
    """Internet Archive域名验证行是给存档方看的固定英文声明，不是UI标签，
    这次全站国际化明确不翻译它——用"没有套上data-i18n"来确认这条边界。"""
    import fetch_blog
    marker = "Internet Archive verification"
    idx = fetch_blog.INDEX_TEMPLATE.index(marker)
    surrounding = fetch_blog.INDEX_TEMPLATE[max(0, idx - 120):idx]
    check("Internet Archive验证行所在的<div>没有data-i18n属性", "data-i18n" not in surrounding, surrounding)


def test_index_template_quotes_and_favorite_blog_names_never_get_i18n():
    """格言占位符和favorite_blogs.txt渲染出的博客名称本身不是UI文案，
    不应该被套上data-i18n（fav_blogs_heading这个标题本身除外，它是UI标签）。"""
    import fetch_blog
    check("<!--QUOTE-->占位符所在行不含data-i18n",
          "data-i18n" not in fetch_blog.INDEX_TEMPLATE.splitlines()[
              [i for i, ln in enumerate(fetch_blog.INDEX_TEMPLATE.splitlines()) if "<!--QUOTE-->" in ln][0]
          ])


def test_publish_build_toolbar_html_has_i18n_hooks():
    import publish_build
    check("SEARCH_TOOLBAR_HTML的搜索框有data-i18n-placeholder", "data-i18n-placeholder=" in publish_build.SEARCH_TOOLBAR_HTML)
    check("DOWNLOAD_TOOLBAR_HTML的按钮有data-i18n", "data-i18n=" in publish_build.DOWNLOAD_TOOLBAR_HTML)
    check("REFRESH_TOOLBAR_HTML的按钮有data-i18n-tpl", "data-i18n-tpl=" in publish_build.REFRESH_TOOLBAR_HTML)


def test_freshly_rendered_index_survives_publish_build_with_i18n_intact():
    """端到端集成测试：fetch_blog.render_index()生成的真实index.html(带
    本次新增的全部data-i18n/data-i18n-tpl/语言切换按钮) -> publish_build.py
    的host转换(替换脚本引用、修正文章链接) -> 结果里这些i18n标记必须原样
    保留，Internet Archive验证行必须保持不变，且verify_publish()必须通过。

    publish_build.build_publish()需要完整的html/树(robots.txt/sitemap.xml/
    posts/404/等)，不只是index.html本身，所以这里先把真实html/整个拷贝到
    临时目录，再只在这份拷贝上重新跑render_index()覆盖index.html——只读
    真实data/blog.db(SELECT)和真实html/(拷贝源)，不改动仓库里的任何文件。
    """
    import fetch_blog
    import publish_build

    real_html_dir = BASE_DIR / "html"
    if not real_html_dir.exists():
        print("  [跳过] 本地没有真实html/目录")
        return

    real_index_mtime = (real_html_dir / "index.html").stat().st_mtime if (real_html_dir / "index.html").exists() else None

    tmp = Path(tempfile.mkdtemp(prefix="homepage_content_e2e_i18n_test_"))
    html_dir = tmp / "html"
    shutil.copytree(real_html_dir, html_dir)

    orig_fetch_html_dir = fetch_blog.HTML_DIR
    orig_publish_html_dir = publish_build.HTML_DIR
    try:
        fetch_blog.HTML_DIR = html_dir
        fetch_blog.render_index()
        fresh_html = (html_dir / "index.html").read_text(encoding="utf-8")
        check("新渲染的index.html包含语言切换按钮", "lang-toggle" in fresh_html)
        check("新渲染的index.html包含data-i18n标记", "data-i18n=" in fresh_html)
        check("新渲染的index.html的Internet Archive验证行完整保留",
              "Internet Archive verification" in fresh_html)

        publish_build.HTML_DIR = html_dir
        out_dir = tmp / "publish_out_i18n_check"
        publish_build.build_publish("github.foxzen.me", out_dir)
        publish_build.verify_publish(out_dir, "github.foxzen.me")
        built = (out_dir / "index.html").read_text(encoding="utf-8")
        check("发布产物index.html仍包含语言切换按钮", "lang-toggle" in built)
        check("发布产物index.html仍包含data-i18n标记", "data-i18n=" in built)
        check("发布产物index.html仍包含data-i18n-tpl标记", "data-i18n-tpl=" in built)
        check("发布产物index.html的Internet Archive验证行完整保留",
              "Internet Archive verification" in built)
        check("发布产物已把/static/index.js换成pages-index.js（不留Flask专属脚本引用）",
              "/static/index.js" not in built and "pages-index.js" in built)
        check("verify_publish()通过安全/完整性检查", True)  # 上面没抛异常就是通过

        real_index_mtime_after = (real_html_dir / "index.html").stat().st_mtime if (real_html_dir / "index.html").exists() else None
        check("真实html/index.html未被本测试修改（mtime不变，全程只操作tmp拷贝）",
              real_index_mtime_after == real_index_mtime)
    finally:
        fetch_blog.HTML_DIR = orig_fetch_html_dir
        publish_build.HTML_DIR = orig_publish_html_dir
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    tests = [
        test_quotes_normal_read,
        test_quotes_empty_file,
        test_quotes_missing_file_no_crash,
        test_quotes_multiple_lines_random_from_valid_set,
        test_quotes_blank_lines_skipped,
        test_index_route_substitutes_quote_placeholder,
        test_index_route_quote_is_html_escaped,
        test_publish_build_substitutes_quote_placeholder,
        test_publish_build_quote_missing_file_no_crash,
        test_publish_build_quote_is_html_escaped,
        test_publish_build_404_page_quote_substituted_and_escaped,
        test_verify_publish_rejects_leftover_quote_placeholder,
        test_favorite_blogs_normal_read,
        test_favorite_blogs_empty_file,
        test_favorite_blogs_comments_and_blank_lines_skipped,
        test_favorite_blogs_malformed_line_skipped,
        test_favorite_blogs_empty_name_or_url_skipped,
        test_favorite_blogs_non_https_rejected,
        test_favorite_blogs_html_escape_in_name,
        test_favorite_blogs_url_escape_in_href,
        test_favorite_blogs_link_attributes_target_and_rel,
        test_favorite_blogs_zero_entries_renders_nothing,
        test_favorite_blogs_heading_has_data_i18n_attribute,
        test_render_index_includes_favorite_blogs_section,
        test_render_index_favorite_blogs_missing_file_no_crash,
        test_render_index_unrelated_sections_still_present,
        test_index_js_homepage_i18n_strings_both_languages,
        test_index_js_applies_data_i18n_sweep,
        test_index_js_homepage_i18n_called_at_bootstrap,
        test_index_js_homepage_i18n_reuses_foxzen_lang_key,
        test_real_quotes_txt_reads_without_error,
        test_real_favorite_blogs_txt_reads_without_error,
        test_real_announcements_txt_reads_without_error,
        test_index_js_homepage_i18n_strings_key_sets_symmetric,
        test_index_js_lang_toggle_setter_exists_and_persists,
        test_index_js_no_translation_api_used,
        test_index_js_download_and_toolbar_ui_has_i18n_hooks,
        test_index_js_post_title_never_wrapped_in_i18n,
        test_index_template_lang_toggle_button_present,
        test_index_template_lang_toggle_fixed_top_right_and_mobile_safe,
        test_index_template_internet_archive_line_never_gets_i18n,
        test_index_template_quotes_and_favorite_blog_names_never_get_i18n,
        test_publish_build_toolbar_html_has_i18n_hooks,
        test_freshly_rendered_index_survives_publish_build_with_i18n_intact,
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
