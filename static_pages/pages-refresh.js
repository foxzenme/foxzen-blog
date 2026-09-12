// GitHub Pages / Cloudflare Pages 专用的4-target公开刷新入口，外加一个
// 公共"刷新本站缓存"按钮。
//
// 这个文件只会出现在github.foxzen.me/cf.foxzen.me这两个纯静态站点上——
// 它们没有自己的后端，按钮点击后发出的请求物理上必须都打到GreenCloud
// （唯一跑Flask的地方），所以这里跟mirror/backup的static/index.js不同，
// 全部用固定的绝对地址REFRESH_API_BASE，不用相对路径。这是一次真正的
// 跨域请求，服务端已经为这四个域名单独配置了CORS allowlist（对
// /api/refresh/*和/api/purge-cache生效，见app.py的_apply_refresh_cors()），
// 不需要也不应该在这里做任何认证/凭据相关的事——两个接口本来就是匿名公开的。
//
// 四个刷新按钮全部显示、都可点击，当前所在站点只做视觉强调（加粗/高亮），
// 不隐藏其他三个——每个按钮永远只请求它自己对应的target，不会因为"当前
// 站点是github"就把mirror按钮偷偷改成也发github请求。"刷新本站缓存"按钮
// 只有一个、不分target，行为和mirror/backup首页上的同名按钮完全一致
// （见static/index.js::doPurgeCache()）——都是POST同一个/api/purge-cache，
// 由后端自己判断有没有真实待处理变化，不是"强制purge"。

