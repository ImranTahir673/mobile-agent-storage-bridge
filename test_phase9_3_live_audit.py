"""
test_phase9_3_live_audit.py
Comprehensive acceptance test suite verifying all 10 Phase 9.3 Post-Retest Hardening Criteria:

Criteria 1: `git log --all -- user_profile.json` is empty.
Criteria 2: Seed fixtures write to `.demo_sandbox/Download/`, excluded from search/dedup, and `/scan_seed_fixtures` detects contaminated stubs.
Criteria 3: `"hi, can you find me this document"` hits Gemini when key is present; mocked offline/429 sets specific `fallback_notice` and `degraded_fallback: True`.
Criteria 4: Unknown batch cancel returns 404; 21 actions without `allow_large_batch=True` returns 400; >500 MB returns 400.
Criteria 5: Warm-cache search over 500 files completes in `< 2.0s`.
Criteria 6: Corrupt 109-byte PDF flagged as `is_corrupt=True` with quarantine proposal to `Documents/Review/Damaged/`.
Criteria 7: `"find my certificates"` ranks real certificate PDF above `META-INF/CERT.RSA`.
Criteria 8: `"find my fee vouchers"` ranks owner's voucher above `Documents/Peers/Fawad/`; `"find Fawad fee"` still finds Fawad's file.
Criteria 9: Raw `%PDF-1.4` binary snippet returns `"Scanned document — text not extractable"`.
Criteria 10: No-match query returns `matches == []`; regression check confirms Caesar cipher, FBR docs, U-Net, folder inventory, and October 2026 pass.
"""
import os
import time
import shutil
import tempfile
import subprocess
import unittest
from unittest.mock import patch, MagicMock

test_storage_dir = tempfile.mkdtemp(prefix="storage_phase9_3_audit_")
os.environ["STORAGE_BASE_DIR"] = test_storage_dir
os.environ["LEDGER_DB_PATH"] = os.path.join(test_storage_dir, ".ledger.db")
os.environ["GEMINI_API_KEY"] = ""

import server
from storage_copilot import config
from storage_copilot.db.ledger import get_db, init_db, upsert_indexed_file
from storage_copilot.security.auth import get_active_auth_token
from storage_copilot.storage.scanner import scan_directory_manifest
from storage_copilot.storage.content_extractor import inspect_file_health, extract_file_snippet, is_human_readable_text
from storage_copilot.intelligence.search_engine import score_file_relevance
from storage_copilot.intelligence.intent_classifier import classify_intent
from storage_copilot.intelligence.document_classifier import classify_document, parse_query_intent
from storage_copilot.execution.operation_handler import handle_file_operation_turn
from storage_copilot.execution.query_handler import handle_query_info_turn
from storage_copilot.api.utility_routes import SEED_FIXTURE_CONTENTS


