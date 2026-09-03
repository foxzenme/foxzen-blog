#!/usr/bin/env python3
"""
针对 publish_build.py（第十七节 publish/ 白名单静态构建）的回归测试。

全部测试都在临时目录里操作，不碰真实 html/、不碰 data/blog.db、不产生
需要手动清理的残留文件。用法: python3 test_publish_build.py
"""
import shutil
import sys
import tempfile
import traceback
from pathlib import Path

failures = []


def check(name, condition, detail=""):
    if condition:
        print(f"  [PASS] {name}")
    else:
        print(f"  [FAIL] {name} {detail}")
        failures.append(name)


def _make_fixture_html_dir(tmp):
    """构造一个最小但覆盖各类白名单/黑名单场景的html/夹具目录，
    比只用真实html/更能稳定测试到canonical(YYYY/MM)目录这类当前本地
    html/里还没有真实数据的场景。"""
    html_dir = tmp / "html"
    html_dir.mkdir()

    (html_dir / "index.html").write_text(
        '<html><body><div id="app"></div>'
        '<script src="/static/index.js"></script>'
        '</body></html>',
        encoding="utf-8",
    )
    (html_dir / "robots.txt").write_text(
        "User-agent: *\nAllow: /\nSitemap: https://mirror.foxzen.me/sitemap.xml\n",
        encoding="utf-8",
    )
    (html_dir / "sitemap.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        '<url><loc>https://mirror.foxzen.me/</loc></url>\n'
        '<url><loc>https://mirror.foxzen.me/2026/07/demo-slug.html</loc></url>\n'
        "</urlset>\n",
        encoding="utf-8",
    )
    (html_dir / "foxzen-download-admin.html").write_text("<html>admin</html>", encoding="utf-8")
    (html_dir / "29bfb801721343b798cc9dfca454d8af.txt").write_text("indexnow-key", encoding="utf-8")

    (html_dir / "404").mkdir()
    (html_dir / "404" / "index.html").write_text("<html>404</html>", encoding="utf-8")

    (html_dir / "images").mkdir()
    (html_dir / "images" / "fox-header.png").write_bytes(b"\x89PNG-fake-bytes")

    (html_dir / "posts" / "111").mkdir(parents=True)
    (html_dir / "posts" / "111" / "index.html").write_text(
        "<html><body>真实文章正文，示例password/token出现在正文里不代表危险</body></html>",
        encoding="utf-8",
    )
    (html_dir / "posts" / "111" / "media").mkdir()
    (html_dir / "posts" / "111" / "media" / "pic.png").write_bytes(b"fake-image-bytes")

    (html_dir / "1").mkdir()
    (html_dir / "1" / "index.html").write_text("<html>短号跳转</html>", encoding="utf-8")

    (html_dir / "2026" / "07").mkdir(parents=True)
    (html_dir / "2026" / "07" / "demo-slug.html").write_text(
        "<html><body>canonical文章</body></html>", encoding="utf-8"
    )

    (html_dir / "foxzen").mkdir()
    (html_dir / "foxzen" / "index.html").write_text("<html>foxzen.me专属页面</html>", encoding="utf-8")

    return html_dir


def with_fixture(fn):
    import publish_build
    tmp = Path(tempfile.mkdtemp(prefix="publish_build_test_"))
    orig_html_dir = publish_build.HTML_DIR
    fixture_html = _make_fixture_html_dir(tmp)
    publish_build.HTML_DIR = fixture_html
    output_dir = tmp / "publish_out"
    try:
        fn(tmp, output_dir)
    finally:
        publish_build.HTML_DIR = orig_html_dir
        shutil.rmtree(tmp, ignore_errors=True)


def test_data_and_db_excluded():
    def _run(tmp, out):
        import publish_build
        # 夹具本身没有data/，这里额外验证builder不会主动创建/引用它
        publish_build.build_publish("github.foxzen.me", out)
        check("publish/中不存在data/目录", not (out / "data").exists())
        check("publish/中不存在任何.db文件",
              not any(p.suffix == ".db" for p in out.rglob("*")))
    with_fixture(_run)


