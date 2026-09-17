"""The properties the headline number has to hold, whatever the checks become."""

import unittest

from website_audit.analysis import CATEGORIES, CATEGORY_WEIGHT, Finding, score

CATS = [*CATEGORIES, "Content"]


def f(category, severity):
    return Finding(severity, category, severity, "t", "d", [], "fix", "auto")


class ScoreInvariantTest(unittest.TestCase):
    def test_a_clean_page_scores_100(self):
        self.assertEqual(score([])["overall"], 100)

    def test_a_critical_finding_cannot_be_hidden(self):
        """The bug this replaced: live lorem ipsum scored 88, a grade B."""
        result = score([f("Content", "critical")], CATS)
        self.assertLessEqual(result["overall"], 70)
        self.assertIn(result["grade"], {"C", "D", "F"})

    def test_severity_still_ranks_two_bad_sites(self):
        """A ceiling that flattened everything would make these equal."""
        one = score([f("Content", "critical")], CATS)["overall"]
        many = score([f("Content", "critical")] + [f(c, "high") for c in CATS[:4]], CATS)["overall"]
        self.assertLess(many, one)

    def test_more_criticals_score_lower(self):
        scores = [
            score([f("Content", "critical")] * n, CATS)["overall"] for n in (1, 2, 3)
        ]
        self.assertEqual(scores, sorted(scores, reverse=True))
        self.assertTrue(len(set(scores)) == 3, scores)

    def test_findings_never_become_free(self):
        """The old model pinned a category at zero, after which nothing counted."""
        scores = [score([f("Findability", "medium")] * n, CATS)["overall"] for n in (6, 12, 20, 30)]
        for earlier, later in zip(scores, scores[1:]):
            self.assertLess(later, earlier, scores)

    def test_a_weightier_category_costs_more(self):
        """Identical findings, different categories: consequence decides."""
        serious = score([f("Accessibility", "medium")] * 3, CATS)["overall"]
        cosmetic = score([f("Consistency", "medium")] * 3, CATS)["overall"]
        self.assertLess(serious, cosmetic)

    def test_low_severity_findings_stay_cheap(self):
        self.assertGreaterEqual(score([f(c, "low") for c in CATS[:8]], CATS)["overall"], 90)

    def test_adding_clean_categories_does_not_inflate_a_bad_score(self):
        """Breadth must not be a way to look better."""
        findings = [f("Content", "critical"), f("Accessibility", "high")]
        narrow = score(findings, CATS[:6] + ["Content"])["overall"]
        wide = score(findings, CATS + ["Extra1", "Extra2", "Extra3"])["overall"]
        self.assertLessEqual(wide, narrow + 1, (narrow, wide))

    def test_every_category_carries_a_declared_weight(self):
        for name in CATS:
            self.assertIn(name, CATEGORY_WEIGHT, f"{name} has no declared weight")

    def test_the_score_never_leaves_its_range(self):
        brutal = [f(c, "critical") for c in CATS] * 4
        self.assertGreaterEqual(score(brutal, CATS)["overall"], 0)
        self.assertLessEqual(score([], CATS)["overall"], 100)


if __name__ == "__main__":
    unittest.main()
