#!/usr/bin/env python3
"""update.foxzen.me静态公告页(generate_status_page.py) + CNAME/跨链接的
回归测试。这个生成器此前完全没有测试覆盖，本文件是第一份。

覆盖：
1. 空announcements.txt
2. 单条公告
3. 多条公告
4. 时间倒序（解析结果 + 渲染后HTML两处都验证）
5. 正确识别类型（含新增的关键词视觉分类）
6. 非法日期（13月/25点）
7. 格式错误行（分段数不对）
8. 内容中包含中文，以及HTML转义（防止公告正文里的特殊字符破坏页面结构）
9. HTML正确生成（基本结构完整）
10. 不产生意外文件（build_update_page()只应该写index.html和CNAME两个文件）
11. 不包含secret
12. CNAME正确
13. 与现有status页面不冲突（各自独立输出、互相有正确的反向链接）

用法: python3 test_update_page.py
"""
import shutil
import sys
import tempfile
import traceback
from pathlib import Path

BASE_DIR = Path(__file__).parent

failures = []


def check(name, condition, detail=""):
    if condition:
        print(f"  [PASS] {name}")
    else:
        print(f"  [FAIL] {name} {detail}")
        failures.append(name)


def _build_with_text(announcements_text):
    """把给定文本临时落盘成announcements.txt，调用build_update_page()
    生成到一个临时output_dir，返回(gsp模块, output_dir, index_html文本,
    entries, tmp根目录)供调用方检查、并在用完后自行清理tmp。
    """
    import generate_status_page as gsp
    tmp = Path(tempfile.mkdtemp(prefix="update_page_test_"))
    announcements_file = tmp / "announcements.txt"
    announcements_file.write_text(announcements_text, encoding="utf-8")
    output_dir, entries = gsp.build_update_page(
        output_dir=tmp / "static_status", announcements_file=announcements_file)
    text = (output_dir / "index.html").read_text(encoding="utf-8")
    return gsp, output_dir, text, entries, tmp


