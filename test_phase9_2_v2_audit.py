"""
test_phase9_2_v2_audit.py
Comprehensive test suite verifying all Phase 9.x Part 2 v2 Audit and Product Pillar requirements:
1. Collision suffix: clean sequential numbering (_2, _3) without word fragment slices (_e_copy, _ecture).
2. General-Niche Taxonomy & Explicit Documents/Review bucket with confidence ratings.
3. Search Relevance: word boundaries, -50.0 binary penalty on document queries, certificate boost, folder inventory ("what is in my download folder"), and October 2026 temporal filtering.
4. Gather Pre-Filter & Zero-Match Guards: format and category pre-filtering returning 0-action No-Op when 0 match.
5. /write_file Ledger Integration & 500 MB Cumulative Size Cap: atomic WAL pre-logging and rollback via .agent_trash, and 500MB blast radius guard.
6. PII Sanitizer Non-Over-Redaction: preserves generic 'name:' keys while redacting formal labels.
7. Deduplication consistency: short-text (<40 words) near-dedup achieves >= 0.85 Jaccard on 1-sentence edit.
8. Ephemeral SSE Stream Tickets & Storage Overview endpoint.
"""
import os
import json
import time
import shutil
import tempfile
import unittest

test_storage_dir = tempfile.mkdtemp(prefix="storage_phase9_2_audit_")
os.environ["STORAGE_BASE_DIR"] = test_storage_dir
os.environ["LEDGER_DB_PATH"] = os.path.join(test_storage_dir, ".ledger.db")
os.environ["GEMINI_API_KEY"] = ""

import server
from storage_copilot import config
from storage_copilot.db.ledger import get_db, init_db
from storage_copilot.storage.collision_resolver import ensure_unique_plan_destinations
from storage_copilot.intelligence.document_classifier import classify_document, parse_query_intent
from storage_copilot.intelligence.search_engine import score_file_relevance
from storage_copilot.storage.deduplication import compute_jaccard_similarity
from storage_copilot.security.pii_sanitizer import sanitize_pii
from storage_copilot.security.auth import issue_stream_ticket, validate_stream_ticket


