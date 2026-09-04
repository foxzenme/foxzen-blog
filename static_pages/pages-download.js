// GitHub Pages / Cloudflare Pages 专用的下载/离线导出实现。
//
// 固定范围（打包下载全站/导出离线版全部/按标签）不在这里现场生成——
// 那三个是publish_build.py构建时预生成好的静态zip（downloads/blog-full.zip、
// downloads/export-all.zip、downloads/export-tag/<tag>.zip），这里只是
// 跳转到对应URL，不需要JSZip参与。
//
// "下载已勾选"/"导出离线版(已勾选)"这两个是运行时任意组合，构建时不可能
// 穷举，用JSZip在浏览器内存里现场从"当前站点已经公开存在的静态文件"
// （文章HTML、media图片、standalone离线版）现拼zip再触发下载——全程只有
// 同源相对路径fetch()，不出现任何绝对域名，天然只会请求当前这个Pages
// 域名自己的文件，不需要也不应该判断当前具体是哪一个Pages部署域名。
//
// 顶部的safeTagFilename/safeArticleFilename/zipArcnameForArticle/
// dedupeZipArcname是不依赖DOM的纯函数，可以在Node里直接require测试
// （见test_publish_build.py），跟publish_build.py里的_safe_tag_filename()/
// _safe_article_filename()/_zip_arcname_for_article()/_dedupe_zip_arcname()
// 保持规则一致——两边是各自独立实现，不是共享代码，一致性靠双边回归测试
// 互相印证。这几条规则本身又都跟app.py::_safe_filename()/_zip_arcname_for()
// （mirror"导出离线版"用的真实命名规则）对齐，不是本轮自创的清洗规则。

