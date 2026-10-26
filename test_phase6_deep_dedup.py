"""
Storage Copilot: Phase 6 Deep Deduplication (3-Gram Jaccard) & Forgotten Files Finder Test Suite
Verifies:
1. 3-Gram Jaccard Similarity Engine (`compute_text_shingles` & `compute_jaccard_similarity`):
   - Accurately computes >= 0.85 similarity for slightly edited/revised documents.
   - Computes low similarity (< 0.25) for distinct documents.
2. Unified Exact SHA-256 + Near-Duplicate Clustering (`find_duplicate_clusters(include_near=True)`):
   - Groups both byte-identical copies (`EXACT_SHA256`) and revised near-duplicates (`NEAR_JACCARD`).
   - Preserves clean canonical original file over `_edit` / `_draft` / `(1)` copies.
   - Exposes `GET/POST /scan_duplicates?include_near=1`.
3. Forgotten Files Finder (`find_forgotten_files` & `/scan_forgotten`):
   - Identifies stale downloads (>90 days old), temporary cache files (`.tmp`, `.cache`, `temp_*`),
     and orphaned screenshots (`Screenshot_*.png`).
   - Leaves recent valid documents untouched.
4. End-to-End Intent & Plan Execution for Forgotten Files & Near-Duplicates:
   - `"Find forgotten files"` -> `QUERY_INFO` audit report (0 mutations).
   - `"Clean forgotten files"` -> `FILE_OPERATION` trash plan + atomic execution + 1-tap rollback.
"""
import os
import time
import unittest
import tempfile
from datetime import datetime, timedelta

test_storage_dir = tempfile.mkdtemp(prefix="storage_phase6_dedup_")
os.environ["STORAGE_BASE_DIR"] = test_storage_dir
os.environ["LEDGER_DB_PATH"] = os.path.join(test_storage_dir, ".ledger.db")
os.environ["GEMINI_API_KEY"] = ""

import server
from storage_copilot.storage.deduplication import (
    compute_text_shingles,
    compute_jaccard_similarity,
    find_duplicate_clusters,
    find_forgotten_files,
)