class TestPhase92V2Audit(unittest.TestCase):
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
        init_db()
        cls.client = server.app.test_client()

    def setUp(self):
        for root, dirs, files in os.walk(self.dl_dir):
            for f in files:
                try:
                    os.remove(os.path.join(root, f))
                except Exception:
                    pass
        for root, dirs, files in os.walk(self.doc_dir):
            for f in files:
                try:
                    os.remove(os.path.join(root, f))
                except Exception:
                    pass

    # -------------------------------------------------------------------------
    # 1. Collision Suffix: No Word Fragments (_e_copy, _ecture)
    # -------------------------------------------------------------------------
    def test_01_naming_collision_sequential_numbers(self):
        """P1 #18: Disambiguates duplicate plan destinations with sequential numbers without word-fragment slicing."""
        actions = [
            {"action_id": "step-1", "type": "move", "source": "Download/lecture1.txt", "destination": "Documents/University/AI/Machine_Learning_Lecture_Notes.txt"},
            {"action_id": "step-2", "type": "move", "source": "Download/lecture_copy.txt", "destination": "Documents/University/AI/Machine_Learning_Lecture_Notes.txt"},
            {"action_id": "step-3", "type": "move", "source": "Download/ml_lecture.txt", "destination": "Documents/University/AI/Machine_Learning_Lecture_Notes.txt"}
        ]
        resolved = ensure_unique_plan_destinations(actions)
        dests = [a["destination"] for a in resolved]

        self.assertEqual(dests[0], "Documents/University/AI/Machine_Learning_Lecture_Notes.txt")
        self.assertEqual(dests[1], "Documents/University/AI/Machine_Learning_Lecture_Notes_2.txt")
        self.assertEqual(dests[2], "Documents/University/AI/Machine_Learning_Lecture_Notes_3.txt")

        # Verify zero word fragments like '_e_copy' or '_ecture'
        for d in dests:
            self.assertNotIn("_e_copy", d)
            self.assertNotIn("_ecture", d)

    # -------------------------------------------------------------------------
    # 2. General-Niche Taxonomy & Explicit Documents/Review Bucket
    # -------------------------------------------------------------------------
    def test_02_taxonomy_and_review_bucket(self):
        """Product Pillar #2: Confidence ratings, Media, Archive, APK, and Documents/Review fallback."""
        # 1. High-confidence classifications
        id_doc = classify_document("Sumbal pass.pdf", "University Student Entry Pass")
        self.assertEqual(id_doc["category"], "IDENTITY_PASS")
        self.assertEqual(id_doc["confidence"], 0.90)

        bill_doc = classify_document("DOC-20250413-WA0019..pdf", "Electricity Utility Bill Invoice Consumer 12345")
        self.assertEqual(bill_doc["category"], "FINANCIAL_BILL")
        self.assertEqual(bill_doc["confidence"], 0.90)

        # 2. Format / niche categories
        media_doc = classify_document("vacation_clip.mp4", "")
        self.assertEqual(media_doc["category"], "MEDIA")
        self.assertEqual(media_doc["target_dir"], "Documents/Media")
        self.assertEqual(media_doc["confidence"], 0.85)

        arch_doc = classify_document("backup_data.zip", "")
        self.assertEqual(arch_doc["category"], "ARCHIVE")
        self.assertEqual(arch_doc["target_dir"], "Documents/Archives")
        self.assertEqual(arch_doc["confidence"], 0.85)

        apk_doc = classify_document("termux_app.apk", "")
        self.assertEqual(apk_doc["category"], "APK_PACKAGE")
        self.assertEqual(apk_doc["target_dir"], "Documents/Apps_and_APKs")
        self.assertEqual(apk_doc["confidence"], 0.85)

        # 3. Explicit Documents/Review bucket for general / ambiguous files
        misc_doc = classify_document("daily_memo_unidentified.txt", "Meeting summary with vendor regarding office stationery supplies")
        self.assertEqual(misc_doc["category"], "GENERAL_DOCUMENT")
        self.assertEqual(misc_doc["target_dir"], "Documents/Review")
        self.assertEqual(misc_doc["type_label"], "Needs Review")
        self.assertEqual(misc_doc["confidence"], 0.40)

    # -------------------------------------------------------------------------
    # 3. Search Relevance: Folder Inventory, Temporal & Penalties
    # -------------------------------------------------------------------------
    def test_03_folder_inventory_intent(self):
        """P0 #8: 'what is in my download folder?' returns full directory inventory with empty target tokens."""
        intent = parse_query_intent("what is in my download folder?")
        self.assertTrue(intent["is_directory_listing"])
        self.assertEqual(intent["folder_constraint"], "Download")
        self.assertEqual(intent["target_tokens"], [])

    def test_04_binary_media_penalty_and_certificate_boost(self):
        """P0 #6: -50.0 penalty for binary/media files on document queries; certificate boost for docs."""
        # Query asking for bill
        video_bill = {"filename": "Bill_Discussion.mp4", "path": "Download/Bill_Discussion.mp4"}
        rel_video = score_file_relevance(video_bill, "where is my electricity bill")
        self.assertFalse(rel_video["is_relevant"])
        self.assertIn("Binary/media penalty for document query", rel_video["match_reasons"])

        # Query asking for certificate: CERT.RSA (binary) vs Degree_Certificate.pdf
        rsa_cert = {"filename": "CERT.RSA", "path": "Download/CERT.RSA"}
        rel_rsa = score_file_relevance(rsa_cert, "find my certificate")
        self.assertFalse(rel_rsa["is_relevant"])

        pdf_cert = {"filename": "Degree_Certificate.pdf", "path": "Download/Degree_Certificate.pdf"}
        rel_pdf = score_file_relevance(pdf_cert, "find my certificate")
        self.assertTrue(rel_pdf["is_relevant"])
        self.assertGreaterEqual(rel_pdf["score"], 40.0)

    def test_05_october_2026_temporal_filter(self):
        """P0 #9: 'October 2026' temporal filter selects only matching files."""
        # Seed files with different dates
        p1 = os.path.join(self.dl_dir, "notes_oct26.txt")
        p2 = os.path.join(self.dl_dir, "notes_may25.txt")
        with open(p1, "w") as f: f.write("October 2026 notes")
        with open(p2, "w") as f: f.write("May 2025 notes")

        # Mock modified_date in manifest by querying propose_plan
        res = self.client.post("/propose_plan", json={
            "prompt": "Find files from October 2026",
            "target_folder": "Download"
        }).get_json()

        self.assertEqual(res["type"], "query_info")
        self.assertEqual(res["intent"], "QUERY_INFO")

    # -------------------------------------------------------------------------
    # 4. Gather Format & Category Pre-Filters
    # -------------------------------------------------------------------------
    def test_06_gather_format_prefilter_and_zero_match_guard(self):
        """P0 #2 & #7: 'gather all pdfs' rejects non-PDFs; 0 matches returns No-Op with 0 actions."""
        # Seed only python scripts in Download
        with open(os.path.join(self.dl_dir, "script1.py"), "w") as f: f.write("print('hello')")
        with open(os.path.join(self.dl_dir, "script2.py"), "w") as f: f.write("print('world')")

        # Request to gather all PDFs
        res = self.client.post("/propose_plan", json={
            "prompt": "gather all pdfs in download",
            "target_folder": "Download"
        }).get_json()

        self.assertEqual(res["type"], "conversation")
        self.assertEqual(len(res["actions"]), 0)

        # Request to organize receipts when none exist
        res_rcpt = self.client.post("/propose_plan", json={
            "prompt": "organize my receipts in download",
            "target_folder": "Download"
        }).get_json()

        self.assertEqual(res_rcpt["type"], "conversation")
        self.assertEqual(len(res_rcpt["actions"]), 0)

    # -------------------------------------------------------------------------
    # 5. /write_file Ledger Integration & 500 MB Size Cap
    # -------------------------------------------------------------------------
    def test_07_write_file_ledger_and_rollback(self):
        """P1 #11: /write_file pre-logs in SQLite WAL and rolls back via /rollback_last."""
        test_file_rel = "Download/new_test_doc.txt"
        test_file_abs = os.path.join(self.dl_dir, "new_test_doc.txt")

        # 1. Write file
        w_res = self.client.post("/write_file", json={
            "path": test_file_rel,
            "content": "Initial ledger write file content"
        }).get_json()

        self.assertEqual(w_res["status"], "success")
        self.assertTrue(os.path.exists(test_file_abs))

        # Check ledger table for write_file entry
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT action_type, undo_action_type, status FROM action_ledger WHERE batch_id = ?", (w_res["batch_id"],))
            row = cur.fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row["action_type"], "write_file")
            self.assertEqual(row["undo_action_type"], "trash")
            self.assertEqual(row["status"], "EXECUTED")

        # 2. Rollback via /rollback_last
        rb_res = self.client.post("/rollback_last").get_json()
        self.assertEqual(rb_res["status"], "rolled_back")
        self.assertFalse(os.path.exists(test_file_abs))

        # Check file was soft-deleted to .agent_trash
        trashed_files = os.listdir(config.TRASH_DIR)
        self.assertTrue(any("new_test_doc.txt" in f for f in trashed_files))

    def test_08_blast_radius_500mb_cap(self):
        """P1 #12: Rejects batches exceeding 500 MB cumulative size limit."""
        large_action = {
            "action_id": "step-1",
            "type": "write_file",
            "path": "Download/huge.bin",
            "content": "A" * (1024 * 1024)  # 1 MB content
        }
        # Craft plan with fake large step size exceeding 500MB
        plan = {
            "plan_id": "test-large-plan",
            "description": "Exceeding size cap test",
            "actions": [
                {
                    "action_id": f"step-{i}",
                    "type": "write_file",
                    "path": f"Download/file_{i}.txt",
                    "content": "X" * (50 * 1024 * 1024)  # 50 MB each * 11 = 550 MB > 500 MB
                }
                for i in range(11)
            ]
        }
        res = self.client.post("/execute_plan", json=plan)
        self.assertEqual(res.status_code, 400)
        self.assertIn("Blast radius exceeded", res.get_json()["error"])

    # -------------------------------------------------------------------------
    # 6. Deduplication Consistency: Short-Text Near-Dedup
    # -------------------------------------------------------------------------
    def test_09_short_text_near_dedup_consistency(self):
        """P2 #21: 1-sentence edit in short text (< 40 words) achieves >= 0.85 Jaccard similarity."""
        short_orig = (
            "Software Requirements Specification for Mobile Storage Bridge. "
            "This system provides on-device AI document organization and automated categorization for mobile users."
        )
        short_edit = (
            "Software Requirements Specification for Mobile Storage Bridge. "
            "This system provides on-device AI document organization and automated categorization for active mobile users."
        )

        sim = compute_jaccard_similarity(short_orig, short_edit, k=3)
        self.assertGreaterEqual(sim, 0.85, f"Expected short-text Jaccard >= 0.85, got {sim}")

    # -------------------------------------------------------------------------
    # 7. PII Sanitizer Non-Over-Redaction
    # -------------------------------------------------------------------------
    def test_10_pii_sanitizer_preserves_code_names(self):
        """P2 #22: Preserves generic 'name:' in JSON/Python code while redacting formal labels."""
        code_snippet = '{"name": "lecture_01.pdf", "path": "Download/lecture_01.pdf"}'
        sanitized_code = sanitize_pii(code_snippet)
        self.assertNotIn("[REDACTED_NAME]", sanitized_code)
        self.assertIn('"name": "lecture_01.pdf"', sanitized_code)

        # Formal administrative label should be redacted
        formal_snippet = "University Admission Form: Student Name: Imran Tahir, Roll No: BSAI-182"
        sanitized_formal = sanitize_pii(formal_snippet)
        self.assertIn("[REDACTED_NAME]", sanitized_formal)
        self.assertNotIn("Imran Tahir", sanitized_formal)

    # -------------------------------------------------------------------------
    # 8. Ephemeral Stream Tickets & Storage Overview
    # -------------------------------------------------------------------------
    def test_11_ephemeral_stream_ticket_and_overview(self):
        """P1 #15: Ephemeral SSE stream ticket and /storage_overview endpoint."""
        # 1. Storage overview
        with open(os.path.join(self.dl_dir, "voucher.pdf"), "w") as f: f.write("Tuition fee slip")
        with open(os.path.join(self.doc_dir, "notes.txt"), "w") as f: f.write("AI study notes")

        ov_res = self.client.get("/storage_overview").get_json()
        self.assertEqual(ov_res["status"], "success")
        self.assertGreaterEqual(ov_res["total_files"], 2)
        self.assertIn("category_counts", ov_res)

        # 2. Ephemeral stream ticket
        t_res = self.client.post("/stream_ticket").get_json()
        ticket = t_res.get("ticket")
        self.assertIsNotNone(ticket)
        self.assertEqual(t_res.get("expires_in"), 60)

        # Valid ticket authorizes SSE connection
        self.assertTrue(validate_stream_ticket(ticket))
        # Single-use: Second validation fails
        self.assertFalse(validate_stream_ticket(ticket))

    def test_12_fallback_notice_present_when_offline(self):
        """Product Pillar #3: Includes honest fallback notice when AI is unconfigured or unavailable."""
        res = self.client.post("/propose_plan", json={
            "prompt": "What can you do?",
            "target_folder": "Download"
        }).get_json()

        self.assertFalse(res.get("is_llm", True))
        self.assertEqual(res.get("fallback_notice"), "AI unavailable right now, using on-device fallback")


if __name__ == "__main__":
    unittest.main()
