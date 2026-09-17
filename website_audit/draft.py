"""Turns an audited home page into a corrected single-page draft.

The draft keeps the site's own words, navigation and calls to action, and
rebuilds the page around them with the audit's fixes applied: two typefaces,
a palette reduced to tokens that pass WCAG AA, a readable body size, correct
heading order, machine-readable identity, and a consistent primary action.

Everything here is deterministic — no model, no tokens — so the same audit
always yields the same draft. Anything the audit could not supply (photos,
an address, a phone number) is rendered as a visible placeholder rather than
invented.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

from jinja2 import Environment, FileSystemLoader

from .analysis import SYSTEM_TOKENS, contrast_ratio, relative_luminance, hex_to_rgb, suggest_accessible_colour

TITLE_SPLIT = re.compile(r"\s+[|–—\-·»:]\s+")
SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")
SOCIAL_HOSTS = ("facebook.", "instagram.", "linkedin.", "youtube.", "x.com", "twitter.", "tiktok.", "pinterest.")
WEB_FONT_WEIGHTS = "400;600;700"

# Findings the draft resolves by construction, and how. Anything not listed is
# outside what a page draft can fix (content strategy, hosting, ops).
RESOLVES = {
    "type-family-sprawl": "Two typefaces carry the whole page: one for headings, one for text.",
    "type-no-fallback": "Every font stack ends in a metric-compatible fallback and a generic family.",
    "type-small-body": "Body text is 17px, with fine print no smaller than 14px.",
    "type-line-height": "Body line-height is 1.55; headings tighten to 1.1.",
    "type-measure": "Prose is capped at 64 characters per line.",
    "type-scale-sprawl": "A six-step type scale replaces the ad-hoc sizes.",
    "colour-palette-bloat": "Six colour tokens replace the clustered palette.",
    "colour-near-duplicates": "Near-duplicate shades collapse into one token each.",
    "colour-no-tokens": "All colours and sizes are CSS custom properties on :root.",
    "colour-no-dark-mode": "A prefers-color-scheme block swaps the surface and ink tokens.",
    "a11y-contrast": "Every text/background pair is checked against WCAG AA (4.5:1) before rendering.",
    "a11y-h1": "Exactly one h1, carrying the hero headline.",
    "a11y-heading-order": "Headings run h1 → h2 → h3 with no skipped levels.",
    "a11y-alt-text": "Placeholder visuals are labelled; every real image slot has an alt attribute.",
    "a11y-tap-targets": "Links and buttons are at least 44px tall.",
    "a11y-lang": "The html element declares its language.",
    "resp-viewport": "Viewport meta is set; the layout is mobile-first.",
    "resp-overflow": "Nothing is wider than the viewport at 390px; the grid collapses to one column.",
    "perf-render-blocking-css": "All CSS is inline; the only external sheet is the font file.",
    "perf-font-display": "Fonts load with font-display: swap.",
    "perf-font-weight-count": "Three weights per family at most.",
    "perf-cls": "Every visual slot has fixed dimensions, so nothing shifts as it loads.",
    "perf-lazy-loading": "Below-the-fold image slots are marked loading=\"lazy\".",
    "perf-weight": "The page is a single file well under 100 KB before photos.",
    "perf-console-errors": "No scripts run beyond the mobile menu toggle.",
    "seo-title": "A title of the form 'Site name — headline'.",
    "seo-description": "A meta description drawn from the page's own opening copy.",
    "seo-description-length": "The description is trimmed to 155 characters.",
    "seo-open-graph": "Open Graph title, description, type and URL are set.",
    "seo-favicon": "An inline SVG favicon (replace with the brand mark).",
    "find-no-structured-data": "JSON-LD identifies the organisation and the site.",
    "find-no-identity-schema": "The JSON-LD @type is Organization, with social profiles as sameAs.",
    "find-no-canonical": "A canonical link points at the page's own URL.",
    "find-js-dependency": "All copy is in the HTML; nothing is rendered by script.",
    "find-faq-not-marked-up": "Question-style headings are grouped and marked up as an FAQPage.",
    "content-placeholder-text": "No lorem ipsum: placeholders are labelled as such and easy to find.",
    "structure-cta-conflict": "One primary call to action, with one label, throughout the page.",
    "structure-expected-pages": "The footer links to contact, privacy and terms.",
}


@dataclass
class Draft:
    html: str
    filename: str
    notes: list[str] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)
    palette: dict[str, str] = field(default_factory=dict)
    fonts: dict[str, str] = field(default_factory=dict)


# ----------------------------------------------------------------- helpers


def _clean(text: str | None) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _sentences(text: str, limit: int, max_chars: int) -> str:
    out: list[str] = []
    for sentence in SENTENCE_SPLIT.split(_clean(text)):
        if not sentence:
            continue
        candidate = " ".join(out + [sentence])
        if out and len(candidate) > max_chars:
            break
        out.append(sentence)
        if len(out) >= limit:
            break
    joined = " ".join(out)
    return joined if len(joined) <= max_chars + 40 else joined[:max_chars].rsplit(" ", 1)[0] + "…"


def _same_site(href: str, host: str) -> bool:
    netloc = urlparse(href).netloc.lower()
    return netloc.replace("www.", "") == host.replace("www.", "")


def _is_home(href: str) -> bool:
    path = urlparse(href).path
    return path in ("", "/")


def site_name(title: str | None, page_titles: list[str] | None = None) -> str:
    """The brand from the title tag: the segment shared across pages, else the last one."""
    parts = [p for p in TITLE_SPLIT.split(_clean(title)) if p]
    if page_titles:
        shared = Counter()
        for other in page_titles:
            for part in TITLE_SPLIT.split(_clean(other)):
                shared[part] += 1
        common = [p for p, n in shared.most_common() if n >= 2 and len(p) > 2]
        if common:
            return common[0]
    if len(parts) > 1:
        return parts[-1]
    return parts[0] if parts else "Your site"


def _pick_family(name: str) -> str:
    """The first named family in a stack — the one the designer chose."""
    for part in name.split(","):
        part = part.strip().strip("'\"")
        if part and part.lower() not in SYSTEM_TOKENS:
            return part
    return ""


def choose_fonts(typography: dict[str, Any]) -> tuple[str, str, list[str]]:
    """Body = the family carrying the most text; display = the one set largest.

    Falls back to a serif/sans pair when the site gives nothing usable. Icon
    fonts and system stacks are never promoted to a role.
    """
    notes: list[str] = []
    usable = []
    for fam in typography.get("families", []):
        if getattr(fam, "is_icon_font", False) or getattr(fam, "is_system", False):
            continue
        name = _pick_family(fam.name)
        if name:
            usable.append((name, fam))
    if not usable:
        notes.append("No named typeface was measured, so the draft uses Fraunces and DM Sans.")
        return "Fraunces", "DM Sans", notes

    body_name, body_fam = max(usable, key=lambda nf: nf[1].chars)

    def largest_size(fam) -> float:
        return max((float(s) for s in fam.sizes), default=0)

    display_candidates = [(n, f) for n, f in usable if n.lower() != body_name.lower()]
    if display_candidates:
        display_name, _ = max(display_candidates, key=lambda nf: (largest_size(nf[1]), nf[1].chars))
    else:
        display_name = body_name
    dropped = sorted({n for n, _ in usable} - {body_name, display_name})
    if dropped:
        notes.append(f"Kept {display_name} for headings and {body_name} for text; dropped {', '.join(dropped)}.")
    else:
        notes.append(f"{display_name} for headings and {body_name} for text, as on the current site.")
    return display_name, body_name, notes


def _lum(hex_value: str) -> float:
    return relative_luminance(hex_to_rgb(hex_value))


def choose_palette(palette: list[Any]) -> tuple[dict[str, str], list[str]]:
    """Six tokens from the measured swatches, every text pair forced to AA."""
    notes: list[str] = []
    swatches = list(palette or [])

    def by_role(role: str):
        return [s for s in swatches if s.role == role]

    surfaces = by_role("surface") or [s for s in swatches if _lum(s.hex) > 0.6]
    surface = max(surfaces, key=lambda s: (s.background_area, _lum(s.hex))).hex if surfaces else "#faf8f4"

    inks = by_role("ink") or [s for s in swatches if _lum(s.hex) < 0.15]
    ink = max(inks, key=lambda s: s.text_chars).hex if inks else "#1c1a17"

    brands = by_role("brand") or by_role("accent")
    brands = [s for s in brands if s.hex not in (surface, ink)]
    brand = max(brands, key=lambda s: s.weight).hex if brands else ink

    neutrals = [s for s in by_role("neutral") if s.text_chars and s.hex not in (surface, ink)]
    muted = max(neutrals, key=lambda s: s.text_chars).hex if neutrals else "#615b55"

    fixed = {}
    ink_ok = suggest_accessible_colour(ink, surface, 4.5) if contrast_ratio(ink, surface) < 4.5 else ink
    if ink_ok != ink:
        fixed["ink"] = (ink, ink_ok)
    muted_ok = suggest_accessible_colour(muted, surface, 4.5) if contrast_ratio(muted, surface) < 4.5 else muted
    if muted_ok != muted:
        fixed["muted"] = (muted, muted_ok)
    # Brand is used as text on the surface and as a button behind surface-coloured text.
    brand_ok = brand
    if contrast_ratio(brand_ok, surface) < 4.5:
        brand_ok = suggest_accessible_colour(brand, surface, 4.5) or brand
        fixed["brand"] = (brand, brand_ok)

    # Buttons are brand-filled: pick whichever of surface/ink reads on them, and
    # keep a brand tint that still passes when the scheme flips to dark.
    on_brand = surface if contrast_ratio(surface, brand_ok) >= 4.5 else ink_ok
    if contrast_ratio(on_brand, brand_ok) < 4.5:
        brand_ok = suggest_accessible_colour(brand_ok, surface, 4.5) or brand_ok
        on_brand = surface
    brand_on_dark = brand_ok if contrast_ratio(brand_ok, ink_ok) >= 4.5 else suggest_accessible_colour(brand_ok, ink_ok, 4.5)

    tokens = {
        "surface": surface,
        "surface_2": _mix(surface, ink_ok, 0.06),
        "ink": ink_ok,
        "muted": muted_ok,
        "brand": brand_ok,
        "on_brand": on_brand,
        "brand_on_dark": brand_on_dark or surface,
        "line": _mix(surface, ink_ok, 0.16),
    }
    for name, (before, after) in fixed.items():
        notes.append(f"{name.capitalize()} {before} failed AA on {surface}; darkened to {after}.")
    if brands:
        notes.append(f"Brand colour {brand} taken from the site's own palette.")
    else:
        notes.append("No brand colour was measured; the ink colour stands in. Replace --brand.")
    return tokens, notes


def _mix(a: str, b: str, amount: float) -> str:
    ra, ga, ba = hex_to_rgb(a)
    rb, gb, bb = hex_to_rgb(b)
    mix = (ra + (rb - ra) * amount, ga + (gb - ga) * amount, ba + (bb - ba) * amount)
    return "#" + "".join(f"{int(round(c)):02x}" for c in mix)


# ----------------------------------------------------------------- content


def _dedupe_links(links: list[dict], host: str, cap: int, internal_only: bool = True) -> list[dict]:
    out: list[dict] = []
    seen: set[str] = set()
    for link in links:
        text = _clean(link.get("text"))
        href = link.get("href") or ""
        if not text or len(text) > 40 or not href:
            continue
        if internal_only and not _same_site(href, host):
            continue
        key = text.lower()
        if key in seen or key in ("home", "menu", "skip to content"):
            continue
        seen.add(key)
        out.append({"text": text, "href": href})
        if len(out) >= cap:
            break
    return out


def _sections(headings: list[dict], body_text: str, h1: str) -> list[dict]:
    """Each top-level section heading with the copy that followed it on the page."""
    text = _clean(body_text)
    lowered = text.lower()
    level = 2 if any(h["level"] == 2 for h in headings) else 3
    candidates = [h for h in headings if h["level"] == level and _clean(h["text"]) and _clean(h["text"]) != h1]

    positions = []
    for h in candidates:
        at = lowered.find(_clean(h["text"]).lower())
        positions.append((at, _clean(h["text"])))

    sections = []
    for i, (at, title) in enumerate(positions):
        if at < 0:
            sections.append({"title": title, "body": ""})
            continue
        end = len(text)
        for later_at, _ in positions[i + 1:]:
            if later_at > at:
                end = min(end, later_at)
        chunk = text[at + len(title):end]
        sections.append({"title": title, "body": _sentences(chunk, 2, 220)})
    # Drop empty duplicates and cap the page at six sections.
    seen: set[str] = set()
    unique = []
    for s in sections:
        if s["title"].lower() in seen:
            continue
        seen.add(s["title"].lower())
        unique.append(s)
    return unique[:6]


def _lede(headings: list[dict], body_text: str, h1: str, description: str | None) -> str:
    text = _clean(body_text)
    if h1 and text:
        at = text.lower().find(h1.lower())
        if at >= 0:
            after = text[at + len(h1):]
            nxt = min(
                (after.lower().find(_clean(h["text"]).lower()) for h in headings
                 if _clean(h["text"]) and _clean(h["text"]) != h1
                 and after.lower().find(_clean(h["text"]).lower()) > 0),
                default=len(after),
            )
            lede = _sentences(after[:nxt], 2, 180)
            if len(lede) > 30:
                return lede
    if description:
        return _sentences(description, 2, 180)
    return _sentences(text, 2, 180)


def _primary_cta(ctas: list[dict], nav: list[dict], host: str) -> dict:
    counts: Counter = Counter()
    hrefs: dict[str, str] = {}
    for cta in ctas or []:
        text = _clean(cta.get("text"))
        href = cta.get("href") or ""
        if not text or len(text) < 3 or not href or href.startswith("javascript"):
            continue
        counts[text] += 1
        hrefs.setdefault(text, href)
    if counts:
        label = counts.most_common(1)[0][0]
        return {"text": label, "href": hrefs[label]}
    for link in nav:
        if "contact" in link["text"].lower():
            return {"text": "Get in touch", "href": link["href"]}
    return {"text": "Get in touch", "href": "#contact"}


def _expected_links(footer: list[dict], nav: list[dict], base: str) -> tuple[list[dict], list[str]]:
    """Contact, privacy and terms: the site's own if linked anywhere, else placeholders."""
    notes = []
    pool = footer + nav
    out = list(footer)
    have = " ".join(l["text"].lower() + " " + l["href"].lower() for l in pool)
    for label, key, path in (("Contact", "contact", "/contact/"), ("Privacy policy", "privacy", "/privacy/"), ("Terms", "terms", "/terms/")):
        if key in have:
            continue
        out.append({"text": label, "href": urljoin(base, path), "placeholder": True})
        notes.append(f"No {key} page was linked; a placeholder {path} link was added to the footer.")
    return out[:9], notes