(function (root, factory) {
  if (typeof module !== "undefined" && module.exports) {
    module.exports = factory();
  } else {
    root.PagesDownload = factory();
  }
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  function safeTagFilename(tag) {
    var name = String(tag).replace(/[\\/:*?"<>|]/g, "_").trim();
    name = name.replace(/\s+/g, " ");
    return name || "untitled";
  }

  // 跟app.py::_safe_filename()、publish_build.py::_safe_article_filename()
  // 逐条对齐的纯字符串清洗规则（字符类/去空白/截断80字符全部一致）。
  function safeArticleFilename(title) {
    var name = String(title).replace(/[\\/:*?"<>|]/g, "_").trim();
    name = name.replace(/\s+/g, " ");
    name = name.slice(0, 80);
    return name || "untitled";
  }

  // 跟app.py::_zip_arcname_for()/publish_build.py::_zip_arcname_for_article()
  // 保持一致的命名规则：年/月/<安全标题>.html——年/月来自article.date
  // ("YYYY-MM-DD"，search-index.json里本来就有的发布日期字段，不是新增
  // 字段)，文件名来自标题清洗，不用canonical地址/Blogger slug/post_id。
  // "网页canonical URL存不存在"和"下载归档内部文件名应该是什么"是两个
  // 独立概念，article.url(网页地址)在这里不参与归档命名。只返回"理想"
  // 文件名，碰撞消解交给dedupeZipArcname()。
  function zipArcnameForArticle(article) {
    var date = String(article.date || "");
    var safeTitle = safeArticleFilename(article.title);
    if (date.length >= 7 && date.charAt(4) === "-") {
      return date.slice(0, 4) + "/" + date.slice(5, 7) + "/" + safeTitle + ".html";
    }
    return safeTitle + ".html";
  }

  // 跟app.py::_zip_arcname_for()里的碰撞消解规则完全一致：撞名时在扩展名前
  // 插入"-{postId}"（postId天然全局唯一），不静默覆盖。
  function dedupeZipArcname(name, postId, usedNames) {
    if (usedNames[name]) {
      var dot = name.lastIndexOf(".");
      name = name.slice(0, dot) + "-" + postId + name.slice(dot);
    }
    usedNames[name] = true;
    return name;
  }

  function getSelectedPostIds() {
    var boxes = document.querySelectorAll('input[data-role="select-post"]:checked');
    return Array.prototype.map.call(boxes, function (b) { return b.getAttribute("data-post-id"); });
  }

  function indexArticlesById(articles) {
    var map = {};
    (articles || []).forEach(function (a) { map[a.id] = a; });
    return map;
  }

  async function fetchAsArrayBuffer(url) {
    var resp = await fetch(url);
    if (!resp.ok) throw new Error("HTTP " + resp.status + " " + url);
    return await resp.arrayBuffer();
  }

  // 下载已勾选：按"文章原来的目录结构"（posts/<id>/index.html + media/*）
  // 打包，跟mirror的_zip_posts()内容对等，只是取材于当前Pages自己已经
  // 公开发布的静态文件，不经过任何服务器现场压缩。
  async function buildRawZip(postIds, articlesById) {
    var zip = new JSZip();
    var missing = [];
    for (var i = 0; i < postIds.length; i++) {
      var article = articlesById[postIds[i]];
      if (!article) { missing.push(postIds[i]); continue; }
      try {
        var htmlBuf = await fetchAsArrayBuffer(article.url);
        zip.file("posts/" + article.id + "/index.html", htmlBuf);
      } catch (e) {
        missing.push(postIds[i]);
        continue;
      }
      var mediaFiles = article.media_files || [];
      for (var j = 0; j < mediaFiles.length; j++) {
        try {
          var mediaBuf = await fetchAsArrayBuffer("/posts/" + article.id + "/media/" + mediaFiles[j]);
          zip.file("posts/" + article.id + "/media/" + mediaFiles[j], mediaBuf);
        } catch (e) {
          // 单个媒体文件抓取失败不影响其余内容，静默跳过——跟fetch_blog.py
          // 对单张图片本地化失败的处理风格一致，不因小失大。
        }
      }
    }
    return { zip: zip, missing: missing };
  }

  // 导出离线版(已勾选)：直接抓每篇文章预生成好的standalone_url（已经
  // base64内联好图片），比buildRawZip简单得多，不需要额外抓media文件。
  async function buildStandaloneZip(postIds, articlesById) {
    var zip = new JSZip();
    var missing = [];
    var usedNames = {};
    for (var i = 0; i < postIds.length; i++) {
      var article = articlesById[postIds[i]];
      if (!article || !article.standalone_url) { missing.push(postIds[i]); continue; }
      try {
        var buf = await fetchAsArrayBuffer(article.standalone_url);
        var name = dedupeZipArcname(zipArcnameForArticle(article), article.id, usedNames);
        zip.file(name, buf);
      } catch (e) {
        missing.push(postIds[i]);
      }
    }
    return { zip: zip, missing: missing };
  }

  function triggerBlobDownload(blob, filename) {
    var url = URL.createObjectURL(blob);
    var a = document.createElement("a");
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    setTimeout(function () { URL.revokeObjectURL(url); }, 1000);
  }

  function reportMissing(missing) {
    if (missing.length) {
      alert("以下文章的部分文件抓取失败，已跳过（其余内容仍会打包）：\n" + missing.join(", "));
    }
  }

  function initPagesDownload(options) {
    var indexUrl = (options && options.indexUrl) || "/search-index.json";
    var hasJSZip = typeof JSZip !== "undefined";
    var hasFetch = typeof fetch !== "undefined";

    var allDownloadBtn = document.querySelector('[data-role="download-all-btn"]');
    if (allDownloadBtn) {
      allDownloadBtn.onclick = function () { window.location.href = "/downloads/blog-full.zip"; };
    }

    var exportAllBtn = document.querySelector('[data-role="export-all-btn"]');
    if (exportAllBtn) {
      exportAllBtn.onclick = function () {
        if (!confirm("全站导出离线版可能体积较大，确认继续？")) return;
        window.location.href = "/downloads/export-all.zip";
      };
    }

    var exportTagBtn = document.querySelector('[data-role="export-tag-btn"]');
    if (exportTagBtn) {
      exportTagBtn.onclick = function () {
        var tagInput = document.querySelector('[data-role="tag"]');
        var tag = tagInput ? tagInput.value.trim() : "";
        if (!tag) { alert("请先在标签框输入要导出的标签"); return; }
        window.location.href = "/downloads/export-tag/" + encodeURIComponent(safeTagFilename(tag)) + ".zip";
      };
    }

    var selectedButtons = [
      document.querySelector('[data-role="download-selected-btn"]'),
      document.querySelector('[data-role="export-selected-btn"]'),
    ].filter(Boolean);

    if (!hasJSZip || !hasFetch) {
      // 已勾选这两个功能依赖JSZip+fetch，浏览器不支持时禁用并提示，而不是
      // 让用户点了没反应；全站/全部/按标签三个固定入口不受影响，仍然可用。
      selectedButtons.forEach(function (btn) {
        btn.disabled = true;
        btn.title = "当前浏览器不支持此功能";
      });
      return;
    }

    fetch(indexUrl)
      .then(function (r) { return r.json(); })
      .then(function (data) {
        var articlesById = indexArticlesById(data.articles);

        var downloadSelectedBtn = document.querySelector('[data-role="download-selected-btn"]');
        if (downloadSelectedBtn) {
          downloadSelectedBtn.onclick = async function () {
            var ids = getSelectedPostIds();
            if (!ids.length) { alert("请先勾选至少一篇文章"); return; }
            var result = await buildRawZip(ids, articlesById);
            reportMissing(result.missing);
            var blob = await result.zip.generateAsync({ type: "blob" });
            triggerBlobDownload(blob, "blog-mirror-selected.zip");
          };
        }

        var exportSelectedBtn = document.querySelector('[data-role="export-selected-btn"]');
        if (exportSelectedBtn) {
          exportSelectedBtn.onclick = async function () {
            var ids = getSelectedPostIds();
            if (!ids.length) { alert("请先勾选至少一篇文章"); return; }
            var result = await buildStandaloneZip(ids, articlesById);
            reportMissing(result.missing);
            var blob = await result.zip.generateAsync({ type: "blob" });
            triggerBlobDownload(blob, "blog-mirror-standalone.zip");
          };
        }
      })
      .catch(function () {
        // 索引加载失败时静默降级：全站/全部/按标签三个固定入口是纯静态
        // 链接，不依赖这次fetch，仍然可用；只有"已勾选"两个按钮会失去响应，
        // 这是可接受的降级，不弹错误打断阅读体验。
      });
  }

  return {
    safeTagFilename: safeTagFilename,
    safeArticleFilename: safeArticleFilename,
    zipArcnameForArticle: zipArcnameForArticle,
    dedupeZipArcname: dedupeZipArcname,
    initPagesDownload: initPagesDownload,
  };
});

if (typeof document !== "undefined") {
  document.addEventListener("DOMContentLoaded", function () {
    if (typeof module === "undefined") {
      window.PagesDownload.initPagesDownload({});
    }
  });
}
