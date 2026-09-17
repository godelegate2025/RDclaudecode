"""The home page draft: built from the audit's own data, and never worse than its source."""

import http.server
import socketserver
import tempfile
import threading
import unittest
from pathlib import Path

from website_audit.analysis import Finding, Swatch, analyse, contrast_ratio
from website_audit.collector import collect
from website_audit.draft import Draft, build_draft, choose_palette, site_name, write_draft

FIXTURES = Path(__file__).parent


class Family:
    def __init__(self, name, chars, sizes, is_icon_font=False, is_system=False):
        self.name, self.chars, self.sizes = name, chars, sizes
        self.is_icon_font, self.is_system = is_icon_font, is_system


def probe_for(**overrides):
    probe = {
        "title": "Contact – Acme Homes",
        "meta_description": None,
        "lang": "en",
        "headings": [
            {"level": 1, "text": "Where your dreams find a home"},
            {"level": 2, "text": "Why choose us"},
            {"level": 2, "text": "How buying works"},
            {"level": 3, "text": "Do you build custom homes?"},
        ],
        "body_text": (
            "Acme Homes Where your dreams find a home We build homes people love. "
            "Every plot is chosen for light and quiet. Why choose us Thirty years of building. "
            "We don't tell you no, we tell you how. How buying works Reserve, design, move in. "
            "Do you build custom homes? Yes."
        ),
        "nav_links": [
            {"text": "Home", "href": "https://acme.example/"},
            {"text": "Properties", "href": "https://acme.example/all/"},
            {"text": "Contact", "href": "https://acme.example/contact/"},
            {"text": "Facebook", "href": "https://facebook.com/acme"},
        ],
        "footer_links": [{"text": "Warranty", "href": "https://acme.example/warranty/"}],
        "ctas": [
            {"text": "Find Your Dream", "href": "https://acme.example/all/"},
            {"text": "Find Your Dream", "href": "https://acme.example/all/"},
            {"text": "Call now", "href": "tel:123"},
        ],
        "links": [],
    }
    probe.update(overrides)
    return probe


def results_for(findings=()):
    return {
        "palette": [
            Swatch(hex="#ffffff", weight=100, role="surface", background_area=90000),
            Swatch(hex="#222222", weight=80, role="ink", text_chars=5000),
            Swatch(hex="#9b8fd6", weight=30, role="brand", text_chars=200),   # too light on white
            Swatch(hex="#999999", weight=10, role="neutral", text_chars=300),  # 2.8:1 on white
        ],
        "typography": {
            "families": [
                Family("Open Sans, sans-serif", 6000, {"16": 5000, "14": 1000}),
                Family("Playfair Display, serif", 400, {"48": 100, "32": 300}),
                Family("Comic Sans MS", 50, {"12": 50}),
                Family("FontAwesome", 20, {"16": 20}, is_icon_font=True),
                Family("system-ui", 300, {"16": 300}, is_system=True),
            ],
        },
        "findings": [
            Finding(id=fid, category="x", severity="medium", title=fid, detail="", evidence=[], fix="", automation="")
            for fid in findings
        ],
    }


