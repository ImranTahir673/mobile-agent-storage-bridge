"""
Storage Copilot: Phase 4 Performance, Smart Lazy Hydration & SSE Streaming Test Suite
Verifies:
1. Smart Lazy Hydration:
   - Cryptic prefixes (`DOC-*`, `WA*`, `1508*`, `document*`, `scan*`, `IMG_*`) and small docs (<= 5 MB) hydrate immediately.
   - Binary/media files (`.png`, `.jpg`, `.mp4`, `.mp3`, `.apk`, `.zip` > 10 MB) skip upfront extraction.
   - Exact filename match skips hydrating unrelated non-cryptic files.
2. SSE Streaming (`GET /stream` & `GET /events`):
   - Emits `connected` handshake frame and sets required anti-buffering headers (`Cache-Control: no-cache`, `X-Accel-Buffering: no`, `Content-Type: text/event-stream`).
   - Deterministically closes with `?once=1` and `?timeout=0.1` without hanging `test_client()`.
   - Replays live telemetry (`scan_progress`, `classifier_intent`, `plan_synthesized`, `execution_progress`).
3. Browser EventSource Query-Token Authentication (`?token=<token>`):
   - Allows `?token=<valid_token>` on `GET /stream` when `ENFORCE_LOCAL_AUTH = True`.
   - Rejects `?token=<invalid_token>` with 401 Unauthorized.
   - Rejects unauthenticated remote/tunnel requests to `GET /stream` with 401 Unauthorized.
4. ManifestIndexManager Cache & Invalidation:
   - Caches directory scan results and invalidates automatically after batch execution.
"""
import os
import json
import unittest
import tempfile

test_storage_dir = tempfile.mkdtemp(prefix="storage_phase4_sse_")
os.environ["STORAGE_BASE_DIR"] = test_storage_dir
os.environ["LEDGER_DB_PATH"] = os.path.join(test_storage_dir, ".ledger.db")
os.environ["GEMINI_API_KEY"] = ""

import server
from storage_copilot.storage.scanner import (
    should_hydrate_file_upfront,
    is_cryptic_filename,
    scan_directory_manifest,
    manifest_index_manager,
    MAX_HYDRATE_CANDIDATES,
)
from storage_copilot.execution.event_bus import event_bus


