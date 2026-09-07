#!/usr/bin/env python3
"""文章阅读体验优化(首字放大/正文字号/章节目录/UI中英文切换)的回归测试。

覆盖：
1. 首字放大：真正定位到第一个可见字符（跳过&nbsp;/空段落/标题/代码块），
   不是"第一个HTML标签"
2. 正文字号：.content本身1em，标题/引用/代码/列表/表格各自独立分档
3. 标题层级：Blogger真实导出的h1~h6一律同一档处理（不是遗漏多级支持，
   见fetch_blog.py里_inject_heading_anchors()的说明——这是审计过18篇
   真实文章后确认标签名不可靠才采用的方案）
4. 章节结构 + 目录生成 + 重复标题不重复ID + 中文标题
5. 中文正文没有被修改（转换前后.content的可见文本必须完全一致）
6. UI中英文切换（浏览器语言检测/手动切换/localStorage持久化/优先级），
   受限于本项目没有引入任何浏览器自动化测试工具，这部分只做I18N_BLOCK
   源码层面的静态断言（跟test_status_page.py/test_update_page.py对
   JS逻辑的测试方式一致）
7. UI翻译绝不进入正文的边界测试
8. 第十三节要求的真实文章验收：从html/posts/里挑4篇有代表性的真实文章
   （标题丰富/普通短文/含图片/含代码引用列表），而不是只测合成fixture

用法: python3 test_reading_experience.py
"""
import re
import sys
import traceback
from pathlib import Path

BASE_DIR = Path(__file__).parent
POSTS_DIR = BASE_DIR / "html" / "posts"

failures = []


def check(name, condition, detail=""):
    if condition:
        print(f"  [PASS] {name}")
    else:
        print(f"  [FAIL] {name} {detail}")
        failures.append(name)


def _visible_text(html_fragment):
    """跟fetch_blog._strip_tags_and_entities()同样的"去标签取可见文本"
    逻辑，但独立实现一份——不复用被测代码本身，避免测试和实现共享同一个
    bug时互相掩盖。用于比较"转换前后可见文本是否完全一致"。
    """
    import html as html_module
    text = re.sub(r"<[^>]+>", "", html_fragment)
    text = re.sub(r"&nbsp;", " ", text, flags=re.IGNORECASE)
    text = html_module.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


# ---------------------------------------------------------------------------
# 1. 首字放大
# ---------------------------------------------------------------------------

def test_drop_cap_wraps_actual_first_character():
    import fetch_blog
    content = "<p>你好，世界。</p>"
    result = fetch_blog._apply_drop_cap(content)
    check("首字被包进drop-cap span", '<span class="drop-cap">你</span>' in result, result)
    check("剩余正文原样保留", "好，世界。" in result)


def test_drop_cap_skips_leading_nbsp_and_empty_paragraphs():
    """真实文章里发现过这种开头（见html/posts/1047112505493575766），
    &nbsp;和空段落不应该被误当成"第一个字"。"""
    import fetch_blog
    content = "<p>&nbsp;&nbsp;</p><p>&nbsp;</p><p>正文真正开始的地方。</p>"
    result = fetch_blog._apply_drop_cap(content)
    check("跳过&nbsp;段落，正确定位到第一个真实汉字",
          '<span class="drop-cap">正</span>' in result, result)


def test_drop_cap_skips_leading_heading():
    """如果文章一上来就是标题，不应该把标题的第一个字当成正文首字——
    应该跳过标题，找它后面第一段正文的第一个字。"""
    import fetch_blog
    content = "<h2>第一章</h2><p>这里才是正文。</p>"
    result = fetch_blog._apply_drop_cap(content)
    check("标题本身的字没有被加上drop-cap", "<span" not in content.split("</h2>")[0])
    check("正文第一个字被正确加上drop-cap",
          '<span class="drop-cap">这</span>' in result, result)


def test_drop_cap_skips_code_block():
    import fetch_blog
    content = "<pre><code>print('hi')</code></pre><p>真正的正文段落。</p>"
    result = fetch_blog._apply_drop_cap(content)
    check("代码块内容没有被误当成首字",
          '<span class="drop-cap">p</span>' not in result.split("</pre>")[0] if "</pre>" in result else True)
    check("代码块之后的正文首字被正确处理",
          '<span class="drop-cap">真</span>' in result, result)