class DraftContentTest(unittest.TestCase):
    def test_brand_name_is_the_segment_shared_across_pages(self):
        titles = ["Acme Homes", "Contact – Acme Homes", "Warranty – Acme Homes"]
        self.assertEqual(site_name("Contact – Acme Homes", titles), "Acme Homes")
        self.assertEqual(site_name("Contact – Acme Homes"), "Acme Homes")
        self.assertEqual(site_name("Acme Homes"), "Acme Homes")

    def test_uses_the_sites_own_words_nav_and_action(self):
        draft = build_draft("https://acme.example/", probe_for(), results_for())
        html = draft.html
        self.assertIn("<h1 id=\"hero-title\">Where your dreams find a home</h1>", html)
        self.assertIn("We build homes people love.", html)          # lede from the copy after the h1
        self.assertIn("<h2 id=\"s1\">Why choose us</h2>", html)
        self.assertIn("Thirty years of building.", html)             # copy under its own heading
        self.assertIn('href="https://acme.example/all/">Properties', html)
        self.assertNotIn(">Home<", html)                              # the wordmark is the home link
        self.assertEqual(draft.html.count("Find Your Dream"), html.count("Find Your Dream"))
        self.assertIn('class="btn" href="https://acme.example/all/">Find Your Dream', html)
        self.assertNotIn("Call now", html)                            # one label, the most used
        self.assertEqual(draft.filename, "acme.example-home-draft.html")

    def test_questions_become_faq_markup(self):
        html = build_draft("https://acme.example/", probe_for(), results_for()).html
        self.assertIn('"@type": "FAQPage"', html)
        self.assertIn("<h3>Do you build custom homes?</h3>", html)

    def test_identity_schema_and_social_profiles(self):
        html = build_draft("https://acme.example/", probe_for(), results_for()).html
        self.assertIn('"@type": "Organization"', html)
        self.assertIn('"name": "Acme Homes"', html)
        self.assertIn("https://facebook.com/acme", html)

    def test_missing_pages_become_labelled_placeholders(self):
        draft = build_draft("https://acme.example/", probe_for(), results_for())
        self.assertIn("https://acme.example/privacy/", draft.html)
        self.assertIn("[placeholder]", draft.html)
        self.assertTrue(any("privacy" in n for n in draft.notes))
        self.assertNotIn("lorem", draft.html.lower())

    def test_fonts_are_the_sites_two_real_families(self):
        draft = build_draft("https://acme.example/", probe_for(), results_for())
        self.assertEqual(draft.fonts, {"display": "Playfair Display", "body": "Open Sans"})
        self.assertIn("family=Playfair+Display", draft.html)
        self.assertNotIn("Comic Sans", draft.html.split("<style>")[1].split("</style>")[0])

    def test_every_text_token_passes_aa_on_the_surface(self):
        tokens, notes = choose_palette(results_for()["palette"])
        for name in ("ink", "muted", "brand"):
            self.assertGreaterEqual(contrast_ratio(tokens[name], tokens["surface"]), 4.5, name)
        self.assertGreaterEqual(contrast_ratio(tokens["on_brand"], tokens["brand"]), 4.5)
        self.assertGreaterEqual(contrast_ratio(tokens["brand_on_dark"], tokens["ink"]), 4.5)
        self.assertTrue(any("failed AA" in n for n in notes))

    def test_notes_say_which_findings_the_draft_resolves(self):
        draft = build_draft(
            "https://acme.example/", probe_for(),
            results_for(["a11y-contrast", "type-family-sprawl", "find-no-sitemap"]),
        )
        self.assertTrue(any(n.startswith("a11y-contrast:") for n in draft.notes))
        self.assertTrue(any(n.startswith("type-family-sprawl:") for n in draft.notes))
        self.assertEqual(draft.unresolved, ["find-no-sitemap"])

    def test_scraped_text_cannot_inject_markup(self):
        probe = probe_for(title="<script>alert(1)</script> – Acme", headings=[{"level": 1, "text": "<img src=x onerror=alert(1)>"}])
        html = build_draft("https://acme.example/", probe, results_for()).html
        self.assertNotIn("<script>alert", html)
        self.assertNotIn("<img src=x", html)

    def test_survives_an_empty_probe(self):
        draft = build_draft("https://acme.example/", {}, {"palette": [], "typography": {}, "findings": []})
        self.assertIsInstance(draft, Draft)
        self.assertIn("<h1", draft.html)
        self.assertEqual(draft.fonts, {"display": "Fraunces", "body": "DM Sans"})


class DraftAuditTest(unittest.TestCase):
    """Audit the fixture, draft it, audit the draft: the draft must come out ahead."""

    server: socketserver.TCPServer

    @classmethod
    def setUpClass(cls):
        cls.root = Path(tempfile.mkdtemp(prefix="draft-test-"))
        (cls.root / "fixture.html").write_bytes((FIXTURES / "fixture.html").read_bytes())
        root = cls.root

        class Handler(http.server.SimpleHTTPRequestHandler):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, directory=str(root), **kwargs)

            def log_message(self, *args):
                pass

        cls.server = socketserver.TCPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        cls.work = Path(tempfile.mkdtemp(prefix="draft-work-"))

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_the_draft_scores_higher_than_its_source(self):
        source = collect(f"{self.base}/fixture.html", self.work)
        source_results = analyse(source)
        draft = build_draft(source.final_url, source.probe, source_results)
        write_draft(draft, self.root / "draft.html")

        drafted = collect(f"{self.base}/draft.html", self.work)
        results = analyse(drafted)
        ids = {f.id for f in results["findings"]}
        severe = [f.id for f in results["findings"] if f.severity in ("critical", "high")]

        self.assertGreater(results["scores"]["overall"], source_results["scores"]["overall"] + 30)
        self.assertEqual(severe, [], f"draft has severe findings: {severe}")
        for fid in ("a11y-contrast", "a11y-h1", "a11y-heading-order", "type-family-sprawl",
                    "type-no-fallback", "resp-viewport", "resp-overflow", "find-no-structured-data",
                    "colour-no-tokens", "seo-description", "a11y-tap-targets"):
            self.assertNotIn(fid, ids)
        self.assertEqual(results["scores"]["categories"]["Accessibility"], 100)
        self.assertEqual(results["scores"]["categories"]["Responsive"], 100)


if __name__ == "__main__":
    unittest.main()
