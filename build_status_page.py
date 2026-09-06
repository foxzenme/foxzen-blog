#!/usr/bin/env python3
"""生成 status.foxzen.me 的静态外壳页面到 publish_status/，供 GitHub Pages /
Cloudflare Pages 使用（只读平台状态页，不要跟generate_status_page.py搞混——
那个脚本名字里也有"status"，但实际内容和用途是update.foxzen.me的公告页）。

跟generate_status_page.py不同：这里生成的HTML/CSS/JS外壳本身几乎不包含任何
构建时确定的数据——mirror/backup/github/cf/GreenCloud的实际状态全部由
PAGE_TEMPLATE里内嵌的纯JS在访客浏览器加载页面的那一刻现场请求得到。这样
设计是为了满足"即使GreenCloud整体宕机，status页面本身仍然尽量可以打开"这个
核心要求：外壳文件不依赖GreenCloud生成，构建过程本身也不向GreenCloud或
任何第三方发起任何请求，可以在任何一台开发机上离线完成，产物可以直接扔给
GitHub Pages/Cloudflare Pages两边分别托管。

页面只有一个自包含的HTML文件（内联CSS/JS），不拆分成多个文件——不像
publish_build.py的pages-*.js需要被多篇文章/多个host共用，这里只有一个
页面，拆分不会带来任何复用收益，只会增加构建脚本的复杂度。

用法:
    python3 build_status_page.py
    （生成 publish_status/index.html + publish_status/CNAME）

尚未完成、需要人工决定的部分（本次不擅自处理）：
    - status.foxzen.me这个子域名的DNS记录当前已经存在但代理到GreenCloud的
      IP；如果采用这里的纯静态方案，需要在Cloudflare控制台把它改成指向
      GitHub Pages/Cloudflare Pages（本轮不修改DNS）；
    - publish_status/最终推送到哪个GitHub仓库/Cloudflare Pages项目、是否
      需要单独的GitHub Actions workflow，需要你确认后再实现（可以参考
      .github/workflows/pages.yml的现有模式）；
    - app.py里新增的status.foxzen.me CORS白名单，需要跟随代码一起部署到
      GreenCloud（走正常的candidate cutover流程）之后，这个页面从浏览器
      发起的跨域读取才会真正被允许——页面上线前必须确认这一步已经完成，
      否则/api/health和/api/refresh/*/status会被浏览器的CORS拦下来。
"""
from pathlib import Path

BASE_DIR = Path(__file__).parent
OUTPUT_DIR = BASE_DIR / "publish_status"
HOST = "status.foxzen.me"

GITHUB_STATUS_PAGE = "https://www.githubstatus.com/"
CLOUDFLARE_STATUS_PAGE = "https://www.cloudflarestatus.com/"