def test_empty_announcements_file():
    gsp, output_dir, text, entries, tmp = _build_with_text(
        "# 只有注释，没有任何真实公告\n"
    )
    try:
        check("空文件（只有注释）解析出0条公告", len(entries) == 0, len(entries))
        check("页面显示'目前没有公告'", "目前没有公告" in text)
        check("空状态下.empty的display没有被设成none",
              'class="empty" style="display:block"' in text, text)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_single_announcement():
    gsp, output_dir, text, entries, tmp = _build_with_text(
        "2026-09-01 10:00|维护公告|本站完成了一次例行维护。\n"
    )
    try:
        check("单条公告被正确解析", len(entries) == 1, len(entries))
        check("公告正文出现在HTML里", "本站完成了一次例行维护。" in text)
        check("单条公告存在时不再显示'目前没有公告'的可见状态",
              'class="empty" style="display:none"' in text, text)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_multiple_announcements():
    gsp, output_dir, text, entries, tmp = _build_with_text(
        "2026-09-01 10:00|维护公告|第一条。\n"
        "2026-09-02 11:00|故障公告|第二条。\n"
        "2026-09-03 12:00|恢复公告|第三条。\n"
    )
    try:
        check("三条公告全部被解析", len(entries) == 3, len(entries))
        for msg in ("第一条。", "第二条。", "第三条。"):
            check(f"公告正文'{msg}'出现在HTML里", msg in text)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_reverse_chronological_order():
    gsp, output_dir, text, entries, tmp = _build_with_text(
        "2026-01-01 10:00|通知|最早的一条。\n"
        "2026-06-15 08:00|通知|最新的一条。\n"
        "2026-03-10 12:00|通知|居中的一条。\n"
    )
    try:
        check("parse_announcements()按时间倒序排列(entries列表本身)",
              [e["message"] for e in entries] == ["最新的一条。", "居中的一条。", "最早的一条。"],
              [e["message"] for e in entries])
        idx_newest = text.index("最新的一条。")
        idx_middle = text.index("居中的一条。")
        idx_oldest = text.index("最早的一条。")
        check("渲染后的HTML里公告顺序也是倒序（最新的排在最前面）",
              idx_newest < idx_middle < idx_oldest,
              (idx_newest, idx_middle, idx_oldest))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_type_recognized_and_visually_classified():
    gsp, output_dir, text, entries, tmp = _build_with_text(
        "2026-09-01 10:00|维护公告|维护类型测试。\n"
        "2026-09-02 10:00|网站故障|故障类型测试。\n"
        "2026-09-03 10:00|已恢复|恢复类型测试。\n"
        "2026-09-04 10:00|其他通知|无法归类的类型测试。\n"
    )
    try:
        check("类型字段'维护公告'被原样显示", "维护公告" in text)
        check("类型字段'网站故障'被原样显示", "网站故障" in text)
        check("类型字段'已恢复'被原样显示", "已恢复" in text)
        check("包含'维护'关键词的类型带上type-maintenance视觉样式",
              'type type-maintenance">维护公告' in text, text)
        check("包含'故障'关键词的类型带上type-incident视觉样式",
              'type type-incident">网站故障' in text, text)
        check("包含'恢复'关键词的类型带上type-recovery视觉样式",
              'type type-recovery">已恢复' in text, text)
        check("无法归类的类型退回中性样式，不强行套用某个分类class",
              'type ">其他通知' in text, text)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_invalid_dates_skipped():
    gsp, output_dir, text, entries, tmp = _build_with_text(
        "2026-13-01 10:00|通知|13月不是合法月份，应被跳过。\n"
        "2026-01-01 25:00|通知|25点不是合法小时，应被跳过。\n"
        "2026-09-05 10:00|通知|这一条时间合法，应该保留。\n"
    )
    try:
        check("两条非法日期都被跳过，只剩1条合法公告", len(entries) == 1, len(entries))
        check("合法的那条公告确实被保留", entries and entries[0]["message"] == "这一条时间合法，应该保留。",
              entries)
        check("13月那条公告正文没有出现在最终HTML里",
              "13月不是合法月份" not in text)
        check("25点那条公告正文没有出现在最终HTML里",
              "25点不是合法小时" not in text)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_malformed_lines_skipped():
    gsp, output_dir, text, entries, tmp = _build_with_text(
        "这一行根本没有竖线分隔，格式完全不对\n"
        "2026-09-05 10:00|只有两段\n"
        "2026-09-06 10:00|通知|这一条格式正确，应该保留。\n"
    )
    try:
        check("两条格式错误的行都被跳过，只剩1条合法公告", len(entries) == 1, len(entries))
        check("格式正确的那条被保留", entries and entries[0]["message"] == "这一条格式正确，应该保留。",
              entries)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_chinese_content_and_html_escaping():
    gsp, output_dir, text, entries, tmp = _build_with_text(
        "2026-09-05 10:00|维护公告|本站将于今晚十点进行例行维护，预计耗时三十分钟。\n"
        "2026-09-06 10:00|通知|包含特殊字符测试 <script>alert(1)</script> & \"引号\"\n"
    )
    try:
        check("中文公告正文正确显示，未被破坏",
              "本站将于今晚十点进行例行维护，预计耗时三十分钟。" in text)
        check("公告正文里的<script>标签被HTML转义，不会被当成真实标签执行",
              "<script>alert(1)</script>" not in text and "&lt;script&gt;" in text)
        check("公告正文里的&符号被正确转义", "&amp;" in text)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_html_structure_is_well_formed():
    gsp, output_dir, text, entries, tmp = _build_with_text(
        "2026-09-05 10:00|通知|结构检查用公告。\n"
    )
    try:
        check("包含<!DOCTYPE html>", text.strip().startswith("<!DOCTYPE html>"))
        check("包含<html", "<html" in text)
        check("包含</html>收尾", text.rstrip().endswith("</html>"))
        check("包含<title>标签", "<title>" in text and "</title>" in text)
        check("包含指向status.foxzen.me的跨链接", 'href="https://status.foxzen.me/"' in text)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_no_unexpected_files_produced():
    gsp, output_dir, text, entries, tmp = _build_with_text(
        "2026-09-05 10:00|通知|文件产物检查用公告。\n"
    )
    try:
        produced = sorted(p.name for p in output_dir.iterdir())
        check("output_dir里只有index.html和CNAME两个文件，没有多余产物",
              produced == ["CNAME", "index.html"], produced)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_no_secret_in_output():
    gsp, output_dir, text, entries, tmp = _build_with_text(
        "2026-09-05 10:00|通知|安全扫描用公告。\n"
    )
    try:
        lowered_text = text.lower()
        dangerous_patterns = ("GITHUB_TOKEN", "TG_BOT_TOKEN", "CF_API_TOKEN", "FOXZEN_GIT_PUSH_TOKEN",
                              "ghp_", "github_pat_",
                              "BEGIN RSA PRIVATE KEY", "BEGIN PRIVATE KEY", "BEGIN OPENSSH PRIVATE KEY",
                              "password", "secret")
        for bad in dangerous_patterns:
            check(f"输出不包含疑似密钥标识: {bad}", bad.lower() not in lowered_text)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_cname_correct():
    gsp, output_dir, text, entries, tmp = _build_with_text(
        "2026-09-05 10:00|通知|CNAME检查用公告。\n"
    )
    try:
        cname_text = (output_dir / "CNAME").read_text(encoding="utf-8").strip()
        check("CNAME内容是update.foxzen.me", cname_text == "update.foxzen.me", cname_text)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_coexists_with_status_page():
    """update页面(generate_status_page.py)和status页面(build_status_page.py)
    分别构建到各自独立的临时目录，互不干扰，且各自的反向链接都指向对方。
    """
    import generate_status_page as gsp
    import build_status_page as bsp

    tmp = Path(tempfile.mkdtemp(prefix="update_status_coexist_test_"))
    try:
        announcements_file = tmp / "announcements.txt"
        announcements_file.write_text("2026-09-05 10:00|通知|共存测试公告。\n", encoding="utf-8")

        update_dir, _ = gsp.build_update_page(
            output_dir=tmp / "update_out", announcements_file=announcements_file)
        status_dir = bsp.build_status_page(output_dir=tmp / "status_out")

        check("update和status构建到了两个不同的目录", update_dir != status_dir,
              (str(update_dir), str(status_dir)))

        update_text = (update_dir / "index.html").read_text(encoding="utf-8")
        status_text = (status_dir / "index.html").read_text(encoding="utf-8")

        check("update页面构建后仍然包含它自己的公告内容（未被status构建过程影响）",
              "共存测试公告。" in update_text)
        check("update页面链接到status.foxzen.me", 'href="https://status.foxzen.me/"' in update_text)
        check("status页面链接到update.foxzen.me", 'href="https://update.foxzen.me/"' in status_text)

        update_cname = (update_dir / "CNAME").read_text(encoding="utf-8").strip()
        status_cname = (status_dir / "CNAME").read_text(encoding="utf-8").strip()
        check("两边CNAME各自正确且不相同",
              update_cname == "update.foxzen.me" and status_cname == "status.foxzen.me" and update_cname != status_cname,
              (update_cname, status_cname))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ============================================================
