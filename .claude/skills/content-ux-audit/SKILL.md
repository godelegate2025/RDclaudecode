---
name: content-ux-audit
description: Audit a whole website — every page discovered via its sitemap — and deliver one Redefine-branded PDF covering design, content and consistency across pages. Use when someone wants the whole site rather than a single page, or asks about content problems: "audit the whole site", "check every page", "content audit", "is the site ready to launch", "check for lorem ipsum", "are our stats consistent", "site-wide review". For a single page, use website-audit instead.
---

# Whole-site content & design audit

Crawls a site, renders every page in real Chromium, measures each one, then
compares the pages against each other. The cross-page view is the point: one
page always looks consistent with itself.

## Running it

```bash
python3 -m website_audit <url> --site [--limit 25] [-o report.pdf]
```

Takes roughly 6–8 seconds per page. 8 pages ≈ one minute; 25 pages ≈ three.

| Flag | Use |
|---|---|
| `--limit N` | Page cap. Default 25. Raise for a full sweep, lower for a quick look. |
| `--ignore-robots` | Crawl paths robots.txt disallows. **Only on sites you control.** |
| `--timeout` | Per-page navigation timeout in ms. |

## How pages are found

1. `robots.txt` for advertised sitemaps, then `/sitemap.xml`, then `/sitemap_index.xml`
2. Failing that, links on the homepage
3. Filtered to the site's own host; assets, feeds, pagination, tag and filter
   URLs dropped; `robots.txt` obeyed unless overridden

The report says which source was used and whether the cap was hit.

## What the cross-page layer catches

Things no single-page audit can see:

- **Typefaces and colours site-wide** — "12 typefaces across 8 pages" is the
  design-system argument in one line
- **Contradicting claims** — 1,400 homes on one page, 1,200 on another
- **US/UK spelling drift**
- **Duplicate titles and meta descriptions**
- **Copy pasted between pages** (shared headers and footers are excluded)
- **Placeholder text still live** — lorem ipsum, "Coming soon", TBD
- **Findings ranked by reach** — "affects 7 of 8 pages"
- **Navigation drift** — pages whose nav differs from the rest of the site
- **CTA conflicts** — the same button label pointing somewhere different
- **Orphan pages** — published, in the sitemap, linked from nowhere
- **Missing expected pages** — contact, privacy, terms

All of it is measured or compared, never judged: no model in the loop, no tokens,
and the same answer every run.

## What it deliberately does NOT do

It makes no judgement about whether copy is *persuasive*, whether the narrative
arc works, or whether a CTA is compelling. Those need an opinion, and a rule that
guesses produces findings nobody trusts. Offer that as your own read of the
report, not as tool output.

## Handling the result

1. Lead with the score, page count, and the two or three findings with the
   widest reach.
2. Send the PDF with `SendUserFile`.
3. Name the limits: it audits a sample of pages as an anonymous visitor, load
   timing is one cold measurement, and findings are heuristics.

## Politeness

This fetches a client's site dozens of times. Keep `--limit` sensible, leave
robots.txt honoured on sites you do not own, and prefer a template sample —
home, a listing page, a detail page, a post, contact — over an exhaustive sweep.
Most sites are five templates repeated.
