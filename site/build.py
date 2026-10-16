"""Build the falsepass website into a folder (default `_site`).

    uv run --no-project --with markdown==3.7 python site/build.py [_site]

The docs pages are rendered from the repository's own Markdown (README, docs/, the skill),
so the website never drifts from the repository. Every page is also published as plain
Markdown next to its HTML, and `llms.txt` / `llms-full.txt` index them for tools that read
documentation as text. BASE is the path the site is served under (`/falsepass` on
GitHub Pages; `FALSEPASS_SITE_BASE=` for a server at the root).
"""

from __future__ import annotations

import html
import os
import re
import shutil
import sys
from datetime import date
from pathlib import Path

import markdown

ROOT = Path(__file__).resolve().parent.parent
SITE = ROOT / "site"
BASE = os.environ.get("FALSEPASS_SITE_BASE", "/falsepass").rstrip("/")
ORIGIN = "https://super-eric.github.io"
REPO = "https://github.com/Super-Eric/falsepass"
VERSION = re.search(r'^version = "(.+)"', (ROOT / "pyproject.toml").read_text(), re.M).group(1)

# Repository files that have a page of their own: link targets inside Markdown are rewritten to them.
DOC_PAGES = {
    "README.md": "/docs/",
    "README.zh-CN.md": "/zh/docs/",
    "docs/OUTPUT.md": "/docs/output/",
    "docs/SEVERITY.md": "/docs/severity/",
    "skills/falsepass/SKILL.md": "/docs/agents/skill/",
}

NAV = {
    "en": [("Docs", "/docs/"), ("For agents", "/docs/agents/"), ("Writing", "/blog/"), ("GitHub", REPO), ("中文", "/zh/")],
    "zh": [("文档", "/zh/docs/"), ("给 AI 代理", "/docs/agents/"), ("文章", "/blog/"), ("GitHub", REPO), ("English", "/")],
}


def url(path: str) -> str:
    return path if path.startswith("http") else BASE + path


def strip_front_matter(text: str) -> tuple[dict[str, str], str]:
    meta: dict[str, str] = {}
    if text.startswith("---\n"):
        head, _, text = text[4:].partition("\n---\n")
        for line in head.splitlines():
            key, _, value = line.partition(":")
            meta[key.strip()] = value.strip().strip('"')
    return meta, text


def rewrite_links(text: str, source: str) -> str:
    """Relative links in a repository file point at its page on the site, or at GitHub."""
    folder = Path(source).parent

    def fix(match: re.Match[str]) -> str:
        label, target = match.group(1), match.group(2)
        blob = f"{REPO}/blob/main/"
        if target.startswith(blob) and target[len(blob):].partition("#")[0] in DOC_PAGES:  # a page this site renders
            path, _, anchor = target[len(blob):].partition("#")
            return f"[{label}]({url(DOC_PAGES[path])}{'#' + anchor if anchor else ''})"
        if re.match(r"[a-z]+:|#|/", target):
            return match.group(0)
        path, _, anchor = target.partition("#")
        resolved = os.path.normpath(folder / path).replace(os.sep, "/")
        if resolved in DOC_PAGES:
            return f"[{label}]({url(DOC_PAGES[resolved])}{'#' + anchor if anchor else ''})"
        if resolved == "docs/demo.gif":
            return f"[{label}]({url('/assets/demo.gif')})"
        kind = "tree" if (ROOT / resolved).is_dir() else "blob"
        return f"[{label}]({REPO}/{kind}/main/{resolved}{'#' + anchor if anchor else ''})"

    return re.sub(r"\[([^\]]*)\]\(([^)\s]+)\)", fix, text)


def render(text: str) -> str:
    return markdown.markdown(text, extensions=["tables", "fenced_code", "sane_lists", "toc"],
                             extension_configs={"toc": {"permalink": "#", "permalink_title": ""}})  # fmt: skip


