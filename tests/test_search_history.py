"""Search history: saving content searches, listing, reopening, CSV and who may delete. No network."""

import os
import unittest
from unittest import mock

from fastapi.testclient import TestClient

from post_audit import finder
from service import auth, search_history, team
from service.app import app
from service.firestore_db import StoreUnavailable
from service.profiles import get_profiles

SETTINGS = {
    "GOOGLE_CLIENT_ID": "123-abc.apps.googleusercontent.com",
    "ALLOWED_EMAILS": "owner@gmail.com",
    "SESSION_SECRET": "a-long-random-test-secret",
}


def row(platform, label, author, engagement, **extra):
    return {"platform": platform, "platform_label": label, "author": author, "content": f"{author} post",
            "url": f"https://www.tiktok.com/@{author}/video/1", "views": 1000, "likes": 100, "comments": 5,
            "shares": 1, "posted_at": "2026-10-01", "days_since": 30, "engagement": engagement,
            "search_term": "land investing", "scraped_at": "2026-11-03T09:12:00+00:00", "auditable": True, **extra}


def result():
    return {
        "mode": "topic", "term": "land investing", "scraped_at": "2026-11-03T09:12:00+00:00",
        "platforms": [{"platform": "tiktok", "label": "TikTok"}, {"platform": "instagram", "label": "Instagram"},
                      {"platform": "reddit", "label": "Reddit"}],
        "rows": [row("tiktok", "TikTok", "low", 3.5), row("instagram", "Instagram", "best", 2191),
                 row("tiktok", "TikTok", "mid", 52), row("tiktok", "TikTok", "blank", None)],
        "errors": [{"platform": "reddit", "label": "Reddit", "message": "The Apify account is out of credit for this month."}],
    }


class BrokenStore:
    def _fail(self, *a, **k):
        raise StoreUnavailable("Firestore is not set up for this project yet.")

    add = get = recent = delete = _fail


class SearchHistoryTest(unittest.TestCase):
    def setUp(self):
        self.searches = search_history.use_store(search_history.memory_store())
        self.addCleanup(search_history.use_store, search_history.memory_store())

    def test_summary_is_what_the_history_card_needs(self):
        search_id = self.searches.save(result(), by="ana@gmail.com", items=10)
        [card] = self.searches.recent()
        self.assertEqual(card["id"], search_id)
        self.assertEqual((card["by"], card["term"], card["mode"], card["items"], card["total"], card["best"]),
                         ("ana@gmail.com", "land investing", "topic", 10, 4, 2191))
        self.assertEqual([t["author"] for t in card["top"]], ["best", "mid", "low"])
        self.assertEqual(card["platforms"], [
            {"platform": "tiktok", "label": "TikTok", "count": 3, "error": ""},
            {"platform": "instagram", "label": "Instagram", "count": 1, "error": ""},
            {"platform": "reddit", "label": "Reddit", "count": 0,
             "error": "The Apify account is out of credit for this month."},
        ])
        self.assertNotIn("result", card)  # the list never carries every row

    def test_reopening_returns_every_row_and_who_ran_it(self):
        search_id = self.searches.save(result(), by="ana@gmail.com", items=10)
        saved = self.searches.get(search_id)
        self.assertEqual(len(saved["rows"]), 4)
        self.assertEqual((saved["history_id"], saved["saved"]["by"]), (search_id, "ana@gmail.com"))

    def test_newest_first_and_bad_ids_are_not_found(self):
        with mock.patch.object(search_history, "datetime") as clock:
            clock.now.return_value.strftime.side_effect = ["2026-11-01T00:00:00Z", "2026-11-02T00:00:00Z"]
            first = self.searches.save(result(), by="a@gmail.com", items=5)
            second = self.searches.save(result(), by="a@gmail.com", items=5)
        self.assertEqual([s["id"] for s in self.searches.recent()], [second, first])
        for bad in ("", "../x", "nothere"):
            with self.assertRaises(search_history.NotFound, msg=bad):
                self.searches.get(bad)

    def test_nothing_found_is_not_a_failure(self):
        data = dict(result(), errors=[{"platform": "reddit", "label": "Reddit", "message": finder.NOTHING_FOUND}])
        self.searches.save(data, by="", items=5)
        reddit = self.searches.recent()[0]["platforms"][2]
        self.assertEqual((reddit["count"], reddit["error"]), (0, ""))

    def test_no_engagement_means_no_best(self):
        data = dict(result(), rows=[row("tiktok", "TikTok", "a", None)])
        search_id = self.searches.save(data, by="", items=5)
        self.assertIsNone(self.searches.recent()[0]["best"])
        self.assertEqual(self.searches.owner_of(search_id), "")

    def test_csv_has_the_sheet_columns_and_defuses_formulas(self):
        text = search_history.to_csv([row("x", "X", "=HYPERLINK(1)", None)])
        self.assertTrue(text.startswith("﻿Search Term,Platform,Scraped At,Posted At,Author,Content,URL,"))
        line = text.splitlines()[1]
        self.assertIn(",'=HYPERLINK(1),", line)
        self.assertTrue(line.endswith(",30,"))  # days since, then a blank engagement