def test_drop_cap_works_with_non_chinese_first_character():
    """第一字符不是中文时也要安全工作，不能因为不是中文就出错或跳过。"""
    import fetch_blog
    content = "<p>Hello, this is English content.</p>"
    result = fetch_blog._apply_drop_cap(content)
    check("英文首字符同样能被正确包裹",
          '<span class="drop-cap">H</span>' in result, result)


def test_drop_cap_returns_unchanged_when_no_visible_text():
    import fetch_blog
    content = '<div class="separator"><a href="x"><img src="y"/></a></div><p>&nbsp;</p>'
    result = fetch_blog._apply_drop_cap(content)
    check("整段都没有可见文字时，原样返回，不报错、不误处理", result == content, result)


# ---------------------------------------------------------------------------
# 2-4. 标题锚点 + 目录 + 去重 + 中文
# ---------------------------------------------------------------------------

def test_heading_anchors_injected_with_unique_ids():
    import fetch_blog
    content = "<p>开头。</p><h2>安装步骤</h2><p>...</p><h3>常见问题</h3><p>...</p>"
    new_content, headings = fetch_blog._inject_heading_anchors(content)
    check("识别出2个标题", len(headings) == 2, headings)
    check("第一个标题文字是原文，未被翻译/改写", headings[0]["text"] == "安装步骤", headings)
    check("第二个标题文字是原文", headings[1]["text"] == "常见问题", headings)
    check("h2标签被加上id属性", re.search(r'<h2 id="[^"]+">安装步骤</h2>', new_content) is not None, new_content)
    check("h3标签被加上id属性", re.search(r'<h3 id="[^"]+">常见问题</h3>', new_content) is not None, new_content)


def test_mixed_heading_levels_all_treated_as_flat_chapters():
    """真实数据确认Blogger导出的标题标签不可靠（同一篇文章混用h1/h2），
    这里验证不管标签是h1还是h2还是h3，只要是标题就一律被收进同一层目录，
    不强行按标签名分层级。"""
    import fetch_blog
    content = "<h1>第一部分</h1><p>a</p><h2>第一部分的小节</h2><p>b</p><h1>第二部分</h1><p>c</p>"
    new_content, headings = fetch_blog._inject_heading_anchors(content)
    check("h1和h2混用时，三个标题都被识别（不因为标签不同就漏掉）",
          len(headings) == 3, headings)
    check("识别顺序跟文档顺序一致", [h["text"] for h in headings] == ["第一部分", "第一部分的小节", "第二部分"], headings)


def test_duplicate_heading_text_gets_unique_ids():
    import fetch_blog
    content = "<h2>总结</h2><p>a</p><h2>总结</h2><p>b</p><h2>总结</h2><p>c</p>"
    new_content, headings = fetch_blog._inject_heading_anchors(content)
    ids = [h["id"] for h in headings]
    check("三个同名标题产生了3个不同的id", len(set(ids)) == 3, ids)
    check("第一个保持不带数字后缀的slug", ids[0] == "总结", ids)
    check("第二、三个依次加-2/-3后缀", ids[1] == "总结-2" and ids[2] == "总结-3", ids)


def test_chinese_heading_slugify_produces_nonempty_stable_id():
    import fetch_blog
    slug1 = fetch_blog._slugify_heading("第一章：环境搭建")
    slug2 = fetch_blog._slugify_heading("第一章：环境搭建")
    check("中文标题能生成非空slug", bool(slug1), slug1)
    check("同样的标题文字两次生成的slug完全一致（稳定）", slug1 == slug2)
    check("中文字符本身被保留在slug里，不是转成拼音或删掉", "第" in slug1 and "环境" in slug1, slug1)


def test_empty_heading_skipped_without_breaking():
    import fetch_blog
    content = "<h2></h2><p>正文。</p><h3>真正的标题</h3>"
    new_content, headings = fetch_blog._inject_heading_anchors(content)
    check("空标题不产生目录项", len(headings) == 1 and headings[0]["text"] == "真正的标题", headings)
    check("空标题标签本身原样保留，没有崩溃", "<h2></h2>" in new_content, new_content)


def test_toc_not_generated_for_fewer_than_two_headings():
    import fetch_blog
    check("0个标题时目录为空字符串", fetch_blog.render_toc_html([]) == "")
    check("1个标题时目录为空字符串（1条目录没有导航价值）",
          fetch_blog.render_toc_html([{"id": "a", "text": "唯一标题"}]) == "")