def _faq(headings: list[dict]) -> list[str]:
    return [_clean(h["text"]) for h in headings if _clean(h["text"]).endswith("?")][:5]


def _social(links: list[dict]) -> list[str]:
    out = []
    for link in links or []:
        href = link if isinstance(link, str) else (link.get("href") or "")
        if any(h in href.lower() for h in SOCIAL_HOSTS) and href not in out:
            out.append(href)
    return out[:5]


# ----------------------------------------------------------------- build


def build_draft(
    url: str,
    probe: dict[str, Any],
    results: dict[str, Any],
    page_titles: list[str] | None = None,
    site_findings: list[Any] | None = None,
) -> Draft:
    """Assemble the draft from one page's probe and analysis.

    `page_titles` (from a whole-site audit) sharpens the brand-name guess, and
    `site_findings` lets the notes say which cross-page findings the draft also
    addresses.
    """
    parsed = urlparse(url)
    host = parsed.netloc
    base = f"{parsed.scheme}://{parsed.netloc}/"

    headings = [
        {"level": int(h.get("level", 2)), "text": _clean(h.get("text"))}
        for h in probe.get("headings", []) if _clean(h.get("text"))
    ]
    title = _clean(probe.get("title"))
    name = site_name(title, page_titles)
    h1s = [h["text"] for h in headings if h["level"] == 1]
    h1 = h1s[0] if h1s else (TITLE_SPLIT.split(title)[0] if title else name)
    body_text = probe.get("body_text") or probe.get("body_text_sample") or ""
    lede = _lede(headings, body_text, h1, probe.get("meta_description"))
    description = _sentences(probe.get("meta_description") or lede, 2, 155)

    nav = _dedupe_links(probe.get("nav_links") or [], host, 6)
    footer_raw = _dedupe_links(probe.get("footer_links") or [], host, 8)
    footer, footer_notes = _expected_links(footer_raw, nav, base)
    cta = _primary_cta(probe.get("ctas") or [], nav, host)
    sections = _sections(headings, body_text, h1)
    faq = _faq(headings)
    social = _social((probe.get("footer_links") or []) + (probe.get("nav_links") or []) + (probe.get("links") or []))

    display_font, body_font, font_notes = choose_fonts(results.get("typography", {}))
    tokens, colour_notes = choose_palette(results.get("palette", []))

    finding_ids = {f.id for f in results.get("findings", [])} | {f.id for f in (site_findings or [])}
    resolved = [f"{fid}: {RESOLVES[fid]}" for fid in sorted(finding_ids) if fid in RESOLVES]
    unresolved = sorted(fid for fid in finding_ids if fid not in RESOLVES)

    notes = font_notes + colour_notes + footer_notes
    notes.append("Photos, the og:image, address and phone number are placeholders; nothing was invented.")

    schema = {
        "@context": "https://schema.org",
        "@graph": [
            {"@type": "Organization", "name": name, "url": base, **({"sameAs": social} if social else {})},
            {"@type": "WebSite", "name": name, "url": base},
        ],
    }
    if faq:
        schema["@graph"].append({
            "@type": "FAQPage",
            "mainEntity": [{"@type": "Question", "name": q, "acceptedAnswer": {"@type": "Answer", "text": "[PLACEHOLDER: answer]"}} for q in faq],
        })

    google_fonts = "https://fonts.googleapis.com/css2?" + "&".join(
        f"family={fam.replace(' ', '+')}:wght@{WEB_FONT_WEIGHTS}" for fam in dict.fromkeys([display_font, body_font])
    ) + "&display=swap"

    env = Environment(loader=FileSystemLoader(Path(__file__).parent / "templates"), autoescape=True)
    html = env.get_template("draft.html.j2").render(
        name=name,
        url=url,
        base=base,
        lang=(probe.get("lang") or "en")[:10],
        h1=h1,
        lede=lede,
        description=description,
        nav=nav,
        footer=footer,
        cta=cta,
        sections=sections,
        faq=faq,
        social=social,
        display_font=display_font,
        body_font=body_font,
        google_fonts=google_fonts,
        tokens=tokens,
        schema_json=json.dumps(schema, ensure_ascii=False, indent=2),
        notes=notes,
        resolved=resolved,
        unresolved=unresolved,
    )
    return Draft(
        html=html,
        filename=f"{host.replace(':', '-')}-home-draft.html",
        notes=notes + resolved,
        unresolved=unresolved,
        palette=tokens,
        fonts={"display": display_font, "body": body_font},
    )


def write_draft(draft: Draft, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(draft.html, encoding="utf-8")
    return path


def _norm(url: str) -> str:
    parsed = urlparse(url)
    return (parsed.netloc.lower().replace("www.", "") + parsed.path.rstrip("/")) or "/"


def draft_from_site(audit) -> Draft | None:
    """The home page draft from a whole-site audit; None if nothing was audited."""
    audited = audit.audited
    if not audited:
        return None
    home = next((p for p in audited if _norm(p.url) == _norm(audit.start_url)), audited[0])
    return build_draft(
        home.url,
        home.probe or {},
        home.results or {},
        page_titles=[p.title for p in audited if p.title],
        site_findings=audit.findings,
    )
