"""Loads a page in Chromium and collects the raw design/performance evidence."""

from __future__ import annotations

import re
import time
from contextlib import nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from playwright.sync_api import Browser
from playwright.sync_api import Error as PlaywrightError

from .browser import browser_session
from .findability import site_files

PROBE = (Path(__file__).parent / "probe.js").read_text()

DESKTOP = {"width": 1440, "height": 900}
MOBILE = {"width": 390, "height": 844}

# An uploaded HTML file is served to Chromium from this address, which only
# exists inside the browser: `.invalid` never resolves (RFC 6761). Serving it
# over https rather than file:// keeps the page off the local disk; a file://
# page can frame other local files.
UPLOAD_ORIGIN = "https://uploaded-design.invalid"
UPLOAD_URL = UPLOAD_ORIGIN + "/"


@dataclass
class PageData:
    url: str
    final_url: str
    status: int | None
    load_ms: int
    probe: dict[str, Any]
    mobile: dict[str, Any]
    raw_html: str = ""
    site_files: Any = None
    resources: list[dict[str, Any]] = field(default_factory=list)
    console_errors: list[str] = field(default_factory=list)
    screenshots: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    source: str = "url"   # "upload" when the page came from an HTML file, not a website
    label: str = ""       # the uploaded file's name
    missing_frames: list[str] = field(default_factory=list)  # frames an upload pointed at files it lacked

    @property
    def is_upload(self) -> bool:
        return self.source == "upload"

    @property
    def display_url(self) -> str:
        return f"Uploaded file: {self.label}" if self.is_upload else self.final_url


# Anything already carrying a scheme is left alone; only a bare host gets https://.
# Prefixing blindly turned "file:///tmp/x.html" into "https://file///tmp/x.html".
HAS_SCHEME = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*:")


def _normalise(url: str) -> str:
    url = url.strip()
    return url if HAS_SCHEME.match(url) else "https://" + url


def collect(
    url: str,
    out_dir: Path,
    timeout_ms: int = 45000,
    browser: Browser | None = None,
    route_guard=None,
    html: str | None = None,
    label: str = "",
) -> PageData:
    """Render the page and gather the evidence.

    Pass an existing `browser` to reuse one Chromium across many audits; without
    one a private session is launched and closed around this call. `route_guard`
    is a Playwright route handler given every request — a public deployment uses
    it to re-check redirect targets against its blocklist.

    Pass `html` to audit a file instead of a website: `url` is ignored, the
    markup is served at UPLOAD_URL, and `label` (the file name) stands in for
    the address in the report. Its relative links have nowhere to resolve, so
    they get a 404; absolute ones (fonts, CDNs) load as usual.
    """
    url = UPLOAD_URL if html is not None else _normalise(url)
    out_dir.mkdir(parents=True, exist_ok=True)
    resources: list[dict[str, Any]] = []
    console_errors: list[str] = []
    warnings: list[str] = []
    missing_frames: list[str] = []

    with browser_session() if browser is None else nullcontext(browser) as active:
        context = active.new_context(
            viewport=DESKTOP,
            device_scale_factor=1,
            locale="en-US",
            timezone_id="UTC",
        )
        page = context.new_page()
        if route_guard is not None:
            page.route("**/*", route_guard)
        if html is not None:
            # Registered after the guard, so Playwright consults it first.
            page.route(UPLOAD_ORIGIN + "/**", lambda route, request: _serve_upload(route, request, html))

        def on_response(response):
            try:
                request = response.request
                size = response.header_value("content-length")
                resources.append(
                    {
                        "url": response.url,
                        "type": request.resource_type,
                        "status": response.status,
                        "bytes": int(size) if size and size.isdigit() else 0,
                    }
                )
                # A frame in an uploaded file pointing at a file that wasn't uploaded.
                if (html is not None and request.resource_type == "document"
                        and response.status == 404 and response.url.startswith(UPLOAD_ORIGIN)
                        and request.frame != page.main_frame):
                    missing_frames.append(response.url[len(UPLOAD_ORIGIN):][:80])
            except Exception:  # noqa: BLE001 - never fail the audit on telemetry
                pass

        page.on("response", on_response)
        def on_console(msg):
            if msg.type == "error" and "favicon" not in msg.text.lower():
                console_errors.append(msg.text[:200])

        page.on("console", on_console)

        started = time.time()
        response = page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)

        # The document exactly as the server sent it — what a crawler that does
        # not execute JavaScript has to work with.
        raw_html = ""
        if response is not None:
            try:
                raw_html = response.text()[:400000]
            except PlaywrightError:
                pass
        try:
            page.wait_for_load_state("networkidle", timeout=8000)
        except PlaywrightError:
            warnings.append("Page kept requesting resources; audited after an 8s settle window.")
        load_ms = int((time.time() - started) * 1000)

        # Trigger lazy content so below-the-fold styles are part of the sample.
        try:
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(700)
            page.evaluate("window.scrollTo(0, 0)")
            page.wait_for_timeout(300)
        except PlaywrightError:
            pass

        probe = page.evaluate(PROBE)

        desktop_shot = out_dir / "desktop.png"
        page.screenshot(path=str(desktop_shot))

        # Mobile pass: overflow and small-screen typography are different findings.
        page.set_viewport_size(MOBILE)
        page.wait_for_timeout(600)
        mobile = page.evaluate(
            """() => ({
                doc_width: document.documentElement.clientWidth,
                scroll_width: document.documentElement.scrollWidth,
                body_font_size: parseFloat(getComputedStyle(document.body).fontSize),
            })"""
        )
        mobile_shot = out_dir / "mobile.png"
        page.screenshot(path=str(mobile_shot))

        final_url = page.url
        context.close()

    return PageData(
        url=url,
        final_url=final_url,
        raw_html=raw_html,
        # robots.txt and a sitemap belong to a host; a file has none.
        site_files=None if html is not None else site_files(final_url),
        status=response.status if response else None,
        load_ms=load_ms,
        probe=probe,
        mobile=mobile,
        resources=resources,
        console_errors=console_errors[:20],
        screenshots={"desktop": str(desktop_shot), "mobile": str(mobile_shot)},
        warnings=warnings,
        source="upload" if html is not None else "url",
        missing_frames=missing_frames,
        label=label or ("design.html" if html is not None else ""),
    )


def _serve_upload(route, request, html: str) -> None:
    if request.url.split("#")[0] == UPLOAD_URL:
        route.fulfill(status=200, content_type="text/html; charset=utf-8", body=html)
    else:
        route.fulfill(status=404, content_type="text/plain", body="Not part of the uploaded file.")
