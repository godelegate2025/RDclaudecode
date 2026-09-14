---
name: website-audit
description: Audit any website's typography, colour palette, accessibility and performance, then deliver a PDF report. Use whenever someone gives a URL and asks to analyse, review, audit or critique the site's design, fonts, colours, contrast, or what could be improved — including "what's wrong with my site", "review this landing page", or "send me a design report".
---

# Website design audit

Turns a URL into a PDF report covering the fonts in use, the colour palette,
WCAG contrast failures, and a ranked list of improvements — each one labelled
with how to automate the fix.

## Running it

```bash
python3 -m website_audit <url> [-o report.pdf] [--json]
```

The command prints the PDF path on stdout and a one-line summary on stderr.
`--json` writes the same findings as structured data next to the PDF, which is
what you want when feeding results into another workflow.

First run in a fresh environment:

```bash
pip install -r requirements.txt && python3 -m playwright install chromium
```

If Chromium is already on the machine, point `AUDIT_CHROMIUM_PATH` at it instead
of downloading one.

## How to handle a request

1. **Get the URL.** If the user named a page (pricing, docs, a specific landing
   page), audit that exact URL — the audit describes one page, not a whole site.
   If they only gave a domain, audit the homepage and say so.
2. **Run the command.** It takes 20–60 seconds. Multiple pages: run it once per
   URL, each with its own `-o` path.
3. **Deliver the PDF.** Send the file with `SendUserFile`. If the user asked for
   it by email and a mail connector is available, attach it there instead — and
   only to the address they named.
4. **Summarise in 3–5 lines**, in chat: the score, the fonts and palette found,
   and the two or three findings that actually matter. Do not restate the report.

## What the report contains

| Section | What it answers |
|---|---|
| Cover | Overall score, per-category scores, desktop and mobile screenshots |
| Typography | Every family in use, share of text, size range, weights, fallbacks, the type scale |
| Colour palette | Clustered swatches with roles, near-duplicate detection, usage |
| Contrast | Each failing text/background pair with a ready-to-use replacement colour |
| Findings | Severity, evidence, the fix, and how to automate it |
| What to automate first | Findings ranked by impact over effort, with tooling |
| Page facts | Meta tags, DOM size, resource weight |

## Auditing on a schedule

For a recurring check (design drift, contrast regressions after a redesign),
create a Routine that re-runs the audit and reports what changed. The `--json`
output is the thing to diff between runs — compare `scores`, `palette` and
`findings[].id`.

## Limits worth stating to the user

- It audits **one rendered page** as an anonymous visitor: no logged-in views,
  and pages behind a cookie wall will report the wall, not the site.
- Text over background images is skipped by the contrast check (the effective
  background is not a single colour) — the report says so.
- Load timing is one cold measurement from wherever the audit runs. Treat it as
  a signal, not a benchmark.
- Findings are heuristics. They are worth reading, not obeying: a deliberate
  design decision can legitimately fail a rule here.
