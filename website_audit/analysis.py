"""Turns raw page evidence into a palette, a type inventory, and scored findings."""

from __future__ import annotations

import colorsys
import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any

from .findability import has_identity_schema, js_dependency, structured_data_types

SEVERITY_WEIGHT = {"critical": 28, "high": 16, "medium": 8, "low": 3}
# CSS keywords and system-stack tokens — not typefaces anyone chose. Named
# fonts (Roboto, Helvetica, Arial) are deliberately NOT here: a site using one
# has made a real choice and it should be counted.
SYSTEM_TOKENS = {
    "serif", "sans-serif", "monospace", "cursive", "fantasy", "system-ui",
    "ui-sans-serif", "ui-serif", "ui-monospace", "ui-rounded", "-apple-system",
    "blinkmacsystemfont", "apple color emoji", "segoe ui emoji",
    "segoe ui symbol", "noto color emoji", "emoji", "math",
    "inherit", "initial", "unset", "revert",
}
ICON_FAMILIES = ("fontawesome", "font awesome", "material icons", "material symbols",
                 "glyphicons", "icomoon", "ionicons", "feather")


# --------------------------------------------------------------------------- colour


def hex_to_rgb(value: str) -> tuple[int, int, int]:
    value = value.lstrip("#")
    return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


def rgb_to_hex(rgb: tuple[float, float, float]) -> str:
    return "#" + "".join(f"{max(0, min(255, int(round(c)))):02x}" for c in rgb)


def relative_luminance(rgb: tuple[int, int, int]) -> float:
    channels = []
    for c in rgb:
        s = c / 255
        channels.append(s / 12.92 if s <= 0.03928 else ((s + 0.055) / 1.055) ** 2.4)
    r, g, b = channels
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio(a: str, b: str) -> float:
    la, lb = relative_luminance(hex_to_rgb(a)), relative_luminance(hex_to_rgb(b))
    lighter, darker = max(la, lb), min(la, lb)
    return (lighter + 0.05) / (darker + 0.05)


def to_hsl(hex_value: str) -> tuple[float, float, float]:
    r, g, b = (c / 255 for c in hex_to_rgb(hex_value))
    h, l, s = colorsys.rgb_to_hls(r, g, b)
    return h * 360, s * 100, l * 100


def colour_distance(a: str, b: str) -> float:
    """Weighted RGB distance — close enough to perceptual for palette clustering."""
    (r1, g1, b1), (r2, g2, b2) = hex_to_rgb(a), hex_to_rgb(b)
    rmean = (r1 + r2) / 2
    dr, dg, db = r1 - r2, g1 - g2, b1 - b2
    return math.sqrt(
        (2 + rmean / 256) * dr * dr + 4 * dg * dg + (2 + (255 - rmean) / 256) * db * db
    )


@dataclass
class Swatch:
    hex: str
    weight: float
    role: str
    text_chars: int = 0
    background_area: int = 0
    border_uses: int = 0
    members: list[str] = field(default_factory=list)

    @property
    def hsl(self) -> tuple[float, float, float]:
        return to_hsl(self.hex)

    @property
    def chroma(self) -> int:
        """Spread between the RGB channels — near-whites and near-blacks score low."""
        rgb = hex_to_rgb(self.hex)
        return max(rgb) - min(rgb)

    @property
    def is_neutral(self) -> bool:
        return self.chroma < 18

    @property
    def is_muted(self) -> bool:
        """Tinted greys — visible hue, too little saturation to act as a brand colour."""
        return not self.is_neutral and self.hsl[1] < 35

    @property
    def structural_role(self) -> str | None:
        """Ink, surface or filler. Near-black and near-white read structurally even
        when they carry a strong hue — a navy this dark is ink, not a brand colour."""
        lightness = self.hsl[2]
        if lightness < 18:
            return "Ink"
        if lightness > 93:
            return "Surface"
        if self.is_neutral:
            return "Neutral"
        if self.is_muted:
            return "Muted tint"
        return None

    @property
    def usage(self) -> str:
        parts = []
        if self.text_chars:
            parts.append(f"{self.text_chars:,} chars of text")
        if self.background_area:
            parts.append("used as a background")
        if self.border_uses:
            parts.append(f"{self.border_uses} borders")
        return " · ".join(parts) or "minor use"

    @property
    def readable_on(self) -> str:
        return "#ffffff" if relative_luminance(hex_to_rgb(self.hex)) < 0.4 else "#111111"


def build_palette(colour_usage: dict[str, dict[str, int]], threshold: float = 40.0) -> list[Swatch]:
    """Collapse near-identical colours into swatches ranked by visual weight."""
    scored: list[tuple[str, float, dict[str, int]]] = []
    for hex_value, use in colour_usage.items():
        if not re.fullmatch(r"#[0-9a-f]{6}", hex_value):
            continue
        # Text characters and border uses matter far more per-unit than raw area.
        weight = use.get("text", 0) * 6 + math.sqrt(max(use.get("background", 0), 0)) * 3 + use.get("border", 0) * 4
        if weight <= 0:
            continue
        scored.append((hex_value, weight, use))

    scored.sort(key=lambda item: item[1], reverse=True)
    swatches: list[Swatch] = []
    for hex_value, weight, use in scored:
        for swatch in swatches:
            if colour_distance(swatch.hex, hex_value) < threshold:
                swatch.weight += weight
                swatch.text_chars += use.get("text", 0)
                swatch.background_area += use.get("background", 0)
                swatch.border_uses += use.get("border", 0)
                swatch.members.append(hex_value)
                break
        else:
            swatches.append(
                Swatch(
                    hex=hex_value,
                    weight=weight,
                    role="",
                    text_chars=use.get("text", 0),
                    background_area=use.get("background", 0),
                    border_uses=use.get("border", 0),
                    members=[hex_value],
                )
            )

    swatches.sort(key=lambda s: s.weight, reverse=True)
    _assign_roles(swatches)
    return swatches


