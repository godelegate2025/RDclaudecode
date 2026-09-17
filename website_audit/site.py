"""Audit a whole site: discover pages, measure each, then look across them.

The cross-page view is the reason this exists. One page in isolation looks
consistent with itself; it is only against its siblings that you see the fifth
blue, the sixth typeface, and the statistic that changed between two pages.
"""

from __future__ import annotations

import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .analysis import CATEGORIES, Finding, analyse, build_palette, score
from .blocking import detect as detect_block
from .blocking import explain as explain_block
from .browser import browser_session
from .collector import collect
from .content import Page, page_findings, site_findings, to_page
from .discovery import discover
from .structure import site_findings as structure_site_findings


@dataclass
class PageResult:
    url: str
    ok: bool
    score: int | None = None
    grade_letter: str | None = None
    findings: int = 0
    title: str | None = None
    error: str | None = None
    results: dict[str, Any] | None = None
    page: Page | None = None
    probe: dict[str, Any] | None = None


@dataclass
class SiteAudit:
    start_url: str
    host: str
    pages: list[PageResult]
    palette: list[Any]
    families: list[tuple[str, int]]
    findings: list[Finding]
    scores: dict[str, Any]
    discovery_source: str
    discovery_notes: list[str]
    elapsed_s: float
    screenshots: dict[str, str] = field(default_factory=dict)

    @property
    def audited(self) -> list[PageResult]:
        return [p for p in self.pages if p.ok]


def _merge_palette(page_results: list[PageResult]) -> list[Any]:
    """Re-cluster every colour from every page, so the palette is the site's."""
    combined: dict[str, dict[str, int]] = {}
    for result in page_results:
        if not result.results:
            continue
        for swatch in result.results["palette"]:
            for member in swatch.members:
                bucket = combined.setdefault(member, {"text": 0, "background": 0, "border": 0})
                bucket["text"] += swatch.text_chars
                bucket["background"] += swatch.background_area
                bucket["border"] += swatch.border_uses
    return build_palette(combined)


def _merge_families(page_results: list[PageResult]) -> list[tuple[str, int]]:
    counts: Counter = Counter()
    for result in page_results:
        if not result.results:
            continue
        # content_families already excludes icon fonts and system stacks.
        for family in result.results["typography"]["content_families"]:
            counts[family.name] += family.chars
    return counts.most_common()


def _reach(page_results: list[PageResult]) -> list[Finding]:
    """Roll per-page findings up, counting how many pages each one affects.

    A footer contrast failure is one finding on one page and the same finding on
    forty. Ranking by reach is what turns a list into a work order.
    """
    by_id: dict[str, Finding] = {}
    pages_hit: Counter = Counter()
    for result in page_results:
        if not result.results:
            continue
        for finding in result.results["findings"]:
            pages_hit[finding.id] += 1
            by_id.setdefault(finding.id, finding)

    rolled: list[Finding] = []
    total = len(page_results) or 1
    for finding_id, count in pages_hit.most_common():
        original = by_id[finding_id]
        scope = f"Affects {count} of {total} audited pages"
        rolled.append(
            Finding(
                id=original.id,
                category=original.category,
                severity=original.severity,
                title=original.title,
                detail=original.detail,
                evidence=[scope, *original.evidence[:4]],
                fix=original.fix,
                automation=original.automation,
            )
        )
    return rolled


def audit_site(
    start_url: str,
    work_dir: Path,
    limit: int = 25,
    timeout_ms: int = 45000,
    obey_robots: bool = True,
    progress: Callable[[str], None] | None = None,
) -> SiteAudit:
    started = time.time()
    say = progress or (lambda _message: None)

    say(f"Discovering pages on {start_url}…")
    found = discover(start_url, limit=limit, obey_robots=obey_robots)
    if not found.urls:
        raise RuntimeError(
            f"No auditable pages found for {start_url}. The site may block crawlers, "
            "or publish no sitemap and no internal links."
        )
    say(f"Found {len(found.urls)} page(s) via {found.source}.")

    results: list[PageResult] = []
    screenshots: dict[str, str] = {}

    # One browser for every page — launching per page would dominate the runtime.
    with browser_session() as browser:
        for index, url in enumerate(found.urls, start=1):
            say(f"[{index}/{len(found.urls)}] {url}")
            page_dir = work_dir / f"page-{index:03d}"
            try:
                data = collect(url, page_dir, timeout_ms=timeout_ms, browser=browser)
                verdict = detect_block(data)
                if verdict:
                    results.append(
                        PageResult(url=url, ok=False, error=explain_block(verdict, data.final_url))
                    )
                    continue

                analysis = analyse(data)
                page = to_page(data)
                results.append(
                    PageResult(
                        url=data.final_url,
                        ok=True,
                        score=analysis["scores"]["overall"],
                        grade_letter=analysis["scores"]["grade"],
                        findings=len(analysis["findings"]),
                        title=data.probe.get("title"),
                        results=analysis,
                        page=page,
                        probe=data.probe,
                    )
                )
                if not screenshots:  # the first page stands in for the site
                    screenshots = dict(data.screenshots)
            except Exception as exc:  # noqa: BLE001 - one bad page must not end the crawl
                results.append(PageResult(url=url, ok=False, error=str(exc)[:200]))

    audited = [r for r in results if r.ok]
    if not audited:
        raise RuntimeError(
            "Every page failed to audit. The site is most likely blocking automated browsers."
        )

    say("Comparing pages…")
    pages = [r.page for r in audited if r.page]
    findings = _reach(audited)
    for page in pages:
        findings.extend(page_findings(page))
    findings.extend(site_findings(pages))
    findings.extend(
        structure_site_findings(
            {r.url: r.probe for r in audited if r.probe},
            [r.url for r in results],
        )
    )

    order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    findings.sort(key=lambda f: (order[f.severity], f.category))

    from urllib.parse import urlparse

    return SiteAudit(
        start_url=start_url,
        host=urlparse(audited[0].url).netloc,
        pages=results,
        palette=_merge_palette(audited),
        families=_merge_families(audited),
        findings=findings,
        scores=score(findings, [*CATEGORIES, "Content"]),
        discovery_source=found.source,
        discovery_notes=found.notes,
        elapsed_s=time.time() - started,
        screenshots=screenshots,
    )
