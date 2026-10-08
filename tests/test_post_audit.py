"""The Post Auditor, end to end, with Apify and Claude faked. No network required.

Media tests generate a short video with ffmpeg; they skip when no ffmpeg is
available (neither on PATH nor from the imageio-ffmpeg package).
"""

import base64
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from fastapi.testclient import TestClient

from post_audit import apify, media, metrics, pipeline
from post_audit.analysis import SCHEMA, AnalysisError, analyse, build_content
from post_audit.models import Post
from post_audit.platforms import UnsupportedURL, detect
from service.app import app
from service.security import validate

try:
    FFMPEG = media.ffmpeg_bin()
except media.MediaError:
    FFMPEG = None

TIKTOK_ITEM = {
    "id": "7543693751290481942",
    "text": "ootd ☁️ #outfit #fit",
    "createTimeISO": "2025-08-28T17:44:35.000Z",
    "authorMeta": {"name": "gretalynnhihi", "nickName": "Greta Lynn", "fans": 51200},
    "musicMeta": {"musicName": "original sound", "musicAuthor": "WYA ADRIAN | DJ"},
    "webVideoUrl": "https://www.tiktok.com/@gretalynnhihi/video/7543693751290481942",
    "mediaUrls": ["https://api.apify.com/v2/key-value-stores/abc/records/video-1"],
    "videoMeta": {"duration": 15, "coverUrl": "https://p16.tiktokcdn.com/cover.jpg",
                  "subtitleLinks": [{"language": "eng-US", "downloadLink": "https://v16.tiktokcdn.com/sub.vtt"}]},
    "diggCount": 23400, "shareCount": 145, "playCount": 145900, "collectCount": 1637, "commentCount": 46,
    "hashtags": [{"name": "outfit"}, {"name": "fit"}],
    "isSlideshow": False,
}

INSTAGRAM_ITEM = {
    "type": "Video", "caption": "The universe is full of clues. #space", "url": "https://www.instagram.com/p/DZN3mhZBQ_Q/",
    "commentsCount": 92, "likesCount": 11481, "videoPlayCount": 241820, "videoDuration": 22.5,
    "videoUrl": "https://scontent.cdninstagram.com/v.mp4", "displayUrl": "https://scontent.cdninstagram.com/t.jpg",
    "ownerFullName": "NASA Webb Telescope", "ownerUsername": "nasawebb", "timestamp": "2026-06-05T19:58:30.000Z",
    "latestComments": [{"text": "hi :)"}, {"text": "amazing"}],
}

FACEBOOK_ITEM = {
    "url": "https://www.facebook.com/reel/895509256298494/", "time": "2026-01-20T14:00:46.000Z",
    "user": {"name": "BBC Earth"}, "pageName": "bbcearth", "text": "Panda handstands.",
    "likes": 147, "comments": 2, "shares": 3, "viewsCount": 309624, "isVideo": True,
    "media": [{"thumbnail": "https://scontent.xx.fbcdn.net/thumb.jpg", "__typename": "Video"}],
}

ANALYSIS = {
    "verdict": "A fast visual reveal paired with a question keeps people watching.",
    "hook": {"opening": "Close-up, text: 'Would you wear this?'", "type": "question",
             "why_it_grabs": "Invites a snap judgement.", "score": 8, "score_reason": "Clear and fast."},
    "structure": [{"beat": "Hook", "timing": "0-3s", "what_happens": "Question on screen."}],
    "pacing": {"editing": "Cut every second.", "on_screen_text": "One line.", "audio": "Trending track."},
    "caption": {"assessment": "Short.", "cta": "", "cta_strength": "none"},
    "performance": {"read": "Saves are high relative to likes.", "signals": ["High save rate"]},
    "why_it_works": ["Question hook"],
    "takeaways": [{"title": "Open on a question", "how_to_apply": "Put the question on screen in frame one."}],
    "avoid": [],
    "limits": "",
}


def fake_client(payload=ANALYSIS, stop_reason="end_turn"):
    response = SimpleNamespace(
        stop_reason=stop_reason,
        content=[SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text=json.dumps(payload))],
        model="claude-opus-5-5",
        usage=SimpleNamespace(input_tokens=9000, output_tokens=1200),
    )
    client = mock.Mock()
    client.beta.messages.create.return_value = response
    return client