class TestPhase4PerformanceSSE(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        server.BASE_STORAGE_DIR = os.path.realpath(test_storage_dir)
        server.BASE_DIR = server.BASE_STORAGE_DIR
        server.TRASH_DIR = os.path.join(server.BASE_DIR, ".agent_trash")
        server.LEDGER_DB_PATH = os.path.join(server.BASE_DIR, ".ledger.db")
        server.ENFORCE_LOCAL_AUTH = False
        os.makedirs(os.path.join(server.BASE_DIR, "Download"), exist_ok=True)
        os.makedirs(os.path.join(server.BASE_DIR, "Documents"), exist_ok=True)
        server.init_db()
        cls.client = server.app.test_client()
        cls.client.post("/seed_fixtures")

    def setUp(self):
        server.ENFORCE_LOCAL_AUTH = False
        event_bus.clear_history()

    def test_01_smart_lazy_hydration_rules(self):
        """Verify cryptic prefixes and small docs hydrate while binaries and large zips are skipped."""
        # Cryptic prefixes must hydrate immediately
        self.assertTrue(is_cryptic_filename("DOC-20250413-WA0017..pdf"))
        self.assertTrue(is_cryptic_filename("WhatsApp_Doc_Ahmed_Receipt.pdf"))
        self.assertTrue(is_cryptic_filename("1508.06576v2_neural_style.pdf"))
        self.assertTrue(is_cryptic_filename("scan_0012.pdf"))
        self.assertTrue(is_cryptic_filename("IMG_20250501.pdf"))

        self.assertTrue(should_hydrate_file_upfront("DOC-20250413-WA0017..pdf", ".pdf", 120_000))
        self.assertTrue(should_hydrate_file_upfront("Lecture_Notes.pdf", ".pdf", 2 * 1024 * 1024))
        self.assertTrue(should_hydrate_file_upfront("Report.docx", ".docx", 500_000))

        # Obvious media / binary extensions must NEVER hydrate upfront
        self.assertFalse(should_hydrate_file_upfront("Screenshot_20241001-142210.png", ".png", 45_000))
        self.assertFalse(should_hydrate_file_upfront("vacation_video.mp4", ".mp4", 15_000_000))
        self.assertFalse(should_hydrate_file_upfront("voice_note.mp3", ".mp3", 800_000))
        self.assertFalse(should_hydrate_file_upfront("app_release.apk", ".apk", 12_000_000))

        # Archives > 10 MB must skip upfront hydration
        self.assertFalse(should_hydrate_file_upfront("backup_dataset.zip", ".zip", 15 * 1024 * 1024))

        # When exact filename match is already found, skip unrelated non-cryptic files
        self.assertFalse(
            should_hydrate_file_upfront("Unrelated_Book.pdf", ".pdf", 500_000, exact_match_found=True)
        )
        # But still hydrate cryptic files even if exact_match_found=True
        self.assertTrue(
            should_hydrate_file_upfront("DOC-20250413-WA0017..pdf", ".pdf", 500_000, exact_match_found=True)
        )

    def test_02_sse_stream_handshake_and_headers(self):
        """Verify GET /stream?once=1 returns text/event-stream headers and connected event without hanging."""
        resp = self.client.get("/stream?once=1")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("text/event-stream", resp.headers.get("Content-Type", ""))
        self.assertIn("no-cache", resp.headers.get("Cache-Control", ""))
        self.assertEqual(resp.headers.get("X-Accel-Buffering"), "no")

        body = resp.get_data(as_text=True)
        self.assertIn("event: connected", body)
        self.assertIn('"status":"connected"', body)

        # Verify /events alias works identically
        resp_alias = self.client.get("/events?once=1&timeout=0.1")
        self.assertEqual(resp_alias.status_code, 200)
        self.assertIn("event: connected", resp_alias.get_data(as_text=True))

    def test_03_sse_telemetry_across_plan_and_execution(self):
        """Verify propose_plan and execute_plan publish structured SSE events to EventBus."""
        plan_resp = self.client.post("/propose_plan", json={
            "prompt": "Sort student vouchers",
            "target_folder": "Download"
        }).get_json()
        self.assertEqual(plan_resp["type"], "plan")

        recent = event_bus.get_recent_events(limit=20)
        event_names = [e["event"] for e in recent]
        self.assertIn("classifier_intent", event_names)
        self.assertIn("scan_progress", event_names)
        self.assertIn("plan_synthesized", event_names)

        # Replay via GET /stream?once=1&replay=1
        stream_body = self.client.get("/stream?once=1&replay=1").get_data(as_text=True)
        self.assertIn("event: classifier_intent", stream_body)
        self.assertIn("event: scan_progress", stream_body)
        self.assertIn("event: plan_synthesized", stream_body)

        # Execute plan & verify execution_progress events
        exec_resp = self.client.post("/execute_plan", json=plan_resp["plan"]).get_json()
        self.assertEqual(exec_resp["status"], "success")
        batch_id = exec_resp["batch_id"]

        exec_events = [e for e in event_bus.get_recent_events(limit=30) if e["event"] == "execution_progress"]
        phases = [e["data"].get("phase") for e in exec_events]
        self.assertIn("started", phases)
        self.assertIn("chunk_committed", phases)
        self.assertIn("completed", phases)

        # Rollback to leave fixtures clean
        self.client.post("/rollback_batch", json={"batch_id": batch_id})

    def test_04_eventsource_query_token_authentication(self):
        """Verify GET /stream supports ?token=<token> for browser EventSource when auth is enforced."""
        valid_token = server.get_or_create_auth_token()
        server.ENFORCE_LOCAL_AUTH = True

        # 1. Missing token when ENFORCE_LOCAL_AUTH=True -> 401
        r_unauth = self.client.get("/stream?once=1")
        self.assertEqual(r_unauth.status_code, 401)

        # 2. Invalid ?token= query param -> 401
        r_bad = self.client.get("/stream?once=1&token=invalid_token_xyz")
        self.assertEqual(r_bad.status_code, 401)

        # 3. Valid ?token= query param -> 200 OK
        r_ok = self.client.get(f"/stream?once=1&token={valid_token}")
        self.assertEqual(r_ok.status_code, 200)
        self.assertIn("event: connected", r_ok.get_data(as_text=True))

        # 4. Simulated Cloudflare tunnel request without token even when ENFORCE_LOCAL_AUTH=False -> 401
        server.ENFORCE_LOCAL_AUTH = False
        r_tunnel_no_tok = self.client.get("/stream?once=1", headers={"Cf-Connecting-Ip": "203.0.113.50"})
        self.assertEqual(r_tunnel_no_tok.status_code, 401)

        # 5. Simulated Cloudflare tunnel request with valid ?token= -> 200 OK
        r_tunnel_ok = self.client.get(
            f"/stream?once=1&token={valid_token}",
            headers={"Cf-Connecting-Ip": "203.0.113.50"}
        )
        self.assertEqual(r_tunnel_ok.status_code, 200)

    def test_05_manifest_index_caching_and_invalidation(self):
        """Verify ManifestIndexManager caches directory scans and invalidates cleanly."""
        scan_res = scan_directory_manifest("Download", lazy_snippets=True)
        target_dir = scan_res["target_dir"]
        cached = manifest_index_manager.get_Cached(target_dir)
        self.assertIsNotNone(cached)
        self.assertEqual(len(cached["manifest"]), len(scan_res["manifest"]))

        manifest_index_manager.invalidate(target_dir)
        self.assertIsNone(manifest_index_manager.get_Cached(target_dir))


if __name__ == "__main__":
    unittest.main()