# 用简单的字符串占位符+replace()而不是str.format()，是因为下面模板里的CSS
# 本身大量使用花括号——.format()会把它们全部当成格式化字段解析，需要把每一个
# "{"/"}"都转义成"{{"/"}}"才能用，既容易漏改又会让CSS变得难读；replace()
# 不care模板里有多少花括号，只精确替换这两个占位符，两个官方状态页URL依然
# 保持"只在Python常量里写一份"这个单一数据源。
PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>FoxZen Status</title>
<style>
  :root { color-scheme: light; }
  * { box-sizing: border-box; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Microsoft YaHei", sans-serif;
    max-width: 720px;
    margin: 40px auto;
    padding: 0 16px 60px;
    line-height: 1.6;
    color: #222;
    background: #fff;
  }
  h1 { font-size: 1.5em; margin-bottom: 4px; }
  .subtitle { color: #666; font-size: 0.9em; margin-bottom: 24px; }
  .browser-note {
    background: #f4f6f8;
    border: 1px solid #dde3e8;
    border-radius: 6px;
    padding: 10px 14px;
    font-size: 0.85em;
    color: #444;
    margin-bottom: 28px;
  }
  h2 {
    font-size: 1.1em;
    border-bottom: 1px solid #ddd;
    padding-bottom: 6px;
    margin-top: 36px;
  }
  .row { border-bottom: 1px solid #eee; padding: 12px 0; }
  .row:last-child { border-bottom: none; }
  .row-head {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 12px;
  }
  .name { font-weight: 600; }
  .badge {
    display: inline-block;
    border-radius: 4px;
    padding: 2px 10px;
    font-size: 0.8em;
    font-weight: 600;
    white-space: nowrap;
  }
  .badge.checking { background: #eee; color: #666; }
  .badge.ok { background: #e3f6e9; color: #1a7f3c; }
  .badge.fail { background: #fbe6e6; color: #b3261e; }
  .badge.unknown { background: #eee; color: #666; }
  .detail { margin-top: 4px; font-size: 0.9em; color: #333; }
  .meta {
    margin-top: 4px;
    font-size: 0.78em;
    color: #888;
    display: flex;
    justify-content: space-between;
    flex-wrap: wrap;
    gap: 8px;
  }
  a { color: #1a5fb4; }
  .official-link { display: inline-block; margin-top: 6px; font-size: 0.9em; }
  .cross-link { margin: 0 0 28px; font-size: 0.9em; }
  footer { margin-top: 48px; font-size: 0.8em; color: #999; }
</style>
</head>
<body>
<h1>FoxZen Status</h1>
<div class="subtitle">status.foxzen.me &mdash; 现在怎么样 (what's the current state)</div>

<div class="browser-note">
  下面所有检查结果都是在你打开这个页面时，由<strong>你的浏览器</strong>实时发起的，不是某个中心化监控服务器上跑出来的结果——
  它反映的是"你的设备现在能不能连上"，而不是一个绝对的全局判断。
  All checks below run live in <strong>your browser</strong> when this page loads. They reflect what your device can currently reach, not a centralized monitoring verdict.
</div>

<p class="cross-link"><a href="https://update.foxzen.me/">View recent updates &rarr;</a></p>

<h2>Foxzen</h2>

<div class="row" data-role="status-row" id="row-mirror">
  <div class="row-head">
    <span class="name">foxzen.me / mirror.foxzen.me</span>
    <span class="badge checking">Checking&hellip;</span>
  </div>
  <div class="detail">Checking&hellip;</div>
  <div class="meta"><span class="source">Source: your browser &rarr; mirror.foxzen.me/api/health</span><span class="checked-at"></span></div>
</div>

<div class="row" data-role="status-row" id="row-backup">
  <div class="row-head">
    <span class="name">backup.foxzen.me</span>
    <span class="badge checking">Checking&hellip;</span>
  </div>
  <div class="detail">Checking&hellip;</div>
  <div class="meta"><span class="source">Source: your browser &rarr; backup.foxzen.me/api/health (direct, bypasses Cloudflare)</span><span class="checked-at"></span></div>
</div>

<div class="row" data-role="status-row" id="row-github">
  <div class="row-head">
    <span class="name">github.foxzen.me</span>
    <span class="badge checking">Checking&hellip;</span>
  </div>
  <div class="detail">Checking&hellip;</div>
  <div class="meta"><span class="source">Source: your browser &rarr; mirror.foxzen.me/api/refresh/github/status + github.foxzen.me reachability</span><span class="checked-at"></span></div>
</div>

<div class="row" data-role="status-row" id="row-cf">
  <div class="row-head">
    <span class="name">cf.foxzen.me</span>
    <span class="badge checking">Checking&hellip;</span>
  </div>
  <div class="detail">Checking&hellip;</div>
  <div class="meta"><span class="source">Source: your browser &rarr; mirror.foxzen.me/api/refresh/cf/status + cf.foxzen.me reachability</span><span class="checked-at"></span></div>
</div>

<div class="row" data-role="status-row" id="row-greencloud">
  <div class="row-head">
    <span class="name">GreenCloud (server infrastructure)</span>
    <span class="badge checking">Checking&hellip;</span>
  </div>
  <div class="detail">Checking&hellip;</div>
  <div class="meta"><span class="source">Derived from the mirror.foxzen.me and backup.foxzen.me checks above</span><span class="checked-at"></span></div>
</div>

<div class="row" data-role="status-row" id="row-hetzner">
  <div class="row-head">
    <span class="name">Hetzner (backup storage)</span>
    <span class="badge unknown">No live check</span>
  </div>
  <div class="detail">Hetzner在这里只用作长期备份存储，没有可公开访问的健康检查接口，因此这里不提供实时探测结果。Used only for long-term backup storage; it has no public health-check endpoint, so no live probe is shown here.</div>
  <div class="meta"><span class="source">No data source available</span><span class="checked-at"></span></div>
</div>

<h2>External Platform Status</h2>

<div class="row" data-role="status-row" id="row-github-official">
  <div class="row-head">
    <span class="name">Official GitHub Status</span>
    <span class="badge checking">Loading&hellip;</span>
  </div>
  <div class="detail">Loading&hellip;</div>
  <a class="official-link" href="__GITHUB_STATUS_PAGE__" target="_blank" rel="noopener">View official GitHub status &rarr;</a>
  <div class="meta"><span class="source">Source: githubstatus.com (GitHub's own status page)</span><span class="checked-at"></span></div>
</div>

<div class="row" data-role="status-row" id="row-cloudflare-official">
  <div class="row-head">
    <span class="name">Official Cloudflare Status</span>
    <span class="badge checking">Loading&hellip;</span>
  </div>
  <div class="detail">Loading&hellip;</div>
  <a class="official-link" href="__CLOUDFLARE_STATUS_PAGE__" target="_blank" rel="noopener">View official Cloudflare status &rarr;</a>
  <div class="meta"><span class="source">Source: cloudflarestatus.com (Cloudflare's own status page)</span><span class="checked-at"></span></div>
</div>

<footer>
  This page is a static, read-only status dashboard. It cannot trigger any refresh, sync, or deploy action.
</footer>

<script>
(function () {
  "use strict";

  var MIRROR_HEALTH_URL = "https://mirror.foxzen.me/api/health";
  var BACKUP_HEALTH_URL = "https://backup.foxzen.me/api/health";
  var GITHUB_PAGES_URL = "https://github.foxzen.me/";
  var CF_PAGES_URL = "https://cf.foxzen.me/";
  var GITHUB_STATUS_API = "https://www.githubstatus.com/api/v2/summary.json";
  var CLOUDFLARE_STATUS_API = "https://www.cloudflarestatus.com/api/v2/summary.json";

  function refreshStatusUrl(target) {
    return "https://mirror.foxzen.me/api/refresh/" + target + "/status";
  }

  function setRow(id, state, badgeText, detailText) {
    var el = document.getElementById(id);
    if (!el) return;
    var badge = el.querySelector(".badge");
    var detail = el.querySelector(".detail");
    var checkedAt = el.querySelector(".checked-at");
    badge.className = "badge " + state;
    badge.textContent = badgeText;
    detail.textContent = detailText;
    if (checkedAt) checkedAt.textContent = "Checked at " + new Date().toLocaleTimeString();
  }

  function withTimeout(promise, ms) {
    return new Promise(function (resolve) {
      var done = false;
      var timer = setTimeout(function () {
        if (!done) { done = true; resolve({ timedOut: true }); }
      }, ms);
      promise.then(function (v) {
        if (!done) { done = true; clearTimeout(timer); resolve(v); }
      }, function () {
        if (!done) { done = true; clearTimeout(timer); resolve({ timedOut: true }); }
      });
    });
  }

  function fetchJSON(url, ms) {
    return withTimeout(
      fetch(url, { credentials: "omit" }).then(function (res) {
        if (!res.ok) return { ok: false, httpStatus: res.status, data: null };
        return res.json().then(function (data) {
          return { ok: true, httpStatus: res.status, data: data };
        }, function () {
          return { ok: false, httpStatus: res.status, data: null };
        });
      }, function () {
        return { ok: false, httpStatus: null, data: null };
      }),
      ms
    ).then(function (r) {
      return r.timedOut ? { ok: false, httpStatus: null, data: null, timedOut: true } : r;
    });
  }

  function pingReachable(url, ms) {
    return withTimeout(
      fetch(url, { mode: "no-cors", credentials: "omit" }).then(function () {
        return true;
      }, function () {
        return false;
      }),
      ms
    ).then(function (r) {
      return r === true;
    });
  }

  function fmtPercent(ratio) {
    return typeof ratio === "number" ? (ratio * 100).toFixed(1) + "%" : "unknown";
  }

  function checkHealthEndpoint(rowId, url, sourceLabel) {
    return fetchJSON(url, 8000).then(function (r) {
      if (r.ok && r.data) {
        var d = r.data;
        var visits = d.visit_stats && typeof d.visit_stats.today === "number" ? d.visit_stats.today : "unknown";
        setRow(rowId, "ok", "OK",
          "posts: " + (d.post_count != null ? d.post_count : "unknown") +
          " · visits today: " + visits +
          " · disk used: " + fmtPercent(d.disk_usage_ratio));
        return true;
      }
      var reason = r.httpStatus ? ("HTTP " + r.httpStatus) : "network error / timeout, checked from your browser";
      setRow(rowId, "fail", "Unreachable", sourceLabel + " did not return a healthy response (" + reason + ").");
      return false;
    });
  }

  function checkPagesTarget(rowId, target, pageUrl, pageName) {
    var statusPromise = fetchJSON(refreshStatusUrl(target), 8000);
    var reachPromise = pingReachable(pageUrl, 8000);
    return Promise.all([statusPromise, reachPromise]).then(function (results) {
      var statusResult = results[0];
      var reachable = results[1];
      var parts = [];
      if (statusResult.ok && statusResult.data && statusResult.data.last_result) {
        var lr = statusResult.data.last_result;
        parts.push("last sync: " + (lr.status || "unknown") +
          (lr.finished_at ? " at " + lr.finished_at : "") +
          (statusResult.data.last_result_is_current === false ? " (stale — a newer attempt did not finish)" : ""));
      } else if (statusResult.ok) {
        parts.push("last sync: no record yet");
      } else {
        parts.push("last sync status: could not be read from your browser");
      }
      parts.push(pageName + " reachability: " + (reachable ? "OK (browser received a response)" : "failed (network error, from your browser)"));
      var state = reachable ? "ok" : "fail";
      setRow(rowId, state, reachable ? "OK" : "Unreachable", parts.join(" · "));
      return reachable;
    });
  }

  function checkGreenCloud(mirrorOkPromise, backupOkPromise) {
    Promise.all([mirrorOkPromise, backupOkPromise]).then(function (results) {
      var mirrorOk = results[0];
      var backupOk = results[1];
      if (mirrorOk && backupOk) {
        setRow("row-greencloud", "ok", "OK", "Reachable via both mirror.foxzen.me and backup.foxzen.me.");
      } else if (mirrorOk && !backupOk) {
        setRow("row-greencloud", "ok", "OK", "Reachable via mirror.foxzen.me. backup.foxzen.me path failed (see above).");
      } else if (!mirrorOk && backupOk) {
        setRow("row-greencloud", "ok", "OK",
          "Reachable via backup.foxzen.me. mirror.foxzen.me path failed — since backup.foxzen.me bypasses Cloudflare, " +
          "this can mean the issue is on the Cloudflare side rather than GreenCloud itself.");
      } else {
        setRow("row-greencloud", "fail", "Unreachable", "Both mirror.foxzen.me and backup.foxzen.me checks failed from your browser.");
      }
    });
  }

  function checkOfficialStatus(rowId, apiUrl) {
    fetchJSON(apiUrl, 8000).then(function (r) {
      if (r.ok && r.data && r.data.status) {
        setRow(rowId, "ok", r.data.status.description || "Reported",
          "Live from the official status API: " + (r.data.status.description || "see link below") + ".");
      } else {
        setRow(rowId, "unknown", "Unavailable here",
          "Could not load the live official status feed from your browser. Use the official link below — it is always authoritative.");
      }
    });
  }

  function main() {
    var mirrorOk = checkHealthEndpoint("row-mirror", MIRROR_HEALTH_URL, "mirror.foxzen.me");
    var backupOk = checkHealthEndpoint("row-backup", BACKUP_HEALTH_URL, "backup.foxzen.me");
    checkGreenCloud(mirrorOk, backupOk);
    checkPagesTarget("row-github", "github", GITHUB_PAGES_URL, "github.foxzen.me");
    checkPagesTarget("row-cf", "cf", CF_PAGES_URL, "cf.foxzen.me");
    checkOfficialStatus("row-github-official", GITHUB_STATUS_API);
    checkOfficialStatus("row-cloudflare-official", CLOUDFLARE_STATUS_API);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", main);
  } else {
    main();
  }
})();
</script>
</body>
</html>
"""


def render_page() -> str:
    text = PAGE_TEMPLATE
    text = text.replace("__GITHUB_STATUS_PAGE__", GITHUB_STATUS_PAGE)
    text = text.replace("__CLOUDFLARE_STATUS_PAGE__", CLOUDFLARE_STATUS_PAGE)
    return text


def build_status_page(output_dir: Path = OUTPUT_DIR) -> Path:
    """生成index.html + CNAME到output_dir，返回该目录路径。每次调用直接
    覆盖已有文件——页面内容完全由PAGE_TEMPLATE决定，不依赖上一次构建的
    残留状态，重复构建天然幂等。
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "index.html").write_text(render_page(), encoding="utf-8")
    (output_dir / "CNAME").write_text(HOST + "\n", encoding="utf-8")
    return output_dir


class StatusPageVerificationError(RuntimeError):
    """生成的status页面没有通过安全/完整性检查时抛出，携带全部发现的问题
    （不是只报第一个）。"""


# 跟publish_build.py的_DANGEROUS_SUFFIXES同一个思路，但这里不需要区分
# "文章正文里出现token字样是正常内容"——这个页面没有文章、没有任何用户
# 提交的内容，Python这边百分之百掌控全部文本，直接对全文做子串扫描即可，
# 不需要publish_build.py那种更复杂的路径名/zip条目扫描逻辑。
_DANGEROUS_SUBSTRINGS = (
    "GITHUB_TOKEN", "TG_BOT_TOKEN", "CF_API_TOKEN", "FOXZEN_GIT_PUSH_TOKEN",
    "ghp_", "github_pat_",
    "BEGIN RSA PRIVATE KEY", "BEGIN PRIVATE KEY", "BEGIN OPENSSH PRIVATE KEY",
)

_REQUIRED_SUBSTRINGS = (
    "https://www.githubstatus.com/",
    "https://www.cloudflarestatus.com/",
    "mirror.foxzen.me/api/health",
    "backup.foxzen.me/api/health",
    "https://update.foxzen.me/",
)


def verify_status_page(output_dir: Path) -> None:
    """对已生成的status页面做安全/完整性检查，只做文本层面的断言。"""
    errors = []
    index_file = output_dir / "index.html"
    cname_file = output_dir / "CNAME"

    if not index_file.exists():
        raise StatusPageVerificationError("缺少 index.html")
    text = index_file.read_text(encoding="utf-8")

    if not cname_file.exists():
        errors.append("缺少 CNAME")
    elif cname_file.read_text(encoding="utf-8").strip() != HOST:
        errors.append(f"CNAME内容不是{HOST}")

    for bad in _DANGEROUS_SUBSTRINGS:
        if bad in text:
            errors.append(f"发现疑似密钥/敏感字符串: {bad}")

    for required in _REQUIRED_SUBSTRINGS:
        if required not in text:
            errors.append(f"缺少必需内容: {required}")

    if "<form" in text.lower():
        errors.append("页面包含<form>标签，只读状态页不应该有表单")

    if "POST" in text:
        errors.append("页面文本中出现POST字样，只读状态页不应该发起任何POST请求")

    if errors:
        raise StatusPageVerificationError("；".join(errors))


def main():
    output_dir = build_status_page()
    verify_status_page(output_dir)
    print(f"已生成并通过安全检查 {output_dir}（host={HOST}）")


if __name__ == "__main__":
    main()
