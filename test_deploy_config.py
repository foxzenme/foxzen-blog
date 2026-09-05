#!/usr/bin/env python3
"""部署配置的静态检查：systemd service文件/nginx配置本身不会在这里真的被
systemd/nginx加载执行（不能SSH/VPS验证真实生效，也不假设VPS上存在这个
仓库之外的配置），只做文本层面的断言，确保两个具体的、审查中发现的问题
不会在未来被无意中改回去：

- B4: gunicorn的--timeout必须覆盖refresh pipeline的真实最坏执行时间，
  不能依赖gunicorn默认的30秒。
- S7: /api/refresh/的nginx层限流必须存在，且只影响mirror/backup两个
  Flask入口，不能改变download.foxzen.me/foxzen.me等其它站点的行为。

用法: python3 test_deploy_config.py
"""
import re
import sys
import traceback
from pathlib import Path

BASE_DIR = Path(__file__).parent
SYSTEMD_FILE = BASE_DIR / "systemd" / "blog-mirror-api.service"
NGINX_FILE = BASE_DIR / "nginx-conf" / "default.conf"

failures = []


def check(name, condition, detail=""):
    if condition:
        print(f"  [PASS] {name}")
    else:
        print(f"  [FAIL] {name} {detail}")
        failures.append(name)


def test_gunicorn_timeout_covers_worst_case_refresh_duration():
    text = SYSTEMD_FILE.read_text(encoding="utf-8")
    exec_start_lines = [ln for ln in text.splitlines() if "ExecStart=" in ln and "gunicorn" in ln]
    check("systemd文件里确实有gunicorn的ExecStart行", len(exec_start_lines) == 1, exec_start_lines)
    if not exec_start_lines:
        return
    exec_start = exec_start_lines[0]

    m = re.search(r"--timeout[= ](\d+)", exec_start)
    check("ExecStart显式指定了--timeout，不再依赖gunicorn默认的30秒", m is not None, exec_start)
    if m:
        timeout_value = int(m.group(1))
        # 与app.py里GIT_PUBLISH_STALE_SECONDS注释里的推导保持一致：
        # content_fetch(300) + git_publish临界区最坏情况约170 +
        # GitHub Actions有界同步等待(90) = 560，必须明显小于这里的timeout。
        worst_case_seconds = 560
        check(f"--timeout={timeout_value}s 明显大于已知最坏情况约{worst_case_seconds}s（留有余量，"
              f"不是刚好卡在临界值）",
              timeout_value > worst_case_seconds + 100, timeout_value)


def _extract_server_blocks(text):
    """粗粒度地按花括号配对切出每个顶层server{...}块。用配对计数而不是
    单纯的非贪婪正则，是因为块内部（location等）还有嵌套的花括号，非贪婪
    正则会在第一个内层"}"处提前截断。
    """
    blocks = []
    idx = 0
    while True:
        start = text.find("server {", idx)
        if start == -1:
            break
        depth = 0
        i = start
        while i < len(text):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    blocks.append(text[start:i + 1])
                    break
            i += 1
        idx = i + 1
    return blocks


def test_nginx_refresh_api_has_dedicated_rate_limit():
    text = NGINX_FILE.read_text(encoding="utf-8")

    check("存在limit_req_zone定义(在文件顶层，不在任何server块内部，"
          "被上层nginx.conf include进http{}上下文)",
          bool(re.search(r"^\s*limit_req_zone\b", text, re.MULTILINE)), text[:200])

    server_blocks = _extract_server_blocks(text)
    check("确实解析出多个server块", len(server_blocks) >= 4, len(server_blocks))

    mirror_block = next((b for b in server_blocks
                          if "server_name mirror.foxzen.me;" in b and "listen 443" in b), None)
    backup_block = next((b for b in server_blocks
                          if "server_name backup.foxzen.me;" in b and "listen 443" in b), None)
    check("找到mirror.foxzen.me的443 server块", mirror_block is not None)
    check("找到backup.foxzen.me的443 server块", backup_block is not None)

    for name, block in (("mirror", mirror_block), ("backup", backup_block)):
        if block is None:
            continue
        m = re.search(r"location\s+/api/refresh/\s*\{.*?\n\s*\}", block, re.DOTALL)
        check(f"{name}.foxzen.me存在专门针对/api/refresh/的location块", m is not None, block)
        if m:
            refresh_location = m.group(0)
            check(f"{name}的/api/refresh/ location块里配置了limit_req",
                  "limit_req " in refresh_location, refresh_location)
            check(f"{name}的/api/refresh/ location块里配置了limit_req_status(与应用层429保持一致)",
                  "limit_req_status" in refresh_location, refresh_location)

    other_blocks = [b for b in server_blocks
                    if "server_name mirror.foxzen.me;" not in b and "server_name backup.foxzen.me;" not in b]
    check("其它站点(download.foxzen.me/foxzen.me等)的server块里没有出现/api/refresh/相关配置"
          "（限流范围严格限定在mirror/backup，没有意外影响其它站点）",
          all("/api/refresh/" not in b for b in other_blocks), len(other_blocks))


def main():
    tests = [
        test_gunicorn_timeout_covers_worst_case_refresh_duration,
        test_nginx_refresh_api_has_dedicated_rate_limit,
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
