"""Renders the Markdown build brief as a styled HTML page for the report panel.

The Markdown is the deliverable (it is what gets pasted into a site builder);
this is only the preview. It is rendered here rather than in the browser so
the page carries no Markdown library and the brief wears the report's own
type and colours. Everything in the brief is text scraped from the audited
site, so the Markdown is escaped before rendering and the result is shown in
a sandboxed frame.
"""

from __future__ import annotations

import re
from html import escape

import markdown

FILL_MARK = re.compile(r"\[FILL IN\]")

STYLE = """
:root { --coral: #FC4452; --ink: #191616; --charcoal: #2C2828; --paper: #F4F3F0; --line: #D8D5CE; --muted: #7C7773; }
* { box-sizing: border-box; }
body { margin: 0; background: #fff; color: var(--ink); font-family: 'Archivo', system-ui, -apple-system, 'Segoe UI', sans-serif; font-size: 15px; line-height: 1.6; }
main { max-width: 860px; margin: 0 auto; padding: 40px clamp(20px, 5vw, 56px) 96px; }
h1, h2, h3 { font-family: 'Anton', 'Arial Narrow', sans-serif; font-weight: 400; letter-spacing: .01em; line-height: 1.05; margin: 0; }
h1 { font-size: 40px; text-transform: uppercase; margin-bottom: 10px; }
h2 { font-size: 26px; text-transform: uppercase; margin-top: 44px; margin-bottom: 12px; padding-top: 22px; border-top: 3px solid var(--ink); }
h3 { font-family: 'Archivo', system-ui, sans-serif; font-weight: 700; font-size: 16px; letter-spacing: .04em; text-transform: uppercase; margin-top: 28px; margin-bottom: 6px; }
p { margin: 10px 0; max-width: 72ch; }
strong { font-weight: 700; }
a { color: var(--coral); }
hr { border: 0; height: 0; margin: 0; }
ul, ol { padding-left: 1.4em; max-width: 78ch; }
li { margin: 4px 0; }
li li { margin: 2px 0; }
blockquote { margin: 12px 0; padding: 4px 16px; border-left: 4px solid var(--coral); color: var(--charcoal); }
table { border-collapse: collapse; width: 100%; margin: 12px 0 18px; font-size: 14px; }
th, td { text-align: left; padding: 8px 10px; border-bottom: 1px solid var(--line); vertical-align: top; }
th { font-size: 11.5px; letter-spacing: .12em; text-transform: uppercase; color: var(--muted); border-bottom: 2px solid var(--ink); }
code { font-family: ui-monospace, Menlo, Consolas, monospace; font-size: 13px; background: var(--paper); padding: 1px 5px; border-radius: 3px; }
pre { background: var(--ink); color: var(--paper); padding: 14px 16px; border-radius: 6px; overflow-x: auto; font-size: 13px; line-height: 1.5; }
pre code { background: none; color: inherit; padding: 0; }
.fill { display: inline-block; background: var(--coral); color: #fff; font-weight: 700; font-size: 11.5px; letter-spacing: .1em; padding: 2px 7px; border-radius: 3px; vertical-align: middle; }
.lede { color: var(--muted); font-size: 14px; }
.count { position: sticky; top: 0; z-index: 2; background: var(--ink); color: var(--paper); font-size: 12.5px; letter-spacing: .06em; padding: 8px 16px; display: flex; gap: 18px; }
.count b { color: var(--coral); }
@media (prefers-reduced-motion: no-preference) { html { scroll-behavior: smooth; } }
"""


def render_brief_html(markdown_text: str, title: str) -> str:
    """The brief as a styled page. Scraped text is escaped before Markdown runs."""
    safe = escape(markdown_text, quote=False)
    body = markdown.markdown(safe, extensions=["tables", "fenced_code", "sane_lists"])
    fills = len(FILL_MARK.findall(markdown_text))
    body = FILL_MARK.sub('<span class="fill">FILL IN</span>', body)
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(title)}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Anton&family=Archivo:wght@400;500;700&display=swap" rel="stylesheet">
<style>{STYLE}</style>
</head>
<body>
<div class="count"><span>Build brief preview</span><span><b>{fills}</b> fields to fill in before building</span><span>Download MD for the file to paste into a builder</span></div>
<main>
{body}
</main>
</body>
</html>
"""
