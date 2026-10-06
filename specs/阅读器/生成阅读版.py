"""将开发文档与其引用的调研逐份转为可离线阅读的 HTML，不修改 Markdown。"""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import unicodedata
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit, urlunsplit

from bs4 import BeautifulSoup, NavigableString
from markdown_it import MarkdownIt
from mdit_py_plugins.tasklists import tasklists_plugin


TOOLS = Path(__file__).resolve().parent
SPECS = TOOLS.parent
GROUPS = [
    ("开发文档", SPECS / "0001-douyin-ai-studio"),
    ("本轮选型试验", SPECS / "selection" / "01-数字人口播"),
    ("已选模型接入核验", SPECS / "selection" / "02-已选模型"),
    ("人物与音色候选", SPECS / "selection" / "03-人物与音色"),
    ("调研资料", SPECS / "research"),
]
DOCUMENTS = [(group, path) for group, folder in GROUPS for path in sorted(folder.glob("*.md"))]
OUTPUTS = {source.resolve(): source.with_suffix(".html") for _, source in DOCUMENTS}


def href_to(target: Path, current: Path) -> str:
    return quote(Path(os.path.relpath(target, current.parent)).as_posix(), safe="/-._~")


def slug(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).lower()
    text = re.sub(r"[^\w\s-]", "", text, flags=re.UNICODE)
    return re.sub(r"[-\s]+", "-", text).strip("-") or "section"


def convert(source: Path) -> tuple[BeautifulSoup, str, str]:
    raw = source.read_text(encoding="utf-8")
    md = MarkdownIt("commonmark", {"html": False}).enable(["table", "strikethrough"])
    md.use(tasklists_plugin)
    soup = BeautifulSoup(md.render(raw), "html.parser")
    first = soup.find("h1")
    title = first.get_text(" ", strip=True) if first else source.stem
    if first:
        first.decompose()

    used: set[str] = set()
    for heading in soup.find_all(re.compile(r"^h[1-6]$")):
        base = slug(heading.get_text(" ", strip=True))
        anchor = base
        n = 1
        while anchor in used:
            anchor = f"{base}-{n}"
            n += 1
        used.add(anchor)
        heading["id"] = anchor

    # 只恢复普通正文里的显式换行，代码及其他原始 HTML 始终按文本显示。
    for text in list(soup.find_all(string=re.compile(r"<br\s*/?>", re.I))):
        if text.find_parent(["code", "pre"]):
            continue
        pieces = re.split(r"<br\s*/?>", str(text), flags=re.I)
        for i, piece in enumerate(pieces):
            if i:
                text.insert_before(soup.new_tag("br"))
            text.insert_before(NavigableString(piece))
        text.extract()

    for link in soup.find_all("a", href=True):
        value = link["href"]
        parsed = urlsplit(value)
        if parsed.scheme in ("http", "https"):
            link["target"] = "_blank"
            link["rel"] = "noopener noreferrer"
        elif parsed.scheme:
            if parsed.scheme not in ("mailto",):
                del link["href"]
        elif parsed.path:
            target = (source.parent / unquote(parsed.path)).resolve()
            if target in OUTPUTS:
                destination = href_to(OUTPUTS[target], source)
                link["href"] = urlunsplit(("", "", destination, parsed.query, parsed.fragment))

    for table in soup.find_all("table"):
        wrapper = soup.new_tag("div", attrs={"class": "table-scroll", "tabindex": "0", "aria-label": "可横向滚动的表格"})
        table.wrap(wrapper)

    for block in list(soup.select("pre > code.language-mermaid")):
        metadata = json.loads((TOOLS / "架构图来源.json").read_text(encoding="utf-8"))
        diagram_hash = hashlib.sha256(block.get_text().strip().encode("utf-8")).hexdigest()
        if diagram_hash != metadata["mermaid_sha256"]:
            raise ValueError("架构图源码已变更：请重新渲染 SVG 并更新来源摘要，防止显示过期图表。")
        figure = soup.new_tag("figure", attrs={"class": "architecture"})
        svg = BeautifulSoup((TOOLS / "架构图.svg").read_text(encoding="utf-8"), "html.parser").find("svg")
        if svg is None:
            raise ValueError("缺少可用的架构图 SVG")
        svg["role"] = "img"
        svg["aria-label"] = "目标架构与生产质量门禁流程"
        viewport = soup.new_tag("div", attrs={"class": "diagram-viewport", "tabindex": "0", "aria-label": "可横向滚动的架构图"})
        viewport.append(svg)
        figure.append(viewport)
        caption = soup.new_tag("figcaption")
        caption.string = "目标架构与关键门禁 · 小屏可横向滚动查看"
        figure.append(caption)
        details = soup.new_tag("details")
        summary = soup.new_tag("summary")
        summary.string = "查看图表源代码"
        details.append(summary)
        pre = soup.new_tag("pre")
        code = soup.new_tag("code", attrs={"class": "language-mermaid"})
        code.string = block.get_text()
        pre.append(code)
        details.append(pre)
        figure.append(details)
        block.parent.replace_with(figure)

    return soup, title, raw


