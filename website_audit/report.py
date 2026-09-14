"""Renders the audit as a printable HTML page and prints it to PDF with Chromium."""

from __future__ import annotations

import base64
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from jinja2 import Environment, FileSystemLoader
from markupsafe import Markup, escape
from playwright.sync_api import sync_playwright

from .analysis import automation_roadmap, primary_family
from .collector import CHROMIUM_PATHS, LAUNCH_ARGS

SEVERITY_COLOUR = {
    "critical": "#d03b3b",
    "high": "#ec835a",
    "medium": "#fab219",
    "low": "#898781",
}


def inline_code(text: str) -> Markup:
    """Render `backticked` spans as <code>. Escapes first, so page content stays inert."""
    return Markup(re.sub(r"`([^`]+)`", r"<code>\1</code>", str(escape(text))))


def score_colour(value: int) -> str:
    if value >= 80:
        return "#0ca30c"
    if value >= 60:
        return "#fab219"
    if value >= 40:
        return "#ec835a"
    return "#d03b3b"


def size_role(size: int) -> str:
    if size >= 40:
        return "Display / hero"
    if size >= 28:
        return "Page heading"
    if size >= 20:
        return "Section heading"
    if size >= 16:
        return "Body"
    if size >= 13:
        return "Small / secondary"
    return "Fine print — check legibility"


def fmt_bytes(value: float) -> str:
    value = float(value or 0)
    for unit in ("B", "KB", "MB"):
        if value < 1024 or unit == "MB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} MB"


def _data_uri(path: str) -> str:
    return "data:image/png;base64," + base64.b64encode(Path(path).read_bytes()).decode()


def render_html(data, results: dict[str, Any]) -> str:
    # Everything in this report is untrusted text scraped from the audited page,
    # so escaping is unconditional.
    env = Environment(
        loader=FileSystemLoader(Path(__file__).parent / "templates"),
        autoescape=True,
    )
    env.filters["code"] = inline_code
    template = env.get_template("report.html.j2")
    resource_rows = sorted(
        results["resource_summary"].items(), key=lambda kv: kv[1]["bytes"], reverse=True
    )[:6]
    return template.render(
        data=data,
        probe=data.probe,
        host=urlparse(data.final_url).netloc or data.final_url,
        generated_at=datetime.now(timezone.utc).strftime("%d %b %Y %H:%M UTC"),
        viewport={"width": 1440, "height": 900},
        palette=results["palette"],
        typography=results["typography"],
        contrast_issues=results["contrast_issues"],
        contrast_unverified=results["contrast_unverified"],
        findings=results["findings"],
        scores=results["scores"],
        roadmap=automation_roadmap(results["findings"]),
        resource_rows=resource_rows,
        total_bytes=results["total_bytes"],
        shots={name: _data_uri(path) for name, path in data.screenshots.items()},
        severity_colour=SEVERITY_COLOUR,
        score_colour=score_colour,
        size_role=size_role,
        fmt_bytes=fmt_bytes,
        primary_family=primary_family,
    )


def html_to_pdf(html: str, pdf_path: Path, work_dir: Path) -> Path:
    """Chromium prints the PDF, so the report renders exactly as the audit saw the page."""
    work_dir.mkdir(parents=True, exist_ok=True)
    html_path = work_dir / "report.html"
    html_path.write_text(html, encoding="utf-8")
    pdf_path.parent.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as pw:
        browser = None
        for path in CHROMIUM_PATHS:
            if path and Path(path).exists():
                browser = pw.chromium.launch(executable_path=path, args=LAUNCH_ARGS)
                break
        if browser is None:
            browser = pw.chromium.launch(args=LAUNCH_ARGS)
        page = browser.new_page()
        page.goto(html_path.as_uri(), wait_until="load")
        page.emulate_media(media="print")
        page.pdf(
            path=str(pdf_path),
            format="A4",
            print_background=True,
            display_header_footer=True,
            header_template="<div></div>",
            footer_template=(
                '<div style="width:100%;font:8px system-ui;color:#898781;padding:0 13mm;'
                'display:flex;justify-content:space-between;">'
                "<span>Website design audit</span>"
                '<span><span class="pageNumber"></span> / <span class="totalPages"></span></span></div>'
            ),
            margin={"top": "14mm", "bottom": "16mm", "left": "13mm", "right": "13mm"},
        )
        browser.close()
    return pdf_path


def write_json(data, results: dict[str, Any], path: Path) -> Path:
    """Machine-readable twin of the PDF, for wiring the audit into other automation."""
    payload = {
        "url": data.final_url,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": data.status,
        "load_ms": data.load_ms,
        "scores": {
            "overall": results["scores"]["overall"],
            "grade": results["scores"]["grade"],
            "categories": results["scores"]["categories"],
            "counts": dict(results["scores"]["counts"]),
        },
        "palette": [
            {
                "hex": s.hex,
                "role": s.role,
                "text_chars": s.text_chars,
                "border_uses": s.border_uses,
                "near_duplicates": s.members[1:],
            }
            for s in results["palette"]
        ],
        "typography": {
            "families": [
                {
                    "name": f.name,
                    "characters": f.chars,
                    "sizes": dict(f.sizes),
                    "weights": dict(f.weights),
                    "has_fallback": f.has_fallback,
                    "is_icon_font": f.is_icon_font,
                }
                for f in results["typography"]["families"]
            ],
            "distinct_sizes": results["typography"]["distinct_sizes"],
            "distinct_weights": results["typography"]["distinct_weights"],
        },
        "contrast_unverified_runs": results["contrast_unverified"],
        "contrast_failures": [
            {k: v for k, v in issue.items() if k != "tags"} | {"elements": issue["tags"]}
            for issue in results["contrast_issues"]
        ],
        "findings": [
            {
                "id": f.id,
                "category": f.category,
                "severity": f.severity,
                "title": f.title,
                "detail": f.detail,
                "evidence": f.evidence,
                "fix": f.fix,
                "automation": f.automation,
            }
            for f in results["findings"]
        ],
        "automation_roadmap": automation_roadmap(results["findings"]),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path