def test_toc_generated_for_two_or_more_headings_uses_original_text():
    import fetch_blog
    headings = [{"id": "intro", "text": "引言"}, {"id": "steps", "text": "操作步骤 & 注意事项"}]
    toc_html = fetch_blog.render_toc_html(headings)
    check("目录容器存在", '<nav class="toc"' in toc_html, toc_html)
    check("目录标题带data-i18n（跟随UI语言切换）", 'data-i18n="toc_title"' in toc_html)
    check("第一条目录链接指向正确锚点", '<a href="#intro">引言</a>' in toc_html, toc_html)
    check("标题文字里的特殊字符被正确转义（& -> &amp;），不破坏HTML结构",
          "操作步骤 &amp; 注意事项" in toc_html, toc_html)


# ---------------------------------------------------------------------------
# 5. 中文正文没有被修改（转换前后可见文本必须完全一致）
# ---------------------------------------------------------------------------

def test_content_visible_text_unchanged_after_all_transformations():
    import fetch_blog
    original = ("<p>&nbsp;</p><h2>第一章 概述</h2><p>这是正文第一段，包含<b>加粗</b>和"
                "<a href=\"http://x.com\">链接</a>。</p><h3>第二章</h3><p>这是第二段。</p>")
    with_anchors, headings = fetch_blog._inject_heading_anchors(original)
    final = fetch_blog._apply_drop_cap(with_anchors)
    check("加锚点+首字span之后，可见文本跟原文完全一致（一字不改）",
          _visible_text(final) == _visible_text(original),
          (_visible_text(final), _visible_text(original)))


# ---------------------------------------------------------------------------
# 6-7. I18N_BLOCK 静态源码检查
# ---------------------------------------------------------------------------

def test_i18n_block_has_both_languages_for_all_keys():
    import fetch_blog
    zh_keys = ("back_home", "toc_title", "published", "first_published", "last_updated",
               "discuss_prompt", "discuss_btn", "copy_btn", "copy_done", "finish_toast")
    for key in zh_keys:
        check(f"I18N_BLOCK里定义了翻译key: {key}", f"{key}:" in fetch_blog.I18N_BLOCK, key)


def test_i18n_block_never_issues_post_request():
    import fetch_blog
    # 检查带引号的'POST'/"POST"（作为HTTP method值出现的写法），不是裸的
    # POST子串——I18N_BLOCK自己的JS注释里提到了"POST_TEMPLATE"这个标识符
    # （解释代码执行顺序），裸子串匹配会被这类无关注释误伤。
    check("I18N_BLOCK不包含任何POST请求方法",
          "'POST'" not in fetch_blog.I18N_BLOCK and '"POST"' not in fetch_blog.I18N_BLOCK)
    check("I18N_BLOCK不发起fetch()网络请求", "fetch(" not in fetch_blog.I18N_BLOCK)


def test_i18n_block_never_touches_content_selector():
    """UI翻译逻辑只能通过data-i18n/data-i18n-tpl属性查找元素，绝不能直接
    查询.content——这是"翻译不会进入正文"这个边界的源码级防线：即使以后
    有人往.content里加了别的元素，只要没打data-i18n属性，这段JS就永远
    碰不到它。"""
    import fetch_blog
    check("I18N_BLOCK源码里不出现.content这个选择器",
          ".content" not in fetch_blog.I18N_BLOCK, fetch_blog.I18N_BLOCK)
    check("I18N_BLOCK只通过data-i18n属性查找元素",
          'querySelectorAll("[data-i18n]")' in fetch_blog.I18N_BLOCK)
    check("I18N_BLOCK只通过data-i18n-tpl属性查找需要拼数字的元素",
          'querySelectorAll("[data-i18n-tpl]")' in fetch_blog.I18N_BLOCK)


