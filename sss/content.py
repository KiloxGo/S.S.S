import html
import os
from pathlib import Path
from urllib.parse import quote

import markdown
from bs4 import BeautifulSoup
from markdownify import markdownify


def link(source, target, label=None):
    target = Path(target)
    if target.name == "index.html":
        target = target.with_name("README.md")
    path = quote(os.path.relpath(target, Path(source).parent).replace("\\", "/"), safe="/.")
    return f"[{(label or Path(target).name).replace('[', '(').replace(']', ')')}]({path})"


def sanitize_html(source):
    soup = BeautifulSoup(source or "", "html.parser")
    for node in soup.select("script, style, iframe, object, embed, form, input, button, meta, link, base"):
        node.decompose()
    for node in soup.find_all(True):
        for key, value in list(node.attrs.items()):
            if key.lower().startswith("on") or key.lower() in ("style", "srcdoc") or key.startswith("data-"):
                del node[key]
            elif key in ("href", "src") and str(value).strip().lower().startswith(("javascript:", "vbscript:", "data:")):
                del node[key]
    return str(soup)


def html_markdown(source):
    return markdownify(sanitize_html(source), heading_style="ATX", bullets="-", escape_underscores=False).strip()


def html_page(title, body):
    body = sanitize_html(body)
    return f'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>{html.escape(title)}</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>body{{max-width:900px;margin:40px auto;padding:0 24px;line-height:1.8;font-family:system-ui,sans-serif;color:#243040}}a{{color:#1765ab}}img{{max-width:100%}}table{{border-collapse:collapse}}td,th{{border:1px solid #ccd4df;padding:6px 12px}}pre{{overflow:auto;background:#f3f5f7;padding:16px}}blockquote{{border-left:3px solid #ccd4df;padding-left:16px}}h1,h2,h3{{line-height:1.3}}</style></head><body>{body}</body></html>'''


def write_pair(store, directory, title, text, *, original_html=None):
    directory = Path(directory)
    store.text(directory / "README.md", text.rstrip() + "\n")
    body = original_html if original_html is not None else markdown.markdown(text, extensions=["tables", "fenced_code"])
    soup = BeautifulSoup(body, "html.parser")
    for node in soup.select("a[href]"):
        if node["href"].endswith("README.md") and not node["href"].startswith(("http:", "https:")):
            node["href"] = node["href"][:-len("README.md")] + "index.html"
    body = str(soup)
    store.text(directory / "index.html", html_page(title, body))