class SearchHistoryApiTest(unittest.TestCase):
    def setUp(self):
        import service.app as app_module

        patcher = mock.patch.dict(os.environ, {**SETTINGS, "APIFY_TOKEN": "t"})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.team = team.use_store(team.MemoryStore())
        self.addCleanup(team.use_store, team.MemoryStore())
        self.team.add("ana@gmail.com", "member", by="owner@gmail.com")
        self.team.add("ben@gmail.com", "member", by="owner@gmail.com")
        self.team.add("adam@gmail.com", "admin", by="owner@gmail.com")
        self.searches = search_history.use_store(search_history.memory_store())
        self.addCleanup(search_history.use_store, search_history.memory_store())
        app_module._hits.clear()

    def client(self, email):
        client = TestClient(app, follow_redirects=False)
        client.cookies.set(auth.COOKIE, auth.make_session(email, SETTINGS["SESSION_SECRET"].encode()))
        get_profiles().set_name(email, "Test Person")  # past the first-visit name step
        return client

    def search(self, email):
        with mock.patch.object(finder, "find", return_value=result()):
            return self.client(email).post("/api/post-find", json={
                "mode": "topic", "query": "land investing", "items": 7, "platforms": ["tiktok", "instagram", "reddit"]})

    def test_a_new_search_is_saved_with_who_ran_it(self):
        response = self.search("ana@gmail.com")
        self.assertEqual(response.status_code, 200)
        search_id = response.json()["history_id"]
        self.assertEqual(self.searches.owner_of(search_id), "ana@gmail.com")

        listing = self.client("ben@gmail.com").get("/api/search-history").json()
        self.assertTrue(listing["ready"])
        self.assertEqual([(s["id"], s["by"], s["items"]) for s in listing["searches"]], [(search_id, "ana@gmail.com", 7)])

        saved = self.client("ben@gmail.com").get(f"/api/search-history/{search_id}").json()
        self.assertEqual((len(saved["rows"]), saved["saved"]["by"]), (4, "ana@gmail.com"))

    def test_csv_download(self):
        search_id = self.search("ana@gmail.com").json()["history_id"]
        response = self.client("ben@gmail.com").get(f"/api/search-history/{search_id}/csv")
        self.assertEqual(response.status_code, 200)
        self.assertIn('filename="content-land-investing-2026-11-03.csv"', response.headers["content-disposition"])
        self.assertEqual(len(response.text.strip().splitlines()), 5)

    def test_a_failed_save_still_returns_the_results(self):
        search_history.use_store(BrokenStore())
        response = self.search("ana@gmail.com")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("history_id", response.json())
        listing = self.client("ana@gmail.com").get("/api/search-history").json()
        self.assertEqual((listing["ready"], listing["searches"]), (False, []))
        self.assertEqual(self.client("ana@gmail.com").get("/api/search-history/abc").status_code, 503)

    def test_missing_search_is_a_404(self):
        for path in ("/api/search-history/nothere", "/api/search-history/nothere/csv"):
            self.assertEqual(self.client("ana@gmail.com").get(path).status_code, 404, path)
        self.assertEqual(self.client("ana@gmail.com").delete("/api/search-history/nothere").status_code, 404)

    def test_who_can_delete(self):
        mine = self.searches.save(result(), by="ana@gmail.com", items=5)
        other = self.searches.save(result(), by="ana@gmail.com", items=5)
        third = self.searches.save(result(), by="ana@gmail.com", items=5)
        self.assertEqual(self.client("ben@gmail.com").delete(f"/api/search-history/{mine}").status_code, 403)
        self.assertEqual(self.client("ana@gmail.com").delete(f"/api/search-history/{mine}").status_code, 200)
        self.assertEqual(self.client("adam@gmail.com").delete(f"/api/search-history/{other}").status_code, 200)
        self.assertEqual(self.client("owner@gmail.com").delete(f"/api/search-history/{third}").status_code, 200)
        self.assertEqual(self.searches.recent(), [])

    def test_api_needs_sign_in(self):
        anon = TestClient(app, follow_redirects=False)
        self.assertEqual(anon.get("/api/search-history").status_code, 401)


if __name__ == "__main__":
    unittest.main()
