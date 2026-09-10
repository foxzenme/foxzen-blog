#!/usr/bin/env python3
"""
针对mirror首页"其他入口"区块的回归测试(第十八节需求)。

只测试render_index()生成的静态HTML本身，不联网抓Blogger、不改data/blog.db。
用法: python3 test_site_entries.py
"""
import html.parser
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


class _StrictHTMLValidator(html.parser.HTMLParser):
    """只用来确认HTML能被标准解析器正常parse完，不代表完整W3C校验，
    但足以捕获"标签没闭合"这类明显的HTML损坏问题。"""
    def error(self, message):
        raise AssertionError(message)


def with_temp_html_dir(fn):
    import fetch_blog
    tmp = Path(tempfile.mkdtemp(prefix="site_entries_test_"))
    orig_html_dir = fetch_blog.HTML_DIR
    fetch_blog.HTML_DIR = tmp
    try:
        fn(tmp)
    finally:
        fetch_blog.HTML_DIR = orig_html_dir
        shutil.rmtree(tmp, ignore_errors=True)


def _generated_index_html(tmp):
    import fetch_blog
    fetch_blog.render_index()
    return (tmp / "index.html").read_text(encoding="utf-8")


def test_entries_block_present():
    def _run(tmp):
        content = _generated_index_html(tmp)
        check("包含'其他入口'区块标题", "FoxZen 的其他入口" in content)
    with_temp_html_dir(_run)


def test_five_expected_domains_present():
    def _run(tmp):
        content = _generated_index_html(tmp)
        for domain in ("foxzen.me", "backup.foxzen.me", "status.foxzen.me",
                       "github.foxzen.me", "cf.foxzen.me"):
            check(f"页面提到域名: {domain}", domain in content)
    with_temp_html_dir(_run)


def test_live_entries_are_real_https_links():
    """已经真实部署验证过的入口(foxzen.me / backup.foxzen.me / github.foxzen.me /
    cf.foxzen.me)必须是可点击的<a href="https://...">链接，不能只是纯文字。"""
    def _run(tmp):
        content = _generated_index_html(tmp)
        for domain in ("foxzen.me", "backup.foxzen.me", "github.foxzen.me", "cf.foxzen.me"):
            needle = f'href="https://{domain}/"'
            check(f"{domain} 是https可点击链接: {needle}", needle in content)
    with_temp_html_dir(_run)


def test_planned_entries_are_not_clickable_links():
    """status.foxzen.me的GreenCloud origin已经生成并返回200，但公网仍被既有
    Cloudflare edge"本站暂时下线"规则拦截，因此仍不能算公网正式上线——不能包在
    <a href>里，否则点击后访客看到的是那条edge拦截页而不是真正的status内容。"""
    def _run(tmp):
        content = _generated_index_html(tmp)
        for domain in ("status.foxzen.me",):
            for scheme in ("http://", "https://"):
                needle = f'href="{scheme}{domain}'
                check(f"{domain} 没有被做成可点击链接: 不应出现 {needle}",
                      needle not in content)
            check(f"{domain} 标注了规划中/尚未上线", "规划中" in content)
    with_temp_html_dir(_run)


def test_no_vip_or_admin_wording():
    """第八节明确要求不能出现VIP/会员/管理员专属这类措辞。"""
    def _run(tmp):
        content = _generated_index_html(tmp)
        for banned in ("VIP", "会员专属", "赞助后解锁", "管理员专属"):
            check(f"页面不包含禁止措辞: {banned!r}", banned not in content)
    with_temp_html_dir(_run)


def test_html_still_parses_without_error():
    def _run(tmp):
        content = _generated_index_html(tmp)
        parser = _StrictHTMLValidator()
        try:
            parser.feed(content)
            parser.close()
            ok = True
        except AssertionError:
            ok = False
        check("生成的HTML能被标准解析器正常解析（没有明显损坏）", ok)
    with_temp_html_dir(_run)


def test_no_new_js_or_script_tag_introduced():
    """确认没有为了这个新区块引入任何新的<script>标签或JS依赖，
    页面里唯一允许存在的script标签是原有的GA统计和/static/index.js。"""
    def _run(tmp):
        content = _generated_index_html(tmp)
        import re
        scripts = re.findall(r'<script[^>]*src="([^"]*)"', content)
        allowed = {
            "https://www.googletagmanager.com/gtag/js?id=G-WW1SLDPH1Z",
            "/static/index.js",
        }
        unexpected = [s for s in scripts if s not in allowed]
        check("没有引入新的外部script标签", unexpected == [], f"got {unexpected}")
    with_temp_html_dir(_run)


def test_unrelated_sections_untouched():
    """确认原有的统计box、排行榜、彩蛋链接等区块仍然存在，
    没有因为插入新区块而被误删。"""
    def _run(tmp):
        content = _generated_index_html(tmp)
        check("统计box仍存在", 'class="stats-box"' in content)
        check("排行榜仍存在", 'class="leaderboard"' in content)
        check("彩蛋链接仍存在", "这个网站藏着一只找不到路的狐狸" in content)
    with_temp_html_dir(_run)


def main():
    tests = [
        test_entries_block_present,
        test_five_expected_domains_present,
        test_live_entries_are_real_https_links,
        test_planned_entries_are_not_clickable_links,
        test_no_vip_or_admin_wording,
        test_html_still_parses_without_error,
        test_no_new_js_or_script_tag_introduced,
        test_unrelated_sections_untouched,
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
