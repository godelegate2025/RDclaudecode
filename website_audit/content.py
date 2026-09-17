"""Content checks that need no judgement — only comparison.

Everything here is arithmetic over text the browser already rendered: no model,
no tokens, and the same answer every run. The checks that genuinely need an
opinion (is this copy persuasive?) are deliberately absent; a rule that has to
guess produces findings nobody trusts.

Cross-page checks are the point. A contradiction between two pages is invisible
when you audit one page at a time, and it is exactly what embarrasses a client.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Any

from .analysis import Finding

PLACEHOLDER_PATTERNS = [
    (r"\blorem\s+ipsum\b", "Lorem ipsum"),
    (r"\bdolor\s+sit\s+amet\b", "Lorem ipsum"),
    (r"\byour\s+(?:text|headline|title|content)\s+here\b", "Template placeholder"),
    (r"\b(?:name|title|description|heading)\s+here\b", "Template placeholder"),
    (r"\bplaceholder\s+(?:text|image|content)\b", "Placeholder marker"),
    (r"\bcoming\s+soon\b", "Unfinished section"),
    (r"\bto\s+be\s+(?:added|confirmed|determined)\b", "Unfinished section"),
    (r"\bTBD\b", "Unfinished section"),
    (r"\bXXX+\b", "Editing marker"),
    (r"\bTODO\b", "Editing marker"),
    (r"\binsert\s+(?:text|image|name|link)\b", "Template placeholder"),
]

# Same word, two spellings. Both present on one site means nobody set a standard.
DIALECT_PAIRS = [
    ("color", "colour"), ("colors", "colours"),
    ("organization", "organisation"), ("organize", "organise"),
    ("realize", "realise"), ("recognize", "recognise"),
    ("center", "centre"), ("centers", "centres"),
    ("analyze", "analyse"), ("customize", "customise"),
    ("catalog", "catalogue"), ("license", "licence"),
    ("program", "programme"), ("traveled", "travelled"),
    ("behavior", "behaviour"), ("favorite", "favourite"),
    ("optimize", "optimise"), ("specialize", "specialise"),
]

# Claims worth checking for consistency: a number attached to a unit.
CLAIM = re.compile(
    r"\b([\d][\d,.]{0,12})\s*\+?\s*"
    r"(years|months|clients|customers|projects|students|hours|countries|"
    r"locations|properties|units|homes|investors|partners|reviews)\b",
    re.I,
)

SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")


@dataclass
class Page:
    """One audited page, reduced to what the content checks need."""

    url: str
    title: str | None
    meta_description: str | None
    text: str
    headings: list[dict[str, Any]]
    word_count: int


def to_page(data) -> Page:
    probe = data.probe
    text = probe.get("body_text") or probe.get("body_text_sample") or ""
    return Page(
        url=data.final_url,
        title=probe.get("title"),
        meta_description=probe.get("meta_description"),
        text=text,
        headings=probe.get("headings") or [],
        word_count=len(text.split()),
    )


def _short(url: str) -> str:
    from urllib.parse import urlparse

    return urlparse(url).path or "/"


# ----------------------------------------------------------------- per page


def page_findings(page: Page) -> list[Finding]:
    findings: list[Finding] = []
    lowered = page.text.lower()

    hits: list[str] = []
    for pattern, label in PLACEHOLDER_PATTERNS:
        for match in re.finditer(pattern, page.text, re.I):
            start = max(0, match.start() - 40)
            snippet = page.text[start : match.end() + 40].strip()
            hits.append(f"{label}: “…{snippet}…”")
            break  # one example per pattern is enough to prove the point
    if hits:
        findings.append(
            Finding(
                id="content-placeholder",
                category="Content",
                severity="critical",
                title=f"Placeholder text is live on {_short(page.url)}",
                detail=(
                    "Unfinished copy on a published page costs more credibility than any "
                    "design flaw. This is the first thing to fix."
                ),
                evidence=hits[:5],
                fix="Replace with real copy, or remove the section until it is written.",
                automation=(
                    "This exact check in CI against the staging URL blocks a release "
                    "carrying placeholder text."
                ),
            )
        )

    if page.word_count < 60 and page.headings:
        findings.append(
            Finding(
                id="content-thin",
                category="Content",
                severity="medium",
                title=f"{_short(page.url)} has almost no copy ({page.word_count} words)",
                detail=(
                    "A page with headings but barely any body text reads as half-finished "
                    "to a visitor and as low value to a search engine."
                ),
                evidence=[f"{page.word_count} words across {len(page.headings)} headings"],
                fix="Write the body copy, merge the page into a fuller one, or unpublish it.",
                automation="Assert a minimum word count per published route in the content tests.",
            )
        )

    if lowered.count("click here") > 1:
        findings.append(
            Finding(
                id="content-click-here",
                category="Content",
                severity="low",
                title=f"“Click here” used as link text on {_short(page.url)}",
                detail=(
                    "Screen-reader users navigate by pulling up a list of links; "
                    "“click here” tells them nothing about the destination."
                ),
                evidence=[f"{lowered.count('click here')} occurrences"],
                fix="Make the link text name its destination — “See pricing”, “Read the case study”.",
                automation="A link-text lint rule catches this on every build.",
            )
        )

    return findings


# ---------------------------------------------------------------- across pages


def site_findings(pages: list[Page]) -> list[Finding]:  # noqa: C901 - a flat rules table
    findings: list[Finding] = []
    if len(pages) < 2:
        return findings

    total = len(pages)

    # --- duplicate titles and descriptions
    titles: dict[str, list[str]] = defaultdict(list)
    descriptions: dict[str, list[str]] = defaultdict(list)
    for page in pages:
        if page.title:
            titles[page.title.strip()].append(page.url)
        if page.meta_description:
            descriptions[page.meta_description.strip()].append(page.url)

    duplicate_titles = {t: u for t, u in titles.items() if len(u) > 1}
    if duplicate_titles:
        findings.append(
            Finding(
                id="site-duplicate-titles",
                category="Content",
                severity="high" if len(duplicate_titles) > 2 else "medium",
                title=f"{len(duplicate_titles)} page title(s) are reused across pages",
                detail=(
                    "The title is what shows in search results, browser tabs and bookmarks. "
                    "Repeated titles make pages indistinguishable to both people and crawlers."
                ),
                evidence=[
                    f"“{title}” on {', '.join(_short(u) for u in urls[:4])}"
                    for title, urls in list(duplicate_titles.items())[:4]
                ],
                fix="Give every page a title naming that page's subject.",
                automation="Assert title uniqueness across routes in the build.",
            )
        )

    duplicate_descriptions = {d: u for d, u in descriptions.items() if len(u) > 1}
    if duplicate_descriptions:
        findings.append(
            Finding(
                id="site-duplicate-descriptions",
                category="Content",
                severity="medium",
                title=f"{len(duplicate_descriptions)} meta description(s) are reused",
                detail="Search engines show the description under the title; repeating it wastes the pitch.",
                evidence=[
                    f"“{desc[:70]}…” on {len(urls)} pages"
                    for desc, urls in list(duplicate_descriptions.items())[:4]
                ],
                fix="Write a distinct 150–160 character description per page.",
                automation="Same uniqueness assertion as titles.",
            )
        )

    # --- copy duplicated between a few pages (not site-wide furniture)
    blocks: dict[str, set[str]] = defaultdict(set)
    for page in pages:
        for sentence in SENTENCE_SPLIT.split(page.text):
            cleaned = sentence.strip()
            if len(cleaned.split()) >= 22:
                blocks[cleaned].add(page.url)
    # Present on several pages but not most of them: copy-paste, not a footer.
    copy_paste = {
        text: urls
        for text, urls in blocks.items()
        if 1 < len(urls) <= max(2, int(total * 0.6))
    }
    if copy_paste:
        findings.append(
            Finding(
                id="site-duplicate-copy",
                category="Content",
                severity="medium",
                title=f"{len(copy_paste)} passage(s) are repeated word-for-word across pages",
                detail=(
                    "Shared headers and footers are expected and were excluded. What is left "
                    "is body copy pasted between pages — it reads as filler and competes with "
                    "itself in search."
                ),
                evidence=[
                    f"“{text[:80]}…” on {', '.join(_short(u) for u in sorted(urls)[:3])}"
                    for text, urls in list(copy_paste.items())[:4]
                ],
                fix="Rewrite each instance for its page, or keep one and link to it.",
                automation="A cross-page duplication report on every content deploy.",
            )
        )

    # --- one word, two spellings
    corpus = " ".join(page.text.lower() for page in pages)
    drift: list[str] = []
    for american, british in DIALECT_PAIRS:
        us = len(re.findall(rf"\b{american}\b", corpus))
        uk = len(re.findall(rf"\b{british}\b", corpus))
        if us and uk:
            drift.append(f"“{american}” ×{us} and “{british}” ×{uk}")
    if drift:
        findings.append(
            Finding(
                id="site-dialect-drift",
                category="Content",
                severity="medium",
                title="The site mixes US and UK spellings",
                detail=(
                    "Both spellings of the same word appear across the site. Readers rarely "
                    "name it, but it reads as careless — and it is trivially fixable."
                ),
                evidence=drift[:6],
                fix="Pick one standard, note it in the style guide, and run a find-and-replace.",
                automation="A spell-check step pinned to one locale fails the build on the other.",
            )
        )

    # --- the same claim with different numbers
    claims: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    for page in pages:
        for value, unit in CLAIM.findall(page.text):
            claims[unit.lower()][value.replace(",", "")].add(page.url)
    contradictions = {
        unit: values for unit, values in claims.items() if len(values) > 1
    }
    if contradictions:
        evidence = []
        for unit, values in list(contradictions.items())[:4]:
            parts = [
                f"{value} {unit} on {_short(sorted(urls)[0])}"
                for value, urls in sorted(values.items())[:3]
            ]
            evidence.append(" vs ".join(parts))
        findings.append(
            Finding(
                id="site-claim-mismatch",
                category="Content",
                severity="high",
                title=f"{len(contradictions)} claim(s) carry different numbers on different pages",
                detail=(
                    "The same statistic is stated two ways. A prospect who notices stops "
                    "trusting every other number on the site. Some of these will be legitimately "
                    "different things — check each before editing."
                ),
                evidence=evidence,
                fix="Decide the true figure, update every instance, and keep it in one place.",
                automation="Store headline stats as data and render them, so one edit updates every page.",
            )
        )

    # --- pages missing the basics
    missing_titles = [p.url for p in pages if not p.title]
    missing_descriptions = [p.url for p in pages if not p.meta_description]
    if missing_descriptions and len(missing_descriptions) > total * 0.3:
        findings.append(
            Finding(
                id="site-missing-descriptions",
                category="Findability",
                severity="medium",
                title=f"{len(missing_descriptions)} of {total} pages have no meta description",
                detail="Search engines fall back to scraping body text, usually badly.",
                evidence=[_short(u) for u in missing_descriptions[:6]],
                fix="Write one per page; template a sensible default for the rest.",
                automation="Meta-tag lint rule across routes.",
            )
        )
    if missing_titles:
        findings.append(
            Finding(
                id="site-missing-titles",
                category="Findability",
                severity="high",
                title=f"{len(missing_titles)} page(s) have no title",
                detail="An untitled page shows its URL in search results and browser tabs.",
                evidence=[_short(u) for u in missing_titles[:6]],
                fix="Add a unique, descriptive title under 60 characters.",
                automation="Assert a non-empty title per route in the smoke test.",
            )
        )

    return findings


def vocabulary(pages: list[Page]) -> Counter:
    """Word frequencies across the site — used for the report's content summary."""
    words = Counter()
    for page in pages:
        words.update(re.findall(r"[a-z]{4,}", page.text.lower()))
    return words
