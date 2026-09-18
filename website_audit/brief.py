"""Turns a whole-site audit into a website build brief, as Markdown.

The brief is the document you paste into a site builder (Lovable, v0, Bolt,
Framer AI, Webflow AI) to rebuild the site. Everything an audit can measure
is filled in: the brand system as it actually renders, the site map as it was
discovered, each page's headings and copy, and the technical rules the
findings call for. Everything only the business knows — mission, audience,
voice, pricing decisions — is a labelled fill-in, never invented.

Deterministic: no model, no tokens, the same audit gives the same brief.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

TITLE_SPLIT = re.compile(r"\s+[|–—\-·»:]\s+")
SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
SOCIAL_HOSTS = ("facebook.", "instagram.", "linkedin.", "youtube.", "x.com", "twitter.", "tiktok.", "pinterest.")
FILL = "[FILL IN]"

ROLE_USE = {
    "surface": "Backgrounds",
    "ink": "Body text and dark sections",
    "neutral": "Secondary text, borders, rules",
    "brand": "The brand colour: buttons, links, markers",
    "accent": "Accent: highlights, states",
}

# Findings that translate into a rule for whoever rebuilds the site. Anything
# not listed is still reported, under "other findings to carry over".
RULES = {
    "a11y-contrast": "Every text and background pair must pass WCAG AA (4.5:1 body, 3:1 large text). The audit found pairs that fail.",
    "a11y-alt-text": "Every image carries alt text; decorative images use an empty alt.",
    "a11y-h1": "Exactly one h1 per page, and it is the page's headline.",
    "a11y-heading-order": "Headings run h1, h2, h3 in order with no skipped levels.",
    "a11y-tap-targets": "Links and buttons are at least 44px tall on mobile.",
    "a11y-lang": "The html element declares its language.",
    "type-family-sprawl": "Two typefaces only: one for display, one for text. The audit counted more.",
    "type-no-fallback": "Every font-family declaration ends in a generic fallback.",
    "type-small-body": "Body text is at least 16px.",
    "type-line-height": "Body line-height between 1.4 and 1.7.",
    "type-measure": "Prose is capped at roughly 65 to 75 characters per line.",
    "type-scale-sprawl": "One type scale of six to eight sizes, used everywhere.",
    "colour-palette-bloat": "A token palette of no more than six colours plus neutrals.",
    "colour-near-duplicates": "One hex value per role. No near-duplicate shades.",
    "colour-no-tokens": "Colours and type sizes are CSS custom properties, never literals in components.",
    "colour-no-dark-mode": "Support prefers-color-scheme, or declare color-scheme light explicitly.",
    "resp-viewport": "Viewport meta tag on every page.",
    "resp-overflow": "Nothing wider than the viewport at 390px. Test every page at that width.",
    "perf-render-blocking-css": "Critical CSS inline; at most one external stylesheet in the head.",
    "perf-font-display": "Every @font-face uses font-display: swap.",
    "perf-font-weight-count": "At most four font files in total.",
    "perf-cls": "Every image has width and height attributes so nothing shifts as it loads.",
    "perf-lazy-loading": "Below-the-fold images use loading=\"lazy\".",
    "perf-oversized-images": "Images are served at the size they are displayed, not scaled down in the browser.",
    "perf-image-format": "Photos are served as WebP or AVIF.",
    "perf-weight": "Each page under 1.5 MB on first load.",
    "perf-load-time": "Each page interactive in under three seconds on a cold load.",
    "perf-console-errors": "No JavaScript errors in the console on load.",
    "seo-title": "Every page has a unique, descriptive title.",
    "seo-description": "Every page has a meta description.",
    "seo-description-length": "Meta descriptions between 70 and 155 characters.",
    "seo-open-graph": "Open Graph title, description, image and URL on every page.",
    "seo-favicon": "A favicon is declared.",
    "find-no-structured-data": "JSON-LD structured data on every page.",
    "find-no-identity-schema": "The JSON-LD identifies the business as an Organization or LocalBusiness with name, URL and social profiles.",
    "find-no-canonical": "A canonical link on every page.",
    "find-js-dependency": "All copy is present in the served HTML. Nothing important is rendered only by JavaScript, because most crawlers and answer engines do not run it.",
    "find-faq-not-marked-up": "Question-style content is marked up as an FAQPage.",
    "find-noindex": "No page carries noindex unless it is meant to be hidden.",
    "find-no-robots": "Publish a robots.txt.",
    "find-no-sitemap": "Publish an XML sitemap and reference it from robots.txt.",
    "find-no-llms-txt": "Publish an llms.txt describing the business for AI crawlers.",
    "content-placeholder": "No placeholder text anywhere. Write [SPECIFIC COPY NEEDED] instead of lorem ipsum.",
    "content-click-here": "Link text says where the link goes. Never \"click here\" or \"read more\" on its own.",
    "content-thin": "Every page has enough copy to stand on its own; the audit found pages that do not.",
    "site-claim-mismatch": "Every statistic appears with one value across the whole site.",
    "site-dialect-drift": "One spelling convention (US or UK) across the whole site.",
    "site-duplicate-copy": "No paragraph is repeated between pages.",
    "site-duplicate-titles": "No two pages share a title.",
    "site-duplicate-descriptions": "No two pages share a meta description.",
    "site-missing-titles": "Every page has a title.",
    "site-missing-descriptions": "Every page has a meta description.",
    "structure-cta-conflict": "One primary call to action with one label, used on every page.",
    "structure-nav-drift": "The same navigation on every page.",
    "structure-nav-size": "Primary navigation of at most seven items.",
    "structure-no-nav": "Every page carries the primary navigation.",
    "structure-orphan-pages": "Every page is reachable from the navigation or another page.",
}


@dataclass
class Brief:
    markdown: str
    filename: str


# ----------------------------------------------------------------- helpers


def _clean(text: str | None) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _sentences(text: str, limit: int, max_chars: int) -> str:
    out: list[str] = []
    for sentence in SENTENCE_SPLIT.split(_clean(text)):
        if not sentence:
            continue
        if out and len(" ".join(out + [sentence])) > max_chars:
            break
        out.append(sentence)
        if len(out) >= limit:
            break
    joined = " ".join(out)
    return joined if len(joined) <= max_chars + 40 else joined[:max_chars].rsplit(" ", 1)[0] + "…"


def _path(url: str) -> str:
    return urlparse(url).path or "/"


def _same_site(href: str, host: str) -> bool:
    return urlparse(href).netloc.lower().replace("www.", "") == host.lower().replace("www.", "")


def site_name(titles: list[str]) -> str:
    """The segment shared by most page titles; else the first title's last segment."""
    shared: Counter = Counter()
    for title in titles:
        for part in dict.fromkeys(p for p in TITLE_SPLIT.split(_clean(title)) if len(p) > 2):
            shared[part] += 1
    if shared and len(titles) > 1:
        part, count = shared.most_common(1)[0]
        if count >= 2:
            return part
    if titles:
        parts = [p for p in TITLE_SPLIT.split(_clean(titles[0])) if p]
        return parts[-1] if len(parts) > 1 else (parts[0] if parts else FILL)
    return FILL