def page(*, title: str, body: str, path: str, lang: str = "en", description: str = "", md: str | None = None,
         kind: str = "doc") -> str:  # fmt: skip
    nav = "".join(
        f'<a href="{url(href)}"{" aria-current=page" if href != "/" and path.startswith(href) else ""}>{html.escape(label)}</a>'
        for label, href in NAV[lang]
    )
    full_title = "falsepass" if path in ("/", "/zh/") else f"{title} · falsepass"
    desc = html.escape(description or "Probe RL environment verifiers for false passes and false fails.")
    alt = f'<link rel="alternate" type="text/markdown" href="{url(md)}">' if md else ""
    md_link = f'<a href="{url(md)}">{"本页 Markdown" if lang == "zh" else "This page as Markdown"}</a> · ' if md else ""
    return f"""<!doctype html>
<html lang="{'zh-CN' if lang == 'zh' else 'en'}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(full_title)}</title>
<meta name="description" content="{desc}">
<meta property="og:title" content="{html.escape(title)}">
<meta property="og:description" content="{desc}">
<meta property="og:type" content="{'article' if kind == 'post' else 'website'}">
<meta property="og:url" content="{ORIGIN}{url(path)}">
<meta property="og:image" content="{ORIGIN}{url('/assets/og.png')}">
<meta name="twitter:card" content="summary_large_image">
<link rel="canonical" href="{ORIGIN}{url(path)}">
{alt}
<link rel="icon" href="{url('/assets/favicon.svg')}" type="image/svg+xml">
<link rel="stylesheet" href="{url('/assets/style.css')}">
</head>
<body class="{kind}">
<header class="top"><div class="wrap">
<a class="brand" href="{url('/zh/' if lang == 'zh' else '/')}"><span class="mark" aria-hidden="true"></span>falsepass</a>
<nav>{nav}</nav>
</div></header>
<main>{body}</main>
<footer><div class="wrap">
<span>{md_link}falsepass {VERSION} · Apache-2.0 · <a href="{REPO}">GitHub</a> · <a href="{url('/llms.txt')}">llms.txt</a></span>
</div></footer>
<script src="{url('/assets/site.js')}" defer></script>
</body>
</html>
"""


def write(out: Path, path: str, content: str) -> None:
    target = out / path.lstrip("/")
    if path.endswith("/"):
        target = target / "index.html"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)


def doc_body(markdown_text: str, *, toc: bool = True) -> str:
    md = markdown.Markdown(extensions=["tables", "fenced_code", "sane_lists", "toc"],
                           extension_configs={"toc": {"permalink": "#", "permalink_title": "", "toc_depth": "2"}})  # fmt: skip
    content = md.convert(markdown_text)
    side = f'<aside class="toc"><div class="toc-title">On this page</div>{md.toc}</aside>' if toc and md.toc_tokens else ""
    return f'<div class="wrap docgrid">{side}<article class="prose">{content}</article></div>'


def readme_for_site(text: str) -> str:
    """The README without its badge block and demo image (the landing page has those)."""
    text = re.sub(r"^\[!\[.*\n", "", text, flags=re.M)
    text = re.sub(r"^!\[[^\]]*\]\([^)]*demo\.gif\)\n\n?", "", text, flags=re.M)
    text = re.sub(r"^(English \| \[中文\]\([^)]*README\.zh-CN\.md\)|\[English\]\([^)]*README\.md\) \| 中文)\n\n?", "", text, flags=re.M)
    text = re.sub(r"^(Website and docs:|网站和文档：).*\n(?:.+\n)*\n?", "", text, flags=re.M)
    return text