def _assign_roles(swatches: list[Swatch]) -> None:
    chromatic_seen = 0
    for swatch in swatches:
        structural = swatch.structural_role
        if structural:
            swatch.role = structural
            continue
        chromatic_seen += 1
        if chromatic_seen == 1:
            swatch.role = "Brand / primary"
        elif swatch.text_chars > 0 and swatch.background_area == 0:
            swatch.role = "Accent text"
        else:
            swatch.role = "Accent"


# ------------------------------------------------------------------------ typography


def family_stack(font_family: str) -> list[str]:
    return [part.strip().strip("\"'") for part in font_family.split(",") if part.strip()]


def primary_family(font_family: str) -> str:
    stack = family_stack(font_family)
    return stack[0] if stack else "unknown"


@dataclass
class FamilyUsage:
    name: str
    chars: int = 0
    sizes: Counter = field(default_factory=Counter)
    weights: Counter = field(default_factory=Counter)
    tags: Counter = field(default_factory=Counter)
    has_fallback: bool = False
    is_icon_font: bool = False
    is_system: bool = False

    @property
    def sample_tag(self) -> str:
        return self.tags.most_common(1)[0][0] if self.tags else "—"

    @property
    def size_range(self) -> str:
        if not self.sizes:
            return "—"
        keys = sorted(self.sizes)
        return f"{keys[0]:g}–{keys[-1]:g}px" if len(keys) > 1 else f"{keys[0]:g}px"

    @property
    def weight_list(self) -> str:
        return ", ".join(str(w) for w in sorted(self.weights)) or "—"


def build_typography(text_runs: list[dict[str, Any]]) -> dict[str, Any]:
    families: dict[str, FamilyUsage] = {}
    sizes: Counter = Counter()
    weights: Counter = Counter()
    body_runs = []

    for run in text_runs:
        name = primary_family(run["font_family"])
        key = name.lower()
        usage = families.setdefault(key, FamilyUsage(name=name))
        usage.chars += run["chars"]
        usage.sizes[round(run["font_size"])] += run["chars"]
        usage.weights[run["font_weight"]] += run["chars"]
        usage.tags[run["tag"]] += run["chars"]
        usage.has_fallback = usage.has_fallback or len(family_stack(run["font_family"])) > 1
        usage.is_icon_font = usage.is_icon_font or any(i in key for i in ICON_FAMILIES)
        usage.is_system = key in SYSTEM_TOKENS

        sizes[round(run["font_size"])] += run["chars"]
        weights[run["font_weight"]] += run["chars"]
        if run["tag"] in {"p", "li", "span", "div", "td", "dd", "blockquote"} and run["chars"] >= 60:
            body_runs.append(run)

    dominant = [(size, chars) for size, chars in sizes.items() if size < 24]
    body_size = max(dominant, key=lambda kv: kv[1])[0] if dominant else None

    ranked = sorted(families.values(), key=lambda f: f.chars, reverse=True)
    # A system stack and an icon font are not typefaces a designer picked, and
    # counting them inflates every "how many typefaces" number in the report.
    content_families = [f for f in ranked if not f.is_icon_font and not f.is_system]

    body = None
    if body_runs:
        body_runs.sort(key=lambda r: r["chars"], reverse=True)
        body = body_runs[0]

    total_chars = sum(f.chars for f in ranked) or 1
    return {
        "families": ranked,
        "content_families": content_families,
        "sizes": sizes,
        "weights": weights,
        "distinct_sizes": len(sizes),
        "distinct_weights": len(weights),
        "body_run": body,
        "body_size": body_size,
        "body_runs": body_runs,
        "total_chars": total_chars,
        "scale_steps": sorted(sizes),
    }


# ------------------------------------------------------------------------- contrast


