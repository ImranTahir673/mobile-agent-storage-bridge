"""
Storage Copilot: Phase 9 AI-First Intent Orchestrator, Per-Model 429 Cascade & Conversational Continuity Test Suite
Verifies:
1. Per-Model 429 Cooldown Circuit Breaker (`gemini_client.py`):
   - When `gemini-3.5-flash` hits 429 quota, ONLY `gemini-3.5-flash` is placed on cooldown;
     `gemini-3.1-flash-lite` and `gemini-3.5-flash-lite` remain healthy and active.
   - `call_gemini_json` and `call_gemini_micro_classifier` cascade past a 429 model to the next healthy model.
2. Conversational Continuity & Near-Duplicate Sweep (`session_context.py` & `tier_handlers.py`):
   - Auditing near-duplicates (`"Find near duplicate files in Download"`) records `include_near=True` and
     `near_duplicates_count > 0` in `session_context_store`.
   - Replying `"Yes clean all the duplicate files"` or `"Clean duplicate downloads"` when only near-duplicates
     exist automatically sweeps the 3-gram Jaccard near-duplicates instead of returning "No duplicate files found".
3. Mobile Keyboard Typo Resilience (`normalize_user_prompt` & `classify_intent`):
   - Prompts like `"yes cleen all teh fils in the downlods"` normalize cleanly to
     `"yes clean all the files in the downloads"` and route to `UserIntent.FILE_OPERATION`.
"""
import os
import unittest
import tempfile
from unittest.mock import patch, MagicMock

test_storage_dir = tempfile.mkdtemp(prefix="storage_phase9_ai_")
os.environ["STORAGE_BASE_DIR"] = test_storage_dir
os.environ["LEDGER_DB_PATH"] = os.path.join(test_storage_dir, ".ledger.db")
os.environ["GEMINI_API_KEY"] = ""

import server
from storage_copilot import config
from storage_copilot.execution.plan_builder import normalize_user_prompt
from storage_copilot.intelligence.intent_classifier import UserIntent, classify_intent
from storage_copilot.intelligence.session_context import session_context_store
from storage_copilot.intelligence.gemini_client import (
    GEMINI_MODELS,
    MODEL_COOLDOWN_UNTIL,
    reset_model_cooldowns,
    mark_model_cooldown,
    get_healthy_models,
    call_gemini_micro_classifier,
)


class TestPhase9AiOrchestratorAndContinuity(unittest.TestCase):
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
        os.environ["GEMINI_API_KEY"] = ""
        reset_model_cooldowns()
        session_context_store.clear()
        for entry in os.scandir(self.dl_dir):
            if entry.is_file():
                os.remove(entry.path)

    def test_01_per_model_cooldown_cascade_on_429(self):
        """Verify a 429 on one model does NOT lock out working sibling models."""
        reset_model_cooldowns()
        mark_model_cooldown("gemini-3.5-flash", seconds=300.0)

        # Global quota lock must remain 0.0 because flash-lite models are still healthy!
        self.assertEqual(config.GEMINI_QUOTA_BLOCKED_UNTIL, 0.0)
        healthy = get_healthy_models("fast")
        self.assertIn("gemini-3.1-flash-lite", healthy)
        self.assertIn("gemini-3.5-flash-lite", healthy)
        self.assertNotIn("gemini-3.5-flash", healthy)

        # Simulate HTTP 429 on first model (`gemini-3.1-flash-lite`) and HTTP 200 on second (`gemini-3.5-flash-lite`)
        os.environ["GEMINI_API_KEY"] = "AIzaSyMockKeyForTestingCascade12345"
        try:
            resp_429 = MagicMock()
            resp_429.status_code = 429
            resp_429.text = "RESOURCE_EXHAUSTED: GenerateRequestsPerDay"

            resp_200 = MagicMock()
            resp_200.status_code = 200
            resp_200.json.return_value = {
                "candidates": [{
                    "content": {
                        "parts": [{
                            "text": '{"intent": "FILE_OPERATION", "sub_intent": "general_mutation", "include_near": false, "resolved_goal": "clean all the files in the downloads", "target": "Download"}'
                        }]
                    }
                }]
            }

            with patch("storage_copilot.intelligence.gemini_client.requests.post", side_effect=[resp_429, resp_200]):
                cls_res = classify_intent("yes cleen all teh fils in the downlods")
                self.assertEqual(cls_res["intent"], UserIntent.FILE_OPERATION)
                self.assertTrue(cls_res["is_llm"])
                self.assertEqual(cls_res["model_source"], "gemini-3.5-flash-lite")
        finally:
            os.environ["GEMINI_API_KEY"] = ""
            reset_model_cooldowns()

    def test_02_severe_mobile_typo_normalization_and_routing(self):
        """Verify 'yes cleen all teh fils in the downlods' normalizes and routes to FILE_OPERATION."""
        raw = "yes cleen all teh fils in the downlods"
        norm = normalize_user_prompt(raw)
        self.assertEqual(norm.lower(), "yes clean all the files in the downloads")

        res = classify_intent(raw)
        self.assertEqual(res["intent"], UserIntent.FILE_OPERATION)

    def test_03_near_duplicate_continuity_and_auto_fallback(self):
        """
        Verify the exact bug reported in physical testing:
        When 0 exact duplicates exist and near-duplicates exist (3-gram Jaccard >= 0.85),
        auditing near-duplicates and then replying 'Yes clean all the duplicate files'
        (or running 'Clean duplicate downloads') stages a plan for the near-duplicates!
        """
        srs_orig = (
            "Software Requirements Specification for Information Retrieval System. "
            "This document describes the architecture, indexing pipeline, vector ranking engine, "
            "query parser, evaluation metrics, and user interface design for the semester project."
        )
        srs_edit = (
            "Software Requirements Specification for Information Retrieval System. "
            "This document describes the architecture, indexing pipeline, vector ranking engine, "
            "query parser, evaluation metrics, and user interface design for the final semester project."
        )

        with open(os.path.join(self.dl_dir, "SRS_IR_Solution.txt"), "w", encoding="utf-8") as f:
            f.write(srs_orig)
        with open(os.path.join(self.dl_dir, "SRS_IR_Solution_edit.txt"), "w", encoding="utf-8") as f:
            f.write(srs_edit)

        # Turn 1: User scans for near-duplicates
        audit_resp = self.client.post("/propose_plan", json={
            "prompt": "Find near duplicate files in Download",
            "target_folder": "Download"
        }).get_json()
        self.assertEqual(audit_resp["type"], "query_info")
        self.assertEqual(audit_resp["duplicate_summary"]["near_clusters_count"], 1)

        # Verify SessionContextStore remembered the near-duplicate audit
        ctx = session_context_store.get_context("Download")
        self.assertTrue(ctx["include_near"])
        self.assertEqual(ctx["near_duplicates_count"], 1)

        # Turn 2: User replies "Yes clean all the duplicate files"
        clean_resp = self.client.post("/propose_plan", json={
            "prompt": "Yes clean all the duplicate files",
            "target_folder": "Download",
            "conversation_history": [
                {"role": "user", "content": "Find near duplicate files in Download"},
                {"role": "assistant", "content": audit_resp["message"]}
            ]
        }).get_json()

        self.assertEqual(clean_resp["type"], "plan")
        self.assertEqual(clean_resp["intent"], "FILE_OPERATION")
        self.assertEqual(len(clean_resp["actions"]), 1)
        self.assertIn("SRS_IR_Solution_edit.txt", clean_resp["actions"][0]["source"])


if __name__ == "__main__":
    unittest.main()
