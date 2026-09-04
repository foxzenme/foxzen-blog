// GitHub Pages / Cloudflare Pages 专用的4-target公开刷新入口。
//
// 这个文件只会出现在github.foxzen.me/cf.foxzen.me这两个纯静态站点上——
// 它们没有自己的后端，四个按钮点击后发出的请求物理上必须都打到GreenCloud
// （唯一跑Flask的地方），所以这里跟mirror/backup的static/index.js不同，
// 全部用固定的绝对地址REFRESH_API_BASE，不用相对路径。这是一次真正的
// 跨域请求，服务端已经为这四个域名单独配置了CORS allowlist（只对
// /api/refresh/*生效，见app.py的_apply_refresh_cors()），不需要也不应该
// 在这里做任何认证/凭据相关的事——整个刷新接口本来就是匿名公开的。
//
// 四个按钮全部显示、都可点击，当前所在站点只做视觉强调（加粗/高亮），
// 不隐藏其他三个——每个按钮永远只请求它自己对应的target，不会因为"当前
// 站点是github"就把mirror按钮偷偷改成也发github请求。

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

  function describeResponse(target, httpStatus, data) {
    if (httpStatus === 429) {
      return "距离上次刷新不足5分钟，请" + (data.cooldown_remaining_seconds || "?") + "秒后再试";
    }
    if (httpStatus === 409) {
      return data.detail || "当前有另一个刷新任务正在占用资源，请稍后重试";
    }
    if (httpStatus === 202) {
      return (data.detail || "内容已推送，结论尚未产出") +
        (data.run_html_url ? "\n查看进度: " + data.run_html_url : "");
    }
    if (data.status === "success") {
      return data.detail ? ("刷新成功：" + data.detail) : "刷新成功";
    }
    if (data.status === "failure") {
      var msg = "刷新失败：" + (data.detail || "未知错误");
      if (data.run_html_url) msg += "\n详情: " + data.run_html_url;
      return msg;
    }
    return "未知响应: " + JSON.stringify(data);
  }

  // DOM部分。

  function initPagesRefresh(opts) {
    opts = opts || {};
    var doc = opts.document || (typeof document !== "undefined" ? document : null);
    var win = opts.window || (typeof window !== "undefined" ? window : null);
    if (!doc) return;

    var current = currentTargetFromHostname(win && win.location ? win.location.hostname : "");

    REFRESH_TARGETS.forEach(function (target) {
      var btn = doc.querySelector('[data-role="refresh-btn-' + target + '"]');
      if (!btn) return;

      if (target === current) {
        btn.classList.add("refresh-btn-current");
      }
      btn.setAttribute("aria-label", "刷新 " + target + (target === current ? "（当前站点）" : ""));

      btn.onclick = function () {
        btn.disabled = true;
        var originalText = btn.textContent;
        btn.textContent = "刷新中...";

        fetch(REFRESH_API_BASE + "/api/refresh/" + target, { method: "POST" })
          .then(function (resp) {
            return resp.json().then(function (data) {
              return { httpStatus: resp.status, data: data };
            });
          })
          .then(function (result) {
            alert(describeResponse(target, result.httpStatus, result.data));
          })
          .catch(function (e) {
            alert("请求失败: " + e);
          })
          .finally(function () {
            btn.disabled = false;
            btn.textContent = originalText;
          });
      };
    });
  }

  return {
    REFRESH_API_BASE: REFRESH_API_BASE,
    REFRESH_TARGETS: REFRESH_TARGETS,
    currentTargetFromHostname: currentTargetFromHostname,
    describeResponse: describeResponse,
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
