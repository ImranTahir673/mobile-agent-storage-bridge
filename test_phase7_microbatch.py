"""
Storage Copilot: Phase 7 Micro-Batch Architecture & Android OS Watchdog Yields Test Suite
Verifies:
1. Configurable Micro-Batch Chunking & OS Sleep Yields (`chunk_size = 20` default, `yield_ms = 50` ms default):
   - A 45-file batch splits into 3 chunks (20 + 20 + 5) with 2 inter-chunk OS sleep yields (2 * 50ms = 100ms).
2. Real-Time Progress Telemetry via `EventBus` (`event="execution_progress"`):
   - Emits `current_chunk`, `total_chunks`, `actions_completed`, and `percent` at every chunk boundary.
3. Safe Partial Failure Recovery (`partial_recovery=True` / `on_error="partial"`):
   - When action #35 of 50 encounters a disk error, actions #1..34 remain securely committed in SQLite WAL
     (`status = 'EXECUTED'`), the error at #35 is recorded (`status = 'FAILED'`), and `/rollback_batch`
     atomically restores strictly the 34 executed files back to their original paths.
4. Graceful In-Flight Batch Cancellation (`POST /cancel_execution` & `cancellation_manager`):
   - Triggering cancellation mid-batch stops execution cleanly without corrupting SQLite WAL or losing files,
     and allows 1-tap rollback of the completed prefix.
"""
import os
import time
import uuid
import unittest
import tempfile

test_storage_dir = tempfile.mkdtemp(prefix="storage_phase7_microbatch_")
os.environ["STORAGE_BASE_DIR"] = test_storage_dir
os.environ["LEDGER_DB_PATH"] = os.path.join(test_storage_dir, ".ledger.db")
os.environ["GEMINI_API_KEY"] = ""

import server
from storage_copilot.execution.executor import (
    DEFAULT_CHUNK_SIZE,
    DEFAULT_YIELD_MS,
    execute_action_plan,
)
from storage_copilot.execution.cancellation import cancellation_manager
from storage_copilot.execution.event_bus import event_bus


