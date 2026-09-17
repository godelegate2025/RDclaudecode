"""Whether machines can find and read the site — search crawlers and answer engines.

The headline check here is how much of the copy survives without JavaScript.
A browser runs the scripts; many crawlers and most answer engines do not. If the
rendered page has 2,000 words and the served HTML has 80, the site is close to
invisible to them — and nothing else in the audit would reveal it, because the
rendered page looks perfect.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

import requests

USER_AGENT = "Mozilla/5.0 (compatible; RedefineAudit/1.0)"
TIMEOUT = 10

SCRIPTISH = re.compile(r"(?is)<(script|style|noscript|template|svg)\b.*?</\1>")
TAG = re.compile(r"(?s)<[^>]+>")
ENTITY = re.compile(r"&(?:#\d+|#x[0-9a-fA-F]+|[a-zA-Z]+);")

# Schema types worth having; the first two identify the business itself.
IDENTITY_TYPES = {"organization", "localbusiness", "corporation", "person", "website"}


@dataclass
class SiteFiles:
    """The files crawlers look for before they look at any page."""

    robots_txt: bool = False
    sitemap: str | None = None
    llms_txt: bool = False
    notes: list[str] = field(default_factory=list)


_cache: dict[str, SiteFiles] = {}


def _head_or_get(url: str) -> requests.Response | None:
    try:
        response = requests.get(
            url, timeout=TIMEOUT, headers={"User-Agent": USER_AGENT}, allow_redirects=True
        )
        return response if response.status_code == 200 else None
    except requests.RequestException:
        return None


def site_files(page_url: str) -> SiteFiles:
    """Check robots.txt, a sitemap and llms.txt once per origin."""
    parsed = urlparse(page_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    if origin in _cache:
        return _cache[origin]

    files = SiteFiles()
    robots = _head_or_get(urljoin(origin, "/robots.txt"))
    if robots is not None and "user-agent" in robots.text.lower():
        files.robots_txt = True
        advertised = re.findall(r"(?im)^\s*sitemap:\s*(\S+)", robots.text)
        if advertised:
            files.sitemap = advertised[0]

    if not files.sitemap:
        for path in ("/sitemap.xml", "/sitemap_index.xml"):
            found = _head_or_get(urljoin(origin, path))
            if found is not None and "<" in found.text[:200]:
                files.sitemap = urljoin(origin, path)
                break

    llms = _head_or_get(urljoin(origin, "/llms.txt"))
    # A site serving HTML for every unknown path would false-positive here.
    if llms is not None and "<html" not in llms.text[:400].lower():
        files.llms_txt = True

    _cache[origin] = files
    return files


def visible_text(html: str) -> str:
    """Approximate what a crawler that does not run JavaScript would read."""
    if not html:
        return ""
    stripped = SCRIPTISH.sub(" ", html)
    stripped = TAG.sub(" ", stripped)
    stripped = ENTITY.sub(" ", stripped)
    return re.sub(r"\s+", " ", stripped).strip()


@dataclass
class JSDependency:
    raw_words: int
    rendered_words: int

    @property
    def ratio(self) -> float:
        """Share of the rendered copy that is already in the served HTML."""
        if self.rendered_words <= 0:
            return 1.0
        return min(1.0, self.raw_words / self.rendered_words)

    @property
    def measurable(self) -> bool:
        # Too little text either way and the ratio is noise.
        return self.rendered_words >= 150


def js_dependency(raw_html: str, rendered_text: str) -> JSDependency:
    return JSDependency(
        raw_words=len(visible_text(raw_html).split()),
        rendered_words=len(rendered_text.split()),
    )


def structured_data_types(blocks: list[str]) -> list[str]:
    """Pull @type values out of the JSON-LD blocks on the page."""
    types: list[str] = []

    def walk(node) -> None:
        if isinstance(node, dict):
            value = node.get("@type")
            if isinstance(value, str):
                types.append(value)
            elif isinstance(value, list):
                types.extend(v for v in value if isinstance(v, str))
            for child in node.values():
                walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    for block in blocks:
        try:
            walk(json.loads(block))
        except (ValueError, TypeError):
            continue
    # Preserve order, drop duplicates.
    seen: set[str] = set()
    ordered: list[str] = []
    for name in types:
        if name.lower() not in seen:
            seen.add(name.lower())
            ordered.append(name)
    return ordered


def has_identity_schema(types: list[str]) -> bool:
    return any(t.lower() in IDENTITY_TYPES for t in types)
