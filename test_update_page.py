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
