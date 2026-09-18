"""The website build brief: measured facts filled in, business facts left as fill-ins."""

import http.server
import socketserver
import tempfile
import threading
import unittest
from pathlib import Path

from website_audit.analysis import Finding, Swatch, analyse
from website_audit.brief import FILL, build_brief, site_name
from website_audit.collector import collect
from website_audit.content import to_page
from website_audit.site import PageResult, SiteAudit, _merge_families, _merge_palette, _reach

FIXTURES = Path(__file__).parent


def finding(fid, **kw):
    base = dict(id=fid, category="x", severity="medium", title=fid, detail="", evidence=["Affects 2 of 2 audited pages"], fix="fix it", automation="")
    base.update(kw)
    return Finding(**base)


def fake_audit():
    def page(path, title, headings, text, ctas, nav, score, findings=()):
        probe = {
            "title": title, "meta_description": None if path == "/" else f"About {path}",
            "headings": headings, "body_text": text,
            "ctas": [{"text": c, "href": f"https://acme.example{path}#go"} for c in ctas],
            "nav_links": [{"text": t, "href": f"https://acme.example{h}"} for t, h in nav],
            "footer_links": [{"text": "Facebook", "href": "https://facebook.com/acme"}],
            "links": [],
        }
        class Data:  # what content.to_page reads
            final_url = f"https://acme.example{path}"
        Data.probe = probe
        return PageResult(
            url=f"https://acme.example{path}", ok=True, score=score, grade_letter="C", findings=len(findings),
            title=title, results={"typography": {"content_families": [], "body_size": 15, "body_run": {"line_height": 21, "font_size": 15}}, "findings": list(findings), "palette": []},
            page=to_page(Data), probe=probe,
        )

    nav = [("Home", "/"), ("Services", "/services/"), ("Contact", "/contact/")]
    pages = [
        page("/", "Acme Homes", [{"level": 1, "text": "Where your dreams find a home"}, {"level": 2, "text": "Why choose us"}],
             "Acme Homes Where your dreams find a home We build homes people love. Why choose us Thirty years of building. Email hello@acme.example",
             ["Find Your Dream", "Find Your Dream"], nav, 74, [finding("a11y-contrast"), finding("type-family-sprawl")]),
        page("/services/", "Services – Acme Homes", [{"level": 1, "text": "Services"}, {"level": 2, "text": "Custom builds"}],
             "Services Custom builds We design around your plot.", ["Find Your Dream", "Call now"], nav, 61, [finding("a11y-contrast")]),
    ]
    return SiteAudit(
        start_url="https://acme.example/", host="acme.example", pages=pages,
        palette=[Swatch(hex="#ffffff", weight=100, role="surface", background_area=90000),
                 Swatch(hex="#222222", weight=80, role="ink", text_chars=5000),
                 Swatch(hex="#5b2c8e", weight=30, role="brand", text_chars=200, border_uses=3)],
        families=[("Open Sans, sans-serif", 6000), ("Playfair Display, serif", 400), ("Comic Sans MS", 50)],
        findings=[finding("a11y-contrast"), finding("type-family-sprawl"), finding("structure-cta-conflict"),
                  finding("perf-console-errors-unknown", title="Something odd", fix="Look into it")],
        scores={"overall": 68, "grade": "C", "categories": {"Accessibility": 60, "Typography": 70}, "counts": {}},
        discovery_source="sitemap", discovery_notes=[], elapsed_s=10.0,
    )