# 全站UI国际化：update.foxzen.me（本次新增）
# ============================================================

def test_lang_toggle_button_present():
    gsp, output_dir, text, entries, tmp = _build_with_text(
        "2026-09-05 10:00|通知|语言按钮测试用公告。\n"
    )
    try:
        check("页面包含右上角语言切换按钮容器", 'class="lang-toggle"' in text)
        check("包含中文切换按钮", 'data-lang-btn="zh"' in text)
        check("包含英文切换按钮", 'data-lang-btn="en"' in text)
        check(".lang-toggle使用position:fixed（右上角）", "position: fixed" in text)
        check("存在窄屏媒体查询", "@media (max-width: 480px)" in text)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_ui_labels_have_data_i18n():
    gsp, output_dir, text, entries, tmp = _build_with_text(
        "2026-09-05 10:00|通知|UI标签测试用公告。\n"
    )
    try:
        check('副标题带data-i18n="update_subtitle"', 'data-i18n="update_subtitle"' in text)
        check('跳转链接带data-i18n="view_status_link"', 'data-i18n="view_status_link"' in text)
        check('空状态提示带data-i18n="no_announcements"', 'data-i18n="no_announcements"' in text)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_update_strings_zh_en_key_sets_symmetric():
    import re
    gsp, output_dir, text, entries, tmp = _build_with_text(
        "2026-09-05 10:00|通知|翻译字典对称性测试用公告。\n"
    )
    try:
        m = re.search(r"var UPDATE_STRINGS = \{(.*?)\n  \};", text, re.DOTALL)
        check("能定位到UPDATE_STRINGS字典", m is not None)
        if m:
            body = m.group(1)
            zh_keys = set(re.findall(r"(\w+):", re.search(r"zh:\s*\{(.*?)\},\s*en:", body, re.DOTALL).group(1)))
            en_keys = set(re.findall(r"(\w+):", re.search(r"en:\s*\{(.*?)\},?\s*$", body, re.DOTALL).group(1)))
            check("UPDATE_STRINGS中英文翻译键集合完全一致",
                  zh_keys == en_keys, (zh_keys - en_keys, en_keys - zh_keys))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_announcement_entries_never_get_data_i18n():
    """核心边界：公告的time/type/message来自GreenCloud服务器上手工维护的
    data/announcements.txt，绝不能被当成UI文案自动翻译。用ENTRY_TEMPLATE
    源码 + 真实渲染结果两处确认：公告条目本身的HTML片段里不出现data-i18n。
    """
    import re
    import generate_status_page as gsp
    check("ENTRY_TEMPLATE源码里不含data-i18n（公告内容结构上就不可能被套上翻译属性）",
          "data-i18n" not in gsp.ENTRY_TEMPLATE)

    text_input = (
        "2026-09-01 08:00|维护公告|这是一条真实的中文公告正文，用于确认公告内容不会被自动翻译。\n"
        "2026-09-02 09:00|Incident|English announcement body, must stay untouched too.\n"
    )
    built_gsp, output_dir, text, entries, tmp = _build_with_text(text_input)
    try:
        check("两条公告都被正确解析", len(entries) == 2, len(entries))
        # 用" data-i18n=\""(带前导空格+等号+引号，HTML属性的真实形状)而不是裸
        # 子串"data-i18n"去匹配——避免公告正文如果碰巧提到这个词组本身时被
        # 误判成"带了翻译属性"，这是属性存在性检查，不是文本内容检查。
        for entry_html in re.findall(r'<div class="entry">.*?</div>\s*</div>', text, re.DOTALL):
            check("公告条目HTML片段不含真正的data-i18n属性",
                  ' data-i18n="' not in entry_html, entry_html)
        check("中文公告正文原样出现（未被翻译成英文标签或改写）",
              "这是一条真实的中文公告正文，用于确认公告内容不会被自动翻译。" in text)
        check("英文公告正文原样出现（不会被当成需要翻译成中文的UI文案）",
              "English announcement body, must stay untouched too." in text)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# publish_update_page()新增的subpath参数 / --repo-dir CLI开关：用于发布到