def test_secret_and_backend_files_excluded():
    def _run(tmp, out):
        import publish_build
        # 模拟仓库根目录混进来的敏感文件类型不会被builder碰到
        # （builder本身只看HTML_DIR即html/，不看仓库根目录，这里直接验证白名单结果）
        publish_build.build_publish("github.foxzen.me", out)
        all_files = [p for p in out.rglob("*") if p.is_file()]
        names = [p.name for p in all_files]
        for banned_ext in (".env", ".pem", ".key", ".secret", ".token", ".py"):
            check(f"publish/中没有{banned_ext}后缀文件",
                  not any(n.endswith(banned_ext) for n in names))
        check("publish/中不存在nginx-conf目录", not (out / "nginx-conf").exists())
        check("publish/中不存在cron目录", not (out / "cron").exists())
        check("publish/中不存在systemd目录", not (out / "systemd").exists())
        check("publish/中不存在foxzen-download-admin.html",
              not any(n == "foxzen-download-admin.html" for n in names))
        check("publish/中不存在__pycache__",
              not any("__pycache__" in str(p) for p in all_files))
    with_fixture(_run)


def test_expected_public_files_present():
    def _run(tmp, out):
        import publish_build
        publish_build.build_publish("github.foxzen.me", out)
        check("index.html存在", (out / "index.html").exists())
        check("根目录404.html存在", (out / "404.html").exists())
        check("robots.txt存在", (out / "robots.txt").exists())
        check("sitemap.xml存在", (out / "sitemap.xml").exists())
        check("至少一篇真实文章静态HTML存在", (out / "posts" / "111" / "index.html").exists())
        check("文章媒体资源存在", (out / "posts" / "111" / "media" / "pic.png").exists())
        check("短号跳转页存在", (out / "1" / "index.html").exists())
        check("canonical静态文章存在", (out / "2026" / "07" / "demo-slug.html").exists())
        check("images/存在", (out / "images" / "fox-header.png").exists())
    with_fixture(_run)


def test_indexnow_key_and_foxzen_site_excluded_by_whitelist():
    """29bfb...txt和foxzen/不在白名单规则内——不是因为它们危险，
    而是它们分别属于mirror域名专属的IndexNow验证、foxzen.me专属页面，
    跟github.foxzen.me/cf.foxzen.me这两个"文章镜像"站点的职责无关。"""
    def _run(tmp, out):
        import publish_build
        publish_build.build_publish("github.foxzen.me", out)
        check("mirror专属IndexNow验证文件未被复制",
              not (out / "29bfb801721343b798cc9dfca454d8af.txt").exists())
        check("foxzen.me专属目录未被复制", not (out / "foxzen").exists())
    with_fixture(_run)


def test_github_hostname_rewrite():
    def _run(tmp, out):
        import publish_build
        publish_build.build_publish("github.foxzen.me", out)
        robots = (out / "robots.txt").read_text(encoding="utf-8")
        sitemap = (out / "sitemap.xml").read_text(encoding="utf-8")
        cname = (out / "CNAME").read_text(encoding="utf-8").strip()
        check("robots.txt使用github.foxzen.me", "https://github.foxzen.me/sitemap.xml" in robots)
        check("robots.txt不再含mirror.foxzen.me", "mirror.foxzen.me" not in robots)
        check("sitemap.xml使用github.foxzen.me", "https://github.foxzen.me/" in sitemap)
        check("sitemap.xml不再含mirror.foxzen.me", "mirror.foxzen.me" not in sitemap)
        check("CNAME内容正确", cname == "github.foxzen.me")
    with_fixture(_run)


def test_cloudflare_hostname_rewrite():
    def _run(tmp, out):
        import publish_build
        publish_build.build_publish("cf.foxzen.me", out)
        robots = (out / "robots.txt").read_text(encoding="utf-8")
        sitemap = (out / "sitemap.xml").read_text(encoding="utf-8")
        cname = (out / "CNAME").read_text(encoding="utf-8").strip()
        check("robots.txt使用cf.foxzen.me", "https://cf.foxzen.me/sitemap.xml" in robots)
        check("sitemap.xml使用cf.foxzen.me", "https://cf.foxzen.me/" in sitemap)
        check("CNAME内容正确", cname == "cf.foxzen.me")
    with_fixture(_run)


