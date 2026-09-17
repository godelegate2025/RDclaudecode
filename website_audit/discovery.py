"""Find the pages of a site worth auditing.

Order of preference: the sitemap the site publishes, then the links on its
homepage. Both are filtered to the site's own host, deduplicated, and checked
against robots.txt — auditing someone's site is not a reason to ignore what
they asked crawlers not to touch.
"""

from __future__ import annotations

import re
import urllib.robotparser
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse, urlunparse

import requests

USER_AGENT = "Mozilla/5.0 (compatible; RedefineAudit/1.0)"
TIMEOUT = 15

# Files that are not pages. Auditing a PDF or an image renders nothing useful.
NON_PAGE = re.compile(
    r"\.(pdf|jpe?g|png|gif|webp|avif|svg|ico|css|js|json|xml|zip|gz|mp4|mp3|wav|woff2?|ttf|eot)(\?|$)",
    re.I,
)

# URL shapes that multiply without adding design information.
NOISE = re.compile(
    r"(/wp-json/|/wp-admin/|/feed/?$|/page/\d+|/tag/|/author/|[?&](utm_|replytocom|s=|q=|filter|sort|page=))",
    re.I,
)


@dataclass
class Discovery:
    urls: list[str]
    source: str
    notes: list[str] = field(default_factory=list)


def _normalise(url: str) -> str:
    """Drop fragments and query strings so one page is not audited five times."""
    parts = urlparse(url)
    path = re.sub(r"/{2,}", "/", parts.path) or "/"
    if len(path) > 1:
        path = path.rstrip("/")
    return urlunparse((parts.scheme.lower(), parts.netloc.lower(), path, "", "", ""))


def _same_site(url: str, host: str) -> bool:
    netloc = urlparse(url).netloc.lower()
    return netloc == host or netloc == f"www.{host}" or host == f"www.{netloc}"


def _get(url: str) -> requests.Response | None:
    try:
        response = requests.get(
            url, timeout=TIMEOUT, headers={"User-Agent": USER_AGENT}, allow_redirects=True
        )
        return response if response.ok else None
    except requests.RequestException:
        return None


def _robots(base: str) -> tuple[urllib.robotparser.RobotFileParser | None, list[str]]:
    """Return the parsed rules and any sitemap locations robots.txt advertises."""
    response = _get(urljoin(base, "/robots.txt"))
    if response is None:
        return None, []
    parser = urllib.robotparser.RobotFileParser()
    try:
        parser.parse(response.text.splitlines())
    except Exception:  # noqa: BLE001 - a malformed robots.txt must not stop the audit
        parser = None
    sitemaps = re.findall(r"(?im)^\s*sitemap:\s*(\S+)", response.text)
    return parser, sitemaps


def _sitemap_urls(url: str, depth: int = 0) -> list[str]:
    """Read a sitemap, following sitemap-index files one level down."""
    if depth > 2:
        return []
    response = _get(url)
    if response is None:
        return []
    try:
        root = ET.fromstring(response.content)
    except ET.ParseError:
        return []

    # Namespaces vary; match on the tag's local name instead.
    def local(tag: str) -> str:
        return tag.rsplit("}", 1)[-1]

    found: list[str] = []
    if local(root.tag) == "sitemapindex":
        for child in root:
            for node in child:
                if local(node.tag) == "loc" and node.text:
                    found.extend(_sitemap_urls(node.text.strip(), depth + 1))
        return found

    for child in root:
        for node in child:
            if local(node.tag) == "loc" and node.text:
                found.append(node.text.strip())
    return found


def discover(start_url: str, limit: int = 40, obey_robots: bool = True) -> Discovery:
    """Find up to `limit` auditable pages, best source first."""
    parsed = urlparse(start_url if "//" in start_url else "https://" + start_url)
    base = f"{parsed.scheme}://{parsed.netloc}"
    host = parsed.netloc.lower()
    notes: list[str] = []

    parser, advertised = _robots(base)
    if obey_robots and parser is None:
        notes.append("No readable robots.txt; proceeded without crawl restrictions.")

    def allowed(url: str) -> bool:
        if not obey_robots or parser is None:
            return True
        try:
            return parser.can_fetch(USER_AGENT, url)
        except Exception:  # noqa: BLE001
            return True

    candidates: list[str] = []
    source = "sitemap"
    for sitemap in advertised or [urljoin(base, "/sitemap.xml"), urljoin(base, "/sitemap_index.xml")]:
        candidates.extend(_sitemap_urls(sitemap))
        if candidates:
            break

    if not candidates:
        source = "homepage links"
        notes.append("No usable sitemap; fell back to the links on the homepage.")
        response = _get(start_url)
        if response is not None:
            candidates = [
                urljoin(response.url, href)
                for href in re.findall(r'href=["\']([^"\'#]+)', response.text)
            ]

    seen: set[str] = set()
    urls: list[str] = []
    skipped_robots = 0
    for candidate in [start_url, *candidates]:
        if not candidate.startswith(("http://", "https://")):
            continue
        if not _same_site(candidate, host) or NON_PAGE.search(candidate) or NOISE.search(candidate):
            continue
        clean = _normalise(candidate)
        if clean in seen:
            continue
        if not allowed(clean):
            skipped_robots += 1
            continue
        seen.add(clean)
        urls.append(clean)
        if len(urls) >= limit:
            notes.append(f"Stopped at the {limit}-page cap; the site has more.")
            break

    if skipped_robots:
        notes.append(f"{skipped_robots} URL(s) skipped because robots.txt disallows them.")
    return Discovery(urls=urls, source=source, notes=notes)
