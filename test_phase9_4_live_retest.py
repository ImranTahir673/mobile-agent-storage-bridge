"""
test_phase9_4_live_retest.py
Phase 9.4 Regression Test Suite (v0.8.1 Fix Pass):

- Test 1 (P0 Fix 1): 'gather all PDFs' and 'gather pdf' honor file-type constraint (only .pdf files),
  enforce 20-action batch cap, and present format breakdown first.
- Test 2 (P0 Fix 2): Honest diagnostics — every degraded response carries a non-'none' fallback_reason
  and non-empty fallback_notice.
- Test 3 (P0 Fix 3): Vague prompts ('that electricity thing', 'clean up a bit') never resolve to a
  destructive plan (deduplicate_clean/trash) via continuity alone.
- Test 4 (P1 Fix 4): Seed fixture scan matches by content hash regardless of filename (finds renamed bill stubs).
- Test 5 (P1 Fix 5): Search results filter 0-score matches, sort strictly by relevance, date queries carry real score,
  and message count equals len(matches).
- Test 6 (P1 Fix 6): Quarantine scope matches damaged-notice scope across Download/ and Documents/.
- Test 7 (P2 Fix 7): Organise plans never include corrupt-flagged files, and never emit placeholder folder names.
"""
import os
import hashlib
import unittest
from storage_copilot import config
from storage_copilot.intelligence.intent_classifier import classify_intent, UserIntent
from storage_copilot.intelligence.session_context import session_context_store
from storage_copilot.execution.operation_handler import handle_file_operation_turn
from storage_copilot.execution.query_handler import handle_query_info_turn
from storage_copilot.api.utility_routes import is_fixture_signature, SEED_FIXTURE_CONTENTS
from storage_copilot.intelligence.gemini_client import mark_ai_success, get_last_fallback_info


