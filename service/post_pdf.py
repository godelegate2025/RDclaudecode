"""A real PDF of a post audit, printed by Chromium from the same page the browser shows.

The report page renders itself from the audit JSON, so the PDF loads that page,
hands it the report, and prints with the page's own print styles: one layout
to maintain, and the download matches the screen.
"""

from __future__ import annotations

import base64
import re
from pathlib import Path

from website_audit.browser import browser_session

PAGE = Path(__file__).parent / "static" / "post.html"
# Only the brand fonts may load. Everything else the page shows is inline (the
# frames are data: URLs), so any other request is refused outright.
ALLOWED = re.compile(r"^https://fonts\.(googleapis|gstatic)\.com/")
# The brand fonts usually arrive in well under a second; past this the PDF
# prints in the fallback fonts rather than keep the visitor waiting.
FONT_WAIT_MS = 4000


def _route(route, request) -> None:
    if request.url.startswith("data:"):
        route.continue_()
    elif ALLOWED.match(request.url):
        # The font stylesheet blocks the page script, so a font host that never
        # answers would stall the PDF. Fetch it with a deadline instead.
        try:
            route.fulfill(response=route.fetch(timeout=FONT_WAIT_MS))
        except Exception:  # noqa: BLE001 - print in the fallback fonts
            route.abort("timedout")
    else:
        route.abort("blockedbyclient")


def filename_for(report: dict) -> str:
    post = report.get("post") or {}
    who = post.get("author_handle") or post.get("author") or "post"
    slug = re.sub(r"[^a-z0-9]+", "-", f"{report.get('platform', 'post')}-{who}".lower()).strip("-")
    return f"{slug[:60] or 'post'}-audit.pdf"


def render_pdf(report: dict) -> bytes:
    with browser_session() as browser:
        page = browser.new_page(viewport={"width": 1100, "height": 1400})
        page.route("**/*", _route)
        page.set_default_timeout(20_000)
        # Not "load": that waits on the font stylesheet, and a slow or blocked
        # Google Fonts would hold the whole audit. Fonts get a bounded wait below.
        page.set_content(PAGE.read_text(encoding="utf-8"), wait_until="domcontentloaded")
        page.evaluate("data => render(data)", report)
        page.evaluate(f"Promise.race([document.fonts.ready, new Promise(r => setTimeout(r, {FONT_WAIT_MS}))])")
        page.emulate_media(media="print")
        pdf = page.pdf(
            format="A4",
            print_background=True,
            display_header_footer=True,
            header_template="<div></div>",
            footer_template=(
                '<div style="width:100%;font:8px system-ui;color:#898781;padding:0 13mm;'
                'display:flex;justify-content:space-between;">'
                "<span>REDEFINE &middot; Post Audit</span>"
                '<span><span class="pageNumber"></span> / <span class="totalPages"></span></span></div>'
            ),
            margin={"top": "12mm", "bottom": "16mm", "left": "12mm", "right": "12mm"},
        )
        page.close()
    return pdf


def attach_pdf(report: dict) -> dict:
    """Add {"pdf": {filename, base64}} to the report."""
    report["pdf"] = {
        "filename": filename_for(report),
        "base64": base64.b64encode(render_pdf(report)).decode(),
    }
    return report
