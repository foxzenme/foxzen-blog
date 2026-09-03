#!/usr/bin/env python3
"""把文章正文里"引用本站另一篇文章"的Blogger绝对永久链接，改写成本站根相对
地址（如 /2026/08/slug.html 或 /posts/<id>/），只在Flask生成响应的那一刻调用
（见app.py），绝不写回磁盘上的html/posts/<id>/index.html本身——那份文件还要
给离线下载/导出功能（app.py的_inline_post_as_base64）原样读取，改写后的相对
路径离线打开时无法解析，磁盘源文件必须保持字节不变。

改写只认"根相对路径"，不拼接任何协议/域名，所以天然不区分host：mirror请求
经过这里输出相对链接，落在mirror；backup/github/cf同理，不需要也不应该在这里
判断request.host。

匹配规则（精确匹配，不做模糊猜测）：
- class="discuss-btn"（本文自己的"到主站讨论"按钮）一律跳过，不解析其href。
- href等于own_permalink（本文自己的Blogger permalink）一律跳过。
- href不在permalink_to_url映射里（外部链接、未收录文章）一律原样保留。
- 只有上面两条都不命中、且href精确等于映射里某个key时才改写，并强制补上
  target="_blank" rel="noopener"（本站文章链接默认新标签页打开）。
"""
import re

_ANCHOR_OPEN_TAG_PATTERN = re.compile(r"<a\b[^>]*>")
_HREF_PATTERN = re.compile(r'href="([^"]*)"')
_ATTR_STRIP_PATTERN = re.compile(r'\s+(?:href|target|rel)="[^"]*"')


def rewrite_internal_links(html: str, permalink_to_url: dict, own_permalink: str | None = None) -> str:
    """html: 单篇文章的完整正文HTML（POST_TEMPLATE渲染结果，不只是.content片段）。
    permalink_to_url: db.get_all_permalinks()的返回值。
    own_permalink: 当前这篇文章自己的Blogger permalink（db.get_source_url(post_id)），
    用于排除"本文自己"这种不算"引用另一篇文章"的情况。
    """
    def _rewrite(match: re.Match) -> str:
        tag = match.group(0)
        if 'class="discuss-btn"' in tag:
            return tag
        href_match = _HREF_PATTERN.search(tag)
        if not href_match:
            return tag
        href = href_match.group(1)
        if own_permalink is not None and href == own_permalink:
            return tag
        real_url = permalink_to_url.get(href)
        if real_url is None:
            return tag
        # 统一重建href/target/rel三个属性，不管原标签里这几个属性长什么样
        # （Blogger原文里有的链接自带target="_blank"、有的带rel="nofollow"，
        # 不做逐个属性的合并判断，直接规范化成要求的最终状态更不容易出错）。
        inner = _ATTR_STRIP_PATTERN.sub("", tag[2:-1])
        return f'<a href="{real_url}"{inner} target="_blank" rel="noopener">'

    return _ANCHOR_OPEN_TAG_PATTERN.sub(_rewrite, html)