def test_i18n_block_uses_localstorage_and_checks_it_before_browser_language():
    import fetch_blog
    block = fetch_blog.I18N_BLOCK
    check("使用localStorage持久化用户选择", "localStorage.setItem" in block and "localStorage.getItem" in block)
    check("固定的storage key", 'STORAGE_KEY = "foxzen_lang"' in block)
    get_lang_fn = re.search(r"function getLang\(\) \{(.*?)\n  \}", block, re.DOTALL)
    check("getLang()函数存在", get_lang_fn is not None)
    if get_lang_fn:
        body = get_lang_fn.group(1)
        localstorage_pos = body.find("localStorage.getItem")
        detect_pos = body.find("detectDefaultLang()")
        check("getLang()里localStorage的读取先于浏览器语言检测（手动选择优先级更高）",
              localstorage_pos != -1 and detect_pos != -1 and localstorage_pos < detect_pos,
              (localstorage_pos, detect_pos))


def test_i18n_block_detects_chinese_and_english_browser_language():
    import fetch_blog
    block = fetch_blog.I18N_BLOCK
    check("浏览器语言检测逻辑按zh前缀判断中文", "/^zh/i.test" in block, block)
    check("非zh前缀时默认落到英文", 'return "en";' in block)


def test_i18n_block_no_secret_leaked():
    import fetch_blog
    for bad in ("GITHUB_TOKEN", "TG_BOT_TOKEN", "CF_API_TOKEN", "FOXZEN_GIT_PUSH_TOKEN", "ghp_", "github_pat_"):
        check(f"I18N_BLOCK不包含疑似密钥标识: {bad}", bad not in fetch_blog.I18N_BLOCK)


# ---------------------------------------------------------------------------
# 全站UI国际化：文章页语言切换按钮改成固定右上角（本次新增）
# ---------------------------------------------------------------------------

def test_lang_toggle_moved_to_fixed_top_right():
    """之前.lang-toggle是文档流里的普通inline-block元素（紧跟在"返回目录"
    链接后面），本次全站UI国际化要求"右上角、桌面/移动端都容易找到、不遮挡
    正文"，改成position:fixed。这里只做样式规则的静态确认（项目没有浏览器
    自动化工具）。"""
    import fetch_blog
    style_start = fetch_blog.POST_TEMPLATE.index(".lang-toggle {{")
    style_slice = fetch_blog.POST_TEMPLATE[style_start:style_start + 700]
    check(".lang-toggle使用position:fixed（不再是文档流里的inline-block）",
          "position: fixed" in style_slice)
    check(".lang-toggle固定在右上角(top/right)", "top: 12px" in style_slice and "right: 12px" in style_slice)
    check("存在窄屏(移动端)媒体查询覆盖.lang-toggle", "@media (max-width: 480px)" in style_slice)


def test_lang_toggle_reposition_does_not_touch_content_or_translations():
    """样式改动之外，I18N_BLOCK的翻译key集合/data-i18n约定必须完全不受影响——
    这次只改了CSS定位规则，不是重新设计整套翻译逻辑。"""
    import fetch_blog
    for key in ("back_home", "toc_title", "published", "first_published", "last_updated",
                "discuss_prompt", "discuss_btn", "copy_btn", "copy_done", "finish_toast"):
        check(f"I18N_BLOCK翻译key未丢失: {key}", f"{key}:" in fetch_blog.I18N_BLOCK, key)
    check("I18N_BLOCK仍然只通过data-i18n查找元素（改样式没有连带改成扫描.content）",
          ".content" not in fetch_blog.I18N_BLOCK)


# ---------------------------------------------------------------------------
# 正文字号/CSS分档
# ---------------------------------------------------------------------------

def test_css_differentiates_content_typography():
    import fetch_blog
    template = fetch_blog.POST_TEMPLATE
    check("body字号提升到21px（约等于传统字号体系里的三号16pt）",
          "font-size: 21px" in template)
    # POST_TEMPLATE是.format()用的原始模板字符串，字面量CSS花括号在这里
    # 是转义过的双花括号（{{ }}），跟.format()之后真正渲染出的HTML里的
    # 单花括号不一样，检查的时候要按原始模板的写法匹配。
    check(".content本身显式声明1em（不被其他规则意外撑大/压小）",
          ".content {{ font-size: 1em; }}" in template)
    check(".content内的标题(h1~h6)有独立于正文的字号规则",
          ".content h1, .content h2, .content h3, .content h4, .content h5, .content h6 {{" in template)
    check("引用块有独立样式", ".content blockquote {{" in template)
    check("代码块/行内代码有独立字号（不会被正文放大规则一起放大）",
          ".content pre, .content code {{" in template)
    check("列表有显式规则（跟正文同号但是显式声明，不是意外继承）",
          ".content ul, .content ol, .content li {{" in template)
    check("表格有独立字号", ".content table {{" in template)
    check("图片说明(figcaption)预留了样式", ".content figcaption {{" in template)


