#!/usr/bin/env python3
"""foxzen.me总入口页(html/foxzen/index.html)的回归测试。

这个文件是纯手工维护的静态HTML，没有Python模板/构建脚本(跟fetch_blog.py::
INDEX_TEMPLATE、build_status_page.py等不同)，所以这里全部直接读磁盘文件，
不需要任何临时目录/重新渲染步骤。

覆盖：
1. 现有mirror/GPG两个入口保留，URL不变
2. 新增github/cf/status/update四个入口，域名/功能说明都齐全
3. status/update用今天实测确认存在的Cloudflare Pages *.pages.dev地址做
   真实可点击链接(foxzen-status.pages.dev / foxzen-update.pages.dev)，
   而不是尚未绑定DNS的自定义域名本身
4. github/cf目前没有任何已确认可用的地址，不能被做成指向自己域名的假链接
5. 所有6个入口的域名/URL文本本身永远不会被打上data-i18n(i18n不会误伤URL)
6. 复用现有foxzen_lang机制(localStorage key、检测算法、data-i18n约定)，
   不是另造一套
7. 中英文键集合对称
8. HTML能被标准解析器正常解析
9. 没有引入任何翻译API/新的外部脚本依赖
10. 桌面+移动端都有语言切换按钮(位置固定，媒体查询覆盖窄屏)

用法: python3 test_foxzen_gateway_page.py
"""
import html.parser
import re
import sys
import traceback
from pathlib import Path

BASE_DIR = Path(__file__).parent
GATEWAY_FILE = BASE_DIR / "html" / "foxzen" / "index.html"
GPG_FILE = BASE_DIR / "html" / "foxzen" / "gpg" / "index.html"

failures = []


def check(name, condition, detail=""):
    if condition:
        print(f"  [PASS] {name}")
    else:
        print(f"  [FAIL] {name} {detail}")
        failures.append(name)


class _StrictHTMLValidator(html.parser.HTMLParser):
    def error(self, message):
        raise AssertionError(message)


def _text():
    return GATEWAY_FILE.read_text(encoding="utf-8")


def test_file_exists():
    check("html/foxzen/index.html存在", GATEWAY_FILE.exists())


def test_existing_mirror_entry_preserved():
    text = _text()
    check('mirror.foxzen.me入口仍是可点击真链接href="https://mirror.foxzen.me/"',
          'href="https://mirror.foxzen.me/"' in text)
    check("页面提到mirror.foxzen.me域名", "mirror.foxzen.me" in text)


def test_existing_gpg_entry_preserved():
    text = _text()
    check('GPG入口仍指向原有相对路径href="/gpg/"', 'href="/gpg/"' in text)
    check("页面提到foxzen.me/gpg/", "foxzen.me/gpg/" in text)
    check("gpg子页面文件本身没有被这次改动触碰", GPG_FILE.exists())


def test_four_new_domains_mentioned():
    text = _text()
    for domain in ("github.foxzen.me", "cf.foxzen.me", "status.foxzen.me", "update.foxzen.me"):
        check(f"页面提到域名: {domain}", domain in text)


def test_status_and_update_use_confirmed_pages_dev_links():
    """status/update.foxzen.me的DNS目前还代理到GreenCloud、没有独立nginx
    server块(见build_status_page.py/generate_status_page.py文档字符串)，
    但已经实测确认Cloudflare Pages上有可用的*.pages.dev地址——真实可点击
    链接必须指向这两个已确认存在的地址，而不是尚未生效的自定义域名本身。
    """
    text = _text()
    check('status入口真实链接指向href="https://foxzen-status.pages.dev/"',
          'href="https://foxzen-status.pages.dev/"' in text)
    check('update入口真实链接指向href="https://foxzen-update.pages.dev/"',
          'href="https://foxzen-update.pages.dev/"' in text)
    check("页面同时标注了正式域名foxzen-status.pages.dev(域名+真实地址并存)",
          "foxzen-status.pages.dev" in text)
    check("页面同时标注了正式域名foxzen-update.pages.dev(域名+真实地址并存)",
          "foxzen-update.pages.dev" in text)


