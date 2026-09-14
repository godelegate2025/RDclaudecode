# Website design audit

Point it at a URL. It loads the page in a real browser, measures the typography,
colour palette, contrast and performance of what actually rendered, and prints a
PDF report that ends with a ranked list of fixes — each labelled with how to
automate it.

```bash
pip install -r requirements.txt
python3 -m playwright install chromium     # skip if Chromium is already installed

python3 -m website_audit example.com
python3 -m website_audit https://example.com/pricing -o reports/pricing.pdf --json
```

The PDF path goes to stdout; a one-line summary goes to stderr:

```
→ Score 79/100 (grade C) · 9 findings · 5 medium, 2 high, 2 low
reports/example-com-audit-2026-09-14.pdf
```

## In Claude Code

`.claude/skills/website-audit/` registers this as a skill, so in a Claude Code
session you can just say:

> audit https://example.com and send me the report

Claude runs the audit, delivers the PDF, and summarises what matters. See
[the skill](.claude/skills/website-audit/SKILL.md) for the workflow it follows
and for scheduling recurring audits.

## What it measures

**Typography** — every font family that rendered text, weighted by how much text
it carries; size range, weights, fallback stacks, icon-font detection, the type
scale, body size, line-height ratio and line length.

**Colour** — every colour used for text, backgrounds and borders, clustered so
near-identical shades collapse into one swatch, each labelled with its role
(surface, ink, neutral, brand, accent) and how it is used.

**Contrast** — the effective background behind every text run (walking ancestors
and blending translucent layers), scored against WCAG AA. Each failing pair comes
with a replacement colour that keeps the original hue and saturation and moves
lightness only until it passes.

**Everything else that is measurable** — viewport meta and horizontal overflow at
390px, alt text, heading order, tap-target sizes, font loading, image weight and
formats, layout-shift risk, render-blocking CSS, page weight, meta and Open Graph
tags, dark-mode support, design-token usage.

Each finding carries a severity, the evidence behind it, the fix, and the
automation that keeps it from coming back. Category scores roll up into one
number, pulled a third of the way toward the weakest category so a single bad
area cannot hide behind six healthy ones.

## Output

- **PDF** — the report, rendered by Chromium so it prints exactly as measured.
- **JSON** (`--json`) — the same findings as data. Diff it between runs to catch
  design drift, or gate a deploy on `scores.categories.Accessibility`.

## Layout

```
website_audit/
  cli.py          argument parsing, output paths
  collector.py    Chromium: navigation, screenshots, network tally
  probe.js        runs in the page — computed styles, colours, structure
  analysis.py     palette clustering, WCAG maths, the findings rules, scoring
  report.py       Jinja → HTML → Chromium print-to-PDF
  templates/      the report layout
tests/            offline end-to-end run against a deliberately flawed fixture
```

Run the tests with `python3 -m unittest discover -s tests -t .` — they serve a
local fixture, so no network is needed.

## Environment notes

- `AUDIT_CHROMIUM_PATH` — use an existing Chromium instead of Playwright's.
- `AUDIT_CHROMIUM_ARGS` — override the launch flags. The default disables
  Encrypted Client Hello, which TLS-inspecting proxies cannot parse; certificate
  verification is untouched.
- Behind a TLS-inspecting proxy, install its CA into the browser trust store
  (`certutil -A -d sql:$HOME/.pki/nssdb -n proxy-ca -t "C,," -i <ca.crt>`).

## Caveats

It audits one page, as an anonymous visitor, at one moment. Load timing is a
single cold measurement. Text over background images is excluded from the
contrast check. The findings are heuristics — a deliberate design decision can
legitimately fail one.
