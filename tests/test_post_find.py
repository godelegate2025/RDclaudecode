"""The content finder: search parsing, actor inputs, row shape, and the endpoint. Apify is faked."""

import os
import unittest
from datetime import datetime, timezone
from unittest import mock

from fastapi.testclient import TestClient

from post_audit import finder
from post_audit.apify import ScrapeError
from service.app import app

NOW = datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc)

TIKTOK = {
    "text": "Geh Geh Shared  land  tips", "createTimeISO": "2025-09-19T10:00:00.000Z",
    "authorMeta": {"name": "barry_job", "nickName": "Barry"},
    "webVideoUrl": "https://www.tiktok.com/@barry_job/video/1",
    "playCount": 51200, "diggCount": 2245, "commentCount": 212, "shareCount": 613,
}
TWEET = {
    "fullText": "Communication is everything", "url": "https://x.com/garyvee/status/1",
    "createdAt": "Sat Jun 05 14:00:00 +0000 2021", "author": {"userName": "garyvee"},
    "viewCount": 29200000, "likeCount": 4900000, "replyCount": 17400, "retweetCount": 100000, "quoteCount": 18100,
}


class SearchParsingTest(unittest.TestCase):
    def test_topic_keeps_the_words(self):
        search = finder.parse_search("topic", "  land   investing ")
        self.assertEqual((search.mode, search.term), ("topic", "land investing"))

    def test_handle_drops_the_at(self):
        self.assertEqual(finder.parse_search("profile", "@garyvee").handle, "garyvee")

    def test_profile_links_name_their_platform(self):
        for link, platform, handle in (
            ("https://www.tiktok.com/@garyvee?lang=en", "tiktok", "garyvee"),
            ("instagram.com/garyvee/", "instagram", "garyvee"),
            ("https://www.youtube.com/@GaryVee", "youtube", "GaryVee"),
            ("https://www.linkedin.com/in/garyvaynerchuk/", "linkedin", "garyvaynerchuk"),
            ("https://x.com/garyvee", "x", "garyvee"),
            ("https://www.reddit.com/user/spez/", "reddit", "spez"),
            ("https://www.facebook.com/gary", "facebook", "gary"),
        ):
            search = finder.parse_search("profile", link)
            self.assertEqual((search.url_platform, search.handle), (platform, handle), link)
            self.assertNotIn("?", search.url)

    def test_facebook_profile_php_keeps_its_id(self):
        search = finder.parse_search("profile", "https://www.facebook.com/profile.php?id=100064792144187&ref=x")
        self.assertEqual(search.url, "https://www.facebook.com/profile.php?id=100064792144187&ref=x")

    def test_bad_searches_say_why(self):
        for mode, raw in (("topic", "  "), ("profile", "not a handle!"), ("profile", "https://example.com/a"),
                          ("other", "x"), ("topic", "x" * 201)):
            with self.assertRaises(finder.FindError, msg=(mode, raw)):
                finder.parse_search(mode, raw)

    def test_platforms_and_items_are_cleaned(self):
        self.assertEqual(finder.clean_platforms(["x", "tiktok", "myspace"]), ["tiktok", "x"])
        with self.assertRaises(finder.FindError):
            finder.clean_platforms([])
        self.assertEqual((finder.clean_items(0), finder.clean_items(500), finder.clean_items("7")), (10, 50, 7))