def _family_name(stack: str) -> str:
    return stack.split(",")[0].strip().strip("'\"") or stack


def _sections(headings: list[dict], text: str) -> list[tuple[int, str, str]]:
    """(level, heading, copy that followed it) for each heading on a page."""
    text = _clean(text)
    lowered = text.lower()
    cleaned = [(int(h.get("level", 2)), _clean(h.get("text"))) for h in headings if _clean(h.get("text"))]
    positions = [(lowered.find(t.lower()), lvl, t) for lvl, t in cleaned]
    out = []
    for i, (at, lvl, title) in enumerate(positions):
        if at < 0:
            out.append((lvl, title, ""))
            continue
        end = min([p for p, _, _ in positions[i + 1:] if p > at] + [len(text)])
        out.append((lvl, title, _sentences(text[at + len(title):end], 2, 240)))
    return out[:14]


def _ctas(pages) -> list[tuple[str, int]]:
    counts: Counter = Counter()
    for page in pages:
        seen = set()
        for cta in (page.probe or {}).get("ctas") or []:
            label = _clean(cta.get("text"))
            if label and label.lower() not in seen:
                seen.add(label.lower())
                counts[label] += 1
    return counts.most_common(8)


def _nav(pages, host: str) -> list[tuple[str, str]]:
    counts: Counter = Counter()
    for page in pages:
        seen = set()
        for link in (page.probe or {}).get("nav_links") or []:
            text, href = _clean(link.get("text")), link.get("href") or ""
            key = (text, _path(href))
            if text and href and _same_site(href, host) and key not in seen and len(text) <= 40:
                seen.add(key)
                counts[key] += 1
    threshold = max(1, len(pages) // 2)
    return [key for key, n in counts.most_common() if n >= threshold][:9]


def _social_and_email(pages, host: str) -> tuple[list[str], list[str]]:
    social: list[str] = []
    emails: Counter = Counter()
    for page in pages:
        probe = page.probe or {}
        for link in (probe.get("footer_links") or []) + (probe.get("nav_links") or []):
            href = link.get("href") or ""
            if any(h in href.lower() for h in SOCIAL_HOSTS) and href not in social:
                social.append(href)
        for link in probe.get("links") or []:
            href = link if isinstance(link, str) else (link.get("href") or "")
            if any(h in href.lower() for h in SOCIAL_HOSTS) and href not in social:
                social.append(href)
        for match in EMAIL.findall(probe.get("body_text") or ""):
            emails[match.lower()] += 1
    return social[:6], [e for e, _ in emails.most_common(3)]


def _fonts_link(families: list[str]) -> str:
    return "https://fonts.googleapis.com/css2?" + "&".join(
        f"family={f.replace(' ', '+')}:wght@400;600;700" for f in families
    ) + "&display=swap"


# ----------------------------------------------------------------- build


def build_brief(audit) -> Brief:
    """The build brief for a whole-site audit (`website_audit.site.SiteAudit`)."""
    pages = audit.audited
    host = audit.host
    base = f"https://{host}/"
    titles = [p.title for p in pages if p.title]
    name = site_name(titles)
    home = next((p for p in pages if _path(p.url) == "/"), pages[0] if pages else None)

    # ---- measured facts
    palette = list(audit.palette or [])
    families = [(_family_name(n), chars) for n, chars in (audit.families or [])]
    total_chars = sum(c for _, c in families) or 1
    display_font = body_font = None
    if families:
        body_font = families[0][0]
        display_font = next((f for f, _ in families[1:] if f.lower() != body_font.lower()), body_font)
    body_sizes: Counter = Counter()
    line_heights: list[float] = []
    for p in pages:
        typ = (p.results or {}).get("typography") or {}
        if typ.get("body_size"):
            body_sizes[typ["body_size"]] += 1
        run = typ.get("body_run") or {}
        if run.get("line_height") and run.get("font_size"):
            line_heights.append(run["line_height"] / run["font_size"])
    body_size = body_sizes.most_common(1)[0][0] if body_sizes else None
    line_height = round(sum(line_heights) / len(line_heights), 2) if line_heights else None

    nav = _nav(pages, host)
    ctas = _ctas(pages)
    social, emails = _social_and_email(pages, host)
    findings = list(audit.findings or [])
    finding_ids = [f.id for f in findings]
    rules = [(fid, RULES[fid]) for fid in finding_ids if fid in RULES]
    other = [f for f in findings if f.id not in RULES]
    scores = audit.scores or {}
    generated = datetime.now(timezone.utc).strftime("%d %b %Y %H:%M UTC")

    L: list[str] = []
    add = L.append

    add(f"# {name} · Website Build Prompt")
    add("")
    add(f"Generated from a whole-site audit of {base} on {generated}. "
        f"{len(pages)} pages audited · score {scores.get('overall', '—')}/100 (grade {scores.get('grade', '—')}) · {len(findings)} findings.")
    add("")
    add("Paste everything below the line into Lovable, v0, Bolt, Framer AI, Relume, Webflow AI or any site builder. "
        f"Everything measured by the audit is filled in. Everything marked `{FILL}` is something only the business knows; fill it in first, and never let a builder invent it.")
    add("")
    add("---")
    add("")
    add("## ROLE")
    add("")
    add(f"You are a senior brand and web designer rebuilding the marketing site for **{name}**. "
        "You keep the brand system below exactly, you reuse the site's own copy where it is given, and you write nothing you cannot source from this brief. "
        "A visitor should not be able to tell this came from a builder.")
    add("")
    add("## MISSION")
    add("")
    add(f"Rebuild the complete website for **{name}** ({base}) so that it keeps what works, fixes every finding in section 9, and scores 90 or higher in every audit category.")
    add("")
    add("The site's jobs, in order:")
    add(f"1. {FILL} — the one thing a visitor should do")
    add(f"2. {FILL}")
    add(f"3. {FILL}")
    add("")
    add(f"Multi-page ({len(pages)} routes, section 6). Mobile-first. Fast. Every page ends in one clear action.")
    add("")
    add("---")
    add("")
    add("## 1. THE COMPANY")
    add("")
    add(f"- **Name:** {name}")
    add(f"- **Domain:** {host}" + (f" · **Email:** {', '.join(emails)}" if emails else f" · **Email:** {FILL}"))
    add(f"- **Social:** {', '.join(social) if social else FILL}")
    add(f"- **Location:** {FILL}")
    add(f"- **Founder / leadership:** {FILL}")
    add("")
    if home and home.page and home.page.meta_description:
        add(f"**How the current site describes itself (home page meta description):** {_clean(home.page.meta_description)}")
        add("")
    if home and home.page and home.page.text:
        add(f"**Opening copy on the current home page:** {_sentences(home.page.text, 3, 320)}")
        add("")
    add(f"**What we are:** {FILL}")
    add("")
    add(f"**What we are not:** {FILL}")
    add("")
    add(f"**The core thesis, which runs through every page:** {FILL}")
    add("")
    add("---")
    add("")
    add("## 2. WHAT THE CURRENT SITE OFFERS")
    add("")
    add("Every page the audit found, weakest first. Keep, merge or drop each one deliberately.")
    add("")
    add("| Page | Title | Score | Findings | Keep? |")
    add("|---|---|---|---|---|")
    for p in sorted(pages, key=lambda p: (p.score if p.score is not None else 0)):
        add(f"| `{_path(p.url)}` | {_clean(p.title) or '—'} | {p.score if p.score is not None else '—'} | {p.findings} | {FILL} |")
    add("")
    add("---")
    add("")
    add("## 3. WHO WE SERVE")
    add("")
    add(f"| Segment | Who exactly | What we lead with |")
    add("|---|---|---|")
    add(f"| {FILL} | {FILL} | {FILL} |")
    add(f"| {FILL} | {FILL} | {FILL} |")
    add("")
    add("The audit cannot see the audience. Write these rows for the person the site should feel written for, not for \"small businesses\".")
    add("")
    add("---")
    add("")
    add("## 4. BRAND SYSTEM · as measured on the current site")
    add("")
    add("### Colour")
    add("")
    if palette:
        add("| Role | Hex | Where it is used today | Keep? |")
        add("|---|---|---|---|")
        for s in palette[:10]:
            use = []
            if getattr(s, "text_chars", 0):
                use.append(f"{s.text_chars:,} characters of text")
            if getattr(s, "background_area", 0):
                use.append("backgrounds")
            if getattr(s, "border_uses", 0):
                use.append("borders")
            role = (s.role or "neutral").capitalize()
            add(f"| {role} | `{s.hex}` | {', '.join(use) or ROLE_USE.get(s.role, '')} | {FILL} |")
        add("")
        add(f"The site renders {len(palette)} distinct colour clusters. Reduce that to at most six tokens plus neutrals, "
            "and give the brand colour a discipline: buttons, markers and one accent phrase per headline, never body text, never large fills except a closing band.")
    else:
        add(f"No palette was measured. Brand colours: {FILL}")
    add("")
    add("### Typography")
    add("")
    if families:
        add("| Family | Share of text | Role in the rebuild |")
        add("|---|---|---|")
        for fam, chars in families[:6]:
            role = "Text" if fam == body_font else ("Display" if fam == display_font else "Drop")
            add(f"| {fam} | {100 * chars / total_chars:.0f}% | {role} |")
        add("")
        add("Google Fonts (swap for self-hosted files if these are not Google fonts):")
        add("```")
        add(_fonts_link(list(dict.fromkeys([display_font, body_font]))))
        add("```")
        add("")
        add(f"- **{display_font}** for display headlines only")
        add(f"- **{body_font}** for everything else. Body {body_size or 16}px (measured: {body_size or 'not measured'}), line-height {line_height or 1.6}")
        if len(families) > 2:
            add(f"- Every other family the audit found ({', '.join(f for f, _ in families[2:6])}) is dropped")
    else:
        add(f"No named typeface was measured. Display: {FILL}. Text: {FILL}.")
    add("")
    add("### Logo and wordmark")
    add("")
    add(f"{FILL} — describe the mark, the wordmark treatment, and how it sits on light and dark backgrounds.")
    add("")
    add("### Signature devices")
    add("")
    add(f"{FILL} — three to seven recurring visual devices that make the site unmistakably this brand. Without them a builder produces a template.")
    add("")
    add("---")
    add("")
    add("## 5. VOICE")
    add("")
    add(f"{FILL} — three adjectives, then a \"never use\" list and a \"do\" list.")
    add("")
    add("**Never use:** invented statistics, fake testimonials, fake client logos, claims to be the only company doing this.")
    add("")
    add("**Do:** lead with the buyer's problem before the solution; concrete specifics over adjectives; paragraphs of three sentences or fewer; headlines that could not be swapped onto a competitor's site.")
    add("")
    add("---")
    add("")
    add("## 6. SITE MAP")
    add("")
    add("As discovered by the audit. Rename, merge or add routes deliberately; every route must appear in section 7.")
    add("")
    add("```")
    width = max((len(_path(p.url)) for p in pages), default=10) + 2
    for p in pages:
        add(f"{_path(p.url):<{width}} {_clean(p.title) or FILL}")
    add("```")
    add("")
    if nav:
        add("Primary navigation on the current site: " + " · ".join(text for text, _ in nav) + ".")
    else:
        add(f"Primary navigation: {FILL} (the audit did not find a consistent navigation).")
    if ctas:
        label, n = ctas[0]
        add(f"Primary call to action: **{label}** (the most used button label, on {n} of {len(pages)} pages)."
            + (" Other labels in use: " + ", ".join(f"\"{l}\"" for l, _ in ctas[1:5]) + ". Pick one." if len(ctas) > 1 else ""))
    else:
        add(f"Primary call to action: {FILL}")
    add("")
    add("---")
    add("")
    add("## 7. PAGE SPECS")
    add("")
    add("Each page as it exists today: its headings in order, the copy under each, and the actions on it. "
        "Reuse the copy that is good, mark what to rewrite, and give every page one closing action.")
    add("")
    for p in pages:
        add(f"### {_path(p.url).upper()} · {_clean(p.title) or FILL}")
        add("")
        probe = p.probe or {}
        desc = _clean(probe.get("meta_description"))
        add(f"**Meta description today:** {desc if desc else 'none — write one'}")
        add("")
        sections = _sections(probe.get("headings") or [], (p.page.text if p.page else "") or probe.get("body_text") or "")
        if sections:
            for lvl, title, copy in sections:
                indent = "  " * max(0, lvl - 1)
                add(f"{indent}- **h{lvl} · {title}**" + (f" — {copy}" if copy else " — (no copy under this heading)"))
        else:
            add(f"- No headings were found on this page. Structure: {FILL}")
        page_ctas = list(dict.fromkeys(_clean(c.get("text")) for c in probe.get("ctas") or [] if _clean(c.get("text"))))
        add("")
        add("**Actions on the page today:** " + (", ".join(f"\"{c}\"" for c in page_ctas[:6]) if page_ctas else "none found"))
        add("")
        add(f"**Closing action for the rebuild:** {FILL}")
        add("")
    add("---")
    add("")
    add("## 8. TECHNICAL REQUIREMENTS")
    add("")
    add("- Mobile-first, fully responsive; test every page at 390px")
    add("- Google Fonts is the only external dependency; fonts load with font-display: swap")
    add("- CSS custom properties for the full colour and type scale")
    add("- Semantic HTML: one h1 per page, headings in order, landmarks for header, nav, main and footer")
    add("- Accessibility: skip link, visible focus states, aria-expanded on menus and accordions, alt text on all images, 44px minimum tap targets, WCAG AA contrast, prefers-reduced-motion respected")
    add("- All copy in the served HTML; nothing important rendered only by JavaScript")
    add("- Every page: unique title, meta description, canonical, Open Graph tags, JSON-LD identifying the business")
    add("- robots.txt, an XML sitemap and llms.txt at the root")
    add("- Every image slot is a labelled placeholder describing the shot needed, with width and height set, lazy below the fold")
    add("- Forms post to a placeholder endpoint, marked with a comment")
    add("")
    add("---")
    add("")
    add("## 9. HARD RULES · from the audit's findings")
    add("")
    if rules:
        add("Each rule exists because the audit found the opposite on the current site. The count says how many audited pages it touched.")
        add("")
        reach = {f.id: (f.evidence[0] if f.evidence else "") for f in findings}
        for i, (fid, rule) in enumerate(rules, 1):
            add(f"{i}. {rule} *({reach.get(fid, '')}; `{fid}`)*")
    else:
        add("The audit found nothing that needs a rule. Keep the technical requirements above.")
        add("")
    add(f"{len(rules) + 1}. Never invent client names, logos, testimonials, metrics, case study figures or awards. Write `[SPECIFIC PROOF NEEDED]`.")
    add(f"{len(rules) + 2}. Never use generic agency phrasing. If a sentence could appear on any competitor's site, rewrite it.")
    add(f"{len(rules) + 3}. Never stack more than two same-background sections in a row.")
    add("")
    if other:
        add("**Other findings to carry over** (no single rule, but fix them):")
        add("")
        for f in other[:12]:
            add(f"- **{f.title}** — {f.fix} *({f.evidence[0] if f.evidence else ''}; `{f.id}`)*")
        add("")
    add("---")
    add("")
    add("## 10. WHAT GOOD LOOKS LIKE")
    add("")
    add(f"- Re-audited with the same tool, every category scores 90 or higher (today: " +
        ", ".join(f"{k} {v}" for k, v in (scores.get("categories") or {}).items()) + ")")
    add(f"- {FILL} — who lands on the home page and what they understand within five seconds")
    add("- Every page ends in exactly one action")
    add("- Nothing on the site could be swapped onto a competitor without breaking")
    add("")
    add("---")
    add("")
    add("## 11. DELIVERABLE")
    add("")
    add(f"Full site, all {len(pages)} routes in section 6, production-ready. If your platform limits pages, build the home page and the three weakest pages in section 2 first, then the rest.")
    add("")
    add("---")
    add("")
    add("## 12. FILL BEFORE BUILDING")
    add("")
    add("```")
    add("Primary action URL (booking, form):  [                              ]")
    add("Contact email and phone:             [                              ]")
    add("Physical address (for JSON-LD):      [                              ]")
    add("Logo files (SVG, light and dark):    [                              ]")
    add("Real photography available:          [ yes / no ]")
    add("Team names to show:                  [                              ]")
    add("Any client names cleared for use:    [                              ]")
    add("Any real metrics cleared for use:    [                              ]")
    add("```")
    add("")

    return Brief(markdown="\n".join(L), filename=f"{host.replace(':', '-')}-site-brief.md")
