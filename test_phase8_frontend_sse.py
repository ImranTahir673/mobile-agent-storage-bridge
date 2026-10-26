"""
Storage Copilot: Phase 8 Mobile Web UI, Selective Diff Execution & SSE Integration Test Suite
Verifies:
1. Web UI Asset Serving (`GET /?ui=1`, `GET /app.js`, `GET /styles.css`):
   - Confirms upgraded Phase 8 Web UI HTML, CSS, and JS assets serve cleanly with HTTP 200 and contain
     the new SSE EventSource bindings, `/cancel_execution` controls, selective diff checkboxes,
     and Phase 6 Deduplication / Forgotten Files toolbar triggers.
2. Selective Action Plan Subset Execution (`POST /execute_plan`):
   - Proposes a multi-action plan via `/propose_plan` (e.g. "Clean Download Names"), filters out a subset
     of actions (simulating user unchecking checkboxes in the UI diff review card), executes only the
     selected actions, and verifies unselected files remain untouched in `Download/`.
3. End-to-End Undo Banner Refresh (`GET /recent_batch` & `POST /rollback_batch`):
   - Confirms `/recent_batch` returns the exact executed action summary for the Web UI Undo Banner
     and `/rollback_batch` restores the selected subset cleanly.
4. Full SSE Telemetry Replay for Web UI (`GET /stream?once=1&replay=1`):
   - Confirms the Web UI receives `connected`, `classifier_intent`, `scan_progress`,
     `plan_synthesized`, and `execution_progress` frames with `percent` and chunk metrics.
"""
import os
import unittest
import tempfile

test_storage_dir = tempfile.mkdtemp(prefix="storage_phase8_webui_")
os.environ["STORAGE_BASE_DIR"] = test_storage_dir
os.environ["LEDGER_DB_PATH"] = os.path.join(test_storage_dir, ".ledger.db")
os.environ["GEMINI_API_KEY"] = ""

import server
from storage_copilot.execution.event_bus import event_bus


class TestPhase8FrontendAndSSE(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        server.BASE_STORAGE_DIR = os.path.realpath(test_storage_dir)
        server.BASE_DIR = server.BASE_STORAGE_DIR
        server.TRASH_DIR = os.path.join(server.BASE_DIR, ".agent_trash")
        server.LEDGER_DB_PATH = os.path.join(server.BASE_DIR, ".ledger.db")
        server.ENFORCE_LOCAL_AUTH = False
        cls.dl_dir = os.path.join(server.BASE_DIR, "Download")
        cls.doc_dir = os.path.join(server.BASE_DIR, "Documents")
        os.makedirs(cls.dl_dir, exist_ok=True)
        os.makedirs(cls.doc_dir, exist_ok=True)
        server.init_db()
        cls.client = server.app.test_client()

    def setUp(self):
        event_bus.clear_history()
        self.client.post("/seed_fixtures", json={"duplicates": True})

    def test_01_web_ui_static_assets_and_phase8_controls(self):
        """Verify GET /?ui=1, /app.js, and /styles.css serve the upgraded Phase 8 Web UI."""
        r_html = self.client.get("/?ui=1", headers={"Accept": "text/html"})
        self.assertEqual(r_html.status_code, 200)
        html_body = r_html.get_data(as_text=True)
        self.assertIn("Storage Copilot", html_body)
        self.assertIn("btn-cancel-execution", html_body)
        self.assertIn("btn-scan-forgotten", html_body)
        self.assertIn("chk-include-near", html_body)

        r_js = self.client.get("/app.js")
        self.assertEqual(r_js.status_code, 200)
        js_body = r_js.get_data(as_text=True)
        self.assertIn("new EventSource", js_body)
        self.assertIn("/cancel_execution", js_body)
        self.assertIn("buildSelectivePlanReviewCard", js_body)

        r_css = self.client.get("/styles.css")
        self.assertEqual(r_css.status_code, 200)
        self.assertIn(".diff-checkbox", r_css.get_data(as_text=True))

    def test_02_selective_action_checkbox_execution_and_undo_banner(self):
        """Simulate user unchecking an action in the Web UI diff card and executing only the approved subset."""
        prop_resp = self.client.post("/propose_plan", json={
            "prompt": "Sort student vouchers",
            "target_folder": "Download"
        }).get_json()
        self.assertEqual(prop_resp["type"], "plan")
        all_actions = prop_resp["actions"]
        self.assertGreaterEqual(len(all_actions), 2)

        # Simulate user unchecking `fawad fee.pdf` and only keeping `BSAI-182_fee_voucher_fall.pdf`
        selected_actions = [
            a for a in all_actions
            if "fawad" not in (a.get("source") or a.get("path") or "").lower()
            and "fawad" not in (a.get("destination") or a.get("path") or "").lower()
        ]
        self.assertGreater(len(selected_actions), 0)
        self.assertLess(len(selected_actions), len(all_actions))

        selective_plan = dict(prop_resp["plan"])
        selective_plan["actions"] = selected_actions

        exec_resp = self.client.post("/execute_plan", json=selective_plan).get_json()
        self.assertEqual(exec_resp["status"], "success")
        self.assertEqual(exec_resp["executed_actions"], len(selected_actions))

        # Unchecked `fawad fee.pdf` must still sit untouched in Download/
        self.assertTrue(os.path.exists(os.path.join(self.dl_dir, "fawad fee.pdf")))
        # Checked `BSAI-182_fee_voucher_fall.pdf` must be moved to Documents/University/Vouchers/
        self.assertTrue(
            os.path.exists(os.path.join(self.doc_dir, "University", "Vouchers", "BSAI-182_fee_voucher_fall.pdf"))
        )

        # Verify /recent_batch endpoint powers the Web UI 1-Tap Undo Banner
        recent_resp = self.client.get("/recent_batch").get_json()
        self.assertEqual(recent_resp["status"], "success")
        self.assertEqual(recent_resp["batch"]["batch_id"], exec_resp["batch_id"])
        self.assertEqual(recent_resp["batch"]["executed_actions"], len(selected_actions))

        # Rollback via 1-Tap Undo
        roll_resp = self.client.post("/rollback_batch", json={"batch_id": exec_resp["batch_id"]}).get_json()
        self.assertEqual(roll_resp["status"], "rolled_back")
        self.assertTrue(os.path.exists(os.path.join(self.dl_dir, "BSAI-182_fee_voucher_fall.pdf")))

    def test_03_end_to_end_sse_telemetry_for_webui(self):
        """Verify full SSE event sequence expected by web/app.js."""
        p_resp = self.client.post("/propose_plan", json={
            "prompt": "Clean Download Names",
            "target_folder": "Download"
        }).get_json()
        self.assertEqual(p_resp["type"], "plan")

        exec_resp = self.client.post("/execute_plan", json=p_resp["plan"]).get_json()
        self.assertEqual(exec_resp["status"], "success")

        sse_text = self.client.get("/stream?once=1&replay=1").get_data(as_text=True)
        self.assertIn("event: connected", sse_text)
        self.assertIn("event: classifier_intent", sse_text)
        self.assertIn("event: scan_progress", sse_text)
        self.assertIn("event: plan_synthesized", sse_text)
        self.assertIn("event: execution_progress", sse_text)
        self.assertIn('"percent":100.0', sse_text)

        self.client.post("/rollback_batch", json={"batch_id": exec_resp["batch_id"]})


if __name__ == "__main__":
    unittest.main()
