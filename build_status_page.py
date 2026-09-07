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
    （只在本地生成 publish_status/index.html + publish_status/CNAME(带
    CNAME，预览"如果发布到GitHub Pages会长什么样")，不做任何git操作）

    python3 build_status_page.py --output-dir <DIR>
    （只在本地生成到DIR，不写CNAME、不做任何git操作——用于GreenCloud等
    直接用Nginx按照status.foxzen.me自己的真实域名提供服务的场景：这个
    输出不面向任何"需要额外绑定自定义域名"的平台，写CNAME反而是误导。
    跟--publish互斥。）

    python3 build_status_page.py --publish <REPO_DIR>
    （生成后立即复用git_publish.py，commit+push到REPO_DIR这个独立的卫星
    仓库——例如GitHub Pages专用的foxzen-status仓库的本地checkout路径。
    REPO_DIR必须是已经clone好、能fast-forward push到origin/master的独立
    git仓库，且默认分支必须是"master"（git_publish.py硬编码检查这一点，
    见其文档字符串B3修复说明）——这跟update.foxzen.me的CNAME、Cloudflare
    Pages使用的static_status/是"foxzen-blog仓库内部的一个子目录"不同：
    这个页面从设计起就没有那种用法（publish_status/本身在.gitignore里，
    从不打算进foxzen-blog自己的git历史），repo_dir就是output_dir本身，
    commit的subpath固定是"."，不需要额外的PUBLISH_SUBPATH参数。需要环境
    变量GITHUB_TOKEN。这个模式从设计起就不写CNAME(write_cname=False)——
    GitHub Pages这一侧的最终决定是保持默认github.io地址
    (https://foxzenme.github.io/foxzen-status/)，不绑定
    status.foxzen.me这个自定义域名，真正拥有这个域名的是GreenCloud，
    两边各自独立、互不代理、互不重定向。

最终的"多渠道、同内容、独立故障域"架构（本次任务确定）：
    同一份PAGE_TEMPLATE
    ├── GitHub Pages(--publish)  → https://foxzenme.github.io/foxzen-status/
    │                                （默认github.io地址，不绑定自定义域名）
    └── GreenCloud(--output-dir) → https://status.foxzen.me/
                                     （Nginx直接按真实域名提供，见
                                     nginx-conf/default.conf新增的
                                     status.foxzen.me server块，root指向
                                     /usr/share/nginx/html/status——这个
                                     目录预期由本命令在GreenCloud上现场
                                     生成，不经过git，参照.gitignore里
                                     html/status/的说明）
    Cloudflare Pages(foxzen-status.pages.dev)本次不变动，暂时继续保留。
    三者任意一个故障，理论上不影响另外两个——彼此没有反向代理、没有跳转
    关系，只是内容来自同一套生成代码。

尚未完成、需要人工决定的部分（本次不擅自处理）：
    - nginx-conf/default.conf已经加上status.foxzen.me的server块，但这只是
      改了仓库里的配置文件本身，还没有同步到GreenCloud真实运行的Nginx、
      也没有reload——本次不做生产部署；
    - status.foxzen.me这个子域名的DNS记录当前代理到GreenCloud的IP，本轮
      不修改DNS，也不确认这个代理当前是否真的把流量送到了上面这个新增的
      Nginx server块（取决于GreenCloud当前实际运行的nginx-conf版本）；
    - 上面nginx server块里的SSL证书沿用了backup.foxzen.me/download.foxzen.me
      /foxzen.me共用的/etc/nginx/certs/foxzen/foxzen.crt——这份证书的真实
      SAN列表里是否已经包含status.foxzen.me，本次没有办法从代码库确认
      （证书文件本身不在这个仓库里），如果是一张按SAN逐个签发、而不是
      泛域名(*.foxzen.me)的证书，可能需要人工重新签发才能覆盖这个新增
      的子域名，这一步不属于代码层面的改动；
    - GitHub Pages这一侧用一个独立的新仓库foxzen-status承载（见
      .github/workflows/deploy-status-pages-github.yml），已经创建完成、
      Pages已开启、SATELLITE_PAGES_TOKEN已配置——这部分基础设施搭建已经
      完成，不再是待办；
    - app.py里新增的两条CORS白名单（status.foxzen.me本身 + GitHub Pages
      的默认域名foxzenme.github.io，见STATUS_READ_CORS_ALLOWED_ORIGINS），
      都需要跟随代码一起部署到GreenCloud（走正常的candidate cutover流程）
      之后，这两个渠道从浏览器发起的跨域读取才会真正被允许——页面上线前
      必须确认这一步已经完成，否则/api/health和/api/refresh/*/status会被
      浏览器的CORS拦下来，页面会打开但所有实时检测行都会显示"无法访问"。
"""
import argparse
import os
import sys
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).parent
OUTPUT_DIR = BASE_DIR / "publish_status"
HOST = "status.foxzen.me"

# 跟generate_status_page.py::publish_update_page()推送用的是同一个机器人
# 身份/超时值——两边都只是把git_publish.commit_and_push()套一层，保持
# 提交作者身份统一没有理由用两套值。
GIT_BOT_NAME = "Foxzen Refresh Bot"
GIT_BOT_EMAIL = "foxzen-refresh-bot@users.noreply.github.com"
GIT_PUSH_TIMEOUT_SECONDS = 60

GITHUB_STATUS_PAGE = "https://www.githubstatus.com/"
CLOUDFLARE_STATUS_PAGE = "https://www.cloudflarestatus.com/"

# 用简单的字符串占位符+replace()而不是str.format()，是因为下面模板里的CSS
# 本身大量使用花括号——.format()会把它们全部当成格式化字段解析，需要把每一个
# "{"/"}"都转义成"{{"/"}}"才能用，既容易漏改又会让CSS变得难读；replace()
# 不care模板里有多少花括号，只精确替换这两个占位符，两个官方状态页URL依然
# 保持"只在Python常量里写一份"这个单一数据源。
PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>FoxZen Status</title>
<style>
  :root { color-scheme: light; }
  * { box-sizing: border-box; }
  .lang-toggle {
    position: fixed; top: 12px; right: 12px; z-index: 100;
    display: inline-block; font-size: 0.85em;
    background: rgba(255,255,255,0.92); padding: 4px 10px; border-radius: 14px;
    box-shadow: 0 1px 4px rgba(0,0,0,0.15);
  }
  .lang-toggle button {
    background: none; border: none; padding: 2px 4px; cursor: pointer;
    color: #999; font-size: 1em; font-family: inherit;
  }
  .lang-toggle button.active { color: #1a73e8; font-weight: bold; }
  @media (max-width: 480px) {
    .lang-toggle { top: 8px; right: 8px; font-size: 0.75em; padding: 3px 8px; }
  }
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
<div class="lang-toggle">
  <button type="button" data-lang-btn="zh" aria-label="切换到中文">中</button> / <button type="button" data-lang-btn="en" aria-label="Switch to English">EN</button>
</div>
<h1>FoxZen Status</h1>
<div class="subtitle">status.foxzen.me &mdash; <span data-i18n="status_subtitle">现在怎么样</span></div>

<div class="browser-note">
  <span data-i18n="browser_note_prefix">下面所有检查结果都是在你打开这个页面时，由</span><strong data-i18n="browser_note_strong">你的浏览器</strong><span data-i18n="browser_note_suffix">实时发起的，不是某个中心化监控服务器上跑出来的结果——它反映的是"你的设备现在能不能连上"，而不是一个绝对的全局判断。</span>
</div>

<p class="cross-link"><a href="https://update.foxzen.me/" data-i18n="view_updates_link">查看最近的更新 →</a></p>

<h2 data-i18n="section_foxzen">Foxzen</h2>

<div class="row" data-role="status-row" id="row-mirror">
  <div class="row-head">
    <span class="name">foxzen.me / mirror.foxzen.me</span>
    <span class="badge checking">检测中…</span>
  </div>
  <div class="detail">检测中…</div>
  <div class="meta"><span class="source" data-i18n="source_mirror">来源：你的浏览器 → mirror.foxzen.me/api/health</span><span class="checked-at"></span></div>
</div>

<div class="row" data-role="status-row" id="row-backup">
  <div class="row-head">
    <span class="name">backup.foxzen.me</span>
    <span class="badge checking">检测中…</span>
  </div>
  <div class="detail">检测中…</div>
  <div class="meta"><span class="source" data-i18n="source_backup">来源：你的浏览器 → backup.foxzen.me/api/health（直连，绕开Cloudflare）</span><span class="checked-at"></span></div>
</div>

<div class="row" data-role="status-row" id="row-github">
  <div class="row-head">
    <span class="name">github.foxzen.me</span>
    <span class="badge checking">检测中…</span>
  </div>
  <div class="detail">检测中…</div>
  <div class="meta"><span class="source" data-i18n="source_github">来源：你的浏览器 → mirror.foxzen.me/api/refresh/github/status + github.foxzen.me可达性</span><span class="checked-at"></span></div>
</div>

<div class="row" data-role="status-row" id="row-cf">
  <div class="row-head">
    <span class="name">cf.foxzen.me</span>
    <span class="badge checking">检测中…</span>
  </div>
  <div class="detail">检测中…</div>
  <div class="meta"><span class="source" data-i18n="source_cf">来源：你的浏览器 → mirror.foxzen.me/api/refresh/cf/status + cf.foxzen.me可达性</span><span class="checked-at"></span></div>
</div>

<div class="row" data-role="status-row" id="row-greencloud">
  <div class="row-head">
    <span class="name" data-i18n="name_greencloud">GreenCloud（服务器基础设施）</span>
    <span class="badge checking">检测中…</span>
  </div>
  <div class="detail">检测中…</div>
  <div class="meta"><span class="source" data-i18n="source_greencloud">由上面mirror.foxzen.me和backup.foxzen.me的检测结果推导得出</span><span class="checked-at"></span></div>
</div>

<div class="row" data-role="status-row" id="row-hetzner">
  <div class="row-head">
    <span class="name" data-i18n="name_hetzner">Hetzner（备份存储）</span>
    <span class="badge unknown" data-i18n="badge_no_live_check">无实时检测</span>
  </div>
  <div class="detail" data-i18n="detail_hetzner">Hetzner在这里只用作长期备份存储，没有可公开访问的健康检查接口，因此这里不提供实时探测结果。</div>
  <div class="meta"><span class="source" data-i18n="source_none">没有可用的数据来源</span><span class="checked-at"></span></div>
</div>

<h2 data-i18n="section_external">External Platform Status</h2>

<div class="row" data-role="status-row" id="row-github-official">
  <div class="row-head">
    <span class="name" data-i18n="name_github_official">GitHub 官方状态</span>
    <span class="badge checking">加载中…</span>
  </div>
  <div class="detail">加载中…</div>
  <a class="official-link" href="__GITHUB_STATUS_PAGE__" target="_blank" rel="noopener" data-i18n="link_github_official">查看 GitHub 官方状态页 →</a>
  <div class="meta"><span class="source" data-i18n="source_github_official">来源：githubstatus.com（GitHub官方状态页）</span><span class="checked-at"></span></div>
</div>

<div class="row" data-role="status-row" id="row-cloudflare-official">
  <div class="row-head">
    <span class="name" data-i18n="name_cloudflare_official">Cloudflare 官方状态</span>
    <span class="badge checking">加载中…</span>
  </div>
  <div class="detail">加载中…</div>
  <a class="official-link" href="__CLOUDFLARE_STATUS_PAGE__" target="_blank" rel="noopener" data-i18n="link_cloudflare_official">查看 Cloudflare 官方状态页 →</a>
  <div class="meta"><span class="source" data-i18n="source_cloudflare_official">来源：cloudflarestatus.com（Cloudflare官方状态页）</span><span class="checked-at"></span></div>
</div>

<footer data-i18n="footer_text">
  本页是一个静态、只读的状态看板，不能触发任何刷新、同步或发布操作。
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

  // 全站UI国际化：跟fetch_blog.py::I18N_BLOCK等其它四份独立实现同一套
  // localStorage key/检测算法/data-i18n约定，这是第六份独立实现（本页面
  // 是自包含的单文件，不依赖任何外部JS，见文件头docstring）。
  //
  // 范围说明（已在最终报告里披露）：只翻译不会被下面setRow()动态覆盖的
  // 静态UI文案（标题/副标题/小节标题/来源说明/页脚/官方链接）以及setRow()
  // 接收到的、来自FoxZen自己代码的固定徽章词（OK/Unreachable/Reported/
  // Unavailable here等）——checkHealthEndpoint/checkPagesTarget/
  // checkGreenCloud里拼出来的完整诊断句子（"last sync: ..."、
  // "reachability: ..."等）本次不做逐句翻译：这些句子由多段条件判断动态
  // 拼接，逐句模板化的改动量和这个页面的次要程度不成比例，保持英文原样，
  // 不影响其准确性。r.data.status.description（GitHub/Cloudflare官方状态
  // API返回的原始文字）任何时候都不翻译/改写。
  var STATUS_STRINGS = {
    zh: {
      status_subtitle: "现在怎么样",
      browser_note_prefix: "下面所有检查结果都是在你打开这个页面时，由",
      browser_note_strong: "你的浏览器",
      browser_note_suffix: "实时发起的，不是某个中心化监控服务器上跑出来的结果——它反映的是“你的设备现在能不能连上”，而不是一个绝对的全局判断。",
      view_updates_link: "查看最近的更新 →",
      section_foxzen: "Foxzen",
      section_external: "External Platform Status",
      name_greencloud: "GreenCloud（服务器基础设施）",
      name_hetzner: "Hetzner（备份存储）",
      name_github_official: "GitHub 官方状态",
      name_cloudflare_official: "Cloudflare 官方状态",
      badge_no_live_check: "无实时检测",
      detail_hetzner: "Hetzner在这里只用作长期备份存储，没有可公开访问的健康检查接口，因此这里不提供实时探测结果。",
      source_mirror: "来源：你的浏览器 → mirror.foxzen.me/api/health",
      source_backup: "来源：你的浏览器 → backup.foxzen.me/api/health（直连，绕开Cloudflare）",
      source_github: "来源：你的浏览器 → mirror.foxzen.me/api/refresh/github/status + github.foxzen.me可达性",
      source_cf: "来源：你的浏览器 → mirror.foxzen.me/api/refresh/cf/status + cf.foxzen.me可达性",
      source_greencloud: "由上面mirror.foxzen.me和backup.foxzen.me的检测结果推导得出",
      source_none: "没有可用的数据来源",
      source_github_official: "来源：githubstatus.com（GitHub官方状态页）",
      source_cloudflare_official: "来源：cloudflarestatus.com（Cloudflare官方状态页）",
      link_github_official: "查看 GitHub 官方状态页 →",
      link_cloudflare_official: "查看 Cloudflare 官方状态页 →",
      footer_text: "本页是一个静态、只读的状态看板，不能触发任何刷新、同步或发布操作。",
      badge_ok: "正常",
      badge_unreachable: "无法访问",
      badge_reported: "已报告",
      badge_unavailable_here: "此处不可用",
      checked_at_prefix: "检测时间 ",
    },
    en: {
      status_subtitle: "what's the current state",
      browser_note_prefix: "All checks below run live in ",
      browser_note_strong: "your browser",
      browser_note_suffix: " when this page loads. They reflect what your device can currently reach, not a centralized monitoring verdict.",
      view_updates_link: "View recent updates →",
      section_foxzen: "Foxzen",
      section_external: "External Platform Status",
      name_greencloud: "GreenCloud (server infrastructure)",
      name_hetzner: "Hetzner (backup storage)",
      name_github_official: "Official GitHub Status",
      name_cloudflare_official: "Official Cloudflare Status",
      badge_no_live_check: "No live check",
      detail_hetzner: "Used only for long-term backup storage; it has no public health-check endpoint, so no live probe is shown here.",
      source_mirror: "Source: your browser → mirror.foxzen.me/api/health",
      source_backup: "Source: your browser → backup.foxzen.me/api/health (direct, bypasses Cloudflare)",
      source_github: "Source: your browser → mirror.foxzen.me/api/refresh/github/status + github.foxzen.me reachability",
      source_cf: "Source: your browser → mirror.foxzen.me/api/refresh/cf/status + cf.foxzen.me reachability",
      source_greencloud: "Derived from the mirror.foxzen.me and backup.foxzen.me checks above",
      source_none: "No data source available",
      source_github_official: "Source: githubstatus.com (GitHub's own status page)",
      source_cloudflare_official: "Source: cloudflarestatus.com (Cloudflare's own status page)",
      link_github_official: "View official GitHub status →",
      link_cloudflare_official: "View official Cloudflare status →",
      footer_text: "This page is a static, read-only status dashboard. It cannot trigger any refresh, sync, or deploy action.",
      badge_ok: "OK",
      badge_unreachable: "Unreachable",
      badge_reported: "Reported",
      badge_unavailable_here: "Unavailable here",
      checked_at_prefix: "Checked at ",
    },
  };

  var FOXZEN_LANG_KEY = "foxzen_lang";

  function detectDefaultFoxzenLang() {
    var langs = (navigator.languages && navigator.languages.length) ? navigator.languages : [navigator.language || ""];
    for (var i = 0; i < langs.length; i++) {
      if (/^zh/i.test(langs[i])) return "zh";
    }
    return "en";
  }

  function getFoxzenLang() {
    try {
      var saved = localStorage.getItem(FOXZEN_LANG_KEY);
      if (saved === "zh" || saved === "en") return saved;
    } catch (e) {}
    return detectDefaultFoxzenLang();
  }

  var FOXZEN_LANG = getFoxzenLang();

  function statusT(key) {
    var dict = STATUS_STRINGS[FOXZEN_LANG] || STATUS_STRINGS.en;
    return dict[key];
  }

  function applyStatusI18n() {
    var dict = STATUS_STRINGS[FOXZEN_LANG] || STATUS_STRINGS.en;
    var nodes = document.querySelectorAll("[data-i18n]");
    for (var i = 0; i < nodes.length; i++) {
      var key = nodes[i].getAttribute("data-i18n");
      if (typeof dict[key] === "string") nodes[i].textContent = dict[key];
    }
    document.documentElement.setAttribute("lang", FOXZEN_LANG === "zh" ? "zh-CN" : "en");
    var btns = document.querySelectorAll("[data-lang-btn]");
    for (var j = 0; j < btns.length; j++) {
      if (btns[j].getAttribute("data-lang-btn") === FOXZEN_LANG) btns[j].classList.add("active");
      else btns[j].classList.remove("active");
    }
  }

  function setFoxzenLang(lang) {
    if (lang !== "zh" && lang !== "en") return;
    FOXZEN_LANG = lang;
    try { localStorage.setItem(FOXZEN_LANG_KEY, lang); } catch (e) {}
    applyStatusI18n();
  }

  function wireLangToggle() {
    var btns = document.querySelectorAll("[data-lang-btn]");
    for (var i = 0; i < btns.length; i++) {
      btns[i].addEventListener("click", function (e) {
        setFoxzenLang(e.currentTarget.getAttribute("data-lang-btn"));
      });
    }
  }

  function refreshStatusUrl(target) {
    return "https://mirror.foxzen.me/api/refresh/" + target + "/status";
  }

  // badgeText/detailText由各check*函数拼好传入——只有badgeText在下面几个
  // 调用点传入的是FoxZen自己代码写死的固定英文词（OK/Unreachable等）时才
  // 值得做语言相关处理，那几个调用点已经直接改成传statusT(...)对应的当前
  // 语言文案（见checkHealthEndpoint/checkPagesTarget/checkGreenCloud/
  // checkOfficialStatus），这里的setRow()本身保持"传什么就显示什么"，
  // 不在这一层做任何字符串判断/替换——避免不小心把第三方状态API返回的
  // 原始文字(r.data.status.description)也当成"已知词"错误替换掉。
  function setRow(id, state, badgeText, detailText) {
    var el = document.getElementById(id);
    if (!el) return;
    var badge = el.querySelector(".badge");
    var detail = el.querySelector(".detail");
    var checkedAt = el.querySelector(".checked-at");
    badge.className = "badge " + state;
    badge.textContent = badgeText;
    detail.textContent = detailText;
    if (checkedAt) checkedAt.textContent = statusT("checked_at_prefix") + new Date().toLocaleTimeString();
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
        setRow(rowId, "ok", statusT("badge_ok"),
          "posts: " + (d.post_count != null ? d.post_count : "unknown") +
          " · visits today: " + visits +
          " · disk used: " + fmtPercent(d.disk_usage_ratio));
        return true;
      }
      var reason = r.httpStatus ? ("HTTP " + r.httpStatus) : "network error / timeout, checked from your browser";
      setRow(rowId, "fail", statusT("badge_unreachable"), sourceLabel + " did not return a healthy response (" + reason + ").");
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
      setRow(rowId, state, reachable ? statusT("badge_ok") : statusT("badge_unreachable"), parts.join(" · "));
      return reachable;
    });
  }

  function checkGreenCloud(mirrorOkPromise, backupOkPromise) {
    Promise.all([mirrorOkPromise, backupOkPromise]).then(function (results) {
      var mirrorOk = results[0];
      var backupOk = results[1];
      if (mirrorOk && backupOk) {
        setRow("row-greencloud", "ok", statusT("badge_ok"), "Reachable via both mirror.foxzen.me and backup.foxzen.me.");
      } else if (mirrorOk && !backupOk) {
        setRow("row-greencloud", "ok", statusT("badge_ok"), "Reachable via mirror.foxzen.me. backup.foxzen.me path failed (see above).");
      } else if (!mirrorOk && backupOk) {
        setRow("row-greencloud", "ok", statusT("badge_ok"),
          "Reachable via backup.foxzen.me. mirror.foxzen.me path failed — since backup.foxzen.me bypasses Cloudflare, " +
          "this can mean the issue is on the Cloudflare side rather than GreenCloud itself.");
      } else {
        setRow("row-greencloud", "fail", statusT("badge_unreachable"), "Both mirror.foxzen.me and backup.foxzen.me checks failed from your browser.");
      }
    });
  }

  function checkOfficialStatus(rowId, apiUrl) {
    fetchJSON(apiUrl, 8000).then(function (r) {
      if (r.ok && r.data && r.data.status) {
        // r.data.status.description是GitHub/Cloudflare官方状态API返回的原始
        // 文字，不属于本次UI国际化的翻译对象（同一条规则贯穿整个status/
        // update页面：数据来源原文永远保持原样，只翻译UI标签），只在它缺失
        // 时才退回下面按语言翻译过的statusT("badge_reported")兜底文案。
        setRow(rowId, "ok", r.data.status.description || statusT("badge_reported"),
          "Live from the official status API: " + (r.data.status.description || "see link below") + ".");
      } else {
        setRow(rowId, "unknown", statusT("badge_unavailable_here"),
          "Could not load the live official status feed from your browser. Use the official link below — it is always authoritative.");
      }
    });
  }

  function main() {
    applyStatusI18n();
    wireLangToggle();
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


def build_status_page(output_dir: Path = OUTPUT_DIR, write_cname: bool = True) -> Path:
    """生成index.html(+可选CNAME)到output_dir，返回该目录路径。每次调用
    直接覆盖已有文件——页面内容完全由PAGE_TEMPLATE决定，不依赖上一次构建的
    残留状态，重复构建天然幂等。

    write_cname=False用于不面向GitHub Pages自定义域名绑定的输出目标（比如
    GreenCloud，域名本来就通过Nginx server_name直接匹配，CNAME文件在那里
    没有任何意义，留着反而让人误以为这份内容是要交给GitHub Pages托管）。
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "index.html").write_text(render_page(), encoding="utf-8")
    if write_cname:
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


def verify_status_page(output_dir: Path, expect_cname: bool = True) -> None:
    """对已生成的status页面做安全/完整性检查，只做文本层面的断言。

    expect_cname必须跟build_status_page()调用时的write_cname保持一致：
    expect_cname=False时不仅不要求CNAME存在，还会在CNAME意外存在时报错——
    这是GitHub Pages卫星仓库发布路径的一道明确的反向检查(见
    publish_status_page())，防止将来有人不小心改回默认参数、导致
    status.foxzen.me自定义域名又被悄悄绑定回GitHub Pages。
    """
    errors = []
    index_file = output_dir / "index.html"
    cname_file = output_dir / "CNAME"

    if not index_file.exists():
        raise StatusPageVerificationError("缺少 index.html")
    text = index_file.read_text(encoding="utf-8")

    if expect_cname:
        if not cname_file.exists():
            errors.append("缺少 CNAME")
        elif cname_file.read_text(encoding="utf-8").strip() != HOST:
            errors.append(f"CNAME内容不是{HOST}")
    elif cname_file.exists():
        errors.append(
            f"不应存在CNAME：这个产物面向不绑定自定义域名的独立渠道"
            f"（GitHub Pages应保持默认github.io地址），意外出现CNAME会让"
            f"GitHub Pages重新以{HOST}这个自定义域名解释这份内容"
        )

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


def publish_status_page(repo_dir: Path) -> dict:
    """生成+校验status页面，然后commit+push到repo_dir这个独立的卫星仓库
    （例如GitHub Pages专用的foxzen-status仓库的本地checkout路径）。

    这个函数专门服务GitHub Pages卫星仓库这一个场景，因此固定
    write_cname=False/expect_cname=False：GitHub Pages这一侧的最终决定是
    保持默认github.io地址(https://foxzenme.github.io/foxzen-status/)，
    不绑定status.foxzen.me这个自定义域名——真正拥有这个域名的是
    GreenCloud（见build_status_page()的--output-dir模式），两边各自独立、
    互不代理、互不重定向。

    跟generate_status_page.py::publish_update_page()同一个"build->
    git_publish.commit_and_push()"模式，但这个页面从设计起就没有"作为
    foxzen-blog自己的子目录被提交"这个场景（publish_status/在.gitignore
    里，从不打算进foxzen-blog自己的git历史）——repo_dir就是output_dir
    本身，commit的subpath固定是"."，不需要额外的PUBLISH_SUBPATH参数，
    也不像update那边保留"不传repo_dir时退回到BASE_DIR"的旧行为。

    repo_dir必须是一个已经clone好、能fast-forward push到origin/master的
    独立git仓库（不是foxzen-blog自己），且默认分支必须是"master"（见
    git_publish.py的B3检查）。push认证读环境变量GITHUB_TOKEN（跟app.py/
    generate_status_page.py同一个约定）。

    返回git_publish.commit_and_push()的原始返回值，不吞掉任何已经分类好
    的错误信息；GITHUB_TOKEN未设置时返回结构相同的credentials_missing
    错误，不抛异常。
    """
    output_dir = build_status_page(repo_dir, write_cname=False)
    verify_status_page(output_dir, expect_cname=False)

    push_token = os.environ.get("GITHUB_TOKEN", "")
    if not push_token:
        return {"pushed": False, "error_category": "credentials_missing",
                "detail": "环境变量GITHUB_TOKEN未设置，无法推送。"}

    import git_publish
    commit_message = f"更新状态页面外壳 ({datetime.now().strftime('%Y-%m-%d %H:%M:%S')})"
    return git_publish.commit_and_push(
        repo_dir, ".", GIT_BOT_NAME, GIT_BOT_EMAIL,
        commit_message, push_token, GIT_PUSH_TIMEOUT_SECONDS,
    )


def main():
    parser = argparse.ArgumentParser(description="生成（可选：发布）status.foxzen.me的静态外壳页")
    parser.add_argument(
        "--publish", metavar="REPO_DIR", type=Path, default=None,
        help="生成后立即commit+push到指定的独立卫星仓库目录(比如GitHub Pages"
             "专用的foxzen-status仓库的本地checkout路径)，复用git_publish.py，"
             "需要环境变量GITHUB_TOKEN。不写CNAME(GitHub Pages这一侧保持"
             "默认github.io地址)。跟--output-dir互斥。不加任何参数时只在"
             "本地生成publish_status/(带CNAME预览)，不做任何git操作。",
    )
    parser.add_argument(
        "--output-dir", metavar="OUTPUT_DIR", type=Path, default=None,
        help="只在本地生成到指定目录，不写CNAME、不做任何git操作——用于"
             "GreenCloud等直接用Nginx按status.foxzen.me真实域名提供服务的"
             "场景(见nginx-conf/default.conf里对应的server块)。跟--publish"
             "互斥。",
    )
    args = parser.parse_args()

    if args.publish is not None and args.output_dir is not None:
        parser.error("--publish 和 --output-dir 不能同时使用（分别对应"
                      "GitHub Pages卫星仓库和GreenCloud两个不同的独立渠道）")

    if args.output_dir is not None:
        output_dir = build_status_page(args.output_dir, write_cname=False)
        verify_status_page(output_dir, expect_cname=False)
        print(f"已生成并通过安全检查 {output_dir}（不含CNAME，供GreenCloud等"
              f"直接按{HOST}真实域名提供服务的独立渠道使用）")
        return

    if args.publish is None:
        output_dir = build_status_page()
        verify_status_page(output_dir)
        print(f"已生成并通过安全检查 {output_dir}（host={HOST}）")
        return

    result = publish_status_page(args.publish)
    if result["pushed"]:
        print(f"已生成并推送，commit={result['commit_sha']}，push_state={result['push_state']}。")
    else:
        print(f"生成成功但推送失败（{result['error_category']}）: {result['detail']}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