def make_video(path: Path, seconds: int = 8) -> Path:
    # Colour changes every 2s give the cut detector something to find.
    subprocess.run([
        FFMPEG, "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", f"testsrc=size=360x640:rate=15:duration={seconds}",
        "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
        "-vf", "hue=h=t*90", "-shortest", "-y", str(path),
    ], check=True)
    return path


class PlatformTest(unittest.TestCase):
    def test_detects_each_network(self):
        cases = {
            "https://www.tiktok.com/@a/video/123?is_from_webapp=1": ("tiktok", "https://www.tiktok.com/@a/video/123"),
            "vm.tiktok.com/ZMabc/": ("tiktok", "https://vm.tiktok.com/ZMabc/"),
            "https://www.instagram.com/reel/Cig/?igsh=x": ("instagram", "https://www.instagram.com/reel/Cig/"),
            "https://www.facebook.com/watch/?v=123": ("facebook", "https://www.facebook.com/watch/?v=123"),
            "https://www.linkedin.com/posts/someone_activity-1-abc": ("linkedin", "https://www.linkedin.com/posts/someone_activity-1-abc"),
        }
        for url, expected in cases.items():
            self.assertEqual(detect(url), expected, url)

    def test_rejects_other_links(self):
        for url in ("", "https://youtube.com/watch?v=1", "https://www.tiktok.com/", "ftp://instagram.com/p/1",
                    "https://notinstagram.com/p/1", "https://instagram.com.evil.example/p/1"):
            with self.assertRaises(UnsupportedURL, msg=url):
                detect(url)


class NormaliseTest(unittest.TestCase):
    def test_tiktok(self):
        post, extras = apify.normalize("tiktok", TIKTOK_ITEM, "u")
        self.assertEqual((post.author, post.author_handle, post.author_followers), ("Greta Lynn", "gretalynnhihi", 51200))
        self.assertEqual((post.views, post.likes, post.comments, post.shares, post.saves), (145900, 23400, 46, 145, 1637))
        self.assertEqual(post.hashtags, ["outfit", "fit"])
        self.assertEqual(post.media_type, "video")
        self.assertTrue(post.video_url.startswith("https://api.apify.com/"))
        self.assertEqual(extras["subtitle_links"][0]["language"], "eng-US")

    def test_instagram(self):
        post, _ = apify.normalize("instagram", INSTAGRAM_ITEM, "u")
        self.assertEqual((post.views, post.likes, post.comments), (241820, 11481, 92))
        self.assertEqual(post.media_type, "video")
        self.assertEqual(post.top_comments, ["hi :)", "amazing"])
        self.assertEqual(post.hashtags, ["space"])

    def test_facebook_without_video_file_uses_thumbnails(self):
        post, _ = apify.normalize("facebook", FACEBOOK_ITEM, "u")
        self.assertEqual((post.views, post.likes, post.shares), (309624, 147, 3))
        self.assertEqual(post.media_type, "video")
        self.assertEqual(post.video_url, "")
        self.assertEqual(post.image_urls, ["https://scontent.xx.fbcdn.net/thumb.jpg"])

    def test_linkedin_tolerates_unknown_shape(self):
        post, _ = apify.normalize("linkedin", {"text": "Hiring! #jobs", "numLikes": "1,204", "authorName": "Ana"}, "u")
        self.assertEqual((post.likes, post.author, post.media_type, post.hashtags), (1204, "Ana", "text", ["jobs"]))

    def test_scraper_error_items_are_reported(self):
        with mock.patch.object(apify, "fetch_items", return_value=[{"error": "Post not found"}]):
            with self.assertRaisesRegex(apify.ScrapeError, "Post not found"):
                apify.fetch_post("tiktok", "https://www.tiktok.com/@a/video/1", "t")

    def test_http_errors_become_readable(self):
        for code, text in ((401, "token"), (402, "credit"), (408, "too long")):
            fake = mock.Mock(status_code=code)
            with mock.patch.object(apify.requests, "post", return_value=fake):
                with self.assertRaisesRegex(apify.ScrapeError, text):
                    apify.fetch_items("tiktok", "https://www.tiktok.com/@a/video/1", "t")


