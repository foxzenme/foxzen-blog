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
      var li = el("li", null, [
        el("a", { href: a.url, text: a.title }),
        el("span", { class: "date", text: " " + humanDate(a.date) + " " + tagsText }),
      ]);
      ul.appendChild(li);
    });
    return ul;
  }

  function renderPagination(state, page, totalPages, total, onChange) {
    var bar = el("div", { id: "pagination-bar", style: "margin-top:16px;display:flex;gap:8px;align-items:center;" });
    var prevBtn = el("button", { text: "← 上一页" });
    prevBtn.disabled = page <= 1;
    prevBtn.onclick = function () { onChange(Object.assign({}, state, { page: page - 1 })); };

    var nextBtn = el("button", { text: "下一页 →" });
    nextBtn.disabled = page >= totalPages;
    nextBtn.onclick = function () { onChange(Object.assign({}, state, { page: page + 1 })); };

    var info = el("span", { style: "color:#888;font-size:0.9em;", text: "第 " + page + " / " + totalPages + " 页，共 " + total + " 篇" });

    [prevBtn, info, nextBtn].forEach(function (x) { bar.appendChild(x); });
    return bar;
  }

  function initPagesSearch(options) {
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