class ActorInputTest(unittest.TestCase):
    def test_every_platform_has_an_input_for_both_modes(self):
        topic = finder.parse_search("topic", "land investing")
        profile = finder.parse_search("profile", "@garyvee")
        for platform in finder.PLATFORMS:
            for search in (topic, profile):
                actor, payload = finder.actor_call(platform, search, 5)
                self.assertIn("~", actor)
                self.assertTrue(payload)

    def test_instagram_topic_uses_reel_search(self):
        actor, payload = finder.actor_call("instagram", finder.parse_search("topic", "land investing"), 20)
        self.assertEqual(actor, finder.DEFAULT_ACTORS["instagram_search"])
        self.assertEqual(payload, {"query": "land investing", "maxPages": 2})

    def test_several_word_topics_are_phrases_where_search_is_loose(self):
        topic = finder.parse_search("topic", "land investing")
        self.assertEqual(finder.actor_call("x", topic, 5)[1]["searchTerms"],
                         [f'"land investing" min_faves:{finder.X_MIN_LIKES}'])
        self.assertEqual(finder.actor_call("reddit", topic, 5)[1]["queries"], ['"land investing"'])
        self.assertIn("keyword_exact_phrase", finder.actor_call("meta_ads", topic, 5)[1]["startUrls"][0]["url"])
        self.assertEqual(finder.actor_call("reddit", finder.parse_search("topic", "land"), 5)[1]["queries"], ["land"])

    def test_reddit_profile_is_an_author_search(self):
        _, payload = finder.actor_call("reddit", finder.parse_search("profile", "@spez"), 5)
        self.assertEqual((payload["queries"], payload["sort"]), (["author:spez"], "new"))

    def test_pasted_link_is_used_on_its_own_platform_only(self):
        search = finder.parse_search("profile", "https://www.youtube.com/@GaryVee/videos")
        _, shorts = finder.actor_call("youtube_shorts", search, 5)
        self.assertEqual(shorts["startUrls"], [{"url": "https://www.youtube.com/@GaryVee/videos"}])
        self.assertEqual((shorts["maxResults"], shorts["maxResultsShorts"]), (0, 5))
        _, tiktok = finder.actor_call("tiktok", search, 5)
        self.assertEqual(tiktok["profiles"], ["GaryVee"])

    def test_facebook_topic_searches_and_profile_reads_the_page(self):
        actor, payload = finder.actor_call("facebook", finder.parse_search("topic", "land investing"), 7)
        self.assertEqual(actor, finder.DEFAULT_ACTORS["facebook_search"])
        self.assertEqual(payload, {"query": "land investing", "resultsCount": 7, "searchType": "top"})
        actor, payload = finder.actor_call("facebook", finder.parse_search("profile", "@redefineph"), 7)
        self.assertEqual(actor, finder.DEFAULT_ACTORS["facebook"])
        self.assertEqual(payload["startUrls"], [{"url": "https://www.facebook.com/redefineph"}])

    def test_a_facebook_link_also_finds_that_pages_ads(self):
        search = finder.parse_search("profile", "https://www.facebook.com/redefineph/")
        _, ads = finder.actor_call("meta_ads", search, 5)
        self.assertEqual(ads["startUrls"], [{"url": "https://www.facebook.com/redefineph/"}])

    def test_linkedin_profile_uses_the_posts_actor(self):
        actor, _ = finder.actor_call("linkedin", finder.parse_search("profile", "@ana"), 5)
        self.assertEqual(actor, finder.DEFAULT_ACTORS["linkedin_profile"])

    def test_actor_can_be_swapped_by_environment(self):
        with mock.patch.dict(os.environ, {"APIFY_FIND_ACTOR_X": "someone/other-scraper"}):
            actor, _ = finder.actor_call("x", finder.parse_search("topic", "a"), 5)
        self.assertEqual(actor, "someone~other-scraper")