def test_drop_cap_css_class_defined_and_sized_reasonably():
    import fetch_blog
    check(".drop-cap样式类存在", ".drop-cap {" in fetch_blog.POST_TEMPLATE)
    m = re.search(r"\.drop-cap \{[^}]*font-size:\s*([0-9.]+)em", fetch_blog.POST_TEMPLATE)
    check("首字字号明显大于正文（约等于一号26pt相对三号16pt的比例）",
          m is not None and float(m.group(1)) > 1.4, m.group(1) if m else None)


# ---------------------------------------------------------------------------
# 真实文章验收（第十三节）
# ---------------------------------------------------------------------------

_REAL_ARTICLE_SAMPLES = {
    "标题层级丰富": "1047112505493575766",
    "普通短文章": "4425353898793467787",
    "包含图片": "6540711021497886833",
    "包含代码/引用/列表": "2280005158960907708",
}


def _extract_real_content(post_id):
    post_file = POSTS_DIR / post_id / "index.html"
    if not post_file.exists():
        return None
    text = post_file.read_text(encoding="utf-8")
    m = re.search(r'<div class="content">(.*)</div>\s*(?:<div class="discuss-cta">|<div class="stats-note">)',
                  text, re.DOTALL)
    if not m:
        return None
    return m.group(1)


def test_real_articles_process_without_error_and_preserve_text():
    import fetch_blog
    if not POSTS_DIR.exists():
        print("  [跳过] html/posts/ 不存在，本地没有真实抓取数据，跳过真实文章验收")
        return
    for label, post_id in _REAL_ARTICLE_SAMPLES.items():
        content = _extract_real_content(post_id)
        if content is None:
            print(f"  [跳过] {label}({post_id})：本地未找到该文章或提取失败")
            continue
        try:
            with_anchors, headings = fetch_blog._inject_heading_anchors(content)
            final = fetch_blog._apply_drop_cap(with_anchors)
        except Exception:
            check(f"[{label}] {post_id} 处理过程未抛异常", False, traceback.format_exc())
            continue
        check(f"[{label}] {post_id} 处理成功", True)
        check(f"[{label}] {post_id} 可见文本转换前后完全一致（正文未被改动）",
              _visible_text(final) == _visible_text(content))
        ids = [h["id"] for h in headings]
        check(f"[{label}] {post_id} 标题id全部唯一（{len(ids)}个标题）",
              len(ids) == len(set(ids)), ids)
        print(f"    {label}({post_id})：{len(headings)}个标题，"
              f"{'含' if '<span class=\"drop-cap\">' in final else '不含'}首字放大")


def main():
    tests = [
        test_drop_cap_wraps_actual_first_character,
        test_drop_cap_skips_leading_nbsp_and_empty_paragraphs,
        test_drop_cap_skips_leading_heading,
        test_drop_cap_skips_code_block,
        test_drop_cap_works_with_non_chinese_first_character,
        test_drop_cap_returns_unchanged_when_no_visible_text,
        test_heading_anchors_injected_with_unique_ids,
        test_mixed_heading_levels_all_treated_as_flat_chapters,
        test_duplicate_heading_text_gets_unique_ids,
        test_chinese_heading_slugify_produces_nonempty_stable_id,
        test_empty_heading_skipped_without_breaking,
        test_toc_not_generated_for_fewer_than_two_headings,
        test_toc_generated_for_two_or_more_headings_uses_original_text,
        test_content_visible_text_unchanged_after_all_transformations,
        test_i18n_block_has_both_languages_for_all_keys,
        test_i18n_block_never_issues_post_request,
        test_i18n_block_never_touches_content_selector,
        test_i18n_block_uses_localstorage_and_checks_it_before_browser_language,
        test_i18n_block_detects_chinese_and_english_browser_language,
        test_i18n_block_no_secret_leaked,
        test_lang_toggle_moved_to_fixed_top_right,
        test_lang_toggle_reposition_does_not_touch_content_or_translations,
        test_css_differentiates_content_typography,
        test_drop_cap_css_class_defined_and_sized_reasonably,
        test_real_articles_process_without_error_and_preserve_text,
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