class BriefContentTest(unittest.TestCase):
    def setUp(self):
        self.brief = build_brief(fake_audit())
        self.md = self.brief.markdown

    def test_named_after_the_site(self):
        self.assertEqual(self.brief.filename, "acme.example-site-brief.md")
        self.assertTrue(self.md.startswith("# Acme Homes · Website Build Prompt"))

    def test_brand_name_from_shared_title_segment(self):
        self.assertEqual(site_name(["Acme Homes", "Services – Acme Homes", "Contact – Acme Homes"]), "Acme Homes")
        self.assertEqual(site_name(["Contact – Acme Homes"]), "Acme Homes")
        self.assertEqual(site_name([]), FILL)

    def test_measured_brand_system(self):
        self.assertIn("| Brand | `#5b2c8e` |", self.md)
        self.assertIn("| Open Sans | 93% | Text |", self.md)
        self.assertIn("| Playfair Display | 6% | Display |", self.md)
        self.assertIn("| Comic Sans MS | 1% | Drop |", self.md)
        self.assertIn("family=Playfair+Display", self.md)
        self.assertIn("Body 15px", self.md)
        self.assertIn("line-height 1.4", self.md)

    def test_site_map_nav_and_cta(self):
        self.assertIn("/services/", self.md)
        self.assertIn("Primary navigation on the current site: Home · Services · Contact.", self.md)
        self.assertIn("Primary call to action: **Find Your Dream** (the most used button label, on 2 of 2 pages).", self.md)
        self.assertIn('"Call now"', self.md)

    def test_page_specs_carry_the_sites_own_copy(self):
        self.assertIn("### / · Acme Homes", self.md)
        self.assertIn("- **h1 · Where your dreams find a home** — We build homes people love.", self.md)
        self.assertIn("  - **h2 · Why choose us** — Thirty years of building.", self.md)
        self.assertIn("**Meta description today:** none — write one", self.md)
        self.assertIn("**Meta description today:** About /services/", self.md)

    def test_rules_come_from_findings(self):
        self.assertIn("Every text and background pair must pass WCAG AA", self.md)
        self.assertIn("`structure-cta-conflict`", self.md)
        self.assertIn("**Something odd** — Look into it", self.md)   # unmapped finding still carried over
        self.assertNotIn("Publish a robots.txt", self.md)            # not found, so not a rule

    def test_company_facts_are_fill_ins_not_inventions(self):
        self.assertIn("**Email:** hello@acme.example", self.md)
        self.assertIn("https://facebook.com/acme", self.md)
        self.assertIn(f"- **Location:** {FILL}", self.md)
        self.assertIn(f"**What we are:** {FILL}", self.md)
        self.assertNotIn("lorem", self.md.lower())

    def test_page_table_weakest_first(self):
        table = self.md.split("## 2. WHAT THE CURRENT SITE OFFERS")[1].split("## 3.")[0]
        self.assertLess(table.index("/services/"), table.index("| `/` |"))


class BriefFromRealAuditTest(unittest.TestCase):
    """Build a brief from real collector output, so the data shapes are the real ones."""

    @classmethod
    def setUpClass(cls):
        root = FIXTURES

        class Handler(http.server.SimpleHTTPRequestHandler):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, directory=str(root), **kwargs)

            def log_message(self, *args):
                pass

        cls.server = socketserver.TCPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        cls.work = Path(tempfile.mkdtemp(prefix="brief-work-"))

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_brief_from_collected_pages(self):
        results = []
        for path in ("/fixture.html", "/spa_fixture.html"):
            data = collect(f"{self.base}{path}", self.work)
            analysis = analyse(data)
            results.append(PageResult(
                url=data.final_url, ok=True, score=analysis["scores"]["overall"], grade_letter=analysis["scores"]["grade"],
                findings=len(analysis["findings"]), title=data.probe.get("title"), results=analysis,
                page=to_page(data), probe=data.probe,
            ))
        audit = SiteAudit(
            start_url=f"{self.base}/fixture.html", host=f"127.0.0.1:{self.server.server_address[1]}", pages=results,
            palette=_merge_palette(results), families=_merge_families(results), findings=_reach(results),
            scores={"overall": 50, "grade": "E", "categories": {}, "counts": {}},
            discovery_source="test", discovery_notes=[], elapsed_s=1.0,
        )
        brief = build_brief(audit)
        md = brief.markdown
        self.assertIn("## 4. BRAND SYSTEM", md)
        self.assertIn("| Hex |", md)
        self.assertIn("## 7. PAGE SPECS", md)
        self.assertIn("### /FIXTURE.HTML", md)
        self.assertIn("## 9. HARD RULES", md)
        self.assertIn("Every text and background pair must pass WCAG AA", md)
        self.assertIn("Affects", md)
        self.assertNotIn("{{", md)


if __name__ == "__main__":
    unittest.main()