def contrast_issues(text_runs: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """Failing pairs, plus the number of text runs whose real background was unmeasurable."""
    seen: dict[tuple[str, str, float, int], dict[str, Any]] = {}
    unverified = 0
    for run in text_runs:
        if run["chars"] < 2:
            continue
        # Text over imagery, or text covered by an overlay: the effective background is
        # not one colour, so any ratio computed from it would be fiction.
        if run["background_unverified"] or run["color"] == run["background"]:
            unverified += 1
            continue
        key = (run["color"], run["background"], run["font_size"], run["font_weight"])
        ratio = contrast_ratio(run["color"], run["background"])
        required = 3.0 if run["is_large_text"] else 4.5
        if ratio >= required:
            continue
        entry = seen.setdefault(
            key,
            {
                "color": run["color"],
                "background": run["background"],
                "ratio": round(ratio, 2),
                "required": required,
                "font_size": run["font_size"],
                "font_weight": run["font_weight"],
                "chars": 0,
                "sample": run["sample"],
                "tags": set(),
            },
        )
        entry["chars"] += run["chars"]
        entry["tags"].add(run["tag"])
        if len(run["sample"]) > len(entry["sample"]):
            entry["sample"] = run["sample"]
    issues = sorted(seen.values(), key=lambda i: (i["ratio"], -i["chars"]))
    for issue in issues:
        issue["tags"] = ", ".join(sorted(issue["tags"]))
        issue["suggestion"] = suggest_accessible_colour(issue["color"], issue["background"], issue["required"])
    return issues, unverified


def suggest_accessible_colour(foreground: str, background: str, required: float) -> str | None:
    """Darken/lighten the foreground in HSL until it clears the required ratio."""
    h, s, l = to_hsl(foreground)
    bg_light = relative_luminance(hex_to_rgb(background)) > 0.4
    step = -2 if bg_light else 2
    lightness = l
    for _ in range(50):
        lightness += step
        if not 0 <= lightness <= 100:
            break
        r, g, b = colorsys.hls_to_rgb(h / 360, lightness / 100, s / 100)
        candidate = rgb_to_hex((r * 255, g * 255, b * 255))
        if contrast_ratio(candidate, background) >= required:
            return candidate
    return "#000000" if bg_light else "#ffffff"


# ------------------------------------------------------------------------- findings


@dataclass
class Finding:
    id: str
    category: str
    severity: str
    title: str
    detail: str
    evidence: list[str]
    fix: str
    automation: str


def _fmt_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB"):
        if n < 1024 or unit == "MB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} MB"


def analyse(data) -> dict[str, Any]:  # noqa: C901 - a rules table, deliberately flat
    probe = data.probe
    palette = build_palette(probe["color_usage"])
    typography = build_typography(probe["text_runs"])
    contrast, unverified_runs = contrast_issues(probe["text_runs"])
    findings: list[Finding] = []

    def add(**kwargs):
        findings.append(Finding(**kwargs))

    # ---- resources
    by_type: dict[str, dict[str, float]] = defaultdict(lambda: {"count": 0, "bytes": 0})
    for res in data.resources:
        bucket = by_type[res["type"]]
        bucket["count"] += 1
        bucket["bytes"] += res["bytes"]
    total_bytes = sum(b["bytes"] for b in by_type.values())
    font_files = by_type.get("font", {"count": 0, "bytes": 0})
    image_bytes = by_type.get("image", {"count": 0, "bytes": 0})["bytes"]

    # ---- typography findings
    content_families = typography["content_families"]
    significant = [f for f in content_families if f.chars >= typography["total_chars"] * 0.02]
    if len(significant) > 3:
        add(
            id="type-family-sprawl",
            category="Typography",
            severity="high" if len(significant) > 4 else "medium",
            title=f"{len(significant)} typefaces are doing real work on one page",
            detail=(
                "A page normally needs two families (one for headings, one for body) plus an optional "
                "monospace. Each extra family costs another font download and blurs the brand voice."
            ),
            evidence=[f"{f.name} — {f.chars:,} characters, mostly <{f.sample_tag}>" for f in significant[:6]],
            fix="Pick one display family and one text family, then map every other usage onto them.",
            automation=(
                "Generate a `--font-display` / `--font-text` token pair and run a CSS codemod that rewrites "
                "every literal font-family declaration to the tokens; a stylelint rule keeps new ones out."
            ),
        )

    no_fallback = [f for f in content_families if not f.has_fallback and f.chars > 200]
    if no_fallback:
        add(
            id="type-no-fallback",
            category="Typography",
            severity="medium",
            title="Font stacks have no fallback family",
            detail=(
                "If the web font fails or is still loading, the browser falls back to its default serif, "
                "which reflows the layout and looks broken."
            ),
            evidence=[f"{f.name} is declared without a fallback ({f.chars:,} characters)" for f in no_fallback[:4]],
            fix="Append a metric-compatible fallback and a generic family to every stack.",
            automation="A build-time PostCSS plugin can append fallbacks to every font-family declaration automatically.",
        )

    body = typography["body_run"]
    body_size = typography["body_size"]
    if body_size and body_size < 16:
        share = 100 * typography["sizes"][body_size] / typography["total_chars"]
        evidence = [f"{body_size}px carries {share:.0f}% of the page's text"]
        if body:
            evidence.append(f"<{body['tag']}> at {body['font_size']:g}px: “{body['sample'][:70]}…”")
        add(
            id="type-small-body",
            category="Typography",
            severity="high" if body_size < 14 else "medium",
            title=f"Most text renders at {body_size:g}px",
            detail="Sustained reading needs 16–18px. Below 16px, mobile Safari also zooms form fields on focus.",
            evidence=evidence,
            fix="Set the body size to 1rem (16px) minimum and scale headings from there.",
            automation="A fluid type scale (`clamp()`) generated from one base size removes per-breakpoint size overrides.",
        )

    if body:
        lh = body["line_height"]
        if lh and body["font_size"]:
            ratio = lh / body["font_size"]
            if ratio < 1.35 or ratio > 1.9:
                add(
                    id="type-line-height",
                    category="Typography",
                    severity="medium" if ratio < 1.25 else "low",
                    title=f"Body line-height is {ratio:.2f}× the font size",
                    detail="Comfortable reading sits between 1.4 and 1.7. Tight leading makes paragraphs feel dense; loose leading breaks them apart.",
                    evidence=[f"{body['font_size']:g}px text with a {lh:g}px line box (<{body['tag']}>)"],
                    fix="Set a unitless line-height of 1.5 on body copy and 1.15–1.25 on large headings.",
                    automation="Bake the two values into the type tokens so no component sets line-height by hand.",
                )

    wide = [r for r in typography["body_runs"] if r["width"] > 0 and r["chars"] > 120]
    if wide:
        widest = max(wide, key=lambda r: r["width"])
        approx_cpl = widest["width"] / (widest["font_size"] * 0.5)
        if approx_cpl > 95:
            add(
                id="type-measure",
                category="Typography",
                severity="medium",
                title=f"Paragraphs run to roughly {approx_cpl:.0f} characters per line",
                detail="Past ~75 characters the eye loses its place returning to the next line.",
                evidence=[f"A {widest['width']}px wide <{widest['tag']}> at {widest['font_size']:g}px"],
                fix="Cap prose containers at 60–75ch (`max-width: 68ch`).",
                automation="One `.prose` utility with a ch-based max-width fixes every long-form block at once.",
            )

    if typography["distinct_sizes"] > 12:
        add(
            id="type-scale-sprawl",
            category="Consistency",
            severity="medium",
            title=f"{typography['distinct_sizes']} distinct font sizes are in use",
            detail="A deliberate type scale has 6–8 steps. More than that usually means sizes were nudged per component.",
            evidence=[f"Sizes found: {', '.join(f'{s:g}px' for s in typography['scale_steps'][:18])}"],
            fix="Round every size onto a modular scale and expose the steps as tokens.",
            automation=(
                "A script can read the computed sizes, snap each to the nearest scale step, and emit the CSS "
                "variable patch as a reviewable diff."
            ),
        )

    faces_without_display = [f for f in probe["face_rules"] if not f.get("display")]
    if font_files["count"] and faces_without_display:
        add(
            id="perf-font-display",
            category="Performance",
            severity="medium",
            title="Web fonts load without `font-display`",
            detail="Without it, Chrome blocks text painting for up to 3 seconds while the font downloads.",
            evidence=[
                f"{len(faces_without_display)} @font-face rule(s) with no font-display",
                f"{int(font_files['count'])} font file(s), {_fmt_bytes(font_files['bytes'])}",
            ],
            fix="Add `font-display: swap;` to every @font-face rule and preload the one used above the fold.",
            automation="A PostCSS plugin injects font-display into every @font-face at build time.",
        )

    if font_files["count"] > 4:
        add(
            id="perf-font-weight-count",
            category="Performance",
            severity="medium" if font_files["count"] <= 8 else "high",
            title=f"{int(font_files['count'])} font files downloaded ({_fmt_bytes(font_files['bytes'])})",
            detail="Every weight and style is a separate download on first paint.",
            evidence=[f"{int(font_files['count'])} font requests totalling {_fmt_bytes(font_files['bytes'])}"],
            fix="Ship one variable font per family, or trim to the 2–3 weights actually used.",
            automation="Subset and convert fonts to woff2 in CI (`glyphhanger` / `fonttools`) and preload the critical face.",
        )

    # ---- colour findings
    accents = [s for s in palette if s.structural_role is None]
    tints = [s for s in palette if s.is_muted]
    if len(accents) > 6:
        add(
            id="colour-palette-bloat",
            category="Colour",
            severity="medium",
            title=f"{len(accents)} distinct non-neutral colours are in play",
            detail="A coherent palette is one brand colour, one or two accents, and a neutral ramp. More reads as drift between components.",
            evidence=[f"{s.hex} ({s.role.lower()})" for s in accents[:8]]
            + ([f"plus {len(tints)} muted tints and {len(palette) - len(accents) - len(tints)} neutrals"] if tints else []),
            fix="Collapse the accents onto a single brand hue plus semantic colours for success/warning/danger.",
            automation="Cluster the extracted colours and emit a `tokens.css` custom-property set, then codemod literals to `var(--…)`.",
        )

    near_duplicates = [s for s in palette if len(s.members) > 2]
    if near_duplicates:
        add(
            id="colour-near-duplicates",
            category="Consistency",
            severity="low" if len(near_duplicates) < 4 else "medium",
            title="Near-identical colours are defined more than once",
            detail="Slightly different shades of the same colour are almost always copy-paste drift, not a design decision.",
            evidence=[f"{s.hex} also appears as {', '.join(s.members[1:4])}" for s in near_duplicates[:5]],
            fix="Pick the canonical value for each cluster and delete the variants.",
            automation="The same extraction can produce a find-and-replace map, applied as a single reviewable commit.",
        )

    if (
        probe["css_var_count"] < 10
        and not probe["inaccessible_sheets"]
        and (len(accents) > 3 or typography["distinct_sizes"] > 8)
    ):
        add(
            id="colour-no-tokens",
            category="Consistency",
            severity="medium",
            title="Colours and sizes are hard-coded rather than tokenised",
            detail=(
                f"Only {probe['css_var_count']} CSS custom properties were found across the stylesheets, yet the page uses "
                f"{len(palette)} colour clusters and {typography['distinct_sizes']} font sizes."
            ),
            evidence=[
                f"{probe['css_var_count']} custom properties declared",
                f"{probe['inline_style_attrs']} elements carry inline style attributes",
            ],
            fix="Introduce a token layer (`--color-*`, `--font-*`, `--space-*`) and reference it everywhere.",
            automation="Generate the token file from this audit's palette, then run a codemod to replace literal values.",
        )

    if contrast:
        worst = contrast[0]
        affected = sum(i["chars"] for i in contrast)
        add(
            id="a11y-contrast",
            category="Accessibility",
            severity="critical" if worst["ratio"] < 3 else "high",
            title=f"{len(contrast)} text/background pairs fail WCAG AA contrast",
            detail=(
                f"About {affected:,} characters of visible text sit below the required ratio. This is the single most "
                "common accessibility failure and it is measurable, so it can be enforced automatically."
            ),
            evidence=[
                f"{i['color']} on {i['background']} — {i['ratio']}:1 (needs {i['required']}:1), {i['chars']:,} chars in <{i['tags']}>"
                for i in contrast[:6]
            ],
            fix="Darken or lighten the foreground until every pair clears 4.5:1 (3:1 for text ≥24px or bold ≥18.7px).",
            automation=(
                "Add a contrast check to CI (axe-core / pa11y on key routes) so a failing colour never reaches production; "
                "this report already lists a passing replacement for each pair."
            ),
        )

    if not probe["prefers_color_scheme"] and not probe["color_scheme_meta"]:
        add(
            id="colour-no-dark-mode",
            category="Colour",
            severity="low",
            title="No dark-mode support detected",
            detail="No `prefers-color-scheme` rules and no `color-scheme` declaration were found, so the page ignores the visitor's system setting.",
            evidence=[f"{probe['media_query_count']} media queries scanned, none matching prefers-color-scheme"],
            fix="Define the palette as tokens once, then override only the tokens inside a dark-scheme media query.",
            automation="A tokenised palette makes dark mode a ~20-line override rather than a redesign.",
        )

    # ---- accessibility / structure
    images = probe["images"]
    if images["missing_alt"]:
        add(
            id="a11y-alt-text",
            category="Accessibility",
            severity="high" if images["missing_alt"] > 3 else "medium",
            title=f"{images['missing_alt']} image(s) have no alt attribute",
            detail="Screen readers announce the filename instead, and the content is invisible to search engines.",
            evidence=[f"{images['missing_alt']} of {images['total']} images lack alt (decorative images should use alt=\"\")"],
            fix="Write alt text for meaningful images and `alt=\"\"` for decorative ones.",
            automation="A CI lint rule blocks new <img> without alt; a vision model can draft alt text for the existing backlog for human review.",
        )

    h1s = [h for h in probe["headings"] if h["level"] == 1]
    if len(h1s) != 1:
        add(
            id="a11y-h1",
            category="Accessibility",
            severity="medium",
            title=f"Page has {len(h1s)} <h1> headings",
            detail="Exactly one h1 anchors the document outline for assistive tech and search engines.",
            evidence=[f"h1 text: “{h['text']}”" for h in h1s[:4]] or ["No h1 element found"],
            fix="Use a single h1 for the page subject and demote the rest to h2/h3.",
            automation="A heading-order assertion in the accessibility test suite catches regressions on every deploy.",
        )

    levels = [h["level"] for h in probe["headings"]]
    skips = [(a, b) for a, b in zip(levels, levels[1:]) if b > a + 1]
    if skips:
        add(
            id="a11y-heading-order",
            category="Accessibility",
            severity="low",
            title="Heading levels skip steps",
            detail="Jumping from h2 to h4 breaks the outline screen-reader users navigate by.",
            evidence=[f"h{a} followed by h{b}" for a, b in skips[:5]],
            fix="Choose heading levels by document structure and style them with classes instead.",
            automation="Same accessibility suite as above — this is a static rule, no judgement needed.",
        )

    if not probe["lang"]:
        add(
            id="a11y-lang",
            category="Accessibility",
            severity="medium",
            title="<html> has no lang attribute",
            detail="Screen readers pick a pronunciation voice from it; without it they guess.",
            evidence=["No lang attribute on the root element"],
            fix='Add `lang="en"` (or the correct language) to the <html> tag.',
            automation="One-line template fix, then assert it in the smoke test.",
        )

    if probe["small_targets"] > 2:
        add(
            id="a11y-tap-targets",
            category="Accessibility",
            severity="medium",
            title=f"{probe['small_targets']} interactive elements are smaller than 24×24px",
            detail="WCAG 2.2 asks for a 24px minimum target; 44px is the comfortable touch size.",
            evidence=[f"{probe['small_targets']} of {probe['interactive_count']} interactive elements are under 24px"],
            fix="Give small controls padding or an invisible expanded hit area.",
            automation="A single utility class plus an automated check over rendered pages keeps this from recurring.",
        )

    # ---- responsive
    if not probe["viewport_meta"]:
        add(
            id="resp-viewport",
            category="Responsive",
            severity="critical",
            title="No viewport meta tag",
            detail="Mobile browsers render the page at desktop width and zoom out, making all text tiny.",
            evidence=["<meta name=\"viewport\"> is absent"],
            fix='Add `<meta name="viewport" content="width=device-width, initial-scale=1">`.',
            automation="Add it to the base template so every page inherits it.",
        )

    mobile = data.mobile
    if mobile and mobile["scroll_width"] > mobile["doc_width"] + 4:
        overflow = mobile["scroll_width"] - mobile["doc_width"]
        add(
            id="resp-overflow",
            category="Responsive",
            severity="high",
            title=f"Page scrolls {overflow}px sideways at 390px wide",
            detail="Horizontal scroll on mobile is almost always one fixed-width element escaping its container.",
            evidence=[f"Document is {mobile['doc_width']}px, content is {mobile['scroll_width']}px"]
            + [f"<{o['tag']}{(' class=' + o['cls']) if o['cls'] else ''}> reaches {o['right']}px" for o in probe["overflowing"][:4]],
            fix="Find the offending element and give it `max-width: 100%`; avoid fixed pixel widths above 320px.",
            automation="A Playwright check that asserts `scrollWidth <= clientWidth` at 3 breakpoints catches this on every PR.",
        )

    # ---- performance
    if total_bytes > 2_500_000:
        add(
            id="perf-weight",
            category="Performance",
            severity="high" if total_bytes > 5_000_000 else "medium",
            title=f"Page weighs {_fmt_bytes(total_bytes)} across {len(data.resources)} requests",
            detail="Above ~2.5MB, first load gets slow on mid-range mobile connections.",
            evidence=[
                f"{kind}: {int(v['count'])} requests, {_fmt_bytes(v['bytes'])}"
                for kind, v in sorted(by_type.items(), key=lambda kv: kv[1]["bytes"], reverse=True)[:5]
            ],
            fix="Compress images, split JavaScript bundles, and defer anything not needed for first paint.",
            automation="Add a bundle-size / Lighthouse budget check to CI so weight regressions fail the build.",
        )

    if images["oversized"]:
        add(
            id="perf-oversized-images",
            category="Performance",
            severity="medium",
            title=f"{len(images['oversized'])} image(s) are served far larger than they display",
            detail="The browser downloads the full resolution and then throws most of the pixels away.",
            evidence=[
                f"{o['src']}: {o['natural']}px downloaded, {o['displayed']}px displayed" for o in images["oversized"][:5]
            ],
            fix="Serve responsive `srcset` sizes, or resize the source assets to ~2× the display width.",
            automation="An image pipeline (sharp / an image CDN) can generate srcset variants and modern formats on upload.",
        )

    if images["total"] and images["modern_format"] == 0 and image_bytes > 400_000:
        add(
            id="perf-image-format",
            category="Performance",
            severity="medium",
            title="No WebP/AVIF images detected",
            detail=f"Images account for {_fmt_bytes(image_bytes)}; modern formats typically cut that by 30–50% at the same quality.",
            evidence=[f"{images['total']} images, none in WebP or AVIF"],
            fix="Convert to WebP with a JPEG/PNG fallback via <picture>.",
            automation="A one-off conversion script plus a build step for new uploads; no design decisions involved.",
        )

    if images["total"] > 6 and images["lazy"] == 0:
        add(
            id="perf-lazy-loading",
            category="Performance",
            severity="low",
            title="Below-the-fold images are not lazy-loaded",
            detail="Every image competes for bandwidth with the content the visitor can actually see.",
            evidence=[f"{images['total']} images, none using loading=\"lazy\""],
            fix='Add `loading="lazy"` to images below the fold (never to the hero image).',
            automation="A template-level default plus a lint rule handles this permanently.",
        )

    if images["no_dimensions"] > 2:
        add(
            id="perf-cls",
            category="Performance",
            severity="medium",
            title=f"{images['no_dimensions']} image(s) have no width/height attributes",
            detail="Without intrinsic dimensions the browser cannot reserve space, so content jumps as images arrive (layout shift).",
            evidence=[f"{images['no_dimensions']} of {images['total']} images lack explicit dimensions"],
            fix="Set width and height attributes (or an aspect-ratio) on every image.",
            automation="A build step can read each image's real dimensions and inject the attributes automatically.",
        )

    render_blocking = [s for s in probe["stylesheets"] if s["in_head"] and s["media"] in ("all", "screen", "")]
    if len(render_blocking) > 4:
        add(
            id="perf-render-blocking-css",
            category="Performance",
            severity="medium",
            title=f"{len(render_blocking)} render-blocking stylesheets in <head>",
            detail="Each one delays first paint by a full network round trip.",
            evidence=[s["href"].split("/")[-1][:60] for s in render_blocking[:6]],
            fix="Bundle them, inline critical CSS, and load the rest asynchronously.",
            automation="A bundler step merges and minifies stylesheets; a critical-CSS plugin inlines above-the-fold rules.",
        )

    if data.load_ms > 4000:
        add(
            id="perf-load-time",
            category="Performance",
            severity="medium" if data.load_ms < 8000 else "high",
            title=f"DOM took {data.load_ms / 1000:.1f}s to become interactive",
            detail=(
                "One cold, uncached measurement from the machine running the audit — a signal to investigate, "
                "not a benchmark. Network conditions between that machine and the site are part of the number."
            ),
            evidence=[f"{data.load_ms} ms to DOMContentLoaded, {len(data.resources)} requests"],
            fix="Profile with Lighthouse; the usual culprits are blocking scripts and uncompressed media.",
            automation="Schedule a nightly Lighthouse run and alert on regressions against a budget.",
        )

    # ---- SEO / meta
    if not probe["meta_description"]:
        add(
            id="seo-description",
            category="Findability",
            severity="medium",
            title="No meta description",
            detail="Search engines and link previews fall back to scraping random body text.",
            evidence=["<meta name=\"description\"> is absent"],
            fix="Write a 150–160 character summary of the page.",
            automation="Template the description from page data, with a lint check for length.",
        )
    elif len(probe["meta_description"]) > 165:
        add(
            id="seo-description-length",
            category="Findability",
            severity="low",
            title=f"Meta description is {len(probe['meta_description'])} characters",
            detail="Google truncates around 160 characters, so the tail is wasted.",
            evidence=[probe["meta_description"][:120] + "…"],
            fix="Trim to 150–160 characters, front-loading the value proposition.",
            automation="Length assertion in the same meta-tag lint rule.",
        )

    if not probe["og_image"] or not probe["og_title"]:
        missing = [name for name, present in (("og:title", probe["og_title"]), ("og:image", probe["og_image"])) if not present]
        add(
            id="seo-open-graph",
            category="Findability",
            severity="low",
            title=f"Missing Open Graph tags ({', '.join(missing)})",
            detail="Links shared to Slack, LinkedIn or iMessage render as a bare URL instead of a card.",
            evidence=[f"{m} not found" for m in missing],
            fix="Add og:title, og:description and a 1200×630 og:image.",
            automation="Generate the share image from page data at build time — one template, every page covered.",
        )

    if not probe["title"]:
        add(
            id="seo-title",
            category="Findability",
            severity="high",
            title="Page has no <title>",
            detail="The title is the primary signal for search results, browser tabs and bookmarks.",
            evidence=["<title> is empty or absent"],
            fix="Add a unique, descriptive title under 60 characters.",
            automation="Assert a non-empty title in the smoke test for every route.",
        )

    if not probe["favicon"]:
        add(
            id="seo-favicon",
            category="Findability",
            severity="low",
            title="No favicon declared",
            detail="Browsers show a generic placeholder in tabs and bookmarks.",
            evidence=["No <link rel=\"icon\"> found"],
            fix="Add a favicon and an apple-touch-icon.",
            automation="A favicon generator produces every required size from one source image.",
        )

    # ---- findability: what machines can read
    probe_files = getattr(data, "site_files", None)
    json_ld = probe.get("json_ld") or []
    schema_types = structured_data_types(json_ld)

    if probe.get("noindex"):
        add(
            id="find-noindex",
            category="Findability",
            severity="critical",
            title="This page tells search engines not to index it",
            detail=(
                "A `noindex` directive is in the page's robots meta tag. If that is left over "
                "from a staging environment, the page is invisible in search and no amount of "
                "other optimisation will change that."
            ),
            evidence=[f"meta robots: {probe.get('robots_meta')}"],
            fix="Remove `noindex` unless the page is deliberately hidden.",
            automation="Assert no noindex on production routes in the deploy smoke test.",
        )

    dependency = js_dependency(getattr(data, "raw_html", ""), probe.get("body_text") or "")
    if dependency.measurable and dependency.ratio < 0.6:
        share = round(100 * (1 - dependency.ratio))
        add(
            id="find-js-dependency",
            category="Findability",
            severity="high" if dependency.ratio < 0.3 else "medium",
            title=f"{share}% of the page's text only appears after JavaScript runs",
            detail=(
                "A browser runs the scripts; many crawlers and most AI answer engines do not. "
                "They see the HTML as served. The rendered page looks complete, which is why "
                "this is invisible to everyone until someone checks."
            ),
            evidence=[
                f"{dependency.raw_words:,} words in the served HTML",
                f"{dependency.rendered_words:,} words once the page finished rendering",
            ],
            fix=(
                "Server-render or pre-render the main content so it is present in the initial "
                "HTML response."
            ),
            automation=(
                "Assert a minimum word count in the raw response for key routes — it catches "
                "the regression the moment a component moves client-side."
            ),
        )

    if not schema_types:
        add(
            id="find-no-structured-data",
            category="Findability",
            severity="medium",
            title="No structured data on the page",
            detail=(
                "Schema.org markup is how a search or answer engine knows what the page is "
                "about rather than guessing from prose. Without it the page competes on text alone."
            ),
            evidence=["No <script type=\"application/ld+json\"> blocks found"],
            fix="Add JSON-LD describing the organisation, and the page's own type where one fits.",
            automation="Generate the JSON-LD from the same data the page renders from.",
        )
    elif not has_identity_schema(schema_types):
        add(
            id="find-no-identity-schema",
            category="Findability",
            severity="low",
            title="Structured data does not identify the business",
            detail=(
                "Schema is present but none of it says who this is. An Organization or "
                "LocalBusiness block is what links the site to a knowledge panel."
            ),
            evidence=[f"Types found: {', '.join(schema_types[:8])}"],
            fix="Add an Organization (or LocalBusiness) block with name, logo, URL and contact details.",
            automation="One shared JSON-LD partial in the base template covers every page.",
        )

    question_headings = [h for h in probe.get("headings", []) if "?" in (h.get("text") or "")]
    if question_headings and not any(t.lower() == "faqpage" for t in schema_types):
        add(
            id="find-faq-not-marked-up",
            category="Findability",
            severity="low",
            title=f"{len(question_headings)} question-style heading(s) are not marked up as FAQ",
            detail=(
                "The page already answers questions. FAQPage markup is what lets an answer "
                "engine quote those answers directly."
            ),
            evidence=[h["text"][:70] for h in question_headings[:4]],
            fix="Wrap the question and answer pairs in FAQPage JSON-LD.",
            automation="Generate the markup from the same content that renders the FAQ.",
        )

    if not probe.get("canonical"):
        add(
            id="find-no-canonical",
            category="Findability",
            severity="low",
            title="No canonical URL declared",
            detail=(
                "Without a canonical tag, the same page reached via different URLs "
                "(trailing slash, tracking parameters, http vs https) can be treated as "
                "several competing pages."
            ),
            evidence=["No <link rel=\"canonical\"> found"],
            fix="Add a self-referencing canonical to every page.",
            automation="One line in the base template.",
        )

    if probe_files is not None:
        if not probe_files.robots_txt:
            add(
                id="find-no-robots",
                category="Findability",
                severity="low",
                title="No robots.txt",
                detail="Crawlers request it first. Its absence is not fatal, but it is where you point them at the sitemap.",
                evidence=["/robots.txt did not return a usable file"],
                fix="Add a robots.txt with a Sitemap: line.",
                automation="Static file, generated at build time.",
            )
        if not probe_files.sitemap:
            add(
                id="find-no-sitemap",
                category="Findability",
                severity="medium",
                title="No sitemap found",
                detail=(
                    "A sitemap is how a crawler learns about pages that are not well linked. "
                    "Without one, discovery depends entirely on your internal linking."
                ),
                evidence=["Neither robots.txt nor the usual paths produced a sitemap"],
                fix="Publish sitemap.xml and reference it from robots.txt.",
                automation="Most site builders generate one; it usually needs enabling, not writing.",
            )
        if not probe_files.llms_txt:
            add(
                id="find-no-llms-txt",
                category="Findability",
                severity="low",
                title="No llms.txt",
                detail=(
                    "An emerging convention: a plain-text file telling AI assistants what the "
                    "site is and which pages matter. Early, optional, and cheap to add."
                ),
                evidence=["/llms.txt not found"],
                fix="Publish a short llms.txt describing the business and linking key pages.",
                automation="A static file, updated when the site structure changes.",
            )

    if data.console_errors:
        add(
            id="perf-console-errors",
            category="Performance",
            severity="low",
            title=f"{len(data.console_errors)} JavaScript console error(s) on load",
            detail="Errors during load often mean a feature silently failed for the visitor.",
            evidence=[e[:110] for e in data.console_errors[:4]],
            fix="Fix the errors, starting with any thrown before first paint.",
            automation="Fail the end-to-end test run when the console reports an error.",
        )

    from .structure import page_findings as structure_findings

    findings.extend(structure_findings(probe, data.final_url))

    order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    findings.sort(key=lambda f: (order[f.severity], f.category))

    return {
        "palette": palette,
        "typography": typography,
        "contrast_issues": contrast,
        "contrast_unverified": unverified_runs,
        "findings": findings,
        "scores": score(findings),
        "resource_summary": {k: dict(v) for k, v in by_type.items()},
        "total_bytes": total_bytes,
    }


CATEGORIES = [
    "Typography", "Colour", "Accessibility", "Performance",
    "Consistency", "Responsive", "Findability", "Structure",
]


# Categories weighted by consequence to the business, not by how much of the
# code measures them. Adding checks to a category must not make it count for
# more.
CATEGORY_WEIGHT = {
    "Accessibility": 1.5,   # legal exposure, and real people shut out
    "Content": 1.5,         # placeholder copy and contradictions cost credibility at a glance
    "Findability": 1.25,    # unreadable to crawlers means the rest never gets seen
    "Performance": 1.25,    # measurably changes whether visitors stay
    "Responsive": 1.0,
    "Structure": 1.0,
    "Typography": 0.75,     # craft: it matters, but visitors rarely name it
    "Colour": 0.75,
    "Consistency": 0.75,    # token hygiene: invisible to visitors, expensive for the team
}

# How fast a category's score falls as findings accumulate. Subtracting a fixed
# amount per finding pinned busy categories at zero, after which further
# problems were free; this curve keeps every finding costing something.
PENALTY_HALF_LIFE = 55.0


def category_score(penalty: float) -> int:
    return round(100 * (1 - penalty / (penalty + PENALTY_HALF_LIFE)))


def score(findings: list[Finding], categories: list[str] | None = None) -> dict[str, Any]:
    """Category scores and one headline number.

    Three things the headline has to do: stay at 100 for a clean page, make a
    severe finding impossible to miss, and still rank two bad sites against each
    other. A weighted mean alone fails the second — a single critical lands in
    one category of nine and barely moves the average — so severity also lowers a
    ceiling the score cannot exceed.
    """
    names = categories or CATEGORIES
    penalties: dict[str, float] = {c: 0.0 for c in names}
    for finding in findings:
        penalties[finding.category] = penalties.get(finding.category, 0.0) + SEVERITY_WEIGHT[finding.severity]
    per_category = {name: category_score(p) for name, p in penalties.items()}

    total_weight = sum(CATEGORY_WEIGHT.get(c, 1.0) for c in per_category)
    weighted_mean = sum(
        per_category[c] * CATEGORY_WEIGHT.get(c, 1.0) for c in per_category
    ) / total_weight
    # Still pulled toward the weakest area, so one broken category cannot hide
    # behind eight healthy ones.
    base = 0.75 * weighted_mean + 0.25 * min(per_category.values())

    counts = Counter(f.severity for f in findings)
    ceiling = 100
    if counts["critical"]:
        ceiling -= 30 + 6 * (counts["critical"] - 1)
    ceiling -= 8 * min(counts["high"], 2) + 3 * max(0, counts["high"] - 2)

    overall = max(0, min(round(base), ceiling))
    return {
        "categories": per_category,
        "overall": overall,
        "grade": grade(overall),
        "counts": counts,
    }


def grade(value: int) -> str:
    for threshold, letter in ((90, "A"), (80, "B"), (70, "C"), (60, "D")):
        if value >= threshold:
            return letter
    return "F"


# --------------------------------------------------------------- automation roadmap

# How much work it is to automate the fix, not to make it once by hand.
EFFORT = {
    "a11y-contrast": ("Low", "axe-core / pa11y in CI"),
    "a11y-alt-text": ("Low", "eslint-plugin-jsx-a11y or an HTML linter"),
    "a11y-lang": ("Low", "Base template + smoke test"),
    "a11y-h1": ("Low", "Accessibility test suite"),
    "a11y-heading-order": ("Low", "Accessibility test suite"),
    "a11y-tap-targets": ("Medium", "Playwright rendered-size assertion"),
    "type-family-sprawl": ("Medium", "CSS codemod + stylelint rule"),
    "type-no-fallback": ("Low", "PostCSS plugin"),
    "type-small-body": ("Low", "Type tokens + clamp() scale"),
    "type-line-height": ("Low", "Type tokens"),
    "type-measure": ("Low", "Single .prose utility"),
    "type-scale-sprawl": ("Medium", "Size-snapping codemod"),
    "colour-palette-bloat": ("Medium", "Token generation + codemod"),
    "colour-near-duplicates": ("Low", "Generated find-and-replace map"),
    "colour-no-tokens": ("Medium", "tokens.css generator + codemod"),
    "colour-no-dark-mode": ("Medium", "Token overrides in a media query"),
    "perf-font-display": ("Low", "PostCSS plugin"),
    "perf-font-weight-count": ("Medium", "glyphhanger / fonttools in CI"),
    "perf-oversized-images": ("Medium", "sharp pipeline or image CDN"),
    "perf-image-format": ("Low", "Batch conversion + build step"),
    "perf-lazy-loading": ("Low", "Template default + lint rule"),
    "perf-cls": ("Low", "Build-time dimension injection"),
    "perf-render-blocking-css": ("Medium", "Bundler + critical-CSS plugin"),
    "perf-weight": ("Medium", "Lighthouse CI budget"),
    "perf-load-time": ("Medium", "Scheduled Lighthouse run"),
    "perf-console-errors": ("Low", "Fail E2E run on console errors"),
    "resp-viewport": ("Low", "Base template"),
    "resp-overflow": ("Low", "Playwright breakpoint assertion"),
    "seo-description": ("Low", "Meta-tag lint rule"),
    "seo-description-length": ("Low", "Meta-tag lint rule"),
    "seo-open-graph": ("Medium", "Build-time share-image generator"),
    "seo-title": ("Low", "Route smoke test"),
    "seo-favicon": ("Low", "Favicon generator"),
}

IMPACT = {"critical": "Critical", "high": "High", "medium": "Medium", "low": "Low"}


def automation_roadmap(findings: list[Finding]) -> list[dict[str, str]]:
    """Rank the findings a machine can fix or police, highest impact and lowest effort first."""
    effort_rank = {"Low": 0, "Medium": 1, "High": 2}
    severity_rank = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    rows = []
    for finding in findings:
        effort, tooling = EFFORT.get(finding.id, ("Medium", "Custom script"))
        rows.append(
            {
                "title": finding.title,
                "category": finding.category,
                "impact": IMPACT[finding.severity],
                "severity": finding.severity,
                "effort": effort,
                "tooling": tooling,
                "automation": finding.automation,
            }
        )
    rows.sort(key=lambda r: (severity_rank[r["severity"]], effort_rank[r["effort"]]))
    return rows
