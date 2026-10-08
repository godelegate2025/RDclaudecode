"""Generate prompt: an audit plus the team's brief becomes a Higgsfield prompt pack. No network."""

import json
import os
import unittest
from unittest import mock

from fastapi.testclient import TestClient

from post_audit import higgsfield
from post_audit.analysis import AnalysisError
from service import history
from service.app import app
from tests.test_post_audit import ANALYSIS, fake_client

PACK = {
    "concept": "A barista asks 'Would you drink this?' over a slow-motion pour, then reveals the price.",
    "why_it_will_work": ["Opens on a question, like the original's hook."],
    "aspect_ratio": "9:16",
    "style_anchor": "Warm café, golden morning light, shallow depth of field.",
    "shots": [
        {"label": "Hook", "timing": "0-3s", "purpose": "Question hook.",
         "start_frame_prompt": "Close-up of iced latte, condensation, warm café light.",
         "video_prompt": "Milk swirls into espresso in slow motion.", "camera_motion": "slow dolly in",
         "on_screen_text": "Would you drink this?", "voiceover": "", "sound": "Ice clink"},
    ],
    "caption": "Only this week. Visit any branch.",
    "hashtags": ["icedlatte", "#coffee"],
    "production_notes": ["Generate the start frame first, then animate it."],
}

AUDIT = {"platform": "tiktok", "platform_label": "TikTok",
         "post": {"url": "https://www.tiktok.com/@a/video/1", "author": "a", "caption": "ootd", "media_type": "video"},
         "metrics": {"engagement_rate_by_views": 7.8}, "analysis": ANALYSIS}


class PromptPackTest(unittest.TestCase):
    def test_brief_is_cleaned(self):
        brief = higgsfield.clean_brief({"topic": "  Iced latte launch ", "format": "gif", "length_seconds": "500",
                                        "brand": "x" * 1000})
        self.assertEqual(brief["topic"], "Iced latte launch")
        self.assertEqual(brief["format"], "video")
        self.assertEqual(brief["length_seconds"], 90)
        self.assertEqual(len(brief["brand"]), 200)
        self.assertIsNone(higgsfield.clean_brief({"topic": "x", "format": "carousel", "length_seconds": 30})["length_seconds"])
        with self.assertRaisesRegex(AnalysisError, "what your post is about"):
            higgsfield.clean_brief({"topic": "   "})

    def test_sends_the_audit_reading_and_the_brief(self):
        client = fake_client(PACK)
        brief = higgsfield.clean_brief({"topic": "Iced latte launch", "brand": "Brew Lab"})
        report = dict(AUDIT, frames=[{"src": "data:image/jpeg;base64,AAAA"}], pdf={"base64": "AAAA"})
        pack, usage = higgsfield.generate(report, brief, client=client)

        kwargs = client.beta.messages.create.call_args.kwargs
        sent = "\n".join(block["text"] for block in kwargs["messages"][0]["content"])
        self.assertIn("A fast visual reveal", sent)       # the verdict
        self.assertIn("Open on a question", sent)         # a takeaway
        self.assertIn("Iced latte launch", sent)          # the brief
        self.assertNotIn("data:image", sent)              # frames never go back to Claude
        self.assertEqual(kwargs["output_config"]["format"]["schema"], higgsfield.SCHEMA)
        self.assertEqual(pack["brief"]["brand"], "Brew Lab")
        self.assertEqual(usage["output_tokens"], 1200)

        text = pack["as_text"]
        for piece in ("HIGGSFIELD PROMPT PACK — Iced latte launch", "SHOT 1 · Hook · 0-3s", "Camera: slow dolly in",
                      "On-screen text: Would you drink this?", "#icedlatte #coffee"):
            self.assertIn(piece, text)
        self.assertNotIn("Voiceover:", text)  # empty fields are left out

    def test_an_audit_without_analysis_is_refused(self):
        with self.assertRaisesRegex(AnalysisError, "no analysis"):
            higgsfield.generate({"post": {}}, higgsfield.clean_brief({"topic": "x"}), client=fake_client(PACK))

    def test_refusal_is_reported(self):
        with self.assertRaisesRegex(AnalysisError, "declined to write this prompt"):
            higgsfield.generate(AUDIT, higgsfield.clean_brief({"topic": "x"}), client=fake_client(PACK, "refusal"))


class PromptApiTest(unittest.TestCase):
    def setUp(self):
        import service.app as app_module

        self.app_module = app_module
        app_module._hits.clear()
        self.history = history.use_store(history.MemoryStore())
        self.addCleanup(history.use_store, history.MemoryStore())
        self.client = TestClient(app)
        env = mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "k"})
        env.start()
        self.addCleanup(env.stop)

    def post(self, body, pack=PACK, side_effect=None):
        fake = mock.Mock(return_value=(dict(pack, as_text="x"), {"output_tokens": 1}), side_effect=side_effect)
        with mock.patch.object(self.app_module.higgsfield, "generate", fake):
            return self.client.post("/api/post-audit/prompt", json=body), fake

    def test_saved_audit_is_read_from_history(self):
        audit_id = self.history.save(dict(AUDIT), by="")
        response, fake = self.post({"audit_id": audit_id, "brief": {"topic": "Iced latte"}})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["concept"], PACK["concept"])
        self.assertEqual(fake.call_args.args[0]["analysis"]["verdict"], ANALYSIS["verdict"])
        self.assertEqual(fake.call_args.args[1]["topic"], "Iced latte")

    def test_unsaved_audit_is_sent_by_the_page(self):
        response, fake = self.post({"audit": AUDIT, "brief": {"topic": "Iced latte"}})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(fake.call_args.args[0]["post"]["caption"], "ootd")

    def test_bad_requests(self):
        self.assertEqual(self.post({"audit": AUDIT, "brief": {}})[0].status_code, 400)
        self.assertEqual(self.post({"brief": {"topic": "x"}})[0].status_code, 400)
        self.assertEqual(self.post({"audit_id": "missing", "brief": {"topic": "x"}})[0].status_code, 404)
        huge = dict(AUDIT, post={"caption": "x" * 300_000})
        self.assertEqual(self.post({"audit": huge, "brief": {"topic": "x"}})[0].status_code, 413)
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": ""}):
            self.assertEqual(self.post({"audit": AUDIT, "brief": {"topic": "x"}})[0].status_code, 503)
        self.assertEqual(self.app_module._hits, {})  # nothing charged

    def test_claude_failure_is_a_502_and_refunded(self):
        response, _ = self.post({"audit": AUDIT, "brief": {"topic": "x"}},
                                side_effect=AnalysisError("Could not reach the Claude API."))
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["detail"], "Could not reach the Claude API.")
        self.assertEqual(sum(len(v) for v in self.app_module._hits.values()), 0)

    def test_page_offers_generate_prompt(self):
        page = self.client.get("/post-audit").text
        self.assertIn("Generate prompt", page)
        self.assertIn('id="promptDialog"', page)
        self.assertNotIn("Copy takeaways", page)


if __name__ == "__main__":
    unittest.main()
