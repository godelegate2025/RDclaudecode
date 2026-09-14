"""Command line entry point: audit a URL, write a PDF (and optionally JSON)."""

from __future__ import annotations

import argparse
import re
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlparse

from .analysis import analyse
from .collector import collect
from .report import html_to_pdf, render_html, write_json


def slug(url: str) -> str:
    host = urlparse(url if "//" in url else "https://" + url).netloc or url
    return re.sub(r"[^a-z0-9]+", "-", host.lower()).strip("-") or "site"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="website-audit",
        description="Audit a website's typography, colour palette and automatable improvements.",
    )
    parser.add_argument("url", help="Website to audit, e.g. example.com or https://example.com/pricing")
    parser.add_argument("-o", "--output", help="PDF path (default: ./reports/<host>-audit-<date>.pdf)")
    parser.add_argument("--json", dest="json_path", nargs="?", const="auto",
                        help="Also write the findings as JSON (path optional)")
    parser.add_argument("--keep-html", action="store_true", help="Keep the intermediate HTML report")
    parser.add_argument("--timeout", type=int, default=45000, help="Navigation timeout in ms (default 45000)")
    parser.add_argument("--quiet", action="store_true", help="Only print the output path")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    from datetime import date

    name = f"{slug(args.url)}-audit-{date.today():%Y-%m-%d}"
    pdf_path = Path(args.output) if args.output else Path("reports") / f"{name}.pdf"
    if pdf_path.is_dir():
        pdf_path = pdf_path / f"{name}.pdf"

    work_dir = pdf_path.parent / ".audit-work" if args.keep_html else Path(tempfile.mkdtemp(prefix="audit-"))

    def log(message: str) -> None:
        if not args.quiet:
            print(message, file=sys.stderr)

    log(f"→ Loading {args.url} in Chromium…")
    try:
        data = collect(args.url, work_dir, timeout_ms=args.timeout)
    except Exception as exc:  # noqa: BLE001 - surfaced to the caller as a clean message
        print(f"Audit failed: {exc}", file=sys.stderr)
        return 1

    log(f"→ Analysing {len(data.probe['text_runs'])} text runs and {len(data.probe['color_usage'])} colours…")
    results = analyse(data)

    log("→ Rendering PDF…")
    html_to_pdf(render_html(data, results), pdf_path, work_dir)

    if args.json_path:
        json_path = pdf_path.with_suffix(".json") if args.json_path == "auto" else Path(args.json_path)
        write_json(data, results, json_path)
        log(f"  JSON: {json_path}")

    scores = results["scores"]
    log(
        f"→ Score {scores['overall']}/100 (grade {scores['grade']}) · "
        f"{len(results['findings'])} findings · "
        + ", ".join(f"{count} {sev}" for sev, count in scores["counts"].most_common())
    )
    print(pdf_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