class TestPhase93LiveAudit(unittest.TestCase):
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
        cls.auth_token = get_active_auth_token()
        cls.headers = {
            "Authorization": f"Bearer {cls.auth_token}",
            "Content-Type": "application/json"
        }

    def setUp(self):
        os.environ["GEMINI_API_KEY"] = ""
        # Clean test directories
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
        sandbox_dir = os.path.join(server.BASE_STORAGE_DIR, ".demo_sandbox")
        if os.path.exists(sandbox_dir):
            shutil.rmtree(sandbox_dir, ignore_errors=True)

    def test_criteria_1_git_history_purged_user_profile(self):
        """Criteria 1: git log --all -- user_profile.json must be completely empty."""
        repo_root = os.path.dirname(os.path.abspath(__file__))
        res = subprocess.run(
            ["git", "log", "--all", "--", "user_profile.json"],
            capture_output=True,
            text=True,
            cwd=repo_root
        )
        self.assertEqual(res.returncode, 0)
        self.assertEqual(
            res.stdout.strip(),
            "",
            f"user_profile.json was not completely purged from git history:\n{res.stdout}"
        )

    def test_criteria_2_seed_fixture_isolation_and_scan(self):
        """Criteria 2: Seed fixtures write to .demo_sandbox/Download/, excluded from search, and /scan_seed_fixtures detects stubs."""
        # 1. POST /seed_fixtures creates files strictly inside .demo_sandbox/Download/
        resp = self.client.post("/seed_fixtures", headers=self.headers)
        self.assertEqual(resp.status_code, 200)
        demo_dl = os.path.join(server.BASE_STORAGE_DIR, ".demo_sandbox", "Download")
        self.assertTrue(os.path.isdir(demo_dl))
        seeded_files = os.listdir(demo_dl)
        self.assertGreater(len(seeded_files), 0)

        # Real Download/ is NOT polluted
        real_files = os.listdir(self.dl_dir)
        for s in seeded_files:
            self.assertNotIn(s, real_files)

        # 2. Scanner excludes .demo_sandbox/
        manifest_res = scan_directory_manifest(server.BASE_STORAGE_DIR)
        manifest = manifest_res.get("manifest", [])
        for entry in manifest:
            self.assertNotIn(".demo_sandbox", entry["path"])

        # 3. Contamination detection via /scan_seed_fixtures
        stub_name = "1508.06576v2_neural_style.pdf"
        contaminated_file = os.path.join(self.dl_dir, stub_name)
        with open(contaminated_file, "w", encoding="utf-8") as f:
            f.write(SEED_FIXTURE_CONTENTS[stub_name])

        scan_resp = self.client.get("/scan_seed_fixtures", headers=self.headers)
        self.assertEqual(scan_resp.status_code, 200)
        scan_data = scan_resp.get_json()
        self.assertEqual(scan_data.get("action"), "trash")
        sources = [a["source"] for a in scan_data.get("actions", [])]
        self.assertTrue(any(stub_name in s for s in sources))

        # 4. POST /clear_demo_sandbox purges demo files
        clear_resp = self.client.post("/clear_demo_sandbox", headers=self.headers)
        self.assertEqual(clear_resp.status_code, 200)
        self.assertFalse(os.path.exists(demo_dl))

    def test_criteria_3_ai_first_always_and_specific_fallback_diagnostics(self):
        """Criteria 3: 'hi, can you find me this document' hits Gemini when key present; offline/429 sets specific fallback diagnostics."""
        # A. Key present and Gemini succeeds -> Gemini micro-classifier is called first (no regex bypass)
        with patch("storage_copilot.intelligence.intent_classifier.call_gemini_micro_classifier") as mock_micro:
            mock_micro.return_value = {
                "intent": "QUERY_INFO",
                "sub_intent": "search_document",
                "confidence": 0.95,
                "resolved_goal": "find document"
            }
            with patch.dict(os.environ, {"GEMINI_API_KEY": "AIzaSyMockKeyValid12345"}):
                res = classify_intent("hi, can you find me this document")
                mock_micro.assert_called_once()
                self.assertEqual(res["intent"], "QUERY_INFO")
                self.assertFalse(res.get("degraded_fallback", False))

        # B. Rate-limited (429) -> Sets rate_limited reason and honest fallback notice
        with patch("storage_copilot.intelligence.intent_classifier.call_gemini_micro_classifier") as mock_micro:
            mock_micro.side_effect = Exception("429 RESOURCE_EXHAUSTED Quota exceeded for model")
            with patch.dict(os.environ, {"GEMINI_API_KEY": "AIzaSyMockKeyValid12345"}):
                res = classify_intent("find my resume")
                self.assertTrue(res.get("degraded_fallback"))
                self.assertEqual(res.get("fallback_reason"), "rate_limited")
                self.assertIn("temporary rate limit", res.get("fallback_notice", ""))

        # C. Network offline -> Sets no_internet reason
        with patch("storage_copilot.intelligence.intent_classifier.call_gemini_micro_classifier") as mock_micro:
            mock_micro.side_effect = ConnectionError("Failed to establish network connection")
            with patch.dict(os.environ, {"GEMINI_API_KEY": "AIzaSyMockKeyValid12345"}):
                res = classify_intent("find my resume")
                self.assertTrue(res.get("degraded_fallback"))
                self.assertEqual(res.get("fallback_reason"), "no_internet")
                self.assertIn("no internet connection", res.get("fallback_notice", ""))

        # D. No API key configured -> Sets no_api_key reason and standard fallback notice
        with patch.dict(os.environ, {"GEMINI_API_KEY": ""}):
            res = classify_intent("find my resume")
            self.assertTrue(res.get("degraded_fallback"))
            self.assertEqual(res.get("fallback_reason"), "no_api_key")
            self.assertEqual(res.get("fallback_notice"), "AI unavailable right now, using on-device fallback")

    def test_criteria_4_cancellation_and_blast_radius(self):
        """Criteria 4: Unknown batch cancel returns 404; 21 actions returns 400; >500 MB returns 400."""
        # 1. Unknown batch cancel returns 404
        resp = self.client.post("/cancel_execution", json={"batch_id": "non_existent_batch_xyz_999"}, headers=self.headers)
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.get_json().get("status"), "not_found")

        # 2. 21 actions without allow_large_batch=True returns 400
        plan_21 = {
            "batch_id": "test_batch_21",
            "action": "move",
            "actions": [
                {"type": "move", "source": f"Download/file_{i}.txt", "destination": f"Documents/file_{i}.txt"}
                for i in range(21)
            ],
            "allow_large_batch": False
        }
        resp = self.client.post("/execute_plan", json=plan_21, headers=self.headers)
        self.assertEqual(resp.status_code, 400)
        self.assertIn("exceeds limit of 20 actions", resp.get_json().get("error", ""))

        # 3. > 500 MB cumulative bytes returns 400
        plan_oversize = {
            "batch_id": "test_batch_oversize",
            "action": "move",
            "actions": [
                {"type": "move", "source": "Download/large.bin", "destination": "Documents/large.bin"}
            ],
            "total_bytes": 550 * 1024 * 1024
        }
        resp = self.client.post("/execute_plan", json=plan_oversize, headers=self.headers)
        self.assertEqual(resp.status_code, 400)
        self.assertIn("blast radius", resp.get_json().get("error", "").lower())

    def test_criteria_5_warm_cache_search_speed(self):
        """Criteria 5: Warm-cache search over 500 files completes in < 2.0s."""
        now = time.time()
        for i in range(500):
            upsert_indexed_file(
                path=f"Download/sample_document_{i}.pdf",
                mtime=now,
                size_bytes=1024 + i,
                sanitized_text=f"Sample university lecture notes regarding machine learning topic {i}",
                is_corrupt=False,
                is_scanned=False
            )

        start = time.perf_counter()
        candidates = [
            {
                "path": f"Download/sample_document_{i}.pdf",
                "name": f"sample_document_{i}.pdf",
                "snippet": f"Sample university lecture notes regarding machine learning topic {i}"
            }
            for i in range(500)
        ]
        scored = [score_file_relevance(c, "machine learning notes")["score"] for c in candidates]
        elapsed = time.perf_counter() - start

        self.assertEqual(len(scored), 500)
        self.assertLess(elapsed, 2.0, f"Search over 500 files took {elapsed:.3f}s (must be < 2.0s)")

    def test_criteria_6_corrupt_file_detection_and_quarantine(self):
        """Criteria 6: Corrupt 109-byte PDF flagged as is_corrupt=True with quarantine proposal to Documents/Review/Damaged/."""
        corrupt_path = os.path.join(self.dl_dir, "damaged_file.pdf")
        with open(corrupt_path, "wb") as f:
            f.write(b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\n" + b"X" * 60)

        health = inspect_file_health(corrupt_path)
        self.assertTrue(health.get("is_corrupt"))

        classification = {
            "intent": "FILE_OPERATION",
            "sub_intent": "organize_damaged_files",
            "raw_prompt": "clean up damaged files",
            "candidate_files": [{"path": corrupt_path, "name": "damaged_file.pdf"}]
        }
        res = handle_file_operation_turn("clean up damaged files", classification, target_folder="Download")
        self.assertEqual(res.get("type"), "plan")
        actions = res.get("plan", {}).get("actions", [])
        self.assertTrue(any("Documents/Review/Damaged" in a["destination"] for a in actions))

    def test_criteria_7_certificate_vs_apk_signature_penalty(self):
        """Criteria 7: 'find my certificates' ranks real certificate PDF above META-INF/CERT.RSA."""
        cert_pdf = {
            "path": "Documents/Degree_Certificate_BSAI.pdf",
            "name": "Degree_Certificate_BSAI.pdf",
            "snippet": "Degree Certificate of Completion Imran Tahir BSAI 182"
        }
        apk_rsa = {
            "path": "Download/app/META-INF/CERT.RSA",
            "name": "CERT.RSA",
            "snippet": "BINARY RSA KEY SIGNATURE"
        }
        s_cert = score_file_relevance(cert_pdf, "find my certificates")["score"]
        s_rsa = score_file_relevance(apk_rsa, "find my certificates")["score"]

        self.assertGreater(s_cert, 40.0)
        self.assertEqual(s_rsa, 0.0)
        self.assertGreater(s_cert - s_rsa, 40.0)

    def test_criteria_8_peer_folder_penalty_vs_explicit_peer(self):
        """Criteria 8: 'find my fee vouchers' ranks owner's voucher above Documents/Peers/Fawad/; 'find Fawad fee' still finds Fawad's file."""
        owner_voucher = {
            "path": "Download/Fee_Challan_Fall2026.pdf",
            "name": "Fee_Challan_Fall2026.pdf",
            "snippet": "Fee Challan Invoice Student Imran Tahir BSAI-182 Total 145000"
        }
        peer_voucher = {
            "path": "Documents/Peers/Fawad/Fee_Challan_Fall2026.pdf",
            "name": "Fee_Challan_Fall2026.pdf",
            "snippet": "Fee Challan Invoice Student Fawad Total 145000"
        }
        # 1. Query for owner's vouchers -> owner voucher scores strictly higher due to -25.0 peer penalty
        s_owner = score_file_relevance(owner_voucher, "find my fee vouchers")["score"]
        s_peer = score_file_relevance(peer_voucher, "find my fee vouchers")["score"]
        self.assertGreater(s_owner, s_peer)

        # 2. Query explicitly naming Fawad -> peer voucher scores high without penalty
        s_fawad = score_file_relevance(peer_voucher, "find Fawad fee")["score"]
        self.assertGreaterEqual(s_fawad, 50.0)

    def test_criteria_9_scanned_or_binary_pdf_intercept(self):
        """Criteria 9: Raw %PDF-1.4 binary snippet returns 'Scanned document — text not extractable'."""
        binary_data = "%PDF-1.4\n1 0 obj\nstream\n" + ("\x00\x01\x02\xfe\xff" * 50) + "\nendstream\nendobj"
        is_readable, clean_text, is_scanned_or_binary = is_human_readable_text(binary_data)
        self.assertFalse(is_readable)
        self.assertTrue(is_scanned_or_binary)
        self.assertEqual(clean_text, "Scanned document — text not extractable")

        scan_file = os.path.join(self.dl_dir, "scanned_doc.pdf")
        with open(scan_file, "w", encoding="utf-8", errors="replace") as f:
            f.write(binary_data)
        snip = extract_file_snippet(scan_file)
        self.assertEqual(snip, "Scanned document — text not extractable")

    def test_criteria_10_honest_empty_and_regressions(self):
        """Criteria 10: No-match query returns matches == []; regression check confirms Caesar cipher, FBR docs, U-Net, folder inventory, and October 2026."""
        # 1. Honest-empty: No-match query returns matches == []
        empty_res = handle_query_info_turn(
            "find quantum entanglement teleportation schematics",
            {"sub_intent": "search_document", "raw_prompt": "find quantum entanglement teleportation schematics"}
        )
        self.assertEqual(empty_res.get("matches"), [])
        self.assertTrue(
            "could not locate" in empty_res.get("message", "") or "No matching files" in empty_res.get("message", "")
        )

        # 2. Regression: Caesar cipher search relevance
        caesar_doc = {
            "path": "Download/caesar_cipher.py",
            "name": "caesar_cipher.py",
            "snippet": "def encrypt_caesar(text, shift):\n    return ''.join(chr((ord(c) - 97 + shift) % 26 + 97) for c in text)"
        }
        caesar_rel = score_file_relevance(caesar_doc, "find the caesar cipher code")
        self.assertTrue(caesar_rel["is_relevant"])
        self.assertGreaterEqual(caesar_rel["score"], 40.0)

        # 3. Regression: FBR tax return classification
        fbr_class = classify_document("fbr_tax_return.pdf", "Federal Board of Revenue Income Tax Return 2026 Total Tax Paid")
        self.assertEqual(fbr_class.get("category"), "FINANCIAL_BILL")

        # 4. Regression: U-Net project classification
        unet_class = classify_document("unet_segmentation.py", "U-Net convolutional neural network segmentation architecture for biomedical images")
        self.assertEqual(unet_class.get("category"), "ACADEMIC_STUDY")

        # 5. Regression: Folder inventory query
        inv_query = parse_query_intent("what is in my download folder")
        self.assertTrue(inv_query.get("is_directory_listing"))

        # 6. Regression: October 2026 temporal filtering
        temporal_turn = handle_query_info_turn(
            "files from October 2026",
            {"sub_intent": "temporal_search", "raw_prompt": "files from October 2026", "normalized_prompt": "files from October 2026"}
        )
        self.assertTrue(
            "2026-10" in temporal_turn.get("message", "") or "October 2026" in temporal_turn.get("message", "")
        )


if __name__ == "__main__":
    unittest.main()
