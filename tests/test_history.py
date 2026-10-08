"""Audit history: saving, listing, reopening, PDFs and who may delete. No network."""

import base64
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from service import auth, history, team
from service.app import app
from service.firestore_db import StoreUnavailable

SETTINGS = {
    "GOOGLE_CLIENT_ID": "123-abc.apps.googleusercontent.com",
    "ALLOWED_EMAILS": "owner@gmail.com",
    "SESSION_SECRET": "a-long-random-test-secret",
}


def jpeg(width=640, height=1136):
    from post_audit.media import ffmpeg_bin

    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "f.jpg"
        subprocess.run([ffmpeg_bin(), "-loglevel", "error", "-f", "lavfi", "-i",
                        f"testsrc=size={width}x{height}", "-frames:v", "1", "-y", str(out)], check=True)
        return "data:image/jpeg;base64," + base64.b64encode(out.read_bytes()).decode()


def report(author="Greta Lynn", frames=None):
    return {
        "platform": "tiktok", "platform_label": "TikTok",
        "post": {"url": "https://www.tiktok.com/@greta/video/1", "author": author, "author_handle": "greta",
                 "views": 120000, "likes": 9000, "comments": 300, "media_type": "video",
                 "posted_at": "2026-09-01T10:00:00Z"},
        "metrics": {"engagement_rate_by_views": 7.8},
        "analysis": {"verdict": "A question hook and fast cuts.", "hook": {"score": 8, "type": "question"},
                     "caption": {"cta_strength": "strong"}},
        "frames": frames or [],
        "notes": [],
        "pdf": {"filename": "x.pdf", "base64": "AAAA"},
    }


class BrokenStore:
    def _fail(self, *a, **k):
        raise StoreUnavailable("Firestore is not set up for this project yet.")

    add = get = recent = delete = _fail


class HistoryTest(unittest.TestCase):
    def setUp(self):
        self.history = history.use_store(history.MemoryStore())
        self.addCleanup(history.use_store, history.MemoryStore())

    def test_saves_a_summary_and_the_full_report(self):
        frames = [{"seconds": 0.0, "label": "hook", "src": jpeg()},
                  {"seconds": 0.5, "label": "hook", "src": jpeg()},
                  {"seconds": 9.0, "label": "body", "src": jpeg()}]
        audit_id = self.history.save(report(frames=frames), by="ana@gmail.com")

        [row] = self.history.recent()
        self.assertEqual(row["id"], audit_id)
        self.assertEqual((row["by"], row["author"], row["hook_score"], row["views"], row["engagement"]),
                         ("ana@gmail.com", "Greta Lynn", 8, 120000, 7.8))
        self.assertTrue(row["cover"].startswith("data:image/jpeg;base64,"))
        self.assertNotIn("report", row)   # the list never carries the heavy parts
        self.assertNotIn("frames", row)

        saved = self.history.get(audit_id)
        self.assertEqual(saved["analysis"]["verdict"], "A question hook and fast cuts.")
        self.assertEqual(saved["history_id"], audit_id)
        self.assertEqual(saved["saved"]["by"], "ana@gmail.com")
        self.assertNotIn("pdf", saved)   # rebuilt on demand, never stored
        self.assertEqual([f["label"] for f in saved["frames"]], ["hook", "hook", "body"])
        # Frames are stored smaller than they came in.
        self.assertLess(len(saved["frames"][0]["src"]), len(frames[0]["src"]))

    def test_frames_stay_inside_the_document_budget(self):
        frames = [{"seconds": float(i), "label": "hook" if i < 2 else "body", "src": jpeg()} for i in range(6)]
        with mock.patch.object(history, "MAX_FRAMES_BYTES", 3 * len(history._shrink(frames[0]["src"], 320)) + 10):
            kept = history._frames_for_storage(frames)
        self.assertEqual(len(kept), 3)
        self.assertEqual([f["label"] for f in kept], ["hook", "hook", "body"])  # the hook is kept first

    def test_remote_or_broken_images_are_dropped(self):
        frames = [{"seconds": 0.0, "label": "hook", "src": "https://example.com/tracker.jpg"},
                  {"seconds": 0.5, "label": "hook", "src": "data:image/jpeg;base64,bm90IGFuIGltYWdl"}]
        self.assertEqual(history._frames_for_storage(frames), [])
        self.assertIsNone(history._cover(frames))

    def test_newest_first_and_unknown_ids(self):
        with mock.patch.object(history, "datetime") as clock:
            clock.now.return_value.strftime.side_effect = ["2026-10-01T00:00:00Z", "2026-10-02T00:00:00Z"]
            first = self.history.save(report("Old"), by="a@gmail.com")
            second = self.history.save(report("New"), by="a@gmail.com")
        self.assertEqual([r["id"] for r in self.history.recent()], [second, first])
        for bad in ("nope", "", "../team_members/x", "a" * 100):
            with self.assertRaises(history.NotFound, msg=bad):
                self.history.get(bad)


