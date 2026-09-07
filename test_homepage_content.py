#!/usr/bin/env python3
""""每日格言"(data/quotes.txt)回归测试：动态路径(app.py，既有代码，本次未改动，
只是审计后补齐测试)+静态发布路径(publish_build.py，本次实际修复的代码——此前
完全没有替换<!--QUOTE-->占位符，github.foxzen.me/cf.foxzen.me的首页和404页
"🦊 "后面都会显示为空)。

范围说明：这个文件之后还会扩展覆盖favorite_blogs.txt/首页UI国际化等内容
（那些是独立于本次quotes修复的工作，随各自功能一起提交，不在这一版里）。

用法: python3 test_homepage_content.py
"""
import shutil
import sys
import tempfile
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
# 一、quotes.txt —— 既有实现审计+补测试（app.py动态路径，mirror/backup）
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
# 二、quotes.txt —— 静态发布路径（publish_build.py，本次实际修复的代码，
#    覆盖github.foxzen.me/cf.foxzen.me的首页和404页两处）
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
    （本身就带<!--QUOTE-->，见html/404/index.html），所以用"拷贝真实html/
    到临时目录"的方式，不修改仓库里的任何文件。
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
# 三、真实quotes.txt只读验收
# ============================================================

def test_real_quotes_txt_reads_without_error():
    real_quotes = BASE_DIR / "data" / "quotes.txt"
    check("真实quotes.txt存在", real_quotes.exists())
    text = real_quotes.read_text(encoding="utf-8")
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    check("真实quotes.txt至少有1条格言", len(lines) >= 1, len(lines))


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
        test_real_quotes_txt_reads_without_error,
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