class RowTest(unittest.TestCase):
    def test_tiktok_row_matches_the_sheet(self):
        row = finder.finish(finder.NORMALISERS["tiktok"](TIKTOK), "land investing", NOW)
        self.assertEqual(row["author"], "barry_job")
        self.assertEqual(row["content"], "Geh Geh Shared land tips")
        self.assertEqual((row["posted_at"], row["days_since"]), ("2025-09-19", 410))
        self.assertEqual(row["engagement"], 7.5)  # (2245 + 212 + 613) / 410, a decimal under 10
        self.assertTrue(row["auditable"])
        self.assertEqual(row["search_term"], "land investing")

    def test_x_reads_its_own_date_format_and_counts_quotes_as_shares(self):
        row = finder.finish(finder.NORMALISERS["x"](TWEET), "@garyvee", NOW)
        self.assertEqual(row["posted_at"], "2021-06-05")
        self.assertEqual(row["shares"], 118100)
        self.assertFalse(row["auditable"])

    def test_missing_counts_leave_engagement_blank(self):
        row = finder.finish(finder.NORMALISERS["meta_ads"](
            {"adArchiveID": "123", "pageName": "Acme", "snapshot": {"body": {"text": "Buy land"}}, "startDate": 1760000000}
        ), "land", NOW)
        self.assertEqual(row["url"], "https://www.facebook.com/ads/library/?id=123")
        self.assertIsNone(row["engagement"])
        self.assertEqual(row["posted_at"], "2025-10-09")

    def test_post_from_today_counts_as_one_day(self):
        item = dict(TIKTOK, createTimeISO=NOW.isoformat())
        self.assertEqual(finder.finish(finder.NORMALISERS["tiktok"](item), "t", NOW)["engagement"], 3070)

    def test_instagram_reel_search_shape(self):
        item = {"code": "Dekb_3USxDZ", "product_type": "clips", "taken_at": 1792184088,
                "caption": {"text": "Comment LAND for the ebook"}, "user": {"username": "landflippingsimplified"},
                "play_count": 10052855, "like_count": 35198, "comment_count": 455, "share_count": 1601}
        row = finder.finish(finder.NORMALISERS["instagram"](item), "land investing", NOW)
        self.assertEqual(row["url"], "https://www.instagram.com/reel/Dekb_3USxDZ/")
        self.assertEqual((row["author"], row["content"]), ("landflippingsimplified", "Comment LAND for the ebook"))
        self.assertEqual((row["views"], row["likes"], row["comments"], row["shares"]), (10052855, 35198, 455, 1601))
        self.assertTrue(row["auditable"])

    def test_facebook_search_and_page_shapes(self):
        searched = {"url": "https://www.facebook.com/example/posts/123456789012345", "timestamp": 1743735597000,
                    "postText": "Land deal closed", "reactionsCount": 25, "commentsCount": 3, "sharesCount": 2,
                    "author": {"name": "Example Author"}}
        row = finder.finish(finder.NORMALISERS["facebook"](searched), "land", NOW)
        self.assertEqual((row["author"], row["content"], row["posted_at"]), ("Example Author", "Land deal closed", "2025-04-04"))
        self.assertEqual((row["likes"], row["comments"], row["shares"]), (25, 3, 2))
        self.assertTrue(row["auditable"])
        page = {"url": "https://www.facebook.com/cern/posts/pfbid02", "time": "2026-01-20T13:00:05.000Z",
                "user": {"name": "CERN"}, "pageName": "cern", "text": "Higgs", "likes": 147, "comments": 2,
                "shares": 3, "viewsCount": 309624}
        row = finder.finish(finder.NORMALISERS["facebook"](page), "@cern", NOW)
        self.assertEqual((row["author"], row["views"], row["likes"], row["posted_at"]), ("CERN", 309624, 147, "2026-01-20"))

    def test_reddit_uses_the_thread_not_the_linked_article(self):
        item = {"title": "Investing in land", "selftext": "", "author": "mrgrassydassy", "score": 326,
                "num_comments": 137, "created_iso": "2025-02-18T10:00:00.000Z",
                "permalink": "https://www.reddit.com/r/investing/comments/1isdous/x/", "url": "https://example.com/a"}
        row = finder.finish(finder.NORMALISERS["reddit"](item), "land", NOW)
        self.assertEqual(row["url"], "https://www.reddit.com/r/investing/comments/1isdous/x/")
        self.assertEqual((row["likes"], row["comments"], row["posted_at"]), (326, 137, "2025-02-18"))

    def test_meta_ads_are_never_auditable(self):
        row = finder.finish(finder.NORMALISERS["meta_ads"]({"adArchiveID": "9", "pageName": "Acme"}), "land", NOW)
        self.assertFalse(row["auditable"])

    def test_non_links_are_dropped(self):
        row = finder.NORMALISERS["reddit"]({"title": "Hi", "url": "javascript:alert(1)", "upVotes": 3})
        self.assertEqual(row["url"], "")


