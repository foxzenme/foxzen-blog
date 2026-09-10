// GitHub Pages / Cloudflare Pages 专用的纯浏览器端搜索/筛选/分页实现。
//
// 跟生产镜像站(mirror.foxzen.me)的 static/index.js 是两套独立代码——那份
// 全部功能都靠打 /api/* 这些Flask接口，纯静态托管环境下这些请求必然失败；
// 这份只做三件事：加载构建时生成的 search-index.json、在浏览器里本地过滤/
// 分页、更新DOM和URL query参数，全程不发出任何 /api/* 请求。
//
// 顶部这几个纯函数(parseQueryFromSearch/filterArticles/paginateArticles/
// buildQueryString)不依赖DOM，可以在Node里直接require测试真实过滤逻辑
// （见 test_publish_build.py 里通过子进程调用node验证的测试），而不是只能
// 靠人工在浏览器里点一遍。

(function (root, factory) {
  if (typeof module !== "undefined" && module.exports) {
    module.exports = factory();
  } else {
    root.PagesIndex = factory();
  }
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  // ===== 全站UI国际化（github.foxzen.me/cf.foxzen.me专用实现） =====
  // 跟mirror/backup用的static/index.js、文章页用的fetch_blog.py::I18N_BLOCK
  // 是同一套约定（localStorage key foxzen_lang、data-i18n纯文本替换、
  // data-i18n-tpl配合data-*属性拼数字、data-i18n-placeholder替换input
  // placeholder），但这里是第三份独立实现——纯静态托管没有Flask，也没有
  // static/index.js那个文件，两边运行时环境不同，没有现成的共享点，跟
  // 首页/文章页之间"各自独立实现一份"是完全一致的架构决定。
  //
  // 这个字典覆盖两类来源的data-i18n标记：(1)html/index.html里(由
  // fetch_blog.py::INDEX_TEMPLATE渲染)已经存在的服务端UI文案——entries-box/
  // stats-box/leaderboard/policy说明/页脚等，跟static/index.js::
  // HOMEPAGE_I18N_STRINGS里对应的key/译文逐一保持一致；(2)本文件自己
  // 动态渲染的搜索结果列表/分页，以及publish_build.py::SEARCH_TOOLBAR_HTML/
  // DOWNLOAD_TOOLBAR_HTML/REFRESH_TOOLBAR_HTML里烘焙好的静态工具栏文案。
  // 文章标题/正文/博客名称/URL/格言内容/Internet Archive验证行都不在这个
  // 字典覆盖范围内——它们在html/index.html里从来没有被打上data-i18n标记，
  // 这个sweep函数结构上就够不到。
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

  var PAGES_I18N_STRINGS = {
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
      page_size_50: "每页50篇",
      download_all_btn: "打包下载全站",
      download_selected_btn: "下载已勾选",
      export_selected_btn: "导出离线版(已勾选)",
      export_tag_btn: "导出离线版(当前标签)",
      export_all_btn: "导出离线版(全部)",
      pages_download_toolbar_label: "下载 / 离线导出（完全由本站静态文件生成，不依赖任何其他服务器）",
      pages_refresh_toolbar_label: "内容刷新（请求会发送到 mirror.foxzen.me 上的执行中心，当前站点已加粗标出）",
      prev_page: "← 上一页",
      next_page: "下一页 →",
      stats_post_count_value: function (count) { return count + " 篇"; },
      stats_download_count_value: function (count) { return count + " 次"; },
      stats_export_size_value: function (size) { return "约 " + size + "（未压缩，实际zip会更小）"; },
      stats_visits_value: function (today, week, month, year, total) {
        return "今日 " + today + " · 本周 " + week + " · 本月 " + month + " · 今年 " + year + " · 累计 " + total;
      },
      rank_views: function (count) { return "（" + count + " 次浏览）"; },
      rank_downloads: function (count) { return "（" + count + " 次下载）"; },
      post_stats: function (views, downloads, size, finishes) {
        return " · 浏览" + views + "次 · 下载" + downloads + "次 · 离线版" + size + " · 完读" + finishes + "次";
      },
      pagination_info: function (page, totalPages, total) {
        return "第 " + page + " / " + totalPages + " 页，共 " + total + " 篇";
      },
      refresh_target_btn: function (target) { return "刷新 " + target; },
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
      page_size_50: "50 per page",
      download_all_btn: "Download entire site",
      download_selected_btn: "Download selected",
      export_selected_btn: "Export offline (selected)",
      export_tag_btn: "Export offline (current tag)",
      export_all_btn: "Export offline (all)",
      pages_download_toolbar_label: "Download / offline export (fully generated from this site's static files, no other server needed)",
      pages_refresh_toolbar_label: "Content refresh (requests go to the execution hub on mirror.foxzen.me; the current site is shown in bold)",
      prev_page: "← Prev",
      next_page: "Next →",
      stats_post_count_value: function (count) { return String(count); },
      stats_download_count_value: function (count) { return String(count); },
      stats_export_size_value: function (size) { return "~" + size + " (uncompressed; actual zip will be smaller)"; },
      stats_visits_value: function (today, week, month, year, total) {
        return "Today " + today + " · This week " + week + " · This month " + month + " · This year " + year + " · Total " + total;
      },
      rank_views: function (count) { return "(" + count + " views)"; },
      rank_downloads: function (count) { return "(" + count + " downloads)"; },
      post_stats: function (views, downloads, size, finishes) {
        return " · " + views + " views · " + downloads + " downloads · offline " + size + " · " + finishes + " finished";
      },
      pagination_info: function (page, totalPages, total) {
        return "Page " + page + " / " + totalPages + ", " + total + " posts total";
      },
      refresh_target_btn: function (target) { return "Refresh " + target; },
    },
  };

  // key -> 需要按顺序从DOM节点读取的data-*属性名列表，跟static/index.js::
  // HOMEPAGE_I18N_TPL_ARGS是同一份映射内容的独立拷贝。
  var PAGES_I18N_TPL_ARGS = {
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

  function pagesT(key) {
    var dict = PAGES_I18N_STRINGS[FOXZEN_LANG] || PAGES_I18N_STRINGS.en;
    return dict[key];
  }

  function applyPagesI18n() {
    var dict = PAGES_I18N_STRINGS[FOXZEN_LANG] || PAGES_I18N_STRINGS.en;

    var nodes = document.querySelectorAll("[data-i18n]");
    for (var i = 0; i < nodes.length; i++) {
      var key = nodes[i].getAttribute("data-i18n");
      if (typeof dict[key] === "string") nodes[i].textContent = dict[key];
    }

    var placeholderNodes = document.querySelectorAll("[data-i18n-placeholder]");
    for (var j = 0; j < placeholderNodes.length; j++) {
      var pKey = placeholderNodes[j].getAttribute("data-i18n-placeholder");
      if (typeof dict[pKey] === "string") placeholderNodes[j].setAttribute("placeholder", dict[pKey]);
    }

    var tplNodes = document.querySelectorAll("[data-i18n-tpl]");
    for (var k = 0; k < tplNodes.length; k++) {
      var tKey = tplNodes[k].getAttribute("data-i18n-tpl");
      var fn = dict[tKey];
      if (typeof fn !== "function") continue;
      var argNames = PAGES_I18N_TPL_ARGS[tKey] || [];
      var args = argNames.map(function (name) { return tplNodes[k].getAttribute("data-" + name) || "0"; });
      tplNodes[k].textContent = fn.apply(null, args);
    }

    document.documentElement.setAttribute("lang", FOXZEN_LANG === "zh" ? "zh-CN" : "en");
    var toggleBtns = document.querySelectorAll("[data-lang-btn]");
    for (var m = 0; m < toggleBtns.length; m++) {
      if (toggleBtns[m].getAttribute("data-lang-btn") === FOXZEN_LANG) toggleBtns[m].classList.add("active");
      else toggleBtns[m].classList.remove("active");
    }
  }

  // 右上角按钮点击后调用：跟static/index.js::setFoxzenLang()同一个思路，
  // 写localStorage、更新FOXZEN_LANG、重新sweep；纯静态站点这里没有
  // 年/月筛选器需要额外刷新(github.foxzen.me/cf.foxzen.me本轮没有年/月
  // 筛选功能)，所以比mirror那份实现更简单。
  function setFoxzenLang(lang) {
    if (lang !== "zh" && lang !== "en") return;
    FOXZEN_LANG = lang;
    try { localStorage.setItem(FOXZEN_LANG_KEY, lang); } catch (e) {}
    applyPagesI18n();
  }

  function wireLangToggle() {
    var btns = document.querySelectorAll("[data-lang-btn]");
    for (var i = 0; i < btns.length; i++) {
      btns[i].addEventListener("click", function (e) {
        setFoxzenLang(e.currentTarget.getAttribute("data-lang-btn"));
      });
    }
  }

  function parseQueryFromSearch(search) {
    var params = new URLSearchParams(search || "");
    return {
      q: params.get("q") || "",
      tag: params.get("tag") || "",
      from: params.get("from") || "",
      to: params.get("to") || "",
      page: parseInt(params.get("page"), 10) || 1,
      pageSize: parseInt(params.get("page_size"), 10) || 10,
    };
  }

  function buildQueryString(state) {
    var params = new URLSearchParams();
    if (state.q) params.set("q", state.q);
    if (state.tag) params.set("tag", state.tag);
    if (state.from) params.set("from", state.from);
    if (state.to) params.set("to", state.to);
    if (state.page && state.page !== 1) params.set("page", String(state.page));
    if (state.pageSize && state.pageSize !== 10) params.set("page_size", String(state.pageSize));
    var qs = params.toString();
    return qs ? "?" + qs : "";
  }

  // 关键词同时匹配标题、正文纯文本、标签；标签筛选与日期范围都是AND关系，
  // 不是OR——跟需求里"关键词Firefox + 标签隐私 => 同时满足"的例子一致。
  function filterArticles(articles, state) {
    var q = (state.q || "").trim().toLowerCase();
    var tag = (state.tag || "").trim().toLowerCase();
    var from = state.from || "";
    var to = state.to || "";

    return articles.filter(function (a) {
      if (q) {
        var haystack = (a.title + " " + a.text + " " + (a.tags || []).join(" ")).toLowerCase();
        if (haystack.indexOf(q) === -1) return false;
      }
      if (tag) {
        var tags = (a.tags || []).map(function (t) { return String(t).toLowerCase(); });
        if (tags.indexOf(tag) === -1) return false;
      }
      if (from && a.date < from) return false;
      if (to && a.date > to) return false;
      return true;
    });
  }

  function paginateArticles(filtered, page, pageSize) {
    var total = filtered.length;
    var totalPages = Math.max(1, Math.ceil(total / pageSize));
    var safePage = Math.min(Math.max(1, page), totalPages);
    var start = (safePage - 1) * pageSize;
    return {
      items: filtered.slice(start, start + pageSize),
      page: safePage,
      totalPages: totalPages,
      total: total,
    };
  }

  function humanDate(d) {
    return d || "";
  }

  function el(tag, attrs, children) {
    var e = document.createElement(tag);
    if (attrs) {
      for (var k in attrs) {
        if (k === "text") e.textContent = attrs[k];
        else e.setAttribute(k, attrs[k]);
      }
    }
    (children || []).forEach(function (c) { e.appendChild(c); });
    return e;
  }

  function renderList(items) {
    var ul = el("ul", { id: "post-list" });
    items.forEach(function (a) {
      var tagsText = (a.tags || []).map(function (t) { return "#" + t; }).join(" ");
      // checkbox是pages-download.js"下载已勾选/导出离线版(已勾选)"读取
      // 选中状态用的钩子，本文件不关心/不处理下载逻辑，两者职责分离。
      var checkbox = el("input", { type: "checkbox", "data-role": "select-post", "data-post-id": a.id });
      var li = el("li", null, [
        checkbox,
        el("a", { href: a.url, text: a.title, target: "_blank", rel: "noopener" }),
        el("span", { class: "date", text: " " + humanDate(a.date) + " " + tagsText }),
      ]);
      ul.appendChild(li);
    });
    return ul;
  }

  function renderPagination(state, page, totalPages, total, onChange) {
    var bar = el("div", { id: "pagination-bar", style: "margin-top:16px;display:flex;gap:8px;align-items:center;" });
    var prevBtn = el("button", { text: "← 上一页", "data-i18n": "prev_page" });
    prevBtn.disabled = page <= 1;
    prevBtn.onclick = function () { onChange(Object.assign({}, state, { page: page - 1 })); };

    var nextBtn = el("button", { text: "下一页 →", "data-i18n": "next_page" });
    nextBtn.disabled = page >= totalPages;
    nextBtn.onclick = function () { onChange(Object.assign({}, state, { page: page + 1 })); };

    var info = el("span", {
      style: "color:#888;font-size:0.9em;", text: "第 " + page + " / " + totalPages + " 页，共 " + total + " 篇",
      "data-i18n-tpl": "pagination_info", "data-page": page, "data-total-pages": totalPages, "data-total": total,
    });

    [prevBtn, info, nextBtn].forEach(function (x) { bar.appendChild(x); });
    return bar;
  }

  function initPagesSearch(options) {
    // i18n sweep先于下面的#app检查执行——entries-box/stats-box/leaderboard/
    // 语言切换按钮遍布整个页面，不是只在#app内部，即使这个页面碰巧没有
    // #app（理论上不会发生，但不应该让i18n因此不生效）也应该正常工作。
    applyPagesI18n();
    wireLangToggle();
    var indexUrl = (options && options.indexUrl) || "/search-index.json";
    var app = document.getElementById("app");
    if (!app) return;

    var state = parseQueryFromSearch(window.location.search);

    fetch(indexUrl)
      .then(function (r) { return r.json(); })
      .then(function (data) {
        var articles = data.articles || [];

        function render() {
          var old = document.getElementById("post-list");
          if (old) old.remove();
          var oldPagination = document.getElementById("pagination-bar");
          if (oldPagination) oldPagination.remove();
          var fallback = document.getElementById("fallback-list");
          if (fallback) fallback.remove();

          var filtered = filterArticles(articles, state);
          var page = paginateArticles(filtered, state.page, state.pageSize);
          state.page = page.page;

          app.appendChild(renderList(page.items));
          app.appendChild(renderPagination(state, page.page, page.totalPages, page.total, function (next) {
            state = next;
            history.replaceState(null, "", window.location.pathname + buildQueryString(state));
            render();
          }));
        }

        var toolbar = document.getElementById("pages-search-toolbar");
        if (toolbar) {
          var qInput = toolbar.querySelector('[data-role="q"]');
          var tagInput = toolbar.querySelector('[data-role="tag"]');
          var fromInput = toolbar.querySelector('[data-role="from"]');
          var toInput = toolbar.querySelector('[data-role="to"]');
          var pageSizeSelect = toolbar.querySelector('[data-role="page-size"]');

          if (qInput) qInput.value = state.q;
          if (tagInput) tagInput.value = state.tag;
          if (fromInput) fromInput.value = state.from;
          if (toInput) toInput.value = state.to;
          if (pageSizeSelect) pageSizeSelect.value = String(state.pageSize);

          function applyFromForm() {
            state = {
              q: qInput ? qInput.value : "",
              tag: tagInput ? tagInput.value : "",
              from: fromInput ? fromInput.value : "",
              to: toInput ? toInput.value : "",
              page: 1,
              pageSize: pageSizeSelect ? (parseInt(pageSizeSelect.value, 10) || 10) : state.pageSize,
            };
            history.replaceState(null, "", window.location.pathname + buildQueryString(state));
            render();
          }

          var searchBtn = toolbar.querySelector('[data-role="search-btn"]');
          if (searchBtn) searchBtn.onclick = applyFromForm;
          if (pageSizeSelect) pageSizeSelect.onchange = applyFromForm;
        }

        render();
      })
      .catch(function () {
        // 索引加载失败(比如网络问题)时，静默保留服务端渲染好的#fallback-list，
        // 不弹错误、不影响文章本身的可读性——跟生产mirror的兜底思路一致。
      });
  }

  return {
    parseQueryFromSearch: parseQueryFromSearch,
    buildQueryString: buildQueryString,
    filterArticles: filterArticles,
    paginateArticles: paginateArticles,
    initPagesSearch: initPagesSearch,
    // 下面几个i18n相关导出主要供Node测试静态断言字典结构用（比如中英文key
    // 集合是否一致），实际DOM操作部分(applyPagesI18n/wireLangToggle)依赖
    // document/localStorage，跟本文件其它DOM相关函数一样不在Node里跑，
    // 只做源码层面的静态检查——见test_publish_build.py里的约定。
    PAGES_I18N_STRINGS: PAGES_I18N_STRINGS,
  };
});

if (typeof document !== "undefined") {
  document.addEventListener("DOMContentLoaded", function () {
    if (typeof module === "undefined") {
      // 浏览器环境下(non-CommonJS)，PagesIndex已经挂在全局上
      window.PagesIndex.initPagesSearch({});
    }
  });
}
