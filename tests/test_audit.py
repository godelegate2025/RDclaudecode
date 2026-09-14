"""End-to-end test against a local fixture — no network required."""

import http.server
import socketserver
import tempfile
import threading
import unittest
from pathlib import Path

from website_audit.analysis import analyse, build_palette, contrast_ratio, suggest_accessible_colour
from website_audit.collector import collect
from website_audit.report import html_to_pdf, render_html

FIXTURE_DIR = Path(__file__).parent


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(FIXTURE_DIR), **kwargs)

    def log_message(self, *args):  # keep test output clean
        pass


class ColourMathTest(unittest.TestCase):
    def test_contrast_ratio_extremes(self):
        self.assertAlmostEqual(contrast_ratio("#000000", "#ffffff"), 21.0, places=1)
        self.assertAlmostEqual(contrast_ratio("#ffffff", "#ffffff"), 1.0, places=2)

    def test_suggestion_clears_the_threshold(self):
        fixed = suggest_accessible_colour("#bbbbbb", "#ffffff", 4.5)
        self.assertGreaterEqual(contrast_ratio(fixed, "#ffffff"), 4.5)

    def test_near_identical_colours_collapse(self):
        palette = build_palette(
            {
                "#ff0055": {"text": 100, "background": 0, "border": 0},
                "#ff0056": {"text": 90, "background": 0, "border": 0},
                "#0b0b0b": {"text": 500, "background": 0, "border": 0},
            }
        )
        merged = [s for s in palette if len(s.members) > 1]
        self.assertTrue(merged, "expected #ff0055 and #ff0056 to merge")
        self.assertTrue(any(s.is_neutral for s in palette), "expected #0b0b0b to read as neutral")


class FixtureAuditTest(unittest.TestCase):
    server: socketserver.TCPServer

    @classmethod
    def setUpClass(cls):
        cls.server = socketserver.TCPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        port = cls.server.server_address[1]
        cls.work = Path(tempfile.mkdtemp(prefix="audit-test-"))
        cls.data = collect(f"http://127.0.0.1:{port}/fixture.html", cls.work)
        cls.results = analyse(cls.data)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def finding_ids(self):
        return {f.id for f in self.results["findings"]}

    def test_detects_the_planted_problems(self):
        expected = {
            "a11y-contrast",
            "a11y-alt-text",
            "a11y-h1",
            "a11y-heading-order",
            "a11y-lang",
            "a11y-tap-targets",
            "type-family-sprawl",
            "type-small-body",
            "type-line-height",
            "type-measure",
            "resp-viewport",
            "resp-overflow",
            "colour-palette-bloat",
            "seo-description",
        }
        self.assertLessEqual(expected, self.finding_ids())

    def test_palette_and_typography_are_populated(self):
        self.assertGreater(len(self.results["palette"]), 3)
        names = {f.name for f in self.results["typography"]["content_families"]}
        self.assertIn("Georgia", names)
        self.assertEqual(self.results["typography"]["body_size"], 13)

    def test_contrast_suggestions_are_valid(self):
        self.assertTrue(self.results["contrast_issues"])
        for issue in self.results["contrast_issues"]:
            self.assertGreaterEqual(
                contrast_ratio(issue["suggestion"], issue["background"]), issue["required"]
            )

    def test_text_over_imagery_is_not_scored(self):
        """White-on-white is never a real measurement — it means the backdrop is an image."""
        for issue in self.results["contrast_issues"]:
            self.assertNotEqual(issue["color"], issue["background"])
        runs = {r["sample"]: r for r in self.data.probe["text_runs"]}
        hero = next((r for k, r in runs.items() if "over a photo" in k), None)
        self.assertIsNotNone(hero, "fixture should contain text over an image")
        self.assertTrue(hero["background_unverified"], "hero text should be flagged unmeasurable")

    def test_scores_are_bounded(self):
        scores = self.results["scores"]
        self.assertTrue(0 <= scores["overall"] <= 100)
        self.assertLess(scores["overall"], 90, "a page this broken should not score well")

    def test_page_content_cannot_inject_html(self):
        self.data.probe["title"] = "<script>alert(1)</script>"
        html = render_html(self.data, self.results)
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("&lt;script&gt;", html)

    def test_pdf_is_produced(self):
        pdf = html_to_pdf(render_html(self.data, self.results), self.work / "out.pdf", self.work)
        self.assertTrue(pdf.exists())
        self.assertEqual(pdf.read_bytes()[:5], b"%PDF-")
        self.assertGreater(pdf.stat().st_size, 20_000)


if __name__ == "__main__":
    unittest.main()
