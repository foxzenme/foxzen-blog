// blog-mirror 前端逻辑：搜索、标签/日期筛选、刷新、打包下载、选择下载
(function () {
  const app = document.getElementById("app");

  const state = { selected: new Set(), page: 1, pageSize: 10, year: null, month: null };
  // year/month筛选状态在URL里(?year=2026&month=8)，不是新加一套单独的前端状态存储——
  // 这样刷新页面/复制链接/浏览器前进后退都能拿到同一份状态，不需要account、不需要
  // 服务端session。下面这行必须在parseArchiveParamsFromLocation定义之前调用也没问题：
  // 这整个文件是一个大IIFE，function声明会整体提升到IIFE作用域顶部。
  Object.assign(state, parseArchiveParamsFromLocation());

  function el(tag, attrs, children) {
    const e = document.createElement(tag);
    if (attrs) for (const k in attrs) {
      if (k === "text") e.textContent = attrs[k];
      else e.setAttribute(k, attrs[k]);
    }
    (children || []).forEach((c) => e.appendChild(c));
    return e;
  }

  // 从服务器的 Content-Disposition 响应头里取真实文件名，取不到才用fallback。
  // 之前这里是写死的"standalone.html"/"blog-mirror-standalone.zip"，
  // 导致不管后端返回什么文件名，浏览器保存对话框永远显示同一个名字。
  function filenameFromDisposition(disposition, fallback) {
    if (!disposition) return fallback;
    const m = /filename="?([^";]+)"?/.exec(disposition);
    return m ? m[1] : fallback;
  }

  // ===== 年份/月份筛选 =====
  // 数据来自已有的 GET /api/archive（db.get_archive_index()，按posts.published分组，
  // 不是新数据库/新字段）。筛选本身复用已有的 GET /api/search?year=&month=，下载筛选
  // 结果复用已有的 POST /api/download/selected（_resolve_scope()早已支持year/month，
  // 这次只是首页从来没有UI去调用它）。

  // 和fetch_blog.py::I18N_BLOCK用同一个localStorage key/同一套zh前缀检测算法/同一套
  // data-i18n(纯文本)、data-i18n-tpl(需要拼数字，配合data-*属性)、data-i18n-placeholder
  // (input placeholder)约定，保证在文章页手动选过的语言，回到首页时这里也一致。不直接
  // 复用I18N_BLOCK那段代码——那是嵌进文章页HTML模板的Python字符串，首页是独立的静态JS
  // 文件，两边运行时环境不同，没有现成的共享点；这里独立实现一份，跟github.foxzen.me/
  // cf.foxzen.me用的static_pages/pages-index.js（同样独立实现一份）是对称关系。
  // 首页右上角语言切换按钮见下面setFoxzenLang()/wireLangToggle()——本次全站UI国际化
  // 新增，取代了之前"首页不提供切换按钮，只有筛选文案双语"这条范围限制。
  const FOXZEN_LANG_KEY = "foxzen_lang";

  function detectDefaultFoxzenLang() {
    const langs = (navigator.languages && navigator.languages.length) ? navigator.languages : [navigator.language || ""];
    for (let i = 0; i < langs.length; i++) {
      if (/^zh/i.test(langs[i])) return "zh";
    }
    return "en";
  }

  function getFoxzenLang() {
    try {
      const saved = localStorage.getItem(FOXZEN_LANG_KEY);
      if (saved === "zh" || saved === "en") return saved;
    } catch (e) {}
    return detectDefaultFoxzenLang();
  }

  let FOXZEN_LANG = getFoxzenLang();  // let而不是const：setFoxzenLang()要在运行时改它
  const MONTH_NAMES_EN = ["January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December"];

  const ARCHIVE_STRINGS = {
    zh: {
      filterByYear: "按年份筛选",
      filterByMonth: "按月份筛选",
      allPosts: "全部文章",
      allMonths: "全部月份",
      noPostsInMonth: "这个月份没有文章。",
      downloadFiltered: (count) => `下载这 ${count} 篇文章`,
      countMonth: (year, month, count) => `${year}年${month}月 · ${count}篇`,
      countYear: (year, count) => `${year}年 · ${count}篇`,
    },
    en: {
      filterByYear: "Filter by year",
      filterByMonth: "Filter by month",
      allPosts: "All posts",
      allMonths: "All months",
      noPostsInMonth: "No posts in this month.",
      downloadFiltered: (count) => `Download these ${count} posts`,
      countMonth: (year, month, count) => `${MONTH_NAMES_EN[month - 1]} ${year} · ${count} posts`,
      countYear: (year, count) => `${year} · ${count} posts`,
    },
  };
  let ARCHIVE_T = ARCHIVE_STRINGS[FOXZEN_LANG] || ARCHIVE_STRINGS.en;  // let：setFoxzenLang()里会重新指向新语言那一份

  // 首页服务端渲染内容(fetch_blog.py::INDEX_TEMPLATE/_render_favorite_blogs_html())
  // 里用data-i18n/data-i18n-tpl标记的文案，用上面已经检测好的FOXZEN_LANG在客户端
  // 替换成对应语言——属性名跟文章页I18N_BLOCK的data-i18n/data-i18n-tpl约定保持
  // 一致，但这是独立的一小段实现（不是导入I18N_BLOCK），原因同上：两边是不同的
  // 静态交付载体，没有共享点。本次全站UI国际化把原本只有fav_blogs_heading一个key
  // 的这个字典扩展成覆盖首页全部服务端渲染UI文案，同时新增setFoxzenLang()支持
  // 右上角按钮点击后不刷新页面动态切换（原来这里只在页面加载时应用一次）。
  // 博客名称/URL本身来自favorite_blogs.txt、格言来自quotes.txt，都不是这里的
  // 翻译对象，永远不翻译；Internet Archive域名验证行同理，不套用data-i18n。
  const HOMEPAGE_I18N_STRINGS = {
    zh: {
      fav_blogs_heading: "🔗 我最喜欢的博客",
      entries_heading: "🔗 FoxZen 的其他入口",
      entries_intro: "FoxZen 还提供以下公开入口，分别承担不同用途；全部完全免费开放，不涉及付费、会员或管理员特权。",
      entry_mirror_name: "当前镜像 / 主要文章入口",
      entry_mirror_desc: "你正在访问的镜像站，同步自主站内容。",
      entry_main_name: "正式主站",
      entry_main_desc: "FoxZen 的正式主站。",
      entry_backup_name: "源站备用入口",
      entry_backup_desc: "绕开 Cloudflare 直连 VPS 源站，主站/Cloudflare 访问异常时可以用这个地址确认源站本身是否正常。",
      entry_status_name: "网站状态与公告（规划中，尚未上线）",
      entry_status_desc: "用于发布维护、故障、恢复等系统级公告（不是文章更新记录），暂未正式部署。",
      entry_github_name: "GitHub 静态镜像",
      entry_github_desc: "基于 GitHub Pages 的独立静态文章镜像，不依赖 VPS。",
      entry_cf_name: "Cloudflare 静态镜像",
      entry_cf_desc: "基于 Cloudflare Pages 的第二个独立静态发布入口。",
      home_mirror_intro_prefix: "本站为 ",
      home_mirror_intro_link: "主站",
      home_mirror_intro_suffix: " 的静态镜像，内容定期同步。",
      archive_note_download: "如果这些文章对你有帮助，欢迎离线保存。知识的价值不仅在于被阅读，也在于能够长期保存和再次使用。欢迎下载、离线阅读和长期保存。转载或引用请注明来源。",
      contact_email_prefix: "联系邮箱：",
      contact_note_prefix: "（注意：",
      contact_note_bold: "foxzen@gmail.com 不是我",
      contact_note_suffix: "，请勿误认）",
      policy_no_paid_promo: "狐斋志异不接受商业付费推荐，也不会因为收取费用而推荐某个产品或服务。",
      policy_genuine_use: "本站推荐的产品、服务和工具，原则上都是我自己使用过，并认为确实值得推荐的。",
      policy_contact_prefix: "如果你是一名预算非常有限的独立开发者，确实需要一些推广，但无力承担商业广告费用，欢迎",
      policy_contact_link: "直接联系我",
      policy_contact_suffix: "。我可以在实际试用你的产品后，根据自己的真实体验决定是否推荐。",
      policy_no_buy: "推荐不能购买，赞助也不会获得推荐权限。",
      policy_final_say: "我最终推荐与否，只取决于产品本身是否值得让读者知道。",
      stats_post_count_label: "文章总数",
      stats_download_count_label: "全站打包下载",
      stats_export_size_label: "全部导出预估体积",
      stats_visits_label: "访问量",
      leaderboard_top_clicked: "🔥 点击排行榜",
      leaderboard_top_downloaded: "📥 下载排行榜",
      footer_updated_label: "最后更新",
      easter_egg_link: "这个网站藏着一只找不到路的狐狸",
      rank_no_data: "暂无数据",
      search_placeholder: "搜索标题或正文...",
      tag_placeholder: "标签筛选",
      search_btn: "搜索",
      page_size_10: "每页10篇",
      page_size_20: "每页20篇",
      download_all_btn: "打包下载全站",
      download_selected_btn: "下载已勾选",
      export_selected_btn: "导出离线版(已勾选)",
      export_tag_btn: "导出离线版(当前标签)",
      export_all_btn: "导出离线版(全部)",
      refresh_label: "内容刷新",
      refreshing_text: "刷新中...",
      cache_purge_hint: "本站内容刚刚更新但仍显示旧版本时，可尝试刷新缓存。",
      cache_purge_btn: "刷新本站缓存",
      cache_purge_checking_text: "检查中...",
      cache_purge_no_changes_text: "当前没有新的内容变化，无需刷新缓存。",
      cache_purge_success_text: "缓存已刷新为最新版本。",
      cache_purge_failure_text: "刷新缓存失败，请稍后再试。",
      cache_purge_busy_text: "已有一次刷新正在进行，请稍后再试。",
      prev_page: "← 上一页",
      next_page: "下一页 →",
      stats_post_count_value: (count) => `${count} 篇`,
      stats_download_count_value: (count) => `${count} 次`,
      stats_export_size_value: (size) => `约 ${size}（未压缩，实际zip会更小）`,
      stats_visits_value: (today, week, month, year, total) =>
        `今日 ${today} · 本周 ${week} · 本月 ${month} · 今年 ${year} · 累计 ${total}`,
      rank_views: (count) => `（${count} 次浏览）`,
      rank_downloads: (count) => `（${count} 次下载）`,
      post_stats: (views, downloads, size, finishes) =>
        ` · 浏览${views}次 · 下载${downloads}次 · 离线版${size} · 完读${finishes}次`,
      pagination_info: (page, totalPages, total) => `第 ${page} / ${totalPages} 页，共 ${total} 篇`,
      refresh_target_btn: (target) => `刷新 ${target}`,
      alert_need_tag: "请先在标签框输入要导出的标签",
      confirm_export_all: "全站导出Base64离线版可能体积较大、耗时较长，确认继续？",
      alert_request_failed: (err) => `请求失败: ${err}`,
      alert_request_failed_backend: (err) => `请求失败，后端可能未启动: ${err}`,
      alert_search_error: (msg) => `搜索出错: ${msg}`,
      alert_download_failed: "下载失败",
      alert_select_one_post: "请先勾选至少一篇文章",
      alert_select_one_or_scope: "请先勾选至少一篇文章，或改用「当前标签」/「全部」导出",
      alert_export_failed: (err) => `导出失败: ${err}`,
      alert_cooldown: (seconds) => `距离上次刷新不足5分钟，请${seconds}秒后再试`,
      alert_refresh_success_detail: (detail) => `刷新成功：${detail}`,
      alert_refresh_success_plain: "刷新成功",
      view_progress_label: "\n查看进度: ",
      detail_label: "\n详情: ",
      alert_refresh_failed_prefix: "刷新失败: ",
    },
    en: {
      fav_blogs_heading: "🔗 My Favorite Blogs",
      entries_heading: "🔗 FoxZen's Other Entry Points",
      entries_intro: "FoxZen also provides the following public entry points, each serving a different purpose; all are completely free, with no payment, membership, or admin privileges involved.",
      entry_mirror_name: "Current mirror / Main article entry",
      entry_mirror_desc: "The mirror site you're currently visiting, synced from the main site's content.",
      entry_main_name: "Official main site",
      entry_main_desc: "FoxZen's official main site.",
      entry_backup_name: "Backup origin entry",
      entry_backup_desc: "Bypasses Cloudflare to connect directly to the VPS origin; use this address to check whether the origin itself is healthy when the main site/Cloudflare has issues.",
      entry_status_name: "Site status & announcements (planned, not yet live)",
      entry_status_desc: "For publishing system-level announcements like maintenance, incidents, and recovery (not article update logs); not yet formally deployed.",
      entry_github_name: "GitHub static mirror",
      entry_github_desc: "An independent static article mirror based on GitHub Pages, not dependent on the VPS.",
      entry_cf_name: "Cloudflare static mirror",
      entry_cf_desc: "A second independent static publishing entry based on Cloudflare Pages.",
      home_mirror_intro_prefix: "This site is a static mirror of the ",
      home_mirror_intro_link: "main site",
      home_mirror_intro_suffix: ", with content synced periodically.",
      archive_note_download: "If these articles are useful to you, feel free to save them offline. Knowledge has value not only in being read, but in being preserved and reused over time. Downloading, reading offline, and long-term preservation are all welcome. Please credit the source when reposting or quoting.",
      contact_email_prefix: "Contact email: ",
      contact_note_prefix: "(Note: ",
      contact_note_bold: "foxzen@gmail.com is not me",
      contact_note_suffix: ", please don't confuse the two)",
      policy_no_paid_promo: "FoxZen does not accept paid promotions, and never recommends a product or service because of payment received.",
      policy_genuine_use: "Products, services, and tools recommended here are, in principle, ones I've personally used and genuinely believe are worth recommending.",
      policy_contact_prefix: "If you're an independent developer on a very limited budget who needs some exposure but can't afford commercial advertising, feel free to ",
      policy_contact_link: "contact me directly",
      policy_contact_suffix: ". After actually trying your product, I'll decide whether to recommend it based on my genuine experience.",
      policy_no_buy: "Recommendations cannot be bought, and sponsorship does not grant recommendation rights.",
      policy_final_say: "Whether I ultimately recommend something depends solely on whether the product itself is worth readers knowing about.",
      stats_post_count_label: "Total posts",
      stats_download_count_label: "Site-wide downloads",
      stats_export_size_label: "Estimated total export size",
      stats_visits_label: "Visits",
      leaderboard_top_clicked: "🔥 Most Viewed",
      leaderboard_top_downloaded: "📥 Most Downloaded",
      footer_updated_label: "Last updated",
      easter_egg_link: "This site is hiding a fox that can't find its way",
      rank_no_data: "No data yet",
      search_placeholder: "Search title or content...",
      tag_placeholder: "Filter by tag",
      search_btn: "Search",
      page_size_10: "10 per page",
      page_size_20: "20 per page",
      download_all_btn: "Download entire site",
      download_selected_btn: "Download selected",
      export_selected_btn: "Export offline (selected)",
      export_tag_btn: "Export offline (current tag)",
      export_all_btn: "Export offline (all)",
      refresh_label: "Content refresh",
      refreshing_text: "Refreshing...",
      cache_purge_hint: "If the site was just updated but still shows an old version, try refreshing the cache.",
      cache_purge_btn: "Refresh site cache",
      cache_purge_checking_text: "Checking...",
      cache_purge_no_changes_text: "There are no new content changes right now, no need to refresh the cache.",
      cache_purge_success_text: "The cache has been refreshed to the latest version.",
      cache_purge_failure_text: "Failed to refresh the cache, please try again later.",
      cache_purge_busy_text: "A refresh is already in progress, please try again shortly.",
      prev_page: "← Prev",
      next_page: "Next →",
      stats_post_count_value: (count) => `${count}`,
      stats_download_count_value: (count) => `${count}`,
      stats_export_size_value: (size) => `~${size} (uncompressed; actual zip will be smaller)`,
      stats_visits_value: (today, week, month, year, total) =>
        `Today ${today} · This week ${week} · This month ${month} · This year ${year} · Total ${total}`,
      rank_views: (count) => `(${count} views)`,
      rank_downloads: (count) => `(${count} downloads)`,
      post_stats: (views, downloads, size, finishes) =>
        ` · ${views} views · ${downloads} downloads · offline ${size} · ${finishes} finished`,
      pagination_info: (page, totalPages, total) => `Page ${page} / ${totalPages}, ${total} posts total`,
      refresh_target_btn: (target) => `Refresh ${target}`,
      alert_need_tag: "Please enter a tag in the tag box first",
      confirm_export_all: "Exporting the entire site as a Base64 offline version may be large and slow. Continue?",
      alert_request_failed: (err) => `Request failed: ${err}`,
      alert_request_failed_backend: (err) => `Request failed — the backend may not be running: ${err}`,
      alert_search_error: (msg) => `Search error: ${msg}`,
      alert_download_failed: "Download failed",
      alert_select_one_post: "Please select at least one post first",
      alert_select_one_or_scope: "Please select at least one post, or use “current tag”/“all” export instead",
      alert_export_failed: (err) => `Export failed: ${err}`,
      alert_cooldown: (seconds) => `Less than 5 minutes since the last refresh, please try again in ${seconds}s`,
      alert_refresh_success_detail: (detail) => `Refresh succeeded: ${detail}`,
      alert_refresh_success_plain: "Refresh succeeded",
      view_progress_label: "\nView progress: ",
      detail_label: "\nDetails: ",
      alert_refresh_failed_prefix: "Refresh failed: ",
    },
  };

  // key -> 需要按顺序从DOM节点读取的data-*属性名列表，供下面data-i18n-tpl分支用，
  // 只在这里维护一份映射，不在每个使用点各自硬编码参数顺序。
  const HOMEPAGE_I18N_TPL_ARGS = {
    stats_post_count_value: ["count"],
    stats_download_count_value: ["count"],
    stats_export_size_value: ["size"],
    stats_visits_value: ["today", "week", "month", "year", "total"],
    rank_views: ["count"],
    rank_downloads: ["count"],
    post_stats: ["views", "downloads", "size", "finishes"],
    pagination_info: ["page", "total-pages", "total"],
    refresh_target_btn: ["target"],
  };

  // alert()/confirm()这类没有常驻DOM节点可以挂data-i18n的地方，直接按当前
  // FOXZEN_LANG查表取文案；每次调用都重新读HOMEPAGE_I18N_STRINGS[FOXZEN_LANG]，
  // 不缓存，所以语言切换后立刻生效，不存在ARCHIVE_T那种需要手动重新赋值的问题。
  function homeT(key) {
    const dict = HOMEPAGE_I18N_STRINGS[FOXZEN_LANG] || HOMEPAGE_I18N_STRINGS.en;
    return dict[key];
  }

  function applyHomepageI18n() {
    // 合并ARCHIVE_STRINGS(年/月筛选器专用，test_archive_filter.py按名字断言过
    // 这个字典必须存在，不能改名/合并掉)和HOMEPAGE_I18N_STRINGS(本文件其余全部
    // UI文案)，这样两边字典各自维护自己的领域，同一个sweep函数都能查到。
    const archiveDict = ARCHIVE_STRINGS[FOXZEN_LANG] || ARCHIVE_STRINGS.en;
    const homeDict = HOMEPAGE_I18N_STRINGS[FOXZEN_LANG] || HOMEPAGE_I18N_STRINGS.en;
    const dict = Object.assign({}, archiveDict, homeDict);

    document.querySelectorAll("[data-i18n]").forEach((node) => {
      const key = node.getAttribute("data-i18n");
      if (typeof dict[key] === "string") node.textContent = dict[key];
    });

    document.querySelectorAll("[data-i18n-placeholder]").forEach((node) => {
      const key = node.getAttribute("data-i18n-placeholder");
      if (typeof dict[key] === "string") node.setAttribute("placeholder", dict[key]);
    });

    document.querySelectorAll("[data-i18n-tpl]").forEach((node) => {
      const key = node.getAttribute("data-i18n-tpl");
      const fn = dict[key];
      if (typeof fn !== "function") return;
      const argNames = HOMEPAGE_I18N_TPL_ARGS[key] || [];
      const args = argNames.map((name) => node.getAttribute("data-" + name) || "0");
      node.textContent = fn(...args);
    });

    document.documentElement.setAttribute("lang", FOXZEN_LANG === "zh" ? "zh-CN" : "en");
    document.querySelectorAll("[data-lang-btn]").forEach((btn) => {
      btn.classList.toggle("active", btn.getAttribute("data-lang-btn") === FOXZEN_LANG);
    });
  }

  // 只解析year/month这两个参数，不吞掉/覆盖URL上其它可能存在的参数（见
  // updateLocationForArchive里用同一个URLSearchParams基底做增量修改）。
  // year/month格式不对时一律当成"没有筛选"处理，不把非法值传给后端——
  // 后端_year_month_range()对非法月份的兜底是"整个筛选条件失效、退化成不筛选"，
  // 而不是报错，为避免用户手改URL传入非法值时误以为筛选生效了，这里在
  // 前端先做一次同样标准的校验。
  function parseArchiveParamsFromLocation() {
    const params = new URLSearchParams(location.search);
    const rawYear = params.get("year");
    const rawMonth = params.get("month");
    let year = null;
    let month = null;
    if (rawYear && /^\d{4}$/.test(rawYear)) {
      year = parseInt(rawYear, 10);
      if (rawMonth) {
        const monthNum = parseInt(rawMonth, 10);
        if (!isNaN(monthNum) && monthNum >= 1 && monthNum <= 12) month = monthNum;
      }
    }
    return { year, month };
  }

  function updateLocationForArchive(push) {
    const params = new URLSearchParams(location.search);
    if (state.year) params.set("year", String(state.year));
    else params.delete("year");
    if (state.year && state.month) params.set("month", String(state.month));
    else params.delete("month");
    const qs = params.toString();
    const url = qs ? `${location.pathname}?${qs}` : location.pathname;
    if (push) history.pushState({ year: state.year, month: state.month }, "", url);
    else history.replaceState({ year: state.year, month: state.month }, "", url);
  }

  let archiveIndexCache = null;
  const archiveBarRefs = {};
  let lastArchiveSummaryTotal = null;  // setFoxzenLang()重新拼summary文案用，不重新发请求

  function populateYearSelect(select, years) {
    select.innerHTML = "";
    select.appendChild(el("option", { value: "", text: ARCHIVE_T.allPosts }));
    years.forEach((y) => {
      const opt = el("option", { value: String(y.year), text: `${y.year} (${y.count})` });
      if (state.year === y.year) opt.setAttribute("selected", "selected");
      select.appendChild(opt);
    });
  }

  function populateMonthSelect(select, year) {
    select.innerHTML = "";
    select.appendChild(el("option", { value: "", text: ARCHIVE_T.allMonths }));
    const yearEntry = (archiveIndexCache || []).find((y) => y.year === year);
    select.disabled = !year || !yearEntry;
    if (!yearEntry) return;
    yearEntry.months.forEach((m) => {
      const label = FOXZEN_LANG === "zh" ? `${m.month}月 (${m.count})` : `${MONTH_NAMES_EN[m.month - 1]} (${m.count})`;
      const opt = el("option", { value: String(m.month), text: label });
      if (state.month === m.month) opt.setAttribute("selected", "selected");
      select.appendChild(opt);
    });
  }

  function buildArchiveFilterBar() {
    const wrap = el("div", { style: "margin-bottom:20px;padding:12px 16px;background:#f7f7f7;border-radius:8px;" });
    const bar = el("div", { style: "display:flex;gap:8px;flex-wrap:wrap;align-items:center;" });

    const yearSelect = el("select", { id: "archive-year", style: "padding:6px;" });
    yearSelect.setAttribute("aria-label", ARCHIVE_T.filterByYear);
    const monthSelect = el("select", { id: "archive-month", style: "padding:6px;" });
    monthSelect.setAttribute("aria-label", ARCHIVE_T.filterByMonth);
    const summary = el("div", { id: "archive-summary", style: "width:100%;font-size:0.9em;color:#555;margin-top:8px;" });

    yearSelect.onchange = () => {
      const val = yearSelect.value;
      state.year = val ? parseInt(val, 10) : null;
      state.month = null;
      state.page = 1;
      populateMonthSelect(monthSelect, state.year);
      updateLocationForArchive(true);
      doSearch();
    };
    monthSelect.onchange = () => {
      const val = monthSelect.value;
      state.month = val ? parseInt(val, 10) : null;
      state.page = 1;
      updateLocationForArchive(true);
      doSearch();
    };

    bar.appendChild(el("span", { style: "font-size:0.9em;color:#666;", text: ARCHIVE_T.filterByYear, "data-i18n": "filterByYear" }));
    bar.appendChild(yearSelect);
    bar.appendChild(el("span", { style: "font-size:0.9em;color:#666;", text: ARCHIVE_T.filterByMonth, "data-i18n": "filterByMonth" }));
    bar.appendChild(monthSelect);
    wrap.appendChild(bar);
    wrap.appendChild(summary);

    archiveBarRefs.yearSelect = yearSelect;
    archiveBarRefs.monthSelect = monthSelect;
    archiveBarRefs.summary = summary;
    populateMonthSelect(monthSelect, state.year);

    fetch("/api/archive").then((r) => r.json()).then((data) => {
      archiveIndexCache = data.years || [];
      populateYearSelect(yearSelect, archiveIndexCache);
      populateMonthSelect(monthSelect, state.year);
    }).catch(() => {
      // /api/archive取不到数据时保留空的年份下拉框(只有"全部文章"一项)，
      // 不影响下面搜索/文章列表区域正常工作(那部分不依赖这个接口)。
    });

    return wrap;
  }

  // 筛选未命中/命中0篇文章时的提示，以及"下载筛选结果"按钮——按需求，0篇结果时
  // 这个按钮不渲染(等同于"不出现"，比渲染一个disabled按钮更直接)。没有选年份时
  // (state.year为空)不显示这一整块，保持首页原有外观不变。
  function updateArchiveSummary(total) {
    const summary = archiveBarRefs.summary;
    if (!summary) return;
    summary.innerHTML = "";
    if (!state.year) return;

    if (total === 0) {
      summary.appendChild(el("span", { text: ARCHIVE_T.noPostsInMonth }));
      return;
    }
    const countText = state.month
      ? ARCHIVE_T.countMonth(state.year, state.month, total)
      : ARCHIVE_T.countYear(state.year, total);
    summary.appendChild(el("span", { text: countText }));
    const downloadBtn = el("button", { type: "button", text: ARCHIVE_T.downloadFiltered(total), style: "margin-left:12px;" });
    downloadBtn.onclick = () => doDownloadFiltered();
    summary.appendChild(downloadBtn);
  }

  // 下载"当前筛选结果"，跟下面的doDownloadSelected()(勾选下载)是两个独立入口，
  // 都打到POST /api/download/selected，body形状不同(year/month vs post_ids)，
  // 后端_resolve_scope()按body里出现的字段分流，互不影响；返回的文件名由
  // app.py::_selected_zip_filename()决定(年/月筛选时是"2026-08.zip"这种形式，
  // 跟整站下载的"blog-mirror-full.zip"、勾选下载的"blog-mirror-selected.zip"
  // 都不会互相覆盖)。
  async function doDownloadFiltered() {
    if (!state.year) return;
    const body = { year: state.year };
    if (state.month) body.month = state.month;
    let resp;
    try {
      resp = await fetch("/api/download/selected", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
    } catch (e) {
      alert(homeT("alert_request_failed")(e));
      return;
    }
    if (!resp.ok) {
      alert(homeT("alert_download_failed"));
      return;
    }
    const disposition = resp.headers.get("Content-Disposition") || "";
    const filename = filenameFromDisposition(disposition, "blog-mirror-selected.zip");
    const blob = await resp.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = filename;
    a.click();
    URL.revokeObjectURL(url);
  }

  // 右上角按钮点击后调用：写localStorage、更新FOXZEN_LANG，重新走一遍
  // applyHomepageI18n()的sweep(处理绝大多数静态/模板类文案)，再单独刷新
  // 年/月筛选器这几个"选项列表长度随数据变化、没法用固定data-i18n-tpl模板
  // 覆盖"的部分——复用已经缓存的archiveIndexCache/state.year/state.month/
  // lastArchiveSummaryTotal，不重新请求/api/archive、/api/search，语言切换
  // 本身不应该产生新的网络请求。
  function setFoxzenLang(lang) {
    if (lang !== "zh" && lang !== "en") return;
    FOXZEN_LANG = lang;
    try { localStorage.setItem(FOXZEN_LANG_KEY, lang); } catch (e) {}
    ARCHIVE_T = ARCHIVE_STRINGS[FOXZEN_LANG] || ARCHIVE_STRINGS.en;
    applyHomepageI18n();
    if (archiveBarRefs.yearSelect) {
      archiveBarRefs.yearSelect.setAttribute("aria-label", ARCHIVE_T.filterByYear);
      populateYearSelect(archiveBarRefs.yearSelect, archiveIndexCache || []);
    }
    if (archiveBarRefs.monthSelect) {
      archiveBarRefs.monthSelect.setAttribute("aria-label", ARCHIVE_T.filterByMonth);
      populateMonthSelect(archiveBarRefs.monthSelect, state.year);
    }
    if (lastArchiveSummaryTotal !== null) updateArchiveSummary(lastArchiveSummaryTotal);
  }

  function wireLangToggle() {
    document.querySelectorAll("[data-lang-btn]").forEach((btn) => {
      btn.addEventListener("click", () => setFoxzenLang(btn.getAttribute("data-lang-btn")));
    });
  }

  function buildToolbar() {
    const bar = el("div", { style: "margin-bottom:20px;display:flex;gap:8px;flex-wrap:wrap;align-items:center;" });

    const q = el("input", { type: "text", id: "q", placeholder: "搜索标题或正文...", style: "flex:1;min-width:200px;padding:6px;", "data-i18n-placeholder": "search_placeholder" });
    const tag = el("input", { type: "text", id: "tag", placeholder: "标签筛选", style: "width:120px;padding:6px;", "data-i18n-placeholder": "tag_placeholder" });
    const from = el("input", { type: "date", id: "from", style: "padding:6px;" });
    const to = el("input", { type: "date", id: "to", style: "padding:6px;" });
    const searchBtn = el("button", { text: "搜索", "data-i18n": "search_btn" });
    searchBtn.onclick = () => { state.page = 1; doSearch(); };

    const pageSizeSelect = el("select", { id: "page-size", style: "padding:6px;" });
    [["10", "每页10篇", "page_size_10"], ["20", "每页20篇", "page_size_20"]].forEach(([val, label, key]) => {
      const opt = el("option", { value: val, text: label, "data-i18n": key });
      if (val === String(state.pageSize)) opt.setAttribute("selected", "selected");
      pageSizeSelect.appendChild(opt);
    });
    pageSizeSelect.onchange = () => {
      state.pageSize = parseInt(pageSizeSelect.value, 10);
      state.page = 1;
      doSearch();
    };

    const downloadAllBtn = el("button", { text: "打包下载全站", "data-i18n": "download_all_btn" });
    downloadAllBtn.onclick = () => { window.location.href = "/api/download/all"; };

    const downloadSelectedBtn = el("button", { text: "下载已勾选", "data-i18n": "download_selected_btn" });
    downloadSelectedBtn.onclick = doDownloadSelected;

    const exportSelectedBtn = el("button", { text: "导出离线版(已勾选)", "data-i18n": "export_selected_btn" });
    exportSelectedBtn.onclick = () => doExportBase64({ post_ids: Array.from(state.selected) });

    const exportTagBtn = el("button", { text: "导出离线版(当前标签)", "data-i18n": "export_tag_btn" });
    exportTagBtn.onclick = () => {
      const t = document.getElementById("tag").value;
      if (!t) { alert(homeT("alert_need_tag")); return; }
      doExportBase64({ tag: t });
    };

    const exportAllBtn = el("button", { text: "导出离线版(全部)", "data-i18n": "export_all_btn" });
    exportAllBtn.onclick = () => {
      if (!confirm(homeT("confirm_export_all"))) return;
      doExportBase64({ all: true });
    };

    [q, tag, from, to, searchBtn, pageSizeSelect, downloadAllBtn, downloadSelectedBtn,
     exportSelectedBtn, exportTagBtn, exportAllBtn].forEach((x) => bar.appendChild(x));
    return bar;
  }

  // 4-target公开刷新入口：mirror/backup/github/cf四个按钮全部显示，不隐藏
  // 任何一个。当前页面在mirror.foxzen.me/backup.foxzen.me上，两者都是同一个
  // Flask后端（target在URL路径里，不再靠Host头猜），所以全部用相对路径
  // fetch()——不管当前停在mirror还是backup，请求都会正确落到同一个后端，
  // 由后端按target参数本身决定要做什么，不需要跨域（github.foxzen.me/
  // cf.foxzen.me没有自己的后端，那两个站点用的是static_pages/pages-refresh.js，
  // 走绝对地址+CORS，是完全独立的另一份实现，这里不复用）。
  const REFRESH_TARGETS = ["mirror", "backup", "github", "cf"];

  // 公共"刷新本站缓存"按钮：面向普通访客，不是上面buildRefreshWidget()那4个
  // 偏技术向的target刷新入口——文案刻意不提Cloudflare/CDN/Purge这类术语，
  // 只描述"页面还是旧版本时可以点这个"。点击只会POST /api/purge-cache，
  // 后端会先判断是否存在真实内容变化、且该变化尚未被成功purge过，没有才
  // 会真的调用Cloudflare（见app.py::purge_cache()），这里的JS本身不做任何
  // "要不要刷新"的判断，只负责发请求和展示结果。
  function buildCachePurgeWidget() {
    const wrap = el("div", { style: "margin-bottom:20px;padding:12px 16px;background:#f7f7f7;border-radius:8px;" });
    const hint = el("div", {
      style: "font-size:0.85em;color:#666;margin-bottom:8px;",
      text: "本站内容刚刚更新但仍显示旧版本时，可尝试刷新缓存。",
      "data-i18n": "cache_purge_hint",
    });
    const btn = el("button", { type: "button", text: "刷新本站缓存", "data-i18n": "cache_purge_btn" });
    btn.onclick = () => doPurgeCache(btn);

    wrap.appendChild(hint);
    wrap.appendChild(btn);
    return wrap;
  }

  function buildRefreshWidget() {
    const wrap = el("div", { style: "margin-bottom:20px;padding:12px 16px;background:#f7f7f7;border-radius:8px;" });
    const label = el("div", { style: "font-size:0.9em;color:#666;margin-bottom:8px;", text: "内容刷新", "data-i18n": "refresh_label" });
    const bar = el("div", { style: "display:flex;gap:8px;flex-wrap:wrap;align-items:center;" });

    const current = current_target();
    REFRESH_TARGETS.forEach((target) => {
      const btn = el("button", { type: "button", text: `刷新 ${target}`, "data-i18n-tpl": "refresh_target_btn", "data-target": target });
      if (target === current) {
        btn.style.fontWeight = "bold";
        btn.style.outline = "2px solid #1a73e8";
      }
      btn.onclick = () => doRefresh(target, btn);
      bar.appendChild(btn);
    });

    wrap.appendChild(label);
    wrap.appendChild(bar);
    return wrap;
  }

  function humanSize(numBytes) {
    let n = Number(numBytes) || 0;
    const units = ["B", "KB", "MB", "GB"];
    let i = 0;
    while (n >= 1024 && i < units.length - 1) {
      n /= 1024;
      i++;
    }
    return i === 0 ? `${n.toFixed(0)}${units[i]}` : `${n.toFixed(1)}${units[i]}`;
  }

  function renderList(posts) {
    const ul = el("ul", { id: "post-list" });
    posts.forEach((p) => {
      const cb = el("input", { type: "checkbox", "data-id": p.post_id });
      cb.onchange = (e) => {
        if (e.target.checked) state.selected.add(p.post_id);
        else state.selected.delete(p.post_id);
      };
      // 优先用canonical_path（年/月/slug格式的正式地址），没有才退回短号/旧路径
      const href = p.canonical_path ? `/${p.canonical_path}.html`
        : (p.number ? `/${p.number}/` : `/posts/${p.post_id}/`);
      const link = el("a", { href, text: p.title, target: "_blank", rel: "noopener" });
      const tagsHtml = (p.tags || []).map((t) => `#${t}`).join(" ");
      const dateText = ` ${p.published} ${tagsHtml}`;
      const views = p.click_count || 0;
      const downloads = p.download_count || 0;
      const size = humanSize(p.export_size_bytes);
      const finishes = p.finish_read_count || 0;
      const statsText = `· 浏览${views}次 · 下载${downloads}次 · 离线版${size} · 完读${finishes}次`;
      const li = el("li", null, [
        cb, link,
        el("span", { class: "date", text: dateText }),
        el("span", {
          class: "count", text: " " + statsText, "data-i18n-tpl": "post_stats",
          "data-views": views, "data-downloads": downloads, "data-size": size, "data-finishes": finishes,
        }),
      ]);
      ul.appendChild(li);
    });
    return ul;
  }

  function renderPagination(page, totalPages, total) {
    const bar = el("div", { style: "margin-top:16px;display:flex;gap:8px;align-items:center;" });
    const prevBtn = el("button", { text: "← 上一页", "data-i18n": "prev_page" });
    prevBtn.disabled = page <= 1;
    prevBtn.onclick = () => { state.page = page - 1; doSearch(); };

    const nextBtn = el("button", { text: "下一页 →", "data-i18n": "next_page" });
    nextBtn.disabled = page >= totalPages;
    nextBtn.onclick = () => { state.page = page + 1; doSearch(); };

    const info = el("span", {
      style: "color:#888;font-size:0.9em;", text: `第 ${page} / ${totalPages} 页，共 ${total} 篇`,
      "data-i18n-tpl": "pagination_info", "data-page": page, "data-total-pages": totalPages, "data-total": total,
    });

    [prevBtn, info, nextBtn].forEach((x) => bar.appendChild(x));
    return bar;
  }

  // q/tag/from/to是已有的搜索条件，year/month是这次新加的筛选条件——两者是AND关系，
  // 复用/api/search已有的组合逻辑(app.py::search()里year/month只在没有显式from/to时
  // 才生效，这条优先级规则是已有行为，这里不改)。year/month不出现在q/tag/from/to
  // 任何一个输入框里，只来自state(而state来自URL)，所以年/月筛选和现有搜索框互不干扰。
  function buildSearchParams() {
    const q = document.getElementById("q").value;
    const tag = document.getElementById("tag").value;
    const from = document.getElementById("from").value;
    const to = document.getElementById("to").value;
    const params = new URLSearchParams();
    if (q) params.set("q", q);
    if (tag) params.set("tag", tag);
    if (from) params.set("from", from);
    if (to) params.set("to", to);
    if (state.year) params.set("year", state.year);
    if (state.year && state.month) params.set("month", state.month);
    params.set("page", state.page);
    params.set("page_size", state.pageSize);
    return params;
  }

  async function doSearch() {
    const params = buildSearchParams();
    try {
      const resp = await fetch(`/api/search?${params.toString()}`);
      const data = await resp.json();
      if (data.error) {
        alert(homeT("alert_search_error")(data.error));
        return;
      }
      refreshList(data.results, data.page || 1, data.total_pages || 1, data.total || 0);
      lastArchiveSummaryTotal = data.total || 0;
      updateArchiveSummary(lastArchiveSummaryTotal);
    } catch (e) {
      alert(homeT("alert_request_failed_backend")(e));
    }
  }

  // POST /api/refresh/<target>，target在URL路径里，四个按钮各自请求自己的
  // target，不会因为点了别的按钮就影响当前站点。响应形状见app.py::
  // refresh_target()：429=冷却中，409=资源被占用(busy)，202=github有界
  // 等待到期(Actions结论未产出，不是失败)，200且status为success/failure=
  // 真实执行完成的结果。
  async function doRefresh(target, btn) {
    // data.detail/data.error这两个字段是后端(safe_errors.py::safe_public_detail())
    // 出于安全考虑构造的固定中文摘要，本次全站UI国际化不改这两个值本身(改动需要
    // 触及app.py/safe_errors.py，超出本轮"只改前端"的范围，已在报告里说明)，
    // 只翻译alert()里包裹这些值的提示文案本身(距离上次刷新/查看进度/详情等)。
    btn.disabled = true;
    btn.textContent = homeT("refreshing_text");
    try {
      const resp = await fetch(`/api/refresh/${target}`, { method: "POST" });
      const data = await resp.json();
      if (resp.status === 429) {
        alert(homeT("alert_cooldown")(data.cooldown_remaining_seconds));
      } else if (resp.status === 409) {
        alert(data.detail);
      } else if (resp.status === 202) {
        alert(`${data.detail}${data.run_html_url ? homeT("view_progress_label") + data.run_html_url : ""}`);
      } else if (data.status === "success") {
        alert(data.detail ? homeT("alert_refresh_success_detail")(data.detail) : homeT("alert_refresh_success_plain"));
        if (target === current_target()) doSearch();
      } else {
        const detail = data.detail || JSON.stringify(data);
        alert(`${homeT("alert_refresh_failed_prefix")}${detail}${data.run_html_url ? homeT("detail_label") + data.run_html_url : ""}`);
      }
    } catch (e) {
      alert(homeT("alert_request_failed")(e));
    } finally {
      btn.disabled = false;
      btn.textContent = homeT("refresh_target_btn")(target);
    }
  }

  function current_target() {
    return location.hostname === "backup.foxzen.me" ? "backup" : "mirror";
  }

  // POST /api/purge-cache：响应形状见app.py::purge_cache() —— 429=5分钟冷却中，
  // 409=已有另一次刷新正在进行(锁被占用)，200且status="no_changes"=检查过了，
  // 没有需要刷新的新变化(不是失败)，200且status="success"=真的刷新了，
  // 200且status其它值(如"failure")=Cloudflare那一步失败。跟doRefresh()一样
  // 用alert()展示结果，不引入新的状态UI组件。
  async function doPurgeCache(btn) {
    btn.disabled = true;
    btn.textContent = homeT("cache_purge_checking_text");
    try {
      const resp = await fetch("/api/purge-cache", { method: "POST" });
      const data = await resp.json().catch(() => ({}));
      if (resp.status === 429) {
        alert(homeT("alert_cooldown")(data.cooldown_remaining_seconds));
      } else if (resp.status === 409) {
        alert(homeT("cache_purge_busy_text"));
      } else if (data.status === "no_changes") {
        alert(homeT("cache_purge_no_changes_text"));
      } else if (data.status === "success") {
        alert(homeT("cache_purge_success_text"));
      } else {
        alert(homeT("cache_purge_failure_text"));
      }
    } catch (e) {
      alert(homeT("alert_request_failed")(e));
    } finally {
      btn.disabled = false;
      btn.textContent = homeT("cache_purge_btn");
    }
  }

  async function doDownloadSelected() {
    if (state.selected.size === 0) {
      alert(homeT("alert_select_one_post"));
      return;
    }
    const resp = await fetch("/api/download/selected", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ post_ids: Array.from(state.selected) }),
    });
    if (!resp.ok) {
      alert(homeT("alert_download_failed"));
      return;
    }
    const disposition = resp.headers.get("Content-Disposition") || "";
    const filename = filenameFromDisposition(disposition, "blog-mirror-selected.zip");
    const blob = await resp.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = filename;
    a.click();
    URL.revokeObjectURL(url);
  }

  async function doExportBase64(scopeBody) {
    if (scopeBody.post_ids && scopeBody.post_ids.length === 0) {
      alert(homeT("alert_select_one_or_scope"));
      return;
    }
    let resp;
    try {
      resp = await fetch("/api/export/base64", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(scopeBody),
      });
    } catch (e) {
      alert(homeT("alert_request_failed")(e));
      return;
    }
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      alert(homeT("alert_export_failed")(err.error || resp.status));
      return;
    }
    const disposition = resp.headers.get("Content-Disposition") || "";
    const isZip = disposition.includes(".zip");
    const filename = filenameFromDisposition(disposition, isZip ? "blog-mirror-standalone.zip" : "standalone.html");
    const blob = await resp.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = filename;
    a.click();
    URL.revokeObjectURL(url);
  }

  function refreshList(posts, page, totalPages, total) {
    const old = document.getElementById("post-list");
    if (old) old.remove();
    const oldPagination = document.getElementById("pagination-bar");
    if (oldPagination) oldPagination.remove();
    const fallback = document.getElementById("fallback-list");
    if (fallback) fallback.remove();
    app.appendChild(renderList(posts));
    if (totalPages) {
      const pager = renderPagination(page, totalPages, total);
      pager.id = "pagination-bar";
      app.appendChild(pager);
    }
  }

  applyHomepageI18n();
  wireLangToggle();
  app.appendChild(buildCachePurgeWidget());
  app.appendChild(buildRefreshWidget());
  app.appendChild(buildArchiveFilterBar());
  app.appendChild(buildToolbar());
  // 用buildSearchParams()而不是原来写死的"page=1&page_size=..."，这样初次加载时
  // 如果URL里带着?year=2026&month=8，首屏就直接显示筛选后的结果(需求：刷新页面后
  // 筛选条件仍然存在)。q/tag/from/to输入框此时都是空的，state.year/month在文件顶部
  // 已经从URL解析好，所以在没有任何查询参数的普通首页访问下，这里生成的查询字符串
  // 和原来完全一样(page=1&page_size=10)，不改变原有首次加载行为。
  fetch(`/api/search?${buildSearchParams().toString()}`).then((r) => r.json()).then((data) => {
    if (data.results) {
      refreshList(data.results, data.page || 1, data.total_pages || 1, data.total || 0);
      lastArchiveSummaryTotal = data.total || 0;
      updateArchiveSummary(lastArchiveSummaryTotal);
    }
  }).catch(() => {
    // 后端不可用，静默保留fallback静态列表
  });

  // 浏览器前进/后退：重新从URL解析year/month，同步下拉框显示，再重新查询。
  // 不需要处理q/tag/from/to的前进后退——那几个从来没有写入过URL，本来就只有
  // year/month这次新增了URL同步。
  window.addEventListener("popstate", () => {
    const parsed = parseArchiveParamsFromLocation();
    state.year = parsed.year;
    state.month = parsed.month;
    state.page = 1;
    if (archiveBarRefs.yearSelect) {
      archiveBarRefs.yearSelect.value = state.year ? String(state.year) : "";
      populateMonthSelect(archiveBarRefs.monthSelect, state.year);
    }
    doSearch();
  });
})();