def build(out: Path) -> None:
    if out.exists():
        shutil.rmtree(out)
    (out / "assets").mkdir(parents=True)
    for asset in (SITE / "assets").iterdir():
        shutil.copy(asset, out / "assets" / asset.name)
    shutil.copy(ROOT / "docs" / "demo.gif", out / "assets" / "demo.gif")

    texts: list[tuple[str, str, str, str]] = []  # (title, page path, markdown path, markdown) for llms.txt
    blurbs: dict[str, str] = {}  # page path -> one-line description

    # Landing pages: hand-written HTML bodies.
    for lang, path, source in (("en", "/", "index.html"), ("zh", "/zh/", "index.zh.html")):
        body = (SITE / "pages" / source).read_text().replace("{BASE}", BASE).replace("{VERSION}", VERSION)
        write(out, path, page(title="falsepass", body=body, path=path, lang=lang, kind="home"))

    # Docs, rendered from the repository's Markdown.
    docs = [
        ("README.md", "Guide", "en", "Install, quick start, every command and check, and how to go from a probe run to a report."),
        ("README.zh-CN.md", "使用指南", "zh", "安装、快速上手、全部命令和检查项，以及从探测结果到报告的流程。"),
        ("docs/OUTPUT.md", "Output files", "en", "The files an audit writes, the JSON formats and the exit codes."),
        ("docs/SEVERITY.md", "Severity scale", "en", "How findings are rated: Critical, High, Medium, Low."),
        ("skills/falsepass/SKILL.md", "Audit skill", "en", "A ready-made audit procedure for coding agents, in the Agent Skills format."),
    ]
    for source, title, lang, description in docs:
        _, text = strip_front_matter((ROOT / source).read_text())
        if source.startswith("README"):
            text = readme_for_site(text)
        text = rewrite_links(text, source)
        path = DOC_PAGES[source]
        md_path = path + "index.md"
        write(out, md_path, text)
        texts.append((title, path, md_path, text))
        blurbs[path] = description
        write(out, path, page(title=title, body=doc_body(text), path=path, lang=lang, description=description, md=md_path))

    # The agents page: hand-written Markdown in site/content.
    meta, text = strip_front_matter((SITE / "content" / "agents.md").read_text())
    text = text.replace("{BASE}", BASE).replace("{ORIGIN}", ORIGIN)
    write(out, "/docs/agents/index.md", text)
    texts.insert(1, (meta["title"], "/docs/agents/", "/docs/agents/index.md", text))
    blurbs["/docs/agents/"] = meta.get("description", "")
    write(out, "/docs/agents/", page(title=meta["title"], body=doc_body(text), path="/docs/agents/",
                                     description=meta.get("description", ""), md="/docs/agents/index.md"))  # fmt: skip

    # Writing.
    posts = []
    for source in sorted((SITE / "content" / "blog").glob("*.md")):
        meta, text = strip_front_matter(source.read_text())
        text = text.replace("{SITE}", ORIGIN + BASE)
        slug = source.stem
        path, md_path = f"/blog/{slug}/", f"/blog/{slug}/index.md"
        posts.append((meta, path))
        write(out, md_path, f"# {meta['title']}\n\n{text}")
        texts.append((meta["title"], path, md_path, f"# {meta['title']}\n\n{text}"))
        blurbs[path] = meta.get("description", "")
        when = date.fromisoformat(meta["date"])
        body = (f'<div class="wrap narrow"><article class="prose post"><p class="kicker"><time datetime="{when}">'
                f'{when:%d %B %Y}</time></p><h1>{html.escape(meta["title"])}</h1>{render(text)}</article></div>')  # fmt: skip
        write(out, path, page(title=meta["title"], body=body, path=path, description=meta.get("description", ""), md=md_path, kind="post"))
    posts.sort(key=lambda p: p[0]["date"], reverse=True)
    items = "".join(
        f'<li><a href="{url(p)}"><span class="post-title">{html.escape(m["title"])}</span></a>'
        f'<span class="post-meta">{date.fromisoformat(m["date"]):%d %B %Y}</span><p>{html.escape(m.get("description", ""))}</p></li>'
        for m, p in posts
    )
    write(out, "/blog/", page(title="Writing", path="/blog/", kind="doc",
                              body=f'<div class="wrap narrow"><article class="prose"><h1>Writing</h1><ul class="posts">{items}</ul></article></div>'))  # fmt: skip

    # 404, llms.txt, sitemap, robots.
    write(out, "/404.html", page(title="Not found", path="/404.html",
                                 body=f'<div class="wrap narrow"><article class="prose"><h1>Not found</h1><p><a href="{url("/")}">Back to the start</a>.</p></article></div>'))  # fmt: skip
    llms = [
        "# falsepass",
        "",
        "> Probe the reward code of RL environments and eval tasks (verifiers / Prime Intellect Environments Hub, "
        "Inspect AI, HUD) for false passes (a known-bad completion is rewarded) and false fails (a known-good one is not). "
        "CLI, MCP server, GitHub Action and an agent skill. Apache-2.0.",
        "",
        "Install: `uvx falsepass demo` (PyPI). MCP: `uvx --from \"falsepass[mcp]\" falsepass mcp`. "
        "Output is a list of leads; reproduce one and read the reward code before calling it a defect.",
        "",
        "## Docs",
        "",
    ]
    llms += [f"- [{title}]({ORIGIN}{url(md)}): {blurbs[path]}" for title, path, md, _ in texts if not path.startswith("/blog/")]
    llms += ["", "## Writing", ""]
    llms += [f"- [{title}]({ORIGIN}{url(md)}): {blurbs[path]}" for title, path, md, _ in texts if path.startswith("/blog/")]
    llms += ["", "## Optional", "", f"- [Source]({REPO})", f"- [Changelog]({REPO}/blob/main/CHANGELOG.md)"]
    write(out, "/llms.txt", "\n".join(llms) + "\n")
    write(out, "/llms-full.txt", "\n\n---\n\n".join(text for _, _, _, text in texts) + "\n")
    pages = ["/", "/zh/", "/docs/agents/", "/blog/"] + [p for _, p, _, _ in texts if p != "/docs/agents/"]
    write(out, "/sitemap.xml", '<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
          + "".join(f"  <url><loc>{ORIGIN}{url(p)}</loc></url>\n" for p in pages) + "</urlset>\n")  # fmt: skip
    write(out, "/robots.txt", f"User-agent: *\nAllow: /\nSitemap: {ORIGIN}{url('/sitemap.xml')}\n")
    (out / ".nojekyll").write_text("")


if __name__ == "__main__":
    build(Path(sys.argv[1] if len(sys.argv) > 1 else ROOT / "_site").resolve())
