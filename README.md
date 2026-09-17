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

# Whole site: discover pages from the sitemap, measure each, compare them
python3 -m website_audit example.com --site --limit 25
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

## As a hosted service

`service/` wraps the same engine in a web app: a form, an audit endpoint that
returns the PDF, and a page that shows the report inline with a download button.
It is stateless, so it scales to zero.

```bash
pip install -r requirements.txt -r requirements-service.txt
uvicorn service.app:app --port 8000     # then open http://127.0.0.1:8000
```

The page is branded for Redefine (coral `#FC4452`, ink `#191616`, paper `#F4F3F0`,
Anton + Archivo). Drop a logo at `service/static/logo.png` and the header uses it;
without one it falls back to a coral monogram.

**Bot walls.** A WAF challenge page renders like any other page and would be
scored like one, producing an authoritative report about someone's firewall. The
service detects them (`website_audit/blocking.py`) and returns 422 with an
explanation instead of a report.

Once deployed, `audit.ps1` runs batches against it from Windows with nothing
installed locally:

```powershell
$env:AUDIT_SERVICE = "https://your-service.run.app"
.\audit.ps1 https://a.com, https://b.com -OutDir .\reports
```

`Dockerfile` builds it for any container host. To put it on Google Cloud Run,
`./deploy.sh` (or `.\deploy.ps1` on Windows) does the whole thing and verifies the result;
[DEPLOY.md](DEPLOY.md) explains each flag, the cost maths (~3,000 audits/month
inside the free tier), and the hardening checklist.

**Public deployments must keep `service/security.py`.** A hosted browser that
fetches whatever a stranger types is a server-side request forgery engine — it
will happily render a cloud metadata endpoint or an internal admin panel and
hand back a screenshot. Every URL is resolved and checked against private,
loopback, link-local and metadata ranges before Chromium sees it, and re-checked
on each redirect.

## Whole-site mode

`--site` discovers pages (robots.txt → sitemap → homepage links), renders each in
Chromium, and adds a cross-page layer that a single-page audit cannot see:
typefaces and colours across the whole site, contradicting statistics, US/UK
spelling drift, duplicate titles, copy pasted between pages, placeholder text
still live, and every finding ranked by how many pages it affects.

All of it is measured or compared — no model, no tokens, same answer every run.
Judgement calls (is this copy persuasive?) are deliberately absent.

`robots.txt` is honoured unless `--ignore-robots` is passed, which is only
appropriate on a site you control.

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

**Structure — whether the site is navigable.** Needs `--site`, because none of it
is visible from one page: navigation that differs between templates, a call to
action whose label promises one thing and goes somewhere different depending on
the page, pages published but linked from nowhere, and the pages a visitor
expects to find (contact, privacy, terms). Page checks match on URL, so they say
plainly when they may be wrong rather than asserting a page is missing that is
merely named differently.

**Findability — whether machines can read the site.** The headline check is how
much of the copy survives without JavaScript: the served HTML is compared against
the rendered DOM, because a browser runs the scripts and most crawlers and answer
engines do not. A page that renders 2,000 words from 80 words of HTML looks
perfect to a human and is close to invisible to them. Also: JSON-LD structured
data and whether it identifies the business, accidental `noindex`, canonical
tags, robots.txt, sitemap, `llms.txt`, and question-style headings that are not
marked up as FAQ.

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
  browser.py      Chromium lifecycle — one launch serves audit and PDF print
  collector.py    Chromium: navigation, screenshots, network tally
  probe.js        runs in the page — computed styles, colours, structure
  analysis.py     palette clustering, WCAG maths, the findings rules, scoring
  report.py       Jinja → HTML → Chromium print-to-PDF
  templates/      the report layout
service/
  app.py          FastAPI: one request runs one audit and returns the PDF
  security.py     SSRF guard — required for any public deployment
  static/         the form and report viewer
tests/            offline end-to-end run against a deliberately flawed fixture
```

Run the tests with `python3 -m unittest discover -s tests -t .` — they serve a
local fixture, so no network is needed.

## Environment notes

- `AUDIT_CHROMIUM_PATH` — use an existing Chromium instead of Playwright's.
- `AUDIT_CHROMIUM_ARGS` — replace the launch flags. The defaults disable
  Encrypted Client Hello (which TLS-inspecting proxies cannot parse; certificate
  verification is untouched) and `/dev/shm` usage (64 MB in most containers).
- `AUDIT_CHROMIUM_EXTRA_ARGS` — append flags instead of replacing them. The
  container image uses this for `--no-sandbox`.
- `RATE_LIMIT_PER_HOUR` — per-IP limit for the hosted service (default 10).
- Behind a TLS-inspecting proxy, install its CA into the browser trust store
  (`certutil -A -d sql:$HOME/.pki/nssdb -n proxy-ca -t "C,," -i <ca.crt>`).

## Caveats

It audits one page, as an anonymous visitor, at one moment. Load timing is a
single cold measurement. Text over background images is excluded from the
contrast check. The findings are heuristics — a deliberate design decision can
legitimately fail one.