class HistoryApiTest(unittest.TestCase):
    def setUp(self):
        import service.app as app_module

        patcher = mock.patch.dict(os.environ, SETTINGS)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.team = team.use_store(team.MemoryStore())
        self.addCleanup(team.use_store, team.MemoryStore())
        self.team.add("ana@gmail.com", "member", by="owner@gmail.com")
        self.team.add("ben@gmail.com", "member", by="owner@gmail.com")
        self.team.add("adam@gmail.com", "admin", by="owner@gmail.com")
        self.history = history.use_store(history.MemoryStore())
        self.addCleanup(history.use_store, history.MemoryStore())
        self.app_module = app_module
        app_module._hits.clear()

    def client(self, email):
        client = TestClient(app, follow_redirects=False)
        client.cookies.set(auth.COOKIE, auth.make_session(email, SETTINGS["SESSION_SECRET"].encode()))
        return client

    def test_a_new_audit_is_saved_with_who_ran_it(self):
        with mock.patch.dict(os.environ, {"APIFY_TOKEN": "t", "ANTHROPIC_API_KEY": "k"}), \
             mock.patch.object(self.app_module, "audit_post", return_value=report()), \
             mock.patch.object(self.app_module, "attach_pdf"):
            response = self.client("ana@gmail.com").post("/api/post-audit",
                                                          json={"url": "https://www.tiktok.com/@greta/video/1"})
        self.assertEqual(response.status_code, 200)
        audit_id = response.json()["history_id"]
        self.assertEqual(self.history.owner_of(audit_id), "ana@gmail.com")

        listing = self.client("ben@gmail.com").get("/api/history").json()
        self.assertTrue(listing["ready"])
        self.assertEqual([a["id"] for a in listing["audits"]], [audit_id])
        self.assertFalse(listing["can_manage"])

        saved = self.client("ben@gmail.com").get(f"/api/history/{audit_id}").json()
        self.assertEqual(saved["post"]["author"], "Greta Lynn")
        self.assertEqual(saved["saved"]["by"], "ana@gmail.com")

    def test_a_failed_save_still_returns_the_report(self):
        history.use_store(BrokenStore())
        with mock.patch.dict(os.environ, {"APIFY_TOKEN": "t", "ANTHROPIC_API_KEY": "k"}), \
             mock.patch.object(self.app_module, "audit_post", return_value=report()), \
             mock.patch.object(self.app_module, "attach_pdf"):
            response = self.client("ana@gmail.com").post("/api/post-audit",
                                                          json={"url": "https://www.tiktok.com/@greta/video/1"})
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("history_id", response.json())

    def test_list_says_when_firestore_is_missing(self):
        history.use_store(BrokenStore())
        listing = self.client("ana@gmail.com").get("/api/history").json()
        self.assertEqual((listing["ready"], listing["audits"]), (False, []))
        self.assertIn("Firestore", listing["problem"])
        self.assertEqual(self.client("ana@gmail.com").get("/api/history/abc").status_code, 503)

    def test_missing_audit_is_a_404(self):
        self.assertEqual(self.client("ana@gmail.com").get("/api/history/nothere").status_code, 404)
        self.assertEqual(self.client("ana@gmail.com").get("/api/history/nothere/pdf").status_code, 404)
        self.assertEqual(self.client("ana@gmail.com").delete("/api/history/nothere").status_code, 404)

    def test_who_can_delete(self):
        mine = self.history.save(report(), by="ana@gmail.com")
        other = self.history.save(report(), by="ana@gmail.com")
        third = self.history.save(report(), by="ana@gmail.com")
        self.assertEqual(self.client("ben@gmail.com").delete(f"/api/history/{mine}").status_code, 403)
        self.assertEqual(self.client("ana@gmail.com").delete(f"/api/history/{mine}").status_code, 200)
        self.assertEqual(self.client("adam@gmail.com").delete(f"/api/history/{other}").status_code, 200)
        self.assertEqual(self.client("owner@gmail.com").delete(f"/api/history/{third}").status_code, 200)
        self.assertEqual(self.history.recent(), [])

    def test_saved_audit_pdf_is_rebuilt(self):
        audit_id = self.history.save(report(), by="ana@gmail.com")
        with mock.patch.object(self.app_module, "render_pdf", return_value=b"%PDF-1.7 test") as render:
            response = self.client("ben@gmail.com").get(f"/api/history/{audit_id}/pdf")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "application/pdf")
        self.assertIn('filename="tiktok-greta-audit.pdf"', response.headers["content-disposition"])
        self.assertEqual(render.call_args.args[0]["history_id"], audit_id)

    def test_pages_need_sign_in_and_link_to_history(self):
        anon = TestClient(app, follow_redirects=False)
        self.assertEqual(anon.get("/history").status_code, 303)
        self.assertEqual(anon.get("/api/history").status_code, 401)
        signed_in = self.client("ana@gmail.com")
        self.assertIn("Audit <span class=\"accent\">history</span>", signed_in.get("/history").text)
        self.assertIn('href="/history"', signed_in.get("/").text)
        self.assertIn('href="/history"', signed_in.get("/post-audit").text)


if __name__ == "__main__":
    unittest.main()
