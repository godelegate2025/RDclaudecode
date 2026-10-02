"""The Post Audit PDF: the audit JSON rendered into its own A4 print design.

The screen page and the PDF read differently — a scrolling report versus a
four-page document with a standalone summary — so the PDF has its own Jinja
template (templates/post_report.html.j2) in the same REDEFINE system as the
website audit report. Chromium prints it.
"""

from __future__ import annotations

import base64
import html
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from jinja2 import Environment, FileSystemLoader

from website_audit.browser import browser_session

TEMPLATES = Path(__file__).parent / "templates"
LOGO = Path(__file__).parent / "static" / "logo.svg"
# Only the brand fonts may load. Everything else in the report is inline (the
# frames and logo are data: URLs), so any other request is refused outright.
ALLOWED = re.compile(r"^https://fonts\.(googleapis|gstatic)\.com/")
# The brand fonts usually arrive in well under a second; past this the PDF
# prints in the fallback fonts rather than keep the visitor waiting.
FONT_WAIT_MS = 4000

HOOK_NAMES = {
    "question": "Question", "bold_claim": "Bold claim", "curiosity_gap": "Curiosity gap",
    "pattern_interrupt": "Pattern interrupt", "relatable_pain": "Relatable pain", "story_open": "Story opener",
    "how_to": "How-to", "list": "List", "contrarian": "Contrarian", "visual_surprise": "Visual surprise",
    "social_proof": "Social proof", "other": "Other",
}
MEDIA_NAMES = {"video": "video", "image": "photo post", "carousel": "carousel", "text": "text post"}

# Scraped captions and model text are untrusted: escaping is unconditional.
_env = Environment(loader=FileSystemLoader(TEMPLATES), autoescape=True)


def _route(route, request) -> None:
    if request.url.startswith("data:"):
        route.continue_()
    elif ALLOWED.match(request.url):
        # The font stylesheet blocks rendering, so a font host that never
        # answers would stall the PDF. Fetch it with a deadline instead. The
        # page may already be printed and closed by the time a slow font
        # arrives; answering a closed route raises, and that must not take
        # the PDF down with it.
        try:
            route.fulfill(response=route.fetch(timeout=FONT_WAIT_MS))
        except Exception:  # noqa: BLE001 - print in the fallback fonts
            try:
                route.abort("timedout")
            except Exception:  # noqa: BLE001 - route already settled or page gone
                pass
    else:
        route.abort("blockedbyclient")


def filename_for(report: dict) -> str:
    post = report.get("post") or {}
    who = post.get("author_handle") or post.get("author") or "post"
    slug = re.sub(r"[^a-z0-9]+", "-", f"{report.get('platform', 'post')}-{who}".lower()).strip("-")
    return f"{slug[:60] or 'post'}-audit.pdf"


def compact(n) -> str | None:
    """1400000 → 1.4M, 10200 → 10K, 1000 → 1K: one decimal below ten, none above."""
    if n is None:
        return None
    for size, suffix in ((1_000_000, "M"), (1_000, "K")):
        if n >= size:
            value = n / size
            text = f"{value:.0f}" if value >= 10 else f"{value:.1f}".removesuffix(".0")
            if text == "1000" and suffix == "K":  # 999,999 rounds up into the next unit
                return "1M"
            return text + suffix
    return str(n)


def _pct(value) -> str | None:
    return None if value is None else f"{value:g}%"


def _date(value: str) -> str:
    try:
        when = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return ""
    return f"{when.day} {when:%B %Y}"


def _timing_span(text: str) -> tuple[float, float] | None:
    """'0-3s', '0.6–~10s', '~20-29s' → (start, end); anything else (e.g. 'slide 2') → None."""
    # Needs an actual seconds value ("10s"), not just a number and a letter s
    # somewhere — "slide 2" is a carousel position, not a time.
    if not re.search(r"\d\s*s\b", text or ""):
        return None
    numbers = [float(n) for n in re.findall(r"\d+(?:\.\d+)?", text)]
    start = numbers[0]
    end = numbers[1] if len(numbers) > 1 else start
    return (start, end) if end >= start else None


def timeline(structure: list[dict], duration: float | None) -> dict | None:
    """Beats laid on a bar to scale — only when every beat has readable timings."""
    if not duration or not structure:
        return None
    spans = [_timing_span(beat.get("timing", "")) for beat in structure]
    if any(span is None for span in spans):
        return None
    segments = []
    for i, (start, end) in enumerate(spans):
        start, end = min(start, duration), min(end, duration)
        segments.append({
            "i": i,
            "left": round(start / duration * 100, 2),
            # Even a split-second beat stays wide enough to carry its number.
            "width": round(max((end - start) / duration * 100, 2.4), 2),
        })
    step = 5 if duration <= 40 else 10 if duration <= 90 else 30
    ticks = list(range(0, int(duration), step))
    if duration - ticks[-1] >= step / 2:
        ticks.append(round(duration))
    return {"segments": segments, "ticks": ticks}


def _frames(frames: list[dict], label: str) -> list[dict]:
    chosen = [f for f in frames if (f.get("label") == "hook") == (label == "hook")]
    return [
        {"src": f["src"], "caption": f"{f['seconds']:.1f}s" if f.get("seconds") is not None else f.get("label", "")}
        for f in chosen
        if str(f.get("src", "")).startswith("data:image/")
    ][:6]