class FindTest(unittest.TestCase):
    def test_one_failing_platform_does_not_hide_the_rest(self):
        def fake(actor, payload, token, timeout_s):
            if "tweet" in actor:
                raise ScrapeError("The Apify account is out of credit for this month.")
            return [TIKTOK, {"error": "blocked"}]

        with mock.patch.object(finder, "run_actor", side_effect=fake):
            result = finder.find(finder.parse_search("topic", "land"), ["tiktok", "x"], 10, "t")
        self.assertEqual(len(result["rows"]), 1)
        self.assertEqual(result["errors"], [{"platform": "x", "label": "X",
                                             "message": "The Apify account is out of credit for this month."}])

    def test_facebook_page_results_are_unwrapped(self):
        page = [{"inputUrl": "https://www.facebook.com/acme", "totalCount": 1,
                 "results": [{"adArchiveID": "7", "pageName": "Acme", "snapshot": {"body": {"text": "Buy land"}}}]}]
        with mock.patch.object(finder, "run_actor", return_value=page):
            result = finder.find(finder.parse_search("profile", "@acme"), ["meta_ads"], 5, "t")
        self.assertEqual([r["content"] for r in result["rows"]], ["Buy land"])

    def test_rows_rank_by_engagement_and_respect_the_item_count(self):
        items = [dict(TIKTOK, diggCount=n, webVideoUrl=f"https://www.tiktok.com/@a/video/{n}") for n in (10, 9000, 500)]
        with mock.patch.object(finder, "run_actor", return_value=items):
            result = finder.find(finder.parse_search("topic", "land"), ["tiktok"], 2, "t")
        self.assertEqual([r["likes"] for r in result["rows"]], [9000, 500])


class FindHTTPTest(unittest.TestCase):
    def setUp(self):
        import service.app as app_module

        self.app_module = app_module
        app_module._hits.clear()
        self.client = TestClient(app)

    def post(self, **body):
        payload = {"mode": "topic", "query": "land investing", "items": 5, "platforms": ["tiktok"], **body}
        return self.client.post("/api/post-find", json=payload)

    def test_unconfigured_server_says_so(self):
        with mock.patch.dict(os.environ, {"APIFY_TOKEN": ""}):
            response = self.post()
        self.assertEqual(response.status_code, 503)

    def test_bad_search_is_a_400_and_costs_nothing(self):
        with mock.patch.dict(os.environ, {"APIFY_TOKEN": "t"}):
            response = self.post(platforms=[])
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.app_module._hits, {})

    def test_success_returns_rows(self):
        with mock.patch.dict(os.environ, {"APIFY_TOKEN": "t"}), \
             mock.patch.object(finder, "run_actor", return_value=[TIKTOK]):
            response = self.post()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["rows"][0]["url"], TIKTOK["webVideoUrl"])

    def test_nothing_found_is_a_502_with_reasons_and_refunded(self):
        with mock.patch.dict(os.environ, {"APIFY_TOKEN": "t"}), \
             mock.patch.object(finder, "run_actor", side_effect=ScrapeError("Apify rejected the API token.")):
            response = self.post()
        self.assertEqual(response.status_code, 502)
        self.assertIn("TikTok: Apify rejected the API token.", response.json()["detail"])
        self.assertTrue(all(not hits for hits in self.app_module._hits.values()))

    def test_page_has_the_finder(self):
        page = self.client.get("/post-audit").text
        self.assertIn('id="findForm"', page)
        self.assertIn("/api/post-find", page)


if __name__ == "__main__":
    unittest.main()