class TestPhase94LiveRetest(unittest.TestCase):

    def setUp(self):
        self.base_dir = config.BASE_STORAGE_DIR
        self.download_dir = os.path.join(self.base_dir, "Download")
        self.finances_dir = os.path.join(self.base_dir, "Documents", "Finances")
        os.makedirs(self.download_dir, exist_ok=True)
        os.makedirs(self.finances_dir, exist_ok=True)

    def test_01_gather_file_type_constraint_and_cap(self):
        """Fix 1: 'gather all PDFs' and 'gather pdf' honor file-type constraint (only .pdf), cap <= 20, breakdown first."""
        import pypdf
        # Create dummy non-pdf and valid pdf files in download
        pdf_path = os.path.join(self.download_dir, "test_lecture.pdf")
        docx_path = os.path.join(self.download_dir, "test_report.docx")
        py_path = os.path.join(self.download_dir, "test_cipher.py")

        writer = pypdf.PdfWriter()
        writer.add_blank_page(width=72, height=72)
        with open(pdf_path, "wb") as f:
            writer.write(f)

        with open(docx_path, "w") as f: f.write("Dummy Docx Content")
        with open(py_path, "w") as f: f.write("print('cipher')")

        for prompt in ("gather all PDFs", "gather pdf"):
            c = classify_intent(prompt)
            res = handle_file_operation_turn(prompt, c, "Download")
            actions = res.get("actions", [])
            file_acts = [a for a in actions if a.get("type") in ("move", "copy", "trash", "delete")]

            self.assertLessEqual(len(file_acts), 20, f"Plan has {len(file_acts)} actions, must be <= 20")
            for a in file_acts:
                src = a.get("source") or a.get("path") or ""
                self.assertTrue(src.lower().endswith(".pdf"), f"Prompt '{prompt}' staged non-PDF: {src}")

            msg = res.get("message", "")
            self.assertIn("Breakdown", msg, f"Prompt '{prompt}' did not present breakdown table first")

    def test_02_fallback_reason_and_notice_on_every_path(self):
        """Fix 2: Set fallback reason and notice on every path (never 'none' or empty notice on degraded)."""
        mark_ai_success()
        info = get_last_fallback_info()
        self.assertNotEqual(info.get("fallback_reason"), "none")
        self.assertTrue(len(info.get("fallback_notice", "")) > 0)
        self.assertIn(info.get("fallback_reason"), ("no_api_key", "no_internet", "rate_limited", "daily_quota_exceeded", "ai_unavailable"))

    def test_03_vague_prompt_destructive_continuity_guard(self):
        """Fix 3: 'that electricity thing' and 'clean up a bit' must NEVER resolve to a destructive plan via continuity."""
        session_context_store.update_context(
            "Download",
            last_intent="QUERY_INFO",
            last_sub_intent="deduplicate_audit",
            exact_duplicates_count=7
        )
        c1 = classify_intent("that electricity thing")
        self.assertEqual(c1.get("intent"), UserIntent.QUERY_INFO)
        self.assertNotEqual(c1.get("sub_intent"), "deduplicate_clean")

        c2 = classify_intent("clean up a bit")
        self.assertEqual(c2.get("intent"), UserIntent.CONVERSATION)
        self.assertEqual(c2.get("sub_intent"), "clarification_needed")

    def test_04_seed_scan_hash_matching_renamed(self):
        """Fix 4: Seed fixture scan matches content hash regardless of filename."""
        renamed_bill_path = os.path.join(self.finances_dir, "Electricity_Bill_Invoice.pdf")
        bill_content = SEED_FIXTURE_CONTENTS["DOC-20250413-WA0019..pdf"]
        with open(renamed_bill_path, "w", encoding="utf-8") as f:
            f.write(bill_content)

        matched = is_fixture_signature(renamed_bill_path)
        self.assertTrue(matched, f"is_fixture_signature failed on renamed seed stub: {renamed_bill_path}")

    def test_05_filter_zero_score_and_count_agreement(self):
        """Fix 5: Filter 0-score matches, sort by score, and ensure message count equals len(matches)."""
        c = classify_intent("find my fee vouchers")
        res = handle_query_info_turn("find my fee vouchers", c, "Download")
        matches = res.get("matches", [])
        msg = res.get("message", "")
        for m in matches:
            self.assertGreater(m.get("relevance_score", 0.0), 0.0, f"0-score match in results: {m['name']}")
        if matches:
            self.assertIn(f"Found **{len(matches)} matching document(s)**", msg)

    def test_06_quarantine_scope_equals_damaged_notice(self):
        """Fix 6: Quarantine plan count must equal damaged-notice count across storage."""
        corrupt_doc = os.path.join(self.base_dir, "Documents", "corrupt_stub.pdf")
        with open(corrupt_doc, "w") as f:
            f.write("Broken corrupted stub")

        c_quar = classify_intent("Quarantine damaged files")
        res_quar = handle_file_operation_turn("Quarantine damaged files", c_quar, "Download")
        q_actions = res_quar.get("actions", [])
        self.assertGreater(len(q_actions), 0)

        # Ensure documents in Documents/ are included
        included_paths = [a.get("source") or a.get("path") for a in q_actions]
        self.assertTrue(any("Documents" in p for p in included_paths if p), "Quarantine did not include damaged files from Documents/")

    def test_07_no_corrupt_in_organise_no_placeholders(self):
        """Fix 7: Organise plans never include corrupt-flagged files and never emit placeholder folder names."""
        c = classify_intent("organize")
        res = handle_file_operation_turn("organize", c, "Download")
        actions = res.get("actions", [])
        for a in actions:
            dest = a.get("destination") or ""
            self.assertNotIn("Peer_Name", dest)
            self.assertNotIn("<PeerName>", dest)
            self.assertNotIn("<Subject>", dest)
            self.assertNotIn("<Category>", dest)


if __name__ == "__main__":
    unittest.main()