class TestPhase6DeepDedupAndForgotten(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        server.BASE_STORAGE_DIR = os.path.realpath(test_storage_dir)
        server.BASE_DIR = server.BASE_STORAGE_DIR
        server.TRASH_DIR = os.path.join(server.BASE_DIR, ".agent_trash")
        server.LEDGER_DB_PATH = os.path.join(server.BASE_DIR, ".ledger.db")
        server.ENFORCE_LOCAL_AUTH = False
        cls.dl_dir = os.path.join(server.BASE_DIR, "Download")
        os.makedirs(cls.dl_dir, exist_ok=True)
        os.makedirs(os.path.join(server.BASE_DIR, "Documents"), exist_ok=True)
        server.init_db()
        cls.client = server.app.test_client()

    def setUp(self):
        # Clean Download directory before each test
        for entry in os.scandir(self.dl_dir):
            if entry.is_file():
                os.remove(entry.path)

    def test_01_three_gram_jaccard_similarity_math(self):
        """Verify 3-gram shingle extraction and Jaccard similarity threshold (>= 0.85)."""
        doc_orig = (
            "Software Requirements Specification for Information Retrieval System. "
            "This document describes the architecture, indexing pipeline, vector ranking engine, "
            "query parser, evaluation metrics, and user interface design for the semester project."
        )
        doc_edited = (
            "Software Requirements Specification for Information Retrieval System. "
            "This document describes the architecture, indexing pipeline, vector ranking engine, "
            "query parser, evaluation metrics, and user interface design for the final semester project."
        )
        doc_unrelated = (
            "Electricity Utility Bill Invoice for March 2025. Consumer ID 948201. "
            "Total amount payable is PKR 8500 before the due date."
        )

        sim_near = compute_jaccard_similarity(doc_orig, doc_edited, k=3)
        sim_diff = compute_jaccard_similarity(doc_orig, doc_unrelated, k=3)

        self.assertGreaterEqual(sim_near, 0.85, f"Expected near-duplicate Jaccard >= 0.85, got {sim_near}")
        self.assertLess(sim_diff, 0.15, f"Expected unrelated Jaccard < 0.15, got {sim_diff}")

    def test_02_exact_and_near_duplicate_clustering(self):
        """Verify find_duplicate_clusters detects both exact SHA-256 and near-duplicate 3-gram Jaccard groups."""
        srs_v1 = (
            "Software Requirements Specification for Information Retrieval System. "
            "This document describes the system architecture, inverted index, BM25 ranking model, "
            "precision and recall evaluation benchmarks, and web interface."
        )
        srs_v2_edit = (
            "Software Requirements Specification for Information Retrieval System. "
            "This document describes the system architecture, inverted index, BM25 ranking model, "
            "precision and recall evaluation benchmarks, and mobile web interface."
        )
        exact_text = "Neural Algorithm of Artistic Style ArXiv Deep Learning Paper content."

        # Seed 1 pair of near-duplicates (different file sizes & hashes, >= 0.85 3-gram Jaccard)
        with open(os.path.join(self.dl_dir, "SRS_IR_Solution.txt"), "w", encoding="utf-8") as f:
            f.write(srs_v1)
        with open(os.path.join(self.dl_dir, "SRS_IR_Solution_edit.txt"), "w", encoding="utf-8") as f:
            f.write(srs_v2_edit)

        # Seed 1 pair of exact SHA-256 duplicates
        with open(os.path.join(self.dl_dir, "Neural_Paper.pdf"), "w", encoding="utf-8") as f:
            f.write(exact_text)
        with open(os.path.join(self.dl_dir, "Neural_Paper (1).pdf"), "w", encoding="utf-8") as f:
            f.write(exact_text)

        # 1. Default exact-only scan (backward-compatible)
        res_exact = find_duplicate_clusters(server.BASE_STORAGE_DIR, "Download", include_near=False)
        self.assertEqual(res_exact["total_clusters"], 1)
        self.assertEqual(res_exact["exact_clusters_count"], 1)
        self.assertEqual(res_exact["near_clusters_count"], 0)

        # 2. Deep scan with include_near=True
        res_deep = find_duplicate_clusters(server.BASE_STORAGE_DIR, "Download", include_near=True, near_threshold=0.85)
        self.assertEqual(res_deep["total_clusters"], 2)
        self.assertEqual(res_deep["exact_clusters_count"], 1)
        self.assertEqual(res_deep["near_clusters_count"], 1)
        self.assertEqual(res_deep["total_duplicates"], 2)

        near_cluster = next(c for c in res_deep["clusters"] if c["match_type"] == "NEAR_JACCARD")
        self.assertEqual(near_cluster["keeper"]["filename"], "SRS_IR_Solution.txt")
        self.assertEqual(near_cluster["duplicates"][0]["filename"], "SRS_IR_Solution_edit.txt")
        self.assertGreaterEqual(near_cluster["similarity"], 0.85)

        # 3. Verify HTTP endpoint /scan_duplicates?include_near=1
        api_res = self.client.get("/scan_duplicates?target_folder=Download&include_near=1").get_json()
        self.assertEqual(api_res["status"], "success")
        self.assertEqual(api_res["near_clusters_count"], 1)

    def test_03_forgotten_files_finder_categories(self):
        """Verify Forgotten Files Finder detects stale downloads (>90d), temp files, and orphaned screenshots."""
        now = datetime.now()

        # 1. Stale download (120 days old)
        stale_path = os.path.join(self.dl_dir, "old_semester_syllabus_2024.pdf")
        with open(stale_path, "w", encoding="utf-8") as f:
            f.write("Old syllabus from 4 months ago.")
        stale_ts = (now - timedelta(days=120)).timestamp()
        os.utime(stale_path, (stale_ts, stale_ts))

        # 2. Temporary cache file (recent, but .tmp)
        temp_path = os.path.join(self.dl_dir, "temp_cache_sync.tmp")
        with open(temp_path, "w", encoding="utf-8") as f:
            f.write("TEMP_CACHE_DATA")

        # 3. Orphaned screenshot
        shot_path = os.path.join(self.dl_dir, "Screenshot_20251001-142210.png")
        with open(shot_path, "w", encoding="utf-8") as f:
            f.write("PNG_BINARY_DATA")

        # 4. Active recent lecture (2 days old) -> MUST NOT be flagged as forgotten
        recent_path = os.path.join(self.dl_dir, "Active_AI_Lecture_04.pdf")
        with open(recent_path, "w", encoding="utf-8") as f:
            f.write("Knowledge Representation Lecture 4.")

        forg = find_forgotten_files(server.BASE_STORAGE_DIR, "Download", stale_days=90.0)
        self.assertEqual(forg["status"], "success")
        self.assertEqual(forg["total_forgotten"], 3)
        self.assertEqual(len(forg["stale_downloads"]), 1)
        self.assertEqual(len(forg["temporary_files"]), 1)
        self.assertEqual(len(forg["orphaned_screenshots"]), 1)

        flagged_names = {it["filename"] for it in forg["items"]}
        self.assertIn("old_semester_syllabus_2024.pdf", flagged_names)
        self.assertIn("temp_cache_sync.tmp", flagged_names)
        self.assertIn("Screenshot_20251001-142210.png", flagged_names)
        self.assertNotIn("Active_AI_Lecture_04.pdf", flagged_names)

        # Verify HTTP endpoint GET /scan_forgotten
        api_forg = self.client.get("/scan_forgotten?target_folder=Download&stale_days=90").get_json()
        self.assertEqual(api_forg["total_forgotten"], 3)

    def test_04_end_to_end_forgotten_files_query_and_clean_rollback(self):
        """Verify natural-language audit ('Find forgotten files') and cleanup ('Clean forgotten files') with rollback."""
        temp_path = os.path.join(self.dl_dir, "temp_cache_sync.tmp")
        with open(temp_path, "w", encoding="utf-8") as f:
            f.write("TEMP_CACHE_DATA")

        # 1. Read-only audit prompt -> QUERY_INFO, 0 actions
        q_resp = self.client.post("/propose_plan", json={
            "prompt": "Find forgotten files in Download",
            "target_folder": "Download"
        }).get_json()
        self.assertEqual(q_resp["type"], "query_info")
        self.assertEqual(q_resp["intent"], "QUERY_INFO")
        self.assertEqual(len(q_resp["actions"]), 0)
        self.assertIn("temp_cache_sync.tmp", q_resp["message"])

        # 2. Cleanup action prompt -> FILE_OPERATION plan
        p_resp = self.client.post("/propose_plan", json={
            "prompt": "Clean forgotten files in Download",
            "target_folder": "Download"
        }).get_json()
        self.assertEqual(p_resp["type"], "plan")
        self.assertEqual(p_resp["intent"], "FILE_OPERATION")
        self.assertEqual(len(p_resp["actions"]), 1)
        self.assertEqual(p_resp["actions"][0]["type"], "trash")

        # 3. Execute & Rollback
        exec_res = self.client.post("/execute_plan", json=p_resp["plan"]).get_json()
        self.assertEqual(exec_res["status"], "success")
        self.assertFalse(os.path.exists(temp_path))

        roll_res = self.client.post("/rollback_batch", json={"batch_id": exec_res["batch_id"]}).get_json()
        self.assertEqual(roll_res["status"], "rolled_back")
        self.assertTrue(os.path.exists(temp_path))


if __name__ == "__main__":
    unittest.main()