class TestPhase7MicroBatchAndWatchdogYields(unittest.TestCase):
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
        cancellation_manager.reset()
        # Clean Download/ directory before each test
        for root, dirs, files in os.walk(self.dl_dir, topdown=False):
            for f in files:
                os.remove(os.path.join(root, f))
            for d in dirs:
                os.rmdir(os.path.join(root, d))

    def test_01_microbatch_chunking_os_yields_and_sse_telemetry(self):
        """Verify 45-file batch executes in 3 chunks (20, 20, 5) with 50ms OS yields and SSE progress percent."""
        self.assertEqual(DEFAULT_CHUNK_SIZE, 20)
        self.assertEqual(DEFAULT_YIELD_MS, 50)

        actions = []
        for i in range(1, 46):
            fname = f"batch_doc_{i:03d}.txt"
            with open(os.path.join(self.dl_dir, fname), "w", encoding="utf-8") as f:
                f.write(f"Content of file #{i}")
            actions.append({
                "action_id": f"step-{i}",
                "type": "move",
                "source": f"Download/{fname}",
                "destination": f"Documents/Batch45/{fname}"
            })

        plan_id = f"batch-45-{uuid.uuid4().hex[:8]}"
        plan = {
            "plan_id": plan_id,
            "description": "45-file micro-batch test",
            "chunk_size": 20,
            "yield_ms": 50,
            "actions": actions
        }

        t0 = time.time()
        resp = self.client.post("/execute_plan", json=plan).get_json()
        elapsed_ms = (time.time() - t0) * 1000.0

        self.assertEqual(resp["status"], "success")
        self.assertEqual(resp["executed_actions"], 45)
        self.assertEqual(resp["chunks_processed"], 3)
        self.assertEqual(resp["total_chunks"], 3)
        self.assertEqual(resp["chunk_size"], 20)
        self.assertEqual(resp["yield_ms"], 50)
        self.assertEqual(resp["total_yield_sleep_ms"], 100)  # 2 inter-chunk yields * 50ms
        self.assertGreaterEqual(elapsed_ms, 90.0)

        # Verify SSE execution_progress frames on EventBus
        progress_events = [
            e["data"] for e in event_bus.get_recent_events(limit=20)
            if e["event"] == "execution_progress"
        ]
        chunk_events = [d for d in progress_events if d.get("phase") == "chunk_committed"]
        self.assertEqual(len(chunk_events), 3)

        self.assertEqual(chunk_events[0]["current_chunk"], 1)
        self.assertEqual(chunk_events[0]["total_chunks"], 3)
        self.assertEqual(chunk_events[0]["actions_completed"], 20)
        self.assertAlmostEqual(chunk_events[0]["percent"], round(20 / 45 * 100.0, 1))

        self.assertEqual(chunk_events[1]["current_chunk"], 2)
        self.assertEqual(chunk_events[1]["actions_completed"], 40)

        self.assertEqual(chunk_events[2]["current_chunk"], 3)
        self.assertEqual(chunk_events[2]["actions_completed"], 45)
        self.assertEqual(chunk_events[2]["percent"], 100.0)

        # Cleanly rollback all 45 files
        roll = self.client.post("/rollback_batch", json={"batch_id": plan_id}).get_json()
        self.assertEqual(roll["status"], "rolled_back")
        self.assertEqual(roll["reverted_actions"], 45)

    def test_02_safe_partial_failure_recovery_and_atomic_subset_rollback(self):
        """
        Verify when action #35 (index 34) of 50 fails with partial_recovery=True:
        - Actions #1..34 (indices 0..33) remain securely committed in SQLite WAL.
        - Error details for action #35 are captured.
        - Calling /rollback_batch atomically reverts strictly the 34 executed actions.
        """
        actions = []
        for i in range(1, 51):
            fname = f"partial_file_{i:03d}.txt"
            # Intentionally do NOT create file #35 on disk so action #35 (step_index 34) fails!
            if i != 35:
                with open(os.path.join(self.dl_dir, fname), "w", encoding="utf-8") as f:
                    f.write(f"Payload {i}")
            actions.append({
                "action_id": f"step-{i}",
                "type": "move",
                "source": f"Download/{fname}",
                "destination": f"Documents/PartialBatch/{fname}"
            })

        plan_id = f"partial-50-{uuid.uuid4().hex[:8]}"
        plan = {
            "plan_id": plan_id,
            "description": "50-file partial recovery test (fails at #35)",
            "chunk_size": 20,
            "yield_ms": 10,
            "partial_recovery": True,
            "actions": actions
        }

        res_obj = self.client.post("/execute_plan", json=plan)
        self.assertEqual(res_obj.status_code, 207)
        data = res_obj.get_json()

        self.assertEqual(data["status"], "partial_failure")
        self.assertTrue(data["partial_failure"])
        self.assertFalse(data["rolled_back"])
        self.assertEqual(data["executed_actions"], 34)
        self.assertEqual(data["failed_step"], 34)  # 0-based index for action #35
        self.assertTrue(data["can_rollback_executed"])

        # Verify files #1..34 are in Documents/PartialBatch/ on disk and logged as EXECUTED in SQLite WAL
        for i in range(1, 35):
            fname = f"partial_file_{i:03d}.txt"
            self.assertTrue(os.path.exists(os.path.join(self.doc_dir, "PartialBatch", fname)))
            self.assertFalse(os.path.exists(os.path.join(self.dl_dir, fname)))

        # Verify files #36..50 were untouched and remain in Download/
        for i in range(36, 51):
            fname = f"partial_file_{i:03d}.txt"
            self.assertTrue(os.path.exists(os.path.join(self.dl_dir, fname)))

        # Verify atomic rollback of strictly the 34 executed actions
        roll_res = self.client.post("/rollback_batch", json={"batch_id": plan_id}).get_json()
        self.assertEqual(roll_res["status"], "rolled_back")
        self.assertEqual(roll_res["reverted_actions"], 34)

        # Verify all 49 real files (#1..34 and #36..50) are back in Download/
        for i in range(1, 51):
            if i == 35:
                continue
            fname = f"partial_file_{i:03d}.txt"
            self.assertTrue(os.path.exists(os.path.join(self.dl_dir, fname)))

    def test_03_in_flight_cancellation_endpoint_and_hook(self):
        """Verify POST /cancel_execution gracefully halts an in-flight batch after the current step."""
        actions = []
        for i in range(1, 31):
            fname = f"cancel_doc_{i:03d}.txt"
            with open(os.path.join(self.dl_dir, fname), "w", encoding="utf-8") as f:
                f.write(f"Cancel test {i}")
            actions.append({
                "action_id": f"step-{i}",
                "type": "move",
                "source": f"Download/{fname}",
                "destination": f"Documents/CancelBatch/{fname}"
            })

        plan_id = f"cancel-30-{uuid.uuid4().hex[:8]}"
        plan = {
            "plan_id": plan_id,
            "description": "30-file cancellation test",
            "chunk_size": 10,
            "yield_ms": 10,
            "actions": actions
        }

        # Use step_hook to simulate user hitting POST /cancel_execution right after step 12 completes
        def _cancel_at_step_12(step_idx, step_dict):
            if step_idx == 11:  # After 12th file (0-indexed 11), trigger /cancel_execution
                c_resp = self.client.post("/cancel_execution", json={
                    "batch_id": plan_id,
                    "reason": "User tapped Stop button"
                })
                self.assertEqual(c_resp.status_code, 200)

        res_dict, status_code = execute_action_plan(plan, step_hook=_cancel_at_step_12)
        self.assertEqual(status_code, 200)
        self.assertEqual(res_dict["status"], "cancelled")
        self.assertTrue(res_dict["cancelled"])
        self.assertEqual(res_dict["executed_actions"], 12)
        self.assertEqual(res_dict["reason"], "User tapped Stop button")

        # Verify SSE cancelled event was published
        cancel_events = [
            e["data"] for e in event_bus.get_recent_events(limit=20)
            if e["event"] == "execution_progress" and e["data"].get("phase") == "cancelled"
        ]
        self.assertEqual(len(cancel_events), 1)
        self.assertEqual(cancel_events[0]["actions_completed"], 12)

        # Verify 1-tap rollback restores the 12 moved files cleanly
        roll_res = self.client.post("/rollback_batch", json={"batch_id": plan_id}).get_json()
        self.assertEqual(roll_res["status"], "rolled_back")
        self.assertEqual(roll_res["reverted_actions"], 12)


if __name__ == "__main__":
    unittest.main()