(function (root, factory) {
  if (typeof module !== "undefined" && module.exports) {
    module.exports = factory();
  } else {
    root.PagesRefresh = factory();
  }
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  var REFRESH_API_BASE = "https://mirror.foxzen.me";
  var REFRESH_TARGETS = ["mirror", "backup", "github", "cf"];

  // 纯函数部分，不依赖DOM，可以在Node里直接require测试。

  function currentTargetFromHostname(hostname) {
    var h = String(hostname || "").toLowerCase();
    if (REFRESH_TARGETS.indexOf(h.split(".")[0]) !== -1 &&
        (h === "mirror.foxzen.me" || h === "backup.foxzen.me" ||
         h === "github.foxzen.me" || h === "cf.foxzen.me")) {
      return h.split(".")[0];
    }
    return null;
  }

  // 全站UI国际化：只翻译包裹在外面的提示文案，data.detail/data.error这两个
  // 字段是后端(app.py/safe_errors.py::safe_public_detail())出于安全考虑
  // 构造的固定中文摘要，改动它们需要触及后端代码，超出本轮"只改前端"的
  // 范围（已在报告里说明），所以未定义/为空时才会用到下面REFRESH_STRINGS
  // 里的中性兜底文案，data.detail有值时始终原样透传，不做任何包装。
  //
  // lang形参可选、默认落到en：test_publish_build.py里已有的
  // test_pages_refresh_js_pure_functions_via_node()调用这个函数时不传
  // lang，这样保持向后兼容，不用改已有测试。真正的浏览器环境下由
  // initPagesRefresh()按当前foxzen_lang传入。
  var REFRESH_STRINGS = {
    zh: {
      cooldown: function (seconds) { return "距离上次刷新不足5分钟，请" + seconds + "秒后再试"; },
      busy_fallback: "当前有另一个刷新任务正在占用资源，请稍后重试",
      pending_fallback: "内容已推送，结论尚未产出",
      view_progress_label: "\n查看进度: ",
      success_detail: function (detail) { return "刷新成功：" + detail; },
      success_plain: "刷新成功",
      failure_prefix: "刷新失败：",
      failure_unknown: "未知错误",
      detail_label: "\n详情: ",
      unknown_response: function (json) { return "未知响应: " + json; },
      refreshing_text: "刷新中...",
      request_failed: function (err) { return "请求失败: " + err; },
      // 以下5个key专属"刷新本站缓存"按钮（doPurgeCache()/describePurgeResponse()），
      // 文案跟static/index.js里mirror/backup同名按钮的cache_purge_*系列
      // 完全一致，故意不共用上面refreshing_text/request_failed以外的
      // 刷新专属文案——/api/purge-cache的响应形状(no_changes/success/
      // failure三种status + 429/409两种httpStatus)跟/api/refresh/<target>
      // 不同，见describePurgeResponse()。
      purge_checking: "检查中...",
      purge_busy: "已有一次刷新正在进行，请稍后再试。",
      purge_no_changes: "当前没有新的内容变化，无需刷新缓存。",
      purge_success: "缓存已刷新为最新版本。",
      purge_failure: "刷新缓存失败，请稍后再试。",
    },
    en: {
      cooldown: function (seconds) { return "Less than 5 minutes since the last refresh, please try again in " + seconds + "s"; },
      busy_fallback: "Another refresh task is currently using this resource, please try again later",
      pending_fallback: "Content has been pushed, the result isn't ready yet",
      view_progress_label: "\nView progress: ",
      success_detail: function (detail) { return "Refresh succeeded: " + detail; },
      success_plain: "Refresh succeeded",
      failure_prefix: "Refresh failed: ",
      failure_unknown: "Unknown error",
      detail_label: "\nDetails: ",
      unknown_response: function (json) { return "Unknown response: " + json; },
      refreshing_text: "Refreshing...",
      request_failed: function (err) { return "Request failed: " + err; },
      purge_checking: "Checking...",
      purge_busy: "A refresh is already in progress, please try again shortly.",
      purge_no_changes: "There are no new content changes right now, no need to refresh the cache.",
      purge_success: "The cache has been refreshed to the latest version.",
      purge_failure: "Failed to refresh the cache, please try again later.",
    },
  };

  function refreshT(lang) {
    return REFRESH_STRINGS[lang] || REFRESH_STRINGS.en;
  }

  function describeResponse(target, httpStatus, data, lang) {
    var t = refreshT(lang);
    if (httpStatus === 429) {
      return t.cooldown(data.cooldown_remaining_seconds || "?");
    }
    if (httpStatus === 409) {
      return data.detail || t.busy_fallback;
    }
    if (httpStatus === 202) {
      return (data.detail || t.pending_fallback) +
        (data.run_html_url ? t.view_progress_label + data.run_html_url : "");
    }
    if (data.status === "success") {
      return data.detail ? t.success_detail(data.detail) : t.success_plain;
    }
    if (data.status === "failure") {
      var msg = t.failure_prefix + (data.detail || t.failure_unknown);
      if (data.run_html_url) msg += t.detail_label + data.run_html_url;
      return msg;
    }
    return t.unknown_response(JSON.stringify(data));
  }

  // /api/purge-cache的响应形状只有429(cooldown)/409(busy)/200+status三种
  // (status: no_changes/success/failure，见app.py::purge_cache())，没有
  // /api/refresh/<target>的202(pending)/run_html_url这些字段，所以用一个
  // 独立的纯函数而不是硬塞进上面describeResponse()多加分支。
  function describePurgeResponse(httpStatus, data, lang) {
    var t = refreshT(lang);
    if (httpStatus === 429) {
      return t.cooldown(data.cooldown_remaining_seconds || "?");
    }
    if (httpStatus === 409) {
      return t.purge_busy;
    }
    if (data.status === "no_changes") {
      return t.purge_no_changes;
    }
    if (data.status === "success") {
      return t.purge_success;
    }
    return t.purge_failure;
  }

  // DOM部分。

  // 跟fetch_blog.py::I18N_BLOCK/static/index.js/static_pages/pages-index.js
  // 同一个localStorage key/同一套检测算法的第四份独立实现——这里只需要
  // "点击那一刻的当前语言"（每次点击都现读localStorage，不缓存），不需要
  // 像pages-index.js那样维护一个可以被setFoxzenLang()动态更新的全局变量，
  // 所以不用监听语言切换按钮的点击事件，也不产生跟pages-index.js之间的
  // 新耦合。aria-label文案仍然只在initPagesRefresh()首次运行时设置一次，
  // 语言切换后不会跟着更新——这是一个次要的、已知的无障碍属性局限（视觉可见
  // 的按钮文字由pages-index.js::applyPagesI18n()的data-i18n-tpl sweep覆盖，
  // 不受此影响），已在报告里说明。
  function getFoxzenLang() {
    try {
      var saved = localStorage.getItem("foxzen_lang");
      if (saved === "zh" || saved === "en") return saved;
    } catch (e) {}
    var langs = (navigator.languages && navigator.languages.length) ? navigator.languages : [navigator.language || ""];
    for (var i = 0; i < langs.length; i++) {
      if (/^zh/i.test(langs[i])) return "zh";
    }
    return "en";
  }

  function initPagesRefresh(opts) {
    opts = opts || {};
    var doc = opts.document || (typeof document !== "undefined" ? document : null);
    var win = opts.window || (typeof window !== "undefined" ? window : null);
    if (!doc) return;

    var current = currentTargetFromHostname(win && win.location ? win.location.hostname : "");
    var initialLangIsZh = getFoxzenLang() === "zh";

    REFRESH_TARGETS.forEach(function (target) {
      var btn = doc.querySelector('[data-role="refresh-btn-' + target + '"]');
      if (!btn) return;

      if (target === current) {
        btn.classList.add("refresh-btn-current");
      }
      btn.setAttribute("aria-label", initialLangIsZh
        ? "刷新 " + target + (target === current ? "（当前站点）" : "")
        : "Refresh " + target + (target === current ? " (current site)" : ""));

      btn.onclick = function () {
        var lang = getFoxzenLang();
        btn.disabled = true;
        var originalText = btn.textContent;
        btn.textContent = refreshT(lang).refreshing_text;

        fetch(REFRESH_API_BASE + "/api/refresh/" + target, { method: "POST" })
          .then(function (resp) {
            return resp.json().then(function (data) {
              return { httpStatus: resp.status, data: data };
            });
          })
          .then(function (result) {
            alert(describeResponse(target, result.httpStatus, result.data, lang));
          })
          .catch(function (e) {
            alert(refreshT(lang).request_failed(e));
          })
          .finally(function () {
            btn.disabled = false;
            btn.textContent = originalText;
          });
      };
    });

    // 公共"刷新本站缓存"按钮：只有一个，不分target，跟上面4个刷新按钮
    // 共用同一个init函数（都是"找data-role元素、绑onclick、fetch
    // REFRESH_API_BASE+路径、alert结果"这一套），不为它单独建一个
    // initPagesPurgeCache()。找不到这个元素（比如某个页面模板暂时还没有
    // 这个按钮）时安静跳过，不报错——跟上面btn查找失败时的处理方式一致。
    var purgeBtn = doc.querySelector('[data-role="purge-cache-btn"]');
    if (purgeBtn) {
      purgeBtn.onclick = function () {
        var lang = getFoxzenLang();
        purgeBtn.disabled = true;
        var originalText = purgeBtn.textContent;
        purgeBtn.textContent = refreshT(lang).purge_checking;

        fetch(REFRESH_API_BASE + "/api/purge-cache", { method: "POST" })
          .then(function (resp) {
            return resp.json().catch(function () { return {}; }).then(function (data) {
              return { httpStatus: resp.status, data: data };
            });
          })
          .then(function (result) {
            alert(describePurgeResponse(result.httpStatus, result.data, lang));
          })
          .catch(function (e) {
            alert(refreshT(lang).request_failed(e));
          })
          .finally(function () {
            purgeBtn.disabled = false;
            purgeBtn.textContent = originalText;
          });
      };
    }
  }

  return {
    REFRESH_API_BASE: REFRESH_API_BASE,
    REFRESH_TARGETS: REFRESH_TARGETS,
    currentTargetFromHostname: currentTargetFromHostname,
    describeResponse: describeResponse,
    describePurgeResponse: describePurgeResponse,
    initPagesRefresh: initPagesRefresh,
  };
});

if (typeof document !== "undefined") {
  document.addEventListener("DOMContentLoaded", function () {
    if (typeof module === "undefined") {
      window.PagesRefresh.initPagesRefresh({});
    }
  });
}