def context(report: dict, now: datetime | None = None) -> dict:
    post = report.get("post") or {}
    metrics = report.get("metrics") or {}
    analysis = report["analysis"]
    platform = report.get("platform_label") or str(report.get("platform", "")).title()

    hook_frames = _frames(report.get("frames") or [], "hook")
    body_frames = _frames(report.get("frames") or [], "body")
    # The cover shows the end of the hook window: by then the hook has landed.
    if hook_frames:
        cover = {**hook_frames[-2 if len(hook_frames) > 1 else -1]}
        cover["caption"] = f"Frame at {cover['caption']} — inside the hook window"
    elif body_frames:
        cover = {**body_frames[0], "caption": "From the post"}
    else:
        cover = None

    counts = [
        ("Views", post.get("views")), ("Likes", post.get("likes")), ("Comments", post.get("comments")),
        ("Shares", post.get("shares")), ("Saves", post.get("saves")),
    ]
    kpis = [(label, compact(value)) for label, value in counts if value is not None]
    if metrics.get("engagement_rate_by_views") is not None:
        kpis.append(("Engagement", _pct(metrics["engagement_rate_by_views"])))
    if post.get("duration_seconds"):
        kpis.append(("Length", f"{round(post['duration_seconds'])}s"))
    if metrics.get("cuts_per_10s") is not None:
        kpis.append(("Cuts / 10s", f"{metrics['cuts_per_10s']:g}"))
    missing = [label.lower() for label, value in counts if value is None]
    if post.get("author_followers") is None:
        missing.append("follower count")

    rate_rows = [
        ("Like rate", "like_rate", "Likes per view"),
        ("Comment rate", "comment_rate", "Comments per view"),
        ("Share rate", "share_rate", "Shares per view"),
        ("Save rate", "save_rate", "Saves per view — a sign of reference value"),
        ("Engagement by views", "engagement_rate_by_views", "All interactions ÷ views"),
        ("Engagement by followers", "engagement_rate_by_followers", "All interactions ÷ followers"),
    ]
    rates = [(label, _pct(metrics.get(key)), note) for label, key, note in rate_rows if metrics.get(key) is not None]

    score = int(analysis["hook"]["score"])
    caption = (post.get("caption") or "").strip()
    url = post.get("url") or ""
    parsed = urlparse(url)
    short_url = (parsed.netloc.removeprefix("www.") + parsed.path).rstrip("/") if parsed.netloc else url

    return {
        "a": analysis,
        "post": post,
        "metrics": metrics,
        "platform": platform,
        "media": MEDIA_NAMES.get(post.get("media_type") or "", "post"),
        "who": post.get("author") or post.get("author_handle") or "Unknown account",
        "handle": post.get("author_handle") if post.get("author_handle") not in (None, "", post.get("author")) else "",
        "posted": _date(post.get("posted_at") or ""),
        "short_url": short_url,
        "generated": f"{(now or datetime.now(timezone.utc)).day} {(now or datetime.now(timezone.utc)):%B %Y}",
        "logo": "data:image/svg+xml;base64," + base64.b64encode(LOGO.read_bytes()).decode() if LOGO.exists() else None,
        "cover": cover,
        "hook_frames": hook_frames,
        "body_frames": body_frames,
        "frame_count": len(report.get("frames") or []),
        "score_colour": "#0f8a3d" if score >= 8 else "#b57500" if score >= 5 else "#FC4452",
        "hook_type": HOOK_NAMES.get(analysis["hook"]["type"], analysis["hook"]["type"]),
        "kpis": kpis[:8],
        "missing": missing,
        "rates": rates,
        "timeline": timeline(analysis.get("structure") or [], post.get("duration_seconds")),
        "caption": caption[:600] + ("…" if len(caption) > 600 else ""),
        "limits": [x for x in [analysis.get("limits"), *(report.get("notes") or [])] if x],
    }


def render_html(report: dict) -> str:
    return _env.get_template("post_report.html.j2").render(**context(report))


def render_pdf(report: dict) -> bytes:
    ctx = context(report)
    footer_who = html.escape(f"{ctx['who']} · {ctx['platform']}")
    with browser_session() as browser:
        page = browser.new_page()
        page.route("**/*", _route)
        page.set_default_timeout(20_000)
        page.set_content(_env.get_template("post_report.html.j2").render(**ctx), wait_until="domcontentloaded")
        page.evaluate(f"Promise.race([document.fonts.ready, new Promise(r => setTimeout(r, {FONT_WAIT_MS}))])")
        pdf = page.pdf(
            format="A4",
            print_background=True,
            prefer_css_page_size=True,
            display_header_footer=True,
            header_template="<div></div>",
            footer_template=(
                '<div style="width:100%;font:7px system-ui;color:#898781;padding:0 13mm;'
                'display:flex;justify-content:space-between;">'
                f"<span>REDEFINE &middot; Post Audit &middot; {footer_who}</span>"
                '<span><span class="pageNumber"></span> / <span class="totalPages"></span></span></div>'
            ),
        )
        # Drop handlers still waiting on a font before closing, so they settle
        # quietly instead of racing the close.
        page.unroute_all(behavior="ignoreErrors")
        page.close()
    return pdf


def attach_pdf(report: dict) -> dict:
    """Add {"pdf": {filename, base64}} to the report."""
    report["pdf"] = {
        "filename": filename_for(report),
        "base64": base64.b64encode(render_pdf(report)).decode(),
    }
    return report