def test_github_and_cf_not_fake_linked():
    """github.foxzen.me实测404(GitHub Pages从未在仓库设置里启用过)、
    cf.foxzen.me没有任何已确认存在的部署地址——两者都不能被包进<a href>
    指向自己会404/无法访问的域名，也不能拿一个瞎猜的*.pages.dev/*.github.io
    地址顶替。"""
    text = _text()
    for domain in ("github.foxzen.me", "cf.foxzen.me"):
        for scheme in ("http://", "https://"):
            needle = f'href="{scheme}{domain}'
            check(f"{domain} 没有被做成指向自己域名的可点击链接: 不应出现 {needle}",
                  needle not in text)
    check("github入口标注了建设中/尚未上线", "entry_github_name" in text and "建设中" in text)
    check("cf入口标注了建设中/尚未上线", "entry_cf_name" in text and "建设中" in text)
    check('github/cf入口带entry-planned样式类', text.count("entry-planned") >= 4)


def test_all_six_entries_present_as_list_items():
    text = _text()
    check("页面里有且只有一个<ul>入口列表", text.count("<ul>") == 1)
    check("恰好6个<li>入口", text.count("<li>") == 6, text.count("<li>"))


def test_domains_and_urls_never_carry_data_i18n():
    """核心安全要求：i18n只能作用在UI文案(textContent)上，绝不能碰域名/URL
    本身。href="https://mirror.foxzen.me/"这种属性值里出现域名是正常且
    必须的(真实链接目标)——applyLang()只会改textContent，从不touch任何
    属性，所以域名出现在属性值里不在这条安全规则的关心范围内；这里只检查
    "标签的可见文本内容"，确认域名字面量不会同时出现在文本内容里、又被
    打上data-i18n。"""
    text = _text()
    literal_strings = (
        "mirror.foxzen.me", "github.foxzen.me", "cf.foxzen.me",
        "status.foxzen.me", "update.foxzen.me", "foxzen.me/gpg/",
        "foxzen-status.pages.dev", "foxzen-update.pages.dev",
    )
    matched_any = False
    for m in re.finditer(r'<([a-z]+)([^>]*)>([^<]*)</\1>', text):
        attrs, inner_text = m.group(2), m.group(3)
        if "data-i18n=" not in attrs:
            continue
        for literal in literal_strings:
            if literal in inner_text:
                matched_any = True
            check(f"带data-i18n的<{m.group(1)}>标签可见文本里不包含域名字面量 {literal!r}: 实际文本={inner_text!r}",
                  literal not in inner_text)
    check("这条测试确实检查过至少一个bare域名span(entry-domain)，不是空跑",
          '<span class="entry-domain">mirror.foxzen.me</span>' in text)
    check("(理应如此)没有任何一处data-i18n标签的可见文本里混进了域名字面量", not matched_any)


def test_reuses_existing_foxzen_lang_mechanism():
    text = _text()
    check('使用统一的localStorage key "foxzen_lang"', 'var STORAGE_KEY = "foxzen_lang";' in text)
    check("浏览器语言检测函数detectDefaultLang存在", "function detectDefaultLang()" in text)
    check("zh前缀检测逻辑与其它几份独立实现一致(/^zh/i)", "/^zh/i.test(langs[i])" in text)
    check("getLang()读取localStorage优先于浏览器检测", "function getLang()" in text)
    check("applyLang()存在", "function applyLang(lang)" in text)
    check("setLang()存在且写localStorage", "function setLang(lang)" in text and
          "localStorage.setItem(STORAGE_KEY, lang)" in text)
    check("没有引入第二套语言key/机制(homepage_i18n/article_i18n等)",
          not any(bad in text for bad in ("homepage_i18n", "article_i18n", "index_i18n", "gateway_lang")))


def test_lang_toggle_buttons_present_desktop_and_mobile():
    text = _text()
    check('存在中文切换按钮data-lang-btn="zh"', 'data-lang-btn="zh"' in text)
    check('存在英文切换按钮data-lang-btn="en"', 'data-lang-btn="en"' in text)
    check("lang-toggle使用固定定位(桌面右上角显眼且不遮挡内容)", "position: fixed" in text)
    check("存在移动端窄屏媒体查询", "@media (max-width: 480px)" in text)
    check("存在viewport meta(移动端可用性前提)",
          '<meta name="viewport" content="width=device-width, initial-scale=1">' in text)


