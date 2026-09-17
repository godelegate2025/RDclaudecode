"""Whether the site is navigable: can a visitor find things, and do the routes agree.

Single pages cannot answer this. A nav that changes between templates, a button
whose text promises one thing on two pages and goes somewhere different, a page
in the sitemap that nothing links to — each is only visible when pages are
compared against each other.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from urllib.parse import urlparse

from .analysis import Finding

# Pages a visitor expects to be able to find. Deliberately conservative: a
# missing pricing page is a business-model question, not a defect.
#
# These match on URL, and sites name these pages whatever they like — an about
# page may live at /why-us or /our-story. The patterns are broad for that reason
# and the finding says plainly that it may be wrong, because a report that
# asserts a page is missing when it is merely named differently costs more trust
# than the finding is worth.
EXPECTED_PAGES = {
    "contact": (
        re.compile(r"/(contact|get-in-touch|enquir|inquir|reach-us|talk-to)", re.I),
        "high",
        "Visitors who want to talk to you have nowhere obvious to go.",
    ),
    "privacy policy": (
        re.compile(r"/(privacy|data-protection|gdpr)", re.I),
        "medium",
        "A privacy policy is a legal requirement in most jurisdictions the site serves.",
    ),
    "terms": (
        re.compile(r"/(terms|tos\b|legal|conditions|disclaimer)", re.I),
        "low",
        "Terms of service are standard for any site collecting enquiries or payments.",
    ),
    "about": (
        re.compile(
            r"/(about|who-we-are|our-story|our-team|meet-|team\b|company|mission|values|history|why-)",
            re.I,
        ),
        "low",
        "An about page is where a first-time visitor decides whether to trust you.",
    ),
}

MAX_COMFORTABLE_NAV = 9


def _path(url: str) -> str:
    return urlparse(url).path or "/"


def _normalise(url: str) -> str:
    parsed = urlparse(url)
    path = parsed.path.rstrip("/") or "/"
    return f"{parsed.netloc.lower()}{path}"


def _nav_signature(nav_links: list[dict]) -> frozenset[str]:
    return frozenset(_normalise(link["href"]) for link in nav_links)


# ------------------------------------------------------------------ per page


def page_findings(probe: dict, url: str) -> list[Finding]:
    findings: list[Finding] = []
    nav = probe.get("nav_links") or []

    if not nav:
        findings.append(
            Finding(
                id="structure-no-nav",
                category="Structure",
                severity="high",
                title=f"No navigation found on {_path(url)}",
                detail=(
                    "No links inside a <nav>, <header> or element marked as navigation. "
                    "Either the page is a dead end for visitors, or the navigation is built "
                    "from markup assistive technology cannot recognise as navigation."
                ),
                evidence=["No links found in nav, header or [role=navigation]"],
                fix="Wrap the primary navigation in a <nav> element.",
                automation="Assert a non-empty <nav> on every route in the accessibility suite.",
            )
        )
    elif len(nav) > MAX_COMFORTABLE_NAV * 2:
        findings.append(
            Finding(
                id="structure-nav-size",
                category="Structure",
                severity="low",
                title=f"{len(nav)} links in the navigation on {_path(url)}",
                detail=(
                    "Past roughly nine top-level choices, visitors stop reading the nav and "
                    "start scanning for search. Some of these may be dropdown children, which "
                    "is fine — the count is a prompt to check, not a verdict."
                ),
                evidence=[", ".join(link["text"] for link in nav[:8] if link["text"])[:150]],
                fix="Group related destinations under a smaller set of top-level items.",
                automation="None — this one is a judgement call for a designer.",
            )
        )

    return findings


# --------------------------------------------------------------- across pages


def site_findings(probes: dict[str, dict], discovered: list[str]) -> list[Finding]:  # noqa: C901
    """probes: url -> probe dict for each successfully audited page."""
    findings: list[Finding] = []
    if len(probes) < 2:
        return findings

    total = len(probes)

    # --- does the navigation agree with itself?
    signatures = {url: _nav_signature(probe.get("nav_links") or []) for url, probe in probes.items()}
    populated = {url: sig for url, sig in signatures.items() if sig}
    if len(populated) >= 3:
        common = Counter(populated.values()).most_common(1)[0][0]
        odd = {
            url: sig for url, sig in populated.items()
            if len(sig ^ common) > 2  # more than a couple of links different
        }
        if odd and len(odd) < len(populated):
            findings.append(
                Finding(
                    id="structure-nav-drift",
                    category="Structure",
                    severity="medium",
                    title=f"The navigation differs on {len(odd)} of {len(populated)} pages",
                    detail=(
                        "Most pages share one navigation; these do not. Usually it means a page "
                        "was built from a different template and has drifted, which leaves "
                        "visitors on a dead end or missing a route the rest of the site offers."
                    ),
                    evidence=[
                        f"{_path(url)} differs by {len(sig ^ common)} link(s)"
                        for url, sig in list(odd.items())[:5]
                    ],
                    fix="Render the navigation from one shared component or include.",
                    automation="Assert an identical nav link set across routes in the crawl test.",
                )
            )

    # --- the same button promising different destinations
    by_text: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    for url, probe in probes.items():
        for cta in probe.get("ctas") or []:
            if not cta.get("href"):
                continue
            label = re.sub(r"\s+", " ", cta["text"]).strip().lower()
            if len(label) < 3:
                continue
            by_text[label][_normalise(cta["href"])].add(url)

    conflicts = {label: targets for label, targets in by_text.items() if len(targets) > 1}
    if conflicts:
        evidence = []
        for label, targets in list(conflicts.items())[:4]:
            destinations = [
                f"{dest} (from {_path(sorted(pages)[0])})" for dest, pages in list(targets.items())[:3]
            ]
            evidence.append(f"“{label}” → " + " vs ".join(destinations))
        findings.append(
            Finding(
                id="structure-cta-conflict",
                category="Structure",
                severity="high",
                title=f"{len(conflicts)} call(s) to action point somewhere different on different pages",
                detail=(
                    "The same button text goes to two destinations depending on the page. "
                    "Visitors learn what a button does once; when it changes, some of them end "
                    "up somewhere they did not intend and the conversion path leaks."
                ),
                evidence=evidence,
                fix="Decide the destination for each action and render the button from one component.",
                automation="Assert a single href per CTA label across routes in the crawl test.",
            )
        )

    # --- pages nothing links to
    linked: set[str] = set()
    for probe in probes.values():
        for href in probe.get("links") or []:
            linked.add(_normalise(href))
    discovered_norm = {_normalise(u): u for u in discovered}
    orphans = [
        original for key, original in discovered_norm.items()
        if key not in linked and key in {_normalise(u) for u in probes}
    ]
    if orphans:
        findings.append(
            Finding(
                id="structure-orphan-pages",
                category="Structure",
                severity="medium",
                title=f"{len(orphans)} page(s) are published but linked from nowhere",
                detail=(
                    "These appear in the sitemap but no audited page links to them. Visitors "
                    "can only arrive by search or a direct link, and search engines treat an "
                    "unlinked page as unimportant."
                ),
                evidence=[_path(url) for url in orphans[:6]],
                fix="Link them from the navigation, a hub page, or the footer — or unpublish them.",
                automation="A crawl test comparing sitemap entries against discovered links.",
            )
        )

    # --- the pages a visitor expects to exist
    haystack = " ".join(discovered_norm) + " " + " ".join(sorted(linked))
    for name, (pattern, severity, why) in EXPECTED_PAGES.items():
        if not pattern.search(haystack):
            findings.append(
                Finding(
                    id=f"structure-missing-{name.split()[0]}",
                    category="Structure",
                    severity=severity,
                    title=f"No {name} page found",
                    detail=(
                        f"{why} No URL among the audited pages or their links looks like a "
                        f"{name} page. This check matches on the URL, so if yours is named "
                        "something the pattern does not recognise, ignore this finding."
                    ),
                    evidence=[
                        f"No URL matching a {name} page across {total} audited pages and their links"
                    ],
                    fix=f"Add a {name} page and link it from the navigation or footer.",
                    automation="Assert the route exists in the smoke test once it is added.",
                )
            )

    return findings
