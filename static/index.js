// blog-mirror 前端逻辑：搜索、标签/日期筛选、刷新、打包下载、选择下载
(function () {
  const app = document.getElementById("app");

  const state = { selected: new Set(), page: 1, pageSize: 10 };

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

  function buildToolbar() {
    const bar = el("div", { style: "margin-bottom:20px;display:flex;gap:8px;flex-wrap:wrap;align-items:center;" });

    const q = el("input", { type: "text", id: "q", placeholder: "搜索标题或正文...", style: "flex:1;min-width:200px;padding:6px;" });
    const tag = el("input", { type: "text", id: "tag", placeholder: "标签筛选", style: "width:120px;padding:6px;" });
    const from = el("input", { type: "date", id: "from", style: "padding:6px;" });
    const to = el("input", { type: "date", id: "to", style: "padding:6px;" });
    const searchBtn = el("button", { text: "搜索" });
    searchBtn.onclick = () => { state.page = 1; doSearch(); };

    const pageSizeSelect = el("select", { id: "page-size", style: "padding:6px;" });
    [["10", "每页10篇"], ["20", "每页20篇"]].forEach(([val, label]) => {
      const opt = el("option", { value: val, text: label });
      if (val === String(state.pageSize)) opt.setAttribute("selected", "selected");
      pageSizeSelect.appendChild(opt);
    });
    pageSizeSelect.onchange = () => {
      state.pageSize = parseInt(pageSizeSelect.value, 10);
      state.page = 1;
      doSearch();
    };

    const downloadAllBtn = el("button", { text: "打包下载全站" });
    downloadAllBtn.onclick = () => { window.location.href = "/api/download/all"; };

    const downloadSelectedBtn = el("button", { text: "下载已勾选" });
    downloadSelectedBtn.onclick = doDownloadSelected;

    const exportSelectedBtn = el("button", { text: "导出离线版(已勾选)" });
    exportSelectedBtn.onclick = () => doExportBase64({ post_ids: Array.from(state.selected) });

    const exportTagBtn = el("button", { text: "导出离线版(当前标签)" });
    exportTagBtn.onclick = () => {
      const t = document.getElementById("tag").value;
      if (!t) { alert("请先在标签框输入要导出的标签"); return; }
      doExportBase64({ tag: t });
    };

    const exportAllBtn = el("button", { text: "导出离线版(全部)" });
    exportAllBtn.onclick = () => {
      if (!confirm("全站导出Base64离线版可能体积较大、耗时较长，确认继续？")) return;
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

  function buildRefreshWidget() {
    const wrap = el("div", { style: "margin-bottom:20px;padding:12px 16px;background:#f7f7f7;border-radius:8px;" });
    const label = el("div", { style: "font-size:0.9em;color:#666;margin-bottom:8px;", text: "内容刷新" });
    const bar = el("div", { style: "display:flex;gap:8px;flex-wrap:wrap;align-items:center;" });

    const current = current_target();
    REFRESH_TARGETS.forEach((target) => {
      const btn = el("button", { type: "button", text: `刷新 ${target}` });
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
      const statsText = `· 浏览${p.click_count || 0}次 · 下载${p.download_count || 0}次 · 离线版${humanSize(p.export_size_bytes)} · 完读${p.finish_read_count || 0}次`;
      const li = el("li", null, [
        cb, link,
        el("span", { class: "date", text: dateText }),
        el("span", { class: "count", text: " " + statsText }),
      ]);
      ul.appendChild(li);
    });
    return ul;
  }

  function renderPagination(page, totalPages, total) {
    const bar = el("div", { style: "margin-top:16px;display:flex;gap:8px;align-items:center;" });
    const prevBtn = el("button", { text: "← 上一页" });
    prevBtn.disabled = page <= 1;
    prevBtn.onclick = () => { state.page = page - 1; doSearch(); };

    const nextBtn = el("button", { text: "下一页 →" });
    nextBtn.disabled = page >= totalPages;
    nextBtn.onclick = () => { state.page = page + 1; doSearch(); };

    const info = el("span", { style: "color:#888;font-size:0.9em;", text: `第 ${page} / ${totalPages} 页，共 ${total} 篇` });

    [prevBtn, info, nextBtn].forEach((x) => bar.appendChild(x));
    return bar;
  }

  async function doSearch() {
    const q = document.getElementById("q").value;
    const tag = document.getElementById("tag").value;
    const from = document.getElementById("from").value;
    const to = document.getElementById("to").value;
    const params = new URLSearchParams();
    if (q) params.set("q", q);
    if (tag) params.set("tag", tag);
    if (from) params.set("from", from);
    if (to) params.set("to", to);
    params.set("page", state.page);
    params.set("page_size", state.pageSize);

    try {
      const resp = await fetch(`/api/search?${params.toString()}`);
      const data = await resp.json();
      if (data.error) {
        alert(`搜索出错: ${data.error}`);
        return;
      }
      refreshList(data.results, data.page || 1, data.total_pages || 1, data.total || 0);
    } catch (e) {
      alert(`请求失败，后端可能未启动: ${e}`);
    }
  }

  // POST /api/refresh/<target>，target在URL路径里，四个按钮各自请求自己的
  // target，不会因为点了别的按钮就影响当前站点。响应形状见app.py::
  // refresh_target()：429=冷却中，409=资源被占用(busy)，202=github有界
  // 等待到期(Actions结论未产出，不是失败)，200且status为success/failure=
  // 真实执行完成的结果。
  async function doRefresh(target, btn) {
    btn.disabled = true;
    const originalText = btn.textContent;
    btn.textContent = "刷新中...";
    try {
      const resp = await fetch(`/api/refresh/${target}`, { method: "POST" });
      const data = await resp.json();
      if (resp.status === 429) {
        alert(`距离上次刷新不足5分钟，请${data.cooldown_remaining_seconds}秒后再试`);
      } else if (resp.status === 409) {
        alert(data.detail);
      } else if (resp.status === 202) {
        alert(`${data.detail}${data.run_html_url ? "\n查看进度: " + data.run_html_url : ""}`);
      } else if (data.status === "success") {
        alert(data.detail ? `刷新成功：${data.detail}` : "刷新成功");
        if (target === current_target()) doSearch();
      } else {
        const detail = data.detail || JSON.stringify(data);
        alert(`刷新失败: ${detail}${data.run_html_url ? "\n详情: " + data.run_html_url : ""}`);
      }
    } catch (e) {
      alert(`请求失败: ${e}`);
    } finally {
      btn.disabled = false;
      btn.textContent = originalText;
    }
  }

  function current_target() {
    return location.hostname === "backup.foxzen.me" ? "backup" : "mirror";
  }

  async function doDownloadSelected() {
    if (state.selected.size === 0) {
      alert("请先勾选至少一篇文章");
      return;
    }
    const resp = await fetch("/api/download/selected", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ post_ids: Array.from(state.selected) }),
    });
    if (!resp.ok) {
      alert("下载失败");
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
      alert("请先勾选至少一篇文章，或改用「当前标签」/「全部」导出");
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
      alert(`请求失败: ${e}`);
      return;
    }
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      alert(`导出失败: ${err.error || resp.status}`);
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

  app.appendChild(buildRefreshWidget());
  app.appendChild(buildToolbar());
  fetch(`/api/search?page=1&page_size=${state.pageSize}`).then((r) => r.json()).then((data) => {
    if (data.results) refreshList(data.results, data.page || 1, data.total_pages || 1, data.total || 0);
  }).catch(() => {
    // 后端不可用，静默保留fallback静态列表
  });
})();