def test_same_builder_same_output_structure_for_both_hosts():
    """同一套builder对两个host产出的文件树结构应该完全一致（只有hostname
    相关内容不同），不能是两套不同的生成逻辑。"""
    def _run(tmp, out):
        import publish_build
        out_gh = tmp / "publish_gh"
        out_cf = tmp / "publish_cf"
        publish_build.build_publish("github.foxzen.me", out_gh)
        publish_build.build_publish("cf.foxzen.me", out_cf)
        rel_gh = sorted(str(p.relative_to(out_gh)) for p in out_gh.rglob("*"))
        rel_cf = sorted(str(p.relative_to(out_cf)) for p in out_cf.rglob("*"))
        check("两个host产出的文件树结构完全一致", rel_gh == rel_cf,
              f"only in gh: {set(rel_gh)-set(rel_cf)}, only in cf: {set(rel_cf)-set(rel_gh)}")
    with_fixture(_run)


def test_index_js_removed_but_fallback_content_kept():
    def _run(tmp, out):
        import publish_build
        publish_build.build_publish("github.foxzen.me", out)
        content = (out / "index.html").read_text(encoding="utf-8")
        check("index.html中已移除依赖Flask API的static/index.js引用",
              "/static/index.js" not in content)
        check("index.html的静态兜底列表(#app容器)仍然保留", 'id="app"' in content)
    with_fixture(_run)


def test_article_body_with_password_token_words_not_treated_as_secret():
    """正文里出现password/token这些技术术语的文章文件本身不应该被误判成
    危险文件而被排除——只按路径/文件名白名单判断，不按正文内容做黑名单删除。"""
    def _run(tmp, out):
        import publish_build
        publish_build.build_publish("github.foxzen.me", out)
        article = out / "posts" / "111" / "index.html"
        check("含password/token字样的正常文章正文未被误删", article.exists())
        text = article.read_text(encoding="utf-8")
        check("正文内容原样保留", "password/token" in text)
    with_fixture(_run)


def test_verify_publish_passes_on_good_build():
    def _run(tmp, out):
        import publish_build
        publish_build.build_publish("github.foxzen.me", out)
        try:
            publish_build.verify_publish(out, "github.foxzen.me")
            ok = True
        except publish_build.PublishVerificationError as e:
            ok = False
            print(f"    unexpected error: {e}")
        check("正常构建的publish/能通过verify_publish()检查", ok)
    with_fixture(_run)


def test_verify_publish_catches_injected_danger_file():
    """人为在构建产物里塞一个不该出现的.db文件，确认verify_publish()会
    识别出来并抛出异常——这是CI在upload artifact前的最后一道防线。"""
    def _run(tmp, out):
        import publish_build
        publish_build.build_publish("github.foxzen.me", out)
        (out / "data").mkdir()
        (out / "data" / "blog.db").write_bytes(b"not a real db, just a test probe")
        raised = False
        message = ""
        try:
            publish_build.verify_publish(out, "github.foxzen.me")
        except publish_build.PublishVerificationError as e:
            raised = True
            message = str(e)
        check("verify_publish()识别出被注入的data/blog.db并拒绝通过", raised)
        check("错误信息里提到data/目录", "data/" in message)
    with_fixture(_run)


def test_verify_publish_catches_wrong_hostname():
    """如果sitemap.xml因为某种原因没有正确替换hostname，verify_publish()
    应该拦下来，而不是让一份还写着mirror.foxzen.me的产物被当成github.foxzen.me
    的正式内容发布出去。"""
    def _run(tmp, out):
        import publish_build
        publish_build.build_publish("github.foxzen.me", out)
        sitemap = out / "sitemap.xml"
        sitemap.write_text(
            sitemap.read_text(encoding="utf-8").replace("github.foxzen.me", "mirror.foxzen.me"),
            encoding="utf-8",
        )
        raised = False
        try:
            publish_build.verify_publish(out, "github.foxzen.me")
        except publish_build.PublishVerificationError:
            raised = True
        check("verify_publish()识别出未正确替换hostname的sitemap.xml", raised)
    with_fixture(_run)