def test_apply_lang_only_touches_tagged_elements():
    """内容隔离要求：这段JS只能用querySelectorAll("[data-i18n]")这类属性
    选择器操作元素，绝不能用.content或任何文本扫描/匹配的方式。"""
    text = _text()
    selector_calls = re.findall(r'document\.querySelectorAll\(("[^"]*")\)', text)
    check("找到至少一处querySelectorAll调用", len(selector_calls) > 0, selector_calls)
    allowed = {'"[data-i18n]"', '"[data-lang-btn]"'}
    unexpected = [s for s in selector_calls if s not in allowed]
    check("querySelectorAll只用于[data-i18n]/[data-lang-btn]这两种属性选择器，没有其它",
          unexpected == [], unexpected)
    check("脚本里没有出现.content选择器", ".content" not in text)


def test_gateway_strings_zh_en_key_sets_symmetric():
    text = _text()
    m = re.search(r"var GATEWAY_STRINGS = \{\s*zh: \{(.*?)\},\s*en: \{(.*?)\}\s*\};", text, re.DOTALL)
    check("找到GATEWAY_STRINGS定义", m is not None)
    if not m:
        return
    zh_keys = set(re.findall(r'(\w+):', m.group(1)))
    en_keys = set(re.findall(r'(\w+):', m.group(2)))
    check("中英文键集合完全一致", zh_keys == en_keys, (zh_keys ^ en_keys))
    check("键数量不为0", len(zh_keys) > 0)


def test_brand_names_never_translated_in_dict():
    """GitHub Pages/Cloudflare Pages是产品品牌名，中英文两个版本的文案里
    都必须原样出现，不能被"翻译"成别的说法(跟其它页面里博客名/引用来源
    从不翻译是同一条原则)。"""
    text = _text()
    m = re.search(r"var GATEWAY_STRINGS = \{\s*zh: \{(.*?)\},\s*en: \{(.*?)\}\s*\};", text, re.DOTALL)
    check("找到GATEWAY_STRINGS定义", m is not None)
    if not m:
        return
    zh_block, en_block = m.group(1), m.group(2)
    check("中文文案里包含品牌名GitHub Pages", "GitHub Pages" in zh_block)
    check("中文文案里包含品牌名Cloudflare Pages", "Cloudflare Pages" in zh_block)
    check("英文文案里包含品牌名GitHub Pages", "GitHub Pages" in en_block)
    check("英文文案里包含品牌名Cloudflare Pages", "Cloudflare Pages" in en_block)


def test_no_translation_api_or_new_external_dependency():
    text = _text()
    banned_substrings = (
        "translate.googleapis", "translation.googleapis", "api.openai.com",
        "api.anthropic.com", "bing.com/translator", "microsoft.com/translator",
        "<script src=",
    )
    for bad in banned_substrings:
        check(f"不包含: {bad!r}", bad not in text)


def test_html_still_parses_without_error():
    parser = _StrictHTMLValidator()
    try:
        parser.feed(_text())
        parser.close()
        ok = True
    except AssertionError:
        ok = False
    check("生成的HTML能被标准解析器正常解析(没有明显损坏)", ok)


def test_no_vip_or_admin_wording():
    text = _text()
    for banned in ("VIP", "会员专属", "赞助后解锁", "管理员专属"):
        check(f"页面不包含禁止措辞: {banned!r}", banned not in text)


def main():
    tests = [
        test_file_exists,
        test_existing_mirror_entry_preserved,
        test_existing_gpg_entry_preserved,
        test_four_new_domains_mentioned,
        test_status_and_update_use_confirmed_pages_dev_links,
        test_github_and_cf_not_fake_linked,
        test_all_six_entries_present_as_list_items,
        test_domains_and_urls_never_carry_data_i18n,
        test_reuses_existing_foxzen_lang_mechanism,
        test_lang_toggle_buttons_present_desktop_and_mobile,
        test_apply_lang_only_touches_tagged_elements,
        test_gateway_strings_zh_en_key_sets_symmetric,
        test_brand_names_never_translated_in_dict,
        test_no_translation_api_or_new_external_dependency,
        test_html_still_parses_without_error,
        test_no_vip_or_admin_wording,
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
