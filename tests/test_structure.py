"""Navigation, CTAs and findability of pages — the cross-page view."""

import unittest

from website_audit.structure import page_findings, site_findings


def probe(nav=None, ctas=None, links=None, footer=None):
    return {
        "nav_links": nav if nav is not None else [{"text": "Home", "href": "https://a.com/"}],
        "footer_links": footer or [],
        "ctas": ctas or [],
        "links": links or [],
    }


def nav(*paths):
    return [{"text": p.strip("/") or "Home", "href": f"https://a.com{p}"} for p in paths]


class PageStructureTest(unittest.TestCase):
    def test_a_page_with_no_navigation_is_flagged(self):
        found = page_findings(probe(nav=[]), "https://a.com/orphan")
        self.assertTrue(any(f.id == "structure-no-nav" for f in found))

    def test_a_normal_navigation_is_not_flagged(self):
        found = page_findings(probe(nav=nav("/", "/about", "/contact")), "https://a.com/")
        self.assertEqual(found, [])


class SiteStructureTest(unittest.TestCase):
    def _probes(self, **overrides):
        """A small but complete site: every expected page present and linked."""
        standard = nav("/", "/about", "/contact", "/privacy", "/terms")
        pages = {
            "https://a.com/": probe(nav=standard, links=["https://a.com/about", "https://a.com/contact"]),
            "https://a.com/about": probe(nav=standard, links=["https://a.com/", "https://a.com/privacy"]),
            "https://a.com/contact": probe(nav=standard, links=["https://a.com/", "https://a.com/terms"]),
            "https://a.com/privacy": probe(nav=standard, links=["https://a.com/"]),
            "https://a.com/terms": probe(nav=standard, links=["https://a.com/"]),
        }
        pages.update(overrides)
        return pages

    def test_a_consistent_site_raises_nothing(self):
        found = site_findings(self._probes(), list(self._probes()))
        self.assertEqual([f.id for f in found], [])

    def test_a_page_with_a_different_nav_is_flagged(self):
        odd = probe(nav=nav("/", "/legacy", "/old", "/gone", "/ancient"), links=["https://a.com/"])
        probes = self._probes(**{"https://a.com/stale": odd})
        found = site_findings(probes, list(probes))
        self.assertTrue(any(f.id == "structure-nav-drift" for f in found))

    def test_one_cta_label_with_two_destinations(self):
        probes = self._probes()
        probes["https://a.com/"]["ctas"] = [{"text": "Get started", "href": "https://a.com/signup"}]
        probes["https://a.com/about"]["ctas"] = [{"text": "Get started", "href": "https://a.com/contact"}]
        found = site_findings(probes, list(probes))
        conflict = next((f for f in found if f.id == "structure-cta-conflict"), None)
        self.assertIsNotNone(conflict)
        self.assertEqual(conflict.severity, "high")

    def test_the_same_cta_everywhere_is_fine(self):
        probes = self._probes()
        for page in probes.values():
            page["ctas"] = [{"text": "Get started", "href": "https://a.com/signup"}]
        self.assertFalse(any(f.id == "structure-cta-conflict" for f in site_findings(probes, list(probes))))

    def test_an_unlinked_page_is_reported_as_an_orphan(self):
        probes = self._probes()
        probes["https://a.com/hidden"] = probe(
            nav=nav("/", "/about", "/contact", "/privacy", "/terms"), links=["https://a.com/"]
        )
        found = site_findings(probes, list(probes))
        orphan = next((f for f in found if f.id == "structure-orphan-pages"), None)
        self.assertIsNotNone(orphan)
        self.assertTrue(any("/hidden" in e for e in orphan.evidence))

    def test_missing_contact_page_is_high_severity(self):
        bare = nav("/", "/about")
        probes = {
            "https://a.com/": probe(nav=bare, links=["https://a.com/about"]),
            "https://a.com/about": probe(nav=bare, links=["https://a.com/"]),
        }
        found = site_findings(probes, list(probes))
        contact = next((f for f in found if f.id == "structure-missing-contact"), None)
        self.assertIsNotNone(contact)
        self.assertEqual(contact.severity, "high")

    def test_a_pricing_page_is_never_demanded(self):
        """Whether a business publishes prices is not a defect."""
        probes = self._probes()
        found = site_findings(probes, list(probes))
        self.assertFalse(any("pricing" in f.id for f in found))


if __name__ == "__main__":
    unittest.main()