class MetricsTest(unittest.TestCase):
    def test_rates(self):
        post = Post(platform="tiktok", url="u", views=10000, likes=800, comments=50, shares=100, saves=50,
                    author_followers=5000, duration_seconds=20)
        m = metrics.compute(post, cuts=8)
        self.assertEqual(m["interactions"], 1000)
        self.assertEqual(m["engagement_rate_by_views"], 10.0)
        self.assertEqual(m["engagement_rate_by_followers"], 20.0)
        self.assertEqual(m["views_per_follower"], 2.0)
        self.assertEqual(m["save_rate"], 0.5)
        self.assertEqual(m["cuts_per_10s"], 4.0)

    def test_missing_numbers_stay_missing(self):
        m = metrics.compute(Post(platform="linkedin", url="u", likes=10))
        self.assertIsNone(m["engagement_rate_by_views"])
        self.assertEqual(m["interactions"], 10)


class MediaHelpersTest(unittest.TestCase):
    def test_vtt_to_text(self):
        vtt = "WEBVTT\n\n1\n00:00:00.000 --> 00:00:01.500\n<c>Stop scrolling.</c>\n\n2\n00:00:01.500 --> 00:00:03.000\nStop scrolling.\nHere is why.\n"
        self.assertEqual(media.vtt_to_text(vtt), "Stop scrolling. Here is why.")

    def test_frame_times_are_dense_in_the_hook(self):
        times = media.frame_times(30)
        hook = [t for t, label in times if label == "hook"]
        self.assertEqual(len(hook), 6)
        self.assertTrue(all(t <= 3 for t in hook))
        self.assertEqual(len([t for t, label in times if label == "body"]), 6)
        self.assertTrue(all(t < 30 for t, _ in times))
        # A 2-second clip cannot have frames past its end.
        self.assertTrue(all(t <= 2 for t, _ in media.frame_times(2)))

    def test_download_refuses_plain_http_and_private_hosts(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(media.MediaError):
                media.download("http://example.com/v.mp4", Path(tmp) / "v", validate, 100)
            with self.assertRaisesRegex(media.MediaError, "reserved or private"):
                media.download("https://127.0.0.1/v.mp4", Path(tmp) / "v", validate, 100)

    def test_download_drops_credentials_on_redirect(self):
        calls = []

        def fake_get(url, **kwargs):
            calls.append((url, kwargs["headers"].get("Authorization")))
            if len(calls) == 1:
                return mock.Mock(is_redirect=True, status_code=302, headers={"location": "https://cdn.example.com/v.mp4"})
            response = mock.MagicMock(is_redirect=False, status_code=200)
            response.iter_content.return_value = [b"data"]
            response.__enter__.return_value = response
            return response

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(media.requests, "get", side_effect=fake_get):
            media.download("https://api.apify.com/v2/x", Path(tmp) / "v", lambda url: None, 100,
                           headers={"Authorization": "Bearer secret"})
        self.assertEqual(calls[0][1], "Bearer secret")
        self.assertIsNone(calls[1][1])


@unittest.skipUnless(FFMPEG, "ffmpeg not available")
class VideoTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.video = make_video(self.tmp / "clip.mp4")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_frames_cuts_and_audio(self):
        duration = media.probe_duration(self.video)
        self.assertAlmostEqual(duration, 8, delta=0.3)
        frames = media.extract_frames(self.video, self.tmp, duration)
        self.assertGreaterEqual(len(frames), 10)
        self.assertEqual(frames[0].seconds, 0.0)
        self.assertTrue(all(f.path.stat().st_size > 0 for f in frames))
        self.assertIsNotNone(media.count_cuts(self.video))
        self.assertTrue(media.has_audio(self.video))
        self.assertIsNotNone(media.extract_audio(self.video, self.tmp / "a.wav"))

    def test_audio_reaches_whisper_as_samples(self):
        from post_audit import transcribe

        samples = transcribe._samples(media.extract_audio(self.video, self.tmp / "a.wav"))
        self.assertEqual(str(samples.dtype), "float32")
        self.assertAlmostEqual(len(samples) / 16000, 8, delta=0.2)
        self.assertLessEqual(float(abs(samples).max()), 1.0)

    def test_audit_post_end_to_end(self):
        def fake_download(url, dest, check, max_bytes, headers=None, timeout=60):
            if url.endswith(".vtt"):
                dest.write_text("WEBVTT\n\n00:00.000 --> 00:02.000\nWould you wear this?\n")
            else:
                shutil.copy(self.video, dest)
            return dest

        post, extras = apify.normalize("tiktok", TIKTOK_ITEM, "u")
        client = fake_client()
        with mock.patch.object(apify, "fetch_post", return_value=(post, extras)), \
             mock.patch.object(media, "download", side_effect=fake_download):
            report = pipeline.audit_post("https://www.tiktok.com/@gretalynnhihi/video/7543693751290481942",
                                         self.tmp, apify_token="t", check=validate, client=client)

        self.assertEqual(report["platform"], "tiktok")
        self.assertEqual(report["analysis"]["hook"]["score"], 8)
        self.assertEqual(report["post"]["transcript"], "Would you wear this?")
        self.assertNotIn("video_url", report["post"])
        self.assertTrue(report["frames"][0]["src"].startswith("data:image/jpeg;base64,"))
        self.assertIsNotNone(report["metrics"]["cuts"])

        kwargs = client.beta.messages.create.call_args.kwargs
        images = [b for b in kwargs["messages"][0]["content"] if b["type"] == "image"]
        self.assertEqual(len(images), len(report["frames"]))
        self.assertEqual(kwargs["output_config"]["format"]["schema"], SCHEMA)
        self.assertEqual(kwargs["fallbacks"], "default")


    def test_unreadable_video_falls_back_to_the_thumbnail(self):
        broken = self.tmp / "broken.mp4"
        broken.write_bytes(b"not a video")
        cover = self.tmp / "cover.jpg"
        media.extract_frames(self.video, self.tmp, 1.0)[0].path.rename(cover)

        def fake_download(url, dest, check, max_bytes, headers=None, timeout=60):
            shutil.copy(cover if "cover" in url else broken, dest)
            return dest

        post, _ = apify.normalize("tiktok", TIKTOK_ITEM, "u")
        with mock.patch.object(apify, "fetch_post", return_value=(post, {})), \
             mock.patch.object(media, "download", side_effect=fake_download):
            report = pipeline.audit_post("https://www.tiktok.com/@a/video/1", self.tmp,
                                         apify_token="t", check=validate, client=fake_client())
        self.assertEqual([f["label"] for f in report["frames"]], ["image 1"])
        self.assertTrue(any("thumbnail" in note for note in report["notes"]))


class AnalysisTest(unittest.TestCase):
    def test_refusal_and_bad_json_are_errors(self):
        post = Post(platform="instagram", url="u", caption="hi")
        with self.assertRaisesRegex(AnalysisError, "declined"):
            analyse(post, {}, [], client=fake_client(stop_reason="refusal"))
        bad = fake_client()
        bad.beta.messages.create.return_value.content = [SimpleNamespace(type="text", text="not json")]
        with self.assertRaises(AnalysisError):
            analyse(post, {}, [], client=bad)

    def test_content_says_what_is_missing(self):
        text = json.dumps(build_content(Post(platform="linkedin", url="u", caption="hi"), {}, []))
        self.assertIn("Transcript: none available.", text)
        self.assertIn("No images or video frames", text)

    def test_schema_requires_every_field(self):
        def check(node):
            if node.get("type") == "object":
                self.assertFalse(node["additionalProperties"])
                self.assertEqual(set(node["required"]), set(node["properties"]))
                for child in node["properties"].values():
                    check(child)
            if node.get("type") == "array":
                check(node["items"])
        check(SCHEMA)
        self.assertEqual(set(SCHEMA["required"]), set(ANALYSIS))


class PostAuditHTTPTest(unittest.TestCase):
    def setUp(self):
        import service.app as app_module

        self.app_module = app_module
        app_module._hits.clear()
        self.client = TestClient(app)

    def test_page_is_served_and_linked_from_home(self):
        response = self.client.get("/post-audit")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Redefine Post Auditor", response.text)
        self.assertIn('href="/post-audit"', self.client.get("/").text)

    def test_unconfigured_server_says_so(self):
        with mock.patch.dict(os.environ, {"APIFY_TOKEN": "", "ANTHROPIC_API_KEY": ""}):
            response = self.client.post("/api/post-audit", json={"url": "https://www.tiktok.com/@a/video/1"})
        self.assertEqual(response.status_code, 503)
        self.assertIn("APIFY_TOKEN", response.json()["detail"])

    def test_bad_link_is_a_400_and_refunded(self):
        with mock.patch.dict(os.environ, {"APIFY_TOKEN": "t", "ANTHROPIC_API_KEY": "k"}):
            response = self.client.post("/api/post-audit", json={"url": "https://youtube.com/watch?v=1"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.app_module._hits, {})

    def test_success_returns_the_report(self):
        report = {"platform": "tiktok", "analysis": ANALYSIS}
        with mock.patch.dict(os.environ, {"APIFY_TOKEN": "t", "ANTHROPIC_API_KEY": "k"}), \
             mock.patch.object(self.app_module, "audit_post", return_value=report) as run:
            response = self.client.post("/api/post-audit", json={"url": "https://www.tiktok.com/@a/video/1"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), report)
        self.assertTrue(report["history_id"])  # saved to the team history
        self.assertEqual(run.call_args.kwargs["check"], validate)

    def test_scraper_failure_is_a_502_with_its_message(self):
        with mock.patch.dict(os.environ, {"APIFY_TOKEN": "t", "ANTHROPIC_API_KEY": "k"}), \
             mock.patch.object(self.app_module, "audit_post", side_effect=apify.ScrapeError("Apify is out of credit.")):
            response = self.client.post("/api/post-audit", json={"url": "https://www.tiktok.com/@a/video/1"})
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["detail"], "Apify is out of credit.")
        self.assertEqual(self.app_module._hits, {})




FACEBOOK_VIDEO_ITEM = {
    "success": True, "videoId": "1807693670071567", "videoUrl": "https://www.facebook.com/reel/1807693670071567/",
    "caption": "Wait for the end #pets", "creatorName": "ahmedesam32", "creatorFollowers": 120000,
    "publishedAt": "2026-09-20T10:00:00Z", "durationSeconds": 28.4, "viewCount": 4100000,
    "reactionCount": 300763, "commentCount": 2061, "shareCount": 5400,
    "videoMp4HdUrl": "https://video.xx.fbcdn.net/hd.mp4", "videoMp4SdUrl": "https://video.xx.fbcdn.net/sd.mp4",
    "thumbnailUrl": "https://scontent.xx.fbcdn.net/thumb.jpg", "transcript": "Wait for it.",
}


class FacebookVideoTest(unittest.TestCase):
    def test_reel_links_go_to_the_video_scraper(self):
        for url in ("https://www.facebook.com/reel/1807693670071567", "https://www.facebook.com/watch/?v=1",
                    "https://fb.watch/abc/", "https://www.facebook.com/page/videos/123/"):
            self.assertEqual(apify.source_for("facebook", url), "facebook_video", url)
        self.assertEqual(apify.source_for("facebook", "https://www.facebook.com/page/posts/pfbid0x"), "facebook")
        self.assertEqual(apify.source_for("instagram", "https://www.instagram.com/reel/x/"), "instagram")
        self.assertEqual(apify.actor_input("facebook_video", "u")["startUrls"], ["u"])

    def test_video_item_normalises_with_the_sd_file_and_captions(self):
        post, _ = apify.normalize("facebook_video", FACEBOOK_VIDEO_ITEM, "u")
        self.assertEqual(post.platform, "facebook")
        self.assertEqual((post.views, post.likes, post.comments, post.shares), (4100000, 300763, 2061, 5400))
        self.assertEqual(post.video_url, "https://video.xx.fbcdn.net/sd.mp4")
        self.assertEqual((post.transcript, post.author_followers), ("Wait for it.", 120000))
        self.assertEqual(post.hashtags, ["pets"])

    def test_failed_video_item_is_an_error(self):
        with mock.patch.object(apify, "fetch_items", return_value=[{"success": False, "videoStatus": "private"}]):
            with self.assertRaisesRegex(apify.ScrapeError, "private"):
                apify.fetch_post("facebook", "https://www.facebook.com/reel/1/", "t")

    def test_reel_link_uses_one_call_to_the_video_scraper(self):
        with mock.patch.object(apify, "fetch_items", return_value=[FACEBOOK_VIDEO_ITEM]) as fetch:
            post, _ = apify.fetch_post("facebook", "https://www.facebook.com/reel/1807693670071567", "t")
        self.assertEqual([c.args[0] for c in fetch.call_args_list], ["facebook_video"])
        self.assertTrue(post.video_url)

    def test_post_link_hiding_a_video_retries_with_the_video_scraper(self):
        counts_only = {"url": "https://www.facebook.com/page/posts/1", "likes": 300763, "isVideo": True}
        with mock.patch.object(apify, "fetch_items", side_effect=[[counts_only], [FACEBOOK_VIDEO_ITEM]]) as fetch:
            post, _ = apify.fetch_post("facebook", "https://www.facebook.com/page/posts/1", "t")
        self.assertEqual([c.args[0] for c in fetch.call_args_list], ["facebook", "facebook_video"])
        self.assertEqual(post.caption, "Wait for the end #pets")


class FacebookShareLinkTest(unittest.TestCase):
    def _redirects(self, *locations):
        responses = [mock.Mock(is_redirect=True, headers={"location": loc}) for loc in locations]
        responses.append(mock.Mock(is_redirect=False, headers={}))
        return mock.patch.object(pipeline.requests, "get", side_effect=responses)

    def test_share_link_resolves_to_the_reel(self):
        with self._redirects("https://www.facebook.com/reel/895509256298494/?mibextid=abc"):
            url = pipeline.resolve_share_link("facebook", "https://www.facebook.com/share/r/1DyZ/", lambda u: None)
        self.assertEqual(url, "https://www.facebook.com/reel/895509256298494/")

    def test_login_wall_and_other_sites_keep_the_original(self):
        original = "https://www.facebook.com/share/r/1DyZ/"
        for target in ("https://www.facebook.com/login/?next=x", "https://evil.example/reel/1"):
            with self._redirects(target):
                self.assertEqual(pipeline.resolve_share_link("facebook", original, lambda u: None), original)

    def test_other_links_are_not_touched(self):
        with mock.patch.object(pipeline.requests, "get") as get:
            for platform, url in (("facebook", "https://www.facebook.com/reel/1/"),
                                  ("tiktok", "https://www.tiktok.com/@a/video/1")):
                self.assertEqual(pipeline.resolve_share_link(platform, url, lambda u: None), url)
        get.assert_not_called()


class NotEnoughToAuditTest(unittest.TestCase):
    def test_counts_only_post_never_reaches_claude(self):
        post, _ = apify.normalize("facebook", {"likes": 300763, "comments": 2061, "user": {"name": "a"}}, "u")
        client = fake_client()
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(apify, "fetch_post", return_value=(post, {})):
            with self.assertRaisesRegex(pipeline.NotEnoughToAudit, "no caption, video or images"):
                pipeline.audit_post("https://www.facebook.com/reel/1/", Path(tmp),
                                    apify_token="t", check=validate, client=client)
        client.beta.messages.create.assert_not_called()

    def test_api_answers_422(self):
        import service.app as app_module

        app_module._hits.clear()
        with mock.patch.dict(os.environ, {"APIFY_TOKEN": "t", "ANTHROPIC_API_KEY": "k"}), \
             mock.patch.object(app_module, "audit_post", side_effect=pipeline.NotEnoughToAudit("nothing")):
            response = TestClient(app).post("/api/post-audit", json={"url": "https://www.facebook.com/reel/1/"})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(app_module._hits, {})


class PostPdfTest(unittest.TestCase):
    REPORT = {
        "platform": "tiktok", "platform_label": "TikTok",
        "post": {"url": "https://www.tiktok.com/@a/video/1", "author": "Greta Lynn", "author_handle": "greta",
                 "caption": "ootd", "views": 1000, "likes": 100},
        "metrics": {"engagement_rate_by_views": 10.0},
        "analysis": ANALYSIS,
        # A remote image must never be fetched by the server's browser.
        "frames": [{"seconds": 0.0, "label": "hook", "src": "https://example.com/tracker.jpg"}],
        "notes": [],
    }

    def test_prints_a_pdf_without_fetching_anything_else(self):
        from service import post_pdf

        seen = []
        original = post_pdf._route

        def spy(route, request):
            seen.append(request.url)
            original(route, request)

        with mock.patch.object(post_pdf, "_route", new=spy):  # a plain function: Playwright reads its arity
            report = post_pdf.attach_pdf(json.loads(json.dumps(self.REPORT)))
        pdf = base64.b64decode(report["pdf"]["base64"])
        self.assertTrue(pdf.startswith(b"%PDF"))
        self.assertGreater(len(pdf), 5000)
        self.assertEqual(report["pdf"]["filename"], "tiktok-greta-audit.pdf")
        # A remote "frame" never reaches the template, so nothing outside the
        # brand fonts is even requested.
        self.assertFalse([u for u in seen if not (u.startswith("data:") or post_pdf.ALLOWED.match(u))], seen)

    def test_template_escapes_scraped_text_and_lays_out_the_summary(self):
        from service import post_pdf

        report = json.loads(json.dumps(self.REPORT))
        report["post"].update(caption="<script>alert(1)</script> nice", duration_seconds=15, likes=10200,
                              posted_at="2026-03-09T10:00:00Z")
        report["frames"] = [{"seconds": 0.0, "label": "hook", "src": "data:image/jpeg;base64,AAAA"},
                            {"seconds": 0.6, "label": "hook", "src": "data:image/jpeg;base64,BBBB"}]
        html = post_pdf.render_html(report)
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("&lt;script&gt;", html)
        self.assertIn("Greta Lynn", html)
        self.assertIn("Posted 9 March 2026", html)
        self.assertIn(">10K<", html)                 # likes, shortened
        self.assertIn("Open on a question", html)     # takeaway in the at-a-glance box
        self.assertIn("data:image/svg+xml;base64,", html)  # the vector logo
        self.assertIn("timeline", html)               # "0-3s" parses onto the bar

    def test_compact_numbers(self):
        from service.post_pdf import compact

        cases = {164: "164", 1000: "1K", 1050: "1.1K", 10200: "10K", 145900: "146K",
                 999_999: "1M", 1_400_000: "1.4M", None: None}
        for n, expected in cases.items():
            self.assertEqual(compact(n), expected, n)

    def test_timeline_only_when_every_beat_has_times(self):
        from service.post_pdf import timeline

        beats = [{"timing": "0-0.6s"}, {"timing": "0.6–~10s (inferred)"}, {"timing": "~20-29s"}]
        bar = timeline(beats, 29)
        self.assertEqual([s["left"] for s in bar["segments"]], [0.0, 2.07, 68.97])
        self.assertEqual(bar["ticks"][-1], 29)
        self.assertIsNone(timeline(beats + [{"timing": "slide 2"}], 29))
        self.assertIsNone(timeline(beats, None))

    def test_pdf_failure_still_returns_the_report(self):
        import service.app as app_module

        app_module._hits.clear()
        with mock.patch.dict(os.environ, {"APIFY_TOKEN": "t", "ANTHROPIC_API_KEY": "k"}), \
             mock.patch.object(app_module, "audit_post", return_value={"platform": "tiktok"}), \
             mock.patch("service.post_pdf.render_pdf", side_effect=RuntimeError("no chromium")):
            response = TestClient(app).post("/api/post-audit", json={"url": "https://www.tiktok.com/@a/video/1"})
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("pdf", response.json())




class BrandAssetsTest(unittest.TestCase):
    def test_logo_and_favicons_are_served_and_linked(self):
        client = TestClient(app)
        icon = client.get("/favicon.ico")
        self.assertEqual(icon.status_code, 200)
        self.assertEqual(icon.headers["content-type"], "image/x-icon")
        for path in ("/static/logo.svg", "/static/favicon.svg", "/static/apple-touch-icon.png"):
            self.assertEqual(client.get(path).status_code, 200, path)
        for page in ("/", "/website-audit", "/post-audit"):
            html = client.get(page).text
            self.assertIn('href="/static/favicon.svg"', html, page)
            self.assertIn('src="/static/logo.svg"', html, page)

    def test_website_audit_pdf_uses_the_vector_logo(self):
        from website_audit.report import brand_logo

        self.assertTrue(brand_logo().startswith("data:image/svg+xml;base64,"))


if __name__ == "__main__":
    unittest.main()