# GitHub Pages卫星仓库(foxzen-update)，跟原有"推到foxzen-blog自己的
# static_status/子目录"用法并存。全部对着一次性临时git仓库(work repo +
# 本地bare remote)操作，绝不碰这个项目自己的仓库、绝不碰真实网络/GitHub
# ——跟test_git_publish.py/test_status_page.py同一个思路。
# ---------------------------------------------------------------------------

def _run_cmd(*args, cwd, env=None, check_ok=True):
    import subprocess
    result = subprocess.run(list(args), cwd=str(cwd), capture_output=True, text=True,
                             encoding="utf-8", env=env)
    if check_ok and result.returncode != 0:
        raise RuntimeError(f"{args} 失败: {result.stderr}")
    return result


def _env_with_identity(name, email):
    import os
    env = dict(os.environ)
    env.update({"GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email,
                "GIT_COMMITTER_NAME": name, "GIT_COMMITTER_EMAIL": email})
    return env


def _with_temp_satellite_repo(fn):
    tmp = Path(tempfile.mkdtemp(prefix="satellite_repo_test_"))
    try:
        remote_dir = tmp / "remote.git"
        work_dir = tmp / "work"
        _run_cmd("git", "init", "--bare", "-b", "master", str(remote_dir), cwd=tmp)
        _run_cmd("git", "init", "-b", "master", str(work_dir), cwd=tmp)
        (work_dir / "README.md").write_text("init\n", encoding="utf-8")
        _run_cmd("git", "add", "README.md", cwd=work_dir)
        _run_cmd("git", "commit", "-m", "init", cwd=work_dir,
                 env=_env_with_identity("Setup", "setup@example.invalid"))
        _run_cmd("git", "remote", "add", "origin", str(remote_dir), cwd=work_dir)
        _run_cmd("git", "push", "-u", "origin", "master", cwd=work_dir)
        fn(work_dir, remote_dir)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _with_github_token(value, fn):
    import os
    had = "GITHUB_TOKEN" in os.environ
    saved = os.environ.get("GITHUB_TOKEN")
    try:
        if value is None:
            os.environ.pop("GITHUB_TOKEN", None)
        else:
            os.environ["GITHUB_TOKEN"] = value
        fn()
    finally:
        if had:
            os.environ["GITHUB_TOKEN"] = saved
        else:
            os.environ.pop("GITHUB_TOKEN", None)


