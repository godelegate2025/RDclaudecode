"""Loads a page in Chromium and collects the raw design/performance evidence."""

from __future__ import annotations

import time
from contextlib import nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from playwright.sync_api import Browser
from playwright.sync_api import Error as PlaywrightError

from .browser import browser_session

PROBE = (Path(__file__).parent / "probe.js").read_text()

DESKTOP = {"width": 1440, "height": 900}
MOBILE = {"width": 390, "height": 844}


@dataclass
class PageData:
    url: str
    final_url: str
    status: int | None
    load_ms: int
    probe: dict[str, Any]
    mobile: dict[str, Any]
    resources: list[dict[str, Any]] = field(default_factory=list)
    console_errors: list[str] = field(default_factory=list)
    screenshots: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


def _normalise(url: str) -> str:
    if not url.startswith(("http://", "https://")):
        return "https://" + url
    return url


def collect(
    url: str,
    out_dir: Path,
    timeout_ms: int = 45000,
    browser: Browser | None = None,
    route_guard=None,
) -> PageData:
    """Render the page and gather the evidence.

    Pass an existing `browser` to reuse one Chromium across many audits; without
    one a private session is launched and closed around this call. `route_guard`
    is a Playwright route handler given every request — a public deployment uses
    it to re-check redirect targets against its blocklist.
    """
    url = _normalise(url)
    out_dir.mkdir(parents=True, exist_ok=True)
    resources: list[dict[str, Any]] = []
    console_errors: list[str] = []
    warnings: list[str] = []

    with browser_session() if browser is None else nullcontext(browser) as active:
        context = active.new_context(
            viewport=DESKTOP,
            device_scale_factor=1,
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36 WebsiteAuditBot/1.0"
            ),
        )
        page = context.new_page()
        if route_guard is not None:
            page.route("**/*", route_guard)

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
            except Exception:  # noqa: BLE001 - never fail the audit on telemetry
                pass

        page.on("response", on_response)
        def on_console(msg):
            if msg.type == "error" and "favicon" not in msg.text.lower():
                console_errors.append(msg.text[:200])

        page.on("console", on_console)

        started = time.time()
        response = page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
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
        status=response.status if response else None,
        load_ms=load_ms,
        probe=probe,
        mobile=mobile,
        resources=resources,
        console_errors=console_errors[:20],
        screenshots={"desktop": str(desktop_shot), "mobile": str(mobile_shot)},
        warnings=warnings,
    )
