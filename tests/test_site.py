"""Discovery, content checks, and the cross-page roll-up. No network required."""

import unittest

from website_audit.analysis import Finding, build_typography
from website_audit.content import Page, page_findings, site_findings
from website_audit.discovery import NOISE, NON_PAGE, _normalise, _same_site


def page(url, title=None, description=None, text="", headings=None):
    return Page(
        url=url, title=title, meta_description=description, text=text,
        headings=headings if headings is not None else [{"level": 1, "text": "H"}],
        word_count=len(text.split()),
    )


class DiscoveryTest(unittest.TestCase):
    def test_normalisation_collapses_the_same_page(self):
        same = {
            _normalise("https://a.com/about"),
            _normalise("https://a.com/about/"),
            _normalise("https://a.com/about#team"),
            _normalise("https://a.com/about?utm_source=x"),
            _normalise("https://A.com//about"),
        }
        self.assertEqual(len(same), 1, same)

    def test_www_counts_as_the_same_site(self):
        self.assertTrue(_same_site("https://www.a.com/x", "a.com"))
        self.assertTrue(_same_site("https://a.com/x", "www.a.com"))
        self.assertFalse(_same_site("https://other.com/x", "a.com"))

    def test_assets_and_noise_are_not_pages(self):
        for url in ("/logo.png", "/style.css", "/doc.pdf", "/app.js"):
            self.assertTrue(NON_PAGE.search(url), url)
        for url in ("/blog/page/2", "/tag/news", "/wp-json/v2", "/x?utm_source=fb"):
            self.assertTrue(NOISE.search(url), url)
        for url in ("/about", "/services/design", "/contact"):
            self.assertFalse(NON_PAGE.search(url) or NOISE.search(url), url)


class ContentCheckTest(unittest.TestCase):
    def test_placeholder_text_is_critical(self):
        found = page_findings(page("https://a.com/x", text="Lorem ipsum dolor sit amet " * 20))
        self.assertTrue(any(f.id == "content-placeholder" and f.severity == "critical" for f in found))

    def test_real_copy_is_left_alone(self):
        found = page_findings(page("https://a.com/x", text="We build homes in Texas. " * 40))
        self.assertEqual([f.id for f in found], [])

    def test_contradicting_claims_are_caught(self):
        pages = [
            page("https://a.com/", text="We have built 1,400 homes for families."),
            page("https://a.com/about", text="Over 1200 homes delivered since 2005."),
        ]
        found = site_findings(pages)
        self.assertTrue(any(f.id == "site-claim-mismatch" for f in found))

    def test_consistent_claims_are_not_flagged(self):
        pages = [
            page("https://a.com/", text="1400 homes built."),
            page("https://a.com/about", text="We have 1,400 homes to our name."),
        ]
        self.assertFalse(any(f.id == "site-claim-mismatch" for f in site_findings(pages)))

    def test_dialect_drift(self):
        pages = [
            page("https://a.com/", text="Our color palette and organization."),
            page("https://a.com/b", text="The colour of our organisation."),
        ]
        self.assertTrue(any(f.id == "site-dialect-drift" for f in site_findings(pages)))

    def test_duplicate_titles(self):
        pages = [page(f"https://a.com/{n}", title="Home") for n in "abc"]
        self.assertTrue(any(f.id == "site-duplicate-titles" for f in site_findings(pages)))

    def test_site_wide_furniture_is_not_reported_as_duplicate_copy(self):
        """A footer on every page is expected; only copy on a few pages is drift."""
        footer = (
            "Registered in Texas with a long boilerplate legal notice that repeats "
            "identically in the footer of every single page on this entire website."
        )
        pages = [page(f"https://a.com/{n}", text=f"Unique body for {n}. {footer}") for n in "abcdef"]
        self.assertFalse(any(f.id == "site-duplicate-copy" for f in site_findings(pages)))

    def test_copy_pasted_between_two_pages_is_reported(self):
        block = (
            "We are a full service design agency delivering brand strategy and web "
            "development for ambitious founders across the United States every day."
        )
        pages = [
            page("https://a.com/a", text=f"{block} One."),
            page("https://a.com/b", text=f"{block} Two."),
            page("https://a.com/c", text="Entirely different copy here for the third page."),
            page("https://a.com/d", text="And different again on the fourth page of the site."),
        ]
        self.assertTrue(any(f.id == "site-duplicate-copy" for f in site_findings(pages)))


class TypefaceCountTest(unittest.TestCase):
    def test_system_stacks_are_not_counted_as_typefaces(self):
        runs = [
            {"font_family": "-apple-system, sans-serif", "font_size": 16, "font_weight": 400,
             "chars": 500, "tag": "p", "line_height": 24},
            {"font_family": "Oswald, sans-serif", "font_size": 16, "font_weight": 400,
             "chars": 500, "tag": "p", "line_height": 24},
            {"font_family": "sans-serif", "font_size": 16, "font_weight": 400,
             "chars": 200, "tag": "p", "line_height": 24},
        ]
        names = {f.name for f in build_typography(runs)["content_families"]}
        self.assertEqual(names, {"Oswald"})

    def test_a_named_font_is_still_counted(self):
        """Roboto is a real choice, not a system fallback — it must be counted."""
        runs = [{"font_family": "Roboto, Arial, sans-serif", "font_size": 16, "font_weight": 400,
                 "chars": 500, "tag": "p", "line_height": 24}]
        names = {f.name for f in build_typography(runs)["content_families"]}
        self.assertEqual(names, {"Roboto"})


if __name__ == "__main__":
    unittest.main()