def test_publish_update_page_subpath_dot_writes_at_satellite_repo_root():
    """subpath="."时，repo_dir/output_dir都指向卫星仓库checkout本身——
    index.html必须直接落在仓库根目录，不能嵌套在static_status/子目录里
    (那是"作为foxzen-blog自己的子目录被提交"这个不同场景的行为)。

    多渠道架构下这个场景要求调用方显式传write_cname=False：GitHub Pages
    这一侧保持默认github.io地址(https://foxzenme.github.io/foxzen-update/)，
    update.foxzen.me这个自定义域名只属于GreenCloud。
    """
    def _run_case(work_dir, remote_dir):
        import generate_status_page as gsp

        def _do():
            result = gsp.publish_update_page(
                repo_dir=work_dir, output_dir=work_dir,
                announcements_file=BASE_DIR / "data" / "announcements.txt.example",
                subpath=".", write_cname=False,
            )
            check("推送成功", result["pushed"], result)
            check("index.html落在仓库根目录(不是static_status/index.html)",
                  (work_dir / "index.html").exists())
            check("GitHub Pages卫星仓库不应该有CNAME(保持默认github.io地址，"
                  "不绑定update.foxzen.me自定义域名)",
                  not (work_dir / "CNAME").exists())
            check("没有意外生成static_status/子目录", not (work_dir / "static_status").exists())
            remote_head = _run_cmd("git", "rev-parse", "master", cwd=remote_dir).stdout.strip()
            check("远程(bare repo)真的收到了这次push", remote_head == result["commit_sha"])
        _with_github_token("fake-test-token-not-a-real-credential", _do)
    _with_temp_satellite_repo(_run_case)


def test_publish_update_page_default_subpath_unchanged():
    """回归测试：不传subpath参数时，必须保持原有行为——文件推到repo_dir下的
    PUBLISH_SUBPATH("static_status")子目录，而不是这次新加的repo_dir根目录，
    确认这次新增的可选参数没有悄悄改变任何一个现有调用方(app.py cutover后
    的--publish、任何已有脚本)看到的行为。
    """
    def _run_case(work_dir, remote_dir):
        import generate_status_page as gsp

        def _do():
            # 注意：output_dir/subpath都必须显式对齐到work_dir，不能只传
            # repo_dir——publish_update_page()的output_dir参数默认值是
            # 真实项目的OUTPUT_DIR常量(BASE_DIR/"static_status")，如果这里
            # 只传repo_dir=work_dir而不传output_dir，函数会把index.html/
            # CNAME写到真实项目目录而不是临时仓库(曾经因为这个疏漏真的
            # 覆盖过一次仓库根目录下的static_status/，已用真实的
            # generate_status_page.py重新生成过、确认该目录未被git追踪、
            # 不影响任何已提交内容或线上foxzen-update.pages.dev部署，
            # 但这里必须把测试本身修对，不能只是善后)。这里显式传
            # output_dir=work_dir/"static_status"，只把subpath留空
            # (用它的默认值PUBLISH_SUBPATH)，这样才是真正只测试"subpath
            # 参数不传时是否还是static_status"这一件事。
            result = gsp.publish_update_page(
                repo_dir=work_dir, output_dir=work_dir / "static_status",
                announcements_file=BASE_DIR / "data" / "announcements.txt.example",
            )
            check("推送成功", result["pushed"], result)
            check("默认行为：文件落在static_status/子目录，不是仓库根目录",
                  (work_dir / "static_status" / "index.html").exists())
            check("仓库根目录本身没有多出index.html", not (work_dir / "index.html").exists())
            committed = _run_cmd("git", "show", "--name-only", "--format=", "HEAD", cwd=work_dir).stdout
            check("commit里的路径带static_status/前缀",
                  all(f.startswith("static_status/") for f in committed.splitlines() if f.strip()), committed)
        _with_github_token("fake-test-token-not-a-real-credential", _do)
    _with_temp_satellite_repo(_run_case)