def page(source: Path, group: str, index: int, css: str, js: str) -> tuple[str, dict]:
    body, title, raw = convert(source)
    escaped = html.escape
    navigation: list[str] = []
    for name, _ in GROUPS:
        items = []
        for g, other in DOCUMENTS:
            if g != name:
                continue
            current = ' aria-current="page"' if other == source else ""
            items.append(f'<a data-document href="{href_to(other.with_suffix(".html"), source)}"{current}>{escaped(other.stem.replace("-", " ", 1))}</a>')
        navigation.append(f'<section class="nav-group"><h2 class="group-title">{name}</h2><nav class="doc-nav" aria-label="{name}">{"".join(items)}</nav></section>')

    headings = body.find_all(["h2", "h3"])
    toc = "".join(f'<a class="toc-{h.name}" href="#{quote(h["id"], safe="-_")}">{escaped(h.get_text(" ", strip=True))}</a>' for h in headings)
    major_count = len(body.find_all("h2"))
    version = re.search(r"版本[：:\s]+(v\d+\.\d+)", raw[:900])
    meta = version.group(1) if version else "2026-09-21 核验记录"
    if group == "开发文档":
        meta += " · 评审版"
    pager = []
    if index > 0:
        prev = DOCUMENTS[index - 1][1]
        pager.append(f'<a rel="prev" href="{href_to(prev.with_suffix(".html"), source)}"><span>上一篇</span>{escaped(prev.stem)}</a>')
    if index + 1 < len(DOCUMENTS):
        nxt = DOCUMENTS[index + 1][1]
        pager.append(f'<a rel="next" href="{href_to(nxt.with_suffix(".html"), source)}"><span>下一篇</span>{escaped(nxt.stem)}</a>')

    notice = '<p class="source-notice">调研原始记录。当前范围和决策以最新开发文档及实测选型记录为准。</p>' if group == "调研资料" else ""
    navigation_count = "<br>".join(f"{sum(1 for item, _ in DOCUMENTS if item == name)} 份{name}" for name, _ in GROUPS)
    home = href_to(DOCUMENTS[0][1].with_suffix(".html"), source)
    source_href = href_to(source, source)
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    rendered = f'''<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light">
<link rel="icon" href="data:,">
<meta name="source-sha256" content="{digest}">
<title>{escaped(title)} · AI 自媒体文档</title>
<style>{css}</style>
</head>
<body>
<a class="skip-link" href="#main">跳到正文</a>
<header class="topbar">
  <button type="button" class="mobile-menu" aria-controls="doc-sidebar" aria-expanded="false">文档目录</button>
  <a class="brand" href="{home}">AI 自媒体<span>开发文档与调研</span></a>
  <div class="toolbar" aria-label="阅读设置">
    <button type="button" class="font-smaller" aria-label="缩小正文字号" title="缩小字号">A−</button>
    <button type="button" class="font-reset" aria-label="恢复默认正文字号" title="恢复字号">默认</button>
    <button type="button" class="font-larger" aria-label="放大正文字号" title="放大字号">A+</button>
    <button type="button" class="print-button">打印</button>
  </div>
</header>
<div class="layout">
  <aside id="doc-sidebar" class="sidebar" aria-label="全部文档">{"".join(navigation)}
    <p class="sidebar-note">{navigation_count}</p>
  </aside>
  <main id="main" tabindex="-1">
    <header class="document-header">
      <p class="eyebrow">{group} / {escaped(source.stem.split('-')[0])}</p>
      <h1>{escaped(title)}</h1>
      <div class="doc-meta"><span>{meta}</span><span>{major_count} 个章节</span><a href="{source_href}">Markdown 原文</a></div>
      {notice}
    </header>
    <div class="reader-tools">
      <form id="search-form" role="search">
        <label class="sr-only" for="search-input">搜索本文</label>
        <input id="search-input" type="search" placeholder="搜索本文，按 Enter 跳转" autocomplete="off">
        <button id="search-next" type="submit">下一处</button>
        <button id="search-clear" type="button">清除</button>
        <span id="search-status" role="status" aria-live="polite"></span>
      </form>
    </div>
    <article id="document-body">{body}</article>
    <nav class="document-pager" aria-label="相邻文档">{"".join(pager)}</nav>
    <footer class="page-footer">与同名 Markdown 对应的阅读版 · 可离线阅读与打印</footer>
  </main>
  <aside class="toc-panel" aria-label="本文章节"><p class="group-title">本页目录</p><nav id="toc">{toc}</nav></aside>
</div>
<button type="button" class="sidebar-scrim" aria-label="关闭文档目录" hidden></button>
<button type="button" class="back-top" aria-label="返回顶部">顶部</button>
<script>{js}</script>
</body>
</html>
'''
    return rendered, {"source": str(source.relative_to(SPECS)), "html": str(source.with_suffix(".html").relative_to(SPECS)), "sha256": digest, "chapters": major_count}


def main() -> None:
    css = (TOOLS / "阅读样式.css").read_text(encoding="utf-8")
    js = (TOOLS / "阅读交互.js").read_text(encoding="utf-8")
    records = []
    for index, (group, source) in enumerate(DOCUMENTS):
        output, record = page(source, group, index, css, js)
        source.with_suffix(".html").write_text(output, encoding="utf-8")
        records.append(record)
    (TOOLS / "生成清单.json").write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Generated {len(records)} offline HTML documents; Markdown sources unchanged.")


if __name__ == "__main__":
    main()