def test_build_is_repeatable():
    """同样的html/输入 + 同样的host，重复构建两次应该得到完全相同的产物
    （不依赖当前时间、访问统计等易变状态）。"""
    def _run(tmp, out):
        import publish_build
        out2 = tmp / "publish_out2"
        publish_build.build_publish("github.foxzen.me", out)
        publish_build.build_publish("github.foxzen.me", out2)
        files1 = {p.relative_to(out): p.read_bytes() for p in out.rglob("*") if p.is_file()}
        files2 = {p.relative_to(out2): p.read_bytes() for p in out2.rglob("*") if p.is_file()}
        check("两次构建产出的文件集合一致", set(files1.keys()) == set(files2.keys()))
        mismatched = [k for k in files1 if files1.get(k) != files2.get(k)]
        check("两次构建产出的文件内容字节级一致", mismatched == [], f"mismatched: {mismatched}")
    with_fixture(_run)


def test_no_unexpected_db_or_secret_file_anywhere_in_output():
    """对最终产物做一次面向路径名的危险文件扫描，作为builder自身白名单逻辑
    之外的第二层保险——扫描的是产物本身，不是builder源码。"""
    def _run(tmp, out):
        import publish_build
        publish_build.build_publish("github.foxzen.me", out)
        danger_suffixes = (".db", ".sqlite", ".sqlite3", ".env", ".pem",
                           ".key", ".p12", ".pfx", ".secret", ".token")
        danger_names = ("id_rsa", "id_ed25519")
        offenders = []
        for p in out.rglob("*"):
            if not p.is_file():
                continue
            if p.suffix in danger_suffixes or p.name in danger_names:
                offenders.append(str(p))
        check("产物中没有任何危险后缀/文件名", offenders == [], f"found {offenders}")
    with_fixture(_run)


def test_output_dir_is_rebuilt_not_appended():
    """确认build_publish()每次都会清空输出目录重建，不会残留上一次构建
    （比如换了host之后）留下的、已经不该存在的旧文件。"""
    def _run(tmp, out):
        import publish_build
        out.mkdir(parents=True)
        stale_file = out / "stale_leftover.html"
        stale_file.write_text("should be removed", encoding="utf-8")
        publish_build.build_publish("github.foxzen.me", out)
        check("重新构建会清除上一次残留的文件", not stale_file.exists())
    with_fixture(_run)


def test_real_local_html_dir_builds_without_error():
    """用当前仓库里真实的html/(不是夹具)跑一次，确认builder在真实数据上
    能正常工作，不只是在人造夹具上正常。"""
    import publish_build
    real_html = Path(__file__).parent / "html"
    if not real_html.exists():
        print("  [SKIP] 未找到真实html/目录")
        return
    tmp = Path(tempfile.mkdtemp(prefix="publish_build_real_test_"))
    try:
        out = tmp / "publish_real"
        result = publish_build.build_publish("github.foxzen.me", out)
        check("真实html/能成功构建出publish/", result.exists())
        check("真实构建产出index.html", (out / "index.html").exists())
        check("真实构建产出robots.txt", (out / "robots.txt").exists())
        check("真实构建产出sitemap.xml", (out / "sitemap.xml").exists())
        check("真实构建产出至少一篇文章", any((out / "posts").glob("*/index.html")))
        check("真实构建不含data/blog.db", not any(p.name == "blog.db" for p in out.rglob("*")))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    tests = [
        test_data_and_db_excluded,
        test_secret_and_backend_files_excluded,
        test_expected_public_files_present,
        test_indexnow_key_and_foxzen_site_excluded_by_whitelist,
        test_github_hostname_rewrite,
        test_cloudflare_hostname_rewrite,
        test_same_builder_same_output_structure_for_both_hosts,
        test_index_js_removed_but_fallback_content_kept,
        test_article_body_with_password_token_words_not_treated_as_secret,
        test_verify_publish_passes_on_good_build,
        test_verify_publish_catches_injected_danger_file,
        test_verify_publish_catches_wrong_hostname,
        test_build_is_repeatable,
        test_no_unexpected_db_or_secret_file_anywhere_in_output,
        test_output_dir_is_rebuilt_not_appended,
        test_real_local_html_dir_builds_without_error,
    ]
    for t in tests:
        print(f"--- {t.__name__} ---")
        try:
            t()
        except Exception:
            print(f"  [FAIL] {t.__name__} 抛出异常:")
            traceback.print_exc()
            failures.append(t.__name__)

    print()
    if failures:
        print(f"共 {len(failures)} 项失败: {failures}")
        sys.exit(1)
    print("全部测试通过。")


if __name__ == "__main__":
    main()