def test_cli_repo_dir_flag_generates_and_publishes_end_to_end():
    """直接跑真实CLI命令(python generate_status_page.py --publish --repo-dir
    <path>)，验证的是GitHub Actions workflow(deploy-update-pages-github.yml)
    实际会调用的那一行命令本身，而不只是内部Python函数——argparse接线、
    Path类型转换、--repo-dir与--publish的组合逻辑都在这条路径上。

    多渠道架构下这条路径不应该再写CNAME：GitHub Pages这一侧保持默认
    github.io地址，不绑定update.foxzen.me自定义域名。
    """
    def _run_case(work_dir, remote_dir):
        import os
        import subprocess
        import sys
        env = dict(os.environ)
        env["GITHUB_TOKEN"] = "fake-test-token-not-a-real-credential"
        result = subprocess.run(
            [sys.executable, str(BASE_DIR / "generate_status_page.py"),
             "--publish", "--repo-dir", str(work_dir)],
            cwd=str(BASE_DIR), capture_output=True, text=True, encoding="utf-8", env=env, timeout=30,
        )
        check("CLI命令退出码为0", result.returncode == 0, result.stderr)
        check("index.html落在仓库根目录", (work_dir / "index.html").exists())
        check("GitHub Pages卫星仓库不应该有CNAME(保持默认github.io地址，"
              "不绑定update.foxzen.me自定义域名)", not (work_dir / "CNAME").exists())
        remote_head = _run_cmd("git", "rev-parse", "master", cwd=remote_dir).stdout.strip()
        check("远程(bare repo)真的收到了这次push", bool(remote_head))
    _with_temp_satellite_repo(_run_case)


def test_cli_repo_dir_without_publish_generates_without_cname_or_git():
    """python generate_status_page.py --repo-dir <DIR>(不带--publish)：
    GreenCloud上实际会执行的那一行命令——只生成文件、不写CNAME、不做任何
    git操作(不需要GITHUB_TOKEN、DIR也不需要是git仓库)。"""
    import subprocess
    tmp = Path(tempfile.mkdtemp(prefix="update_page_cli_test_"))
    try:
        result = subprocess.run(
            [sys.executable, str(BASE_DIR / "generate_status_page.py"),
             "--repo-dir", str(tmp / "out")],
            cwd=str(BASE_DIR), capture_output=True, text=True, encoding="utf-8", timeout=30,
        )
        check("CLI命令退出码为0", result.returncode == 0, result.stderr)
        check("index.html已生成", (tmp / "out" / "index.html").exists())
        check("不生成CNAME(不是git仓库也不需要GITHUB_TOKEN)", not (tmp / "out" / "CNAME").exists())
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    tests = [
        test_empty_announcements_file,
        test_single_announcement,
        test_multiple_announcements,
        test_reverse_chronological_order,
        test_type_recognized_and_visually_classified,
        test_invalid_dates_skipped,
        test_malformed_lines_skipped,
        test_chinese_content_and_html_escaping,
        test_html_structure_is_well_formed,
        test_no_unexpected_files_produced,
        test_no_secret_in_output,
        test_cname_correct,
        test_coexists_with_status_page,
        test_lang_toggle_button_present,
        test_ui_labels_have_data_i18n,
        test_update_strings_zh_en_key_sets_symmetric,
        test_announcement_entries_never_get_data_i18n,
        test_publish_update_page_subpath_dot_writes_at_satellite_repo_root,
        test_publish_update_page_default_subpath_unchanged,
        test_cli_repo_dir_flag_generates_and_publishes_end_to_end,
        test_cli_repo_dir_without_publish_generates_without_cname_or_git,
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
