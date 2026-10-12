import os
import sys
sys.stdout.reconfigure(line_buffering=True)
import json
import tempfile
import threading
import requests
from werkzeug.serving import make_server

# Set up test environment with isolated temporary directory
test_storage_dir = tempfile.mkdtemp(prefix="storage_phase1_test_")
os.environ["STORAGE_BASE_DIR"] = test_storage_dir
os.environ["LEDGER_DB_PATH"] = os.path.join(test_storage_dir, ".ledger.db")

import server
server.BASE_STORAGE_DIR = os.path.realpath(test_storage_dir)
server.BASE_DIR = server.BASE_STORAGE_DIR
server.TRASH_DIR = os.path.join(server.BASE_DIR, ".agent_trash")
server.LEDGER_DB_PATH = os.path.join(server.BASE_DIR, ".ledger.db")
os.makedirs(server.BASE_DIR, exist_ok=True)
os.makedirs(server.TRASH_DIR, exist_ok=True)
os.makedirs(os.path.join(server.BASE_DIR, "Download"), exist_ok=True)
os.makedirs(os.path.join(server.BASE_DIR, "Documents"), exist_ok=True)
server.init_db()

# Start test server on dynamic free port
srv = make_server("127.0.0.1", 0, server.app)
thread = threading.Thread(target=srv.serve_forever, daemon=True)
thread.start()
BASE_URL = f"http://127.0.0.1:{srv.server_port}"

def run_phase1_tests():
    passed = 0
    total = 0

    def assert_test(cond, title):
        nonlocal passed, total
        total += 1
        if cond:
            passed += 1
            print(f"  [PASS] {title}")
        else:
            print(f"  [FAIL] {title}")
            raise AssertionError(f"Test failed: {title}")

    print("\n" + "="*65)
    print(" Storage Copilot: Phase 1 Deduplication & Sweeper Test Suite")
    print("="*65)

    # -------------------------------------------------------------
    # 1. Unit Tests: Byte formatting & Keeper Scoring
    # -------------------------------------------------------------
    print("\n--- Test 1: Unit Helpers (Formatting & Keeper Scoring) ---")
    assert_test(server.format_bytes_human(500) == "500 B", "Formats bytes < 1024 as B")
    assert_test(server.format_bytes_human(2048) == "2.0 KB", "Formats KB correctly")
    assert_test(server.format_bytes_human(2500000) == "2.4 MB", "Formats MB correctly")

    score_orig = server.score_keeper_candidate({"filename": "Lecture_04.pdf", "path": "Download/Lecture_04.pdf", "mtime": 100})
    score_copy1 = server.score_keeper_candidate({"filename": "Lecture_04 (1).pdf", "path": "Download/Lecture_04 (1).pdf", "mtime": 200})
    score_copy2 = server.score_keeper_candidate({"filename": "Lecture_04_copy.pdf", "path": "Download/Lecture_04_copy.pdf", "mtime": 300})
    assert_test(score_orig > score_copy1, f"Clean canonical file scores higher than (1) suffix ({score_orig:.1f} > {score_copy1:.1f})")
    assert_test(score_orig > score_copy2, f"Clean canonical file scores higher than _copy suffix ({score_orig:.1f} > {score_copy2:.1f})")

    score_doc = server.score_keeper_candidate({"filename": "bill.pdf", "path": "Documents/Finances/bill.pdf", "mtime": 100})
    score_dl = server.score_keeper_candidate({"filename": "bill.pdf", "path": "Download/bill.pdf", "mtime": 100})
    assert_test(score_doc > score_dl, f"Categorized document in Documents/ scores higher than unorganized copy in Download/ ({score_doc:.1f} > {score_dl:.1f})")

    # -------------------------------------------------------------
    # 2. Duplicate Cluster Detection via Content Hashing
    # -------------------------------------------------------------
    print("\n--- Test 2: Exact Duplicate Cluster Detection ---")
    dl_dir = os.path.join(server.BASE_DIR, "Download")

    content_paper = "Neural Algorithm of Artistic Style ArXiv Deep Learning Paper content..."
    content_bill = "Electricity Utility Bill Invoice March 2025 Amount PKR 8500..."
    content_unique = "Unique single file that has no duplicates whatsoever..."

    # Seed 1 keeper + 2 duplicates of paper
    with open(os.path.join(dl_dir, "Neural_Style.pdf"), "w", encoding="utf-8") as f:
        f.write(content_paper)
    with open(os.path.join(dl_dir, "Neural_Style (1).pdf"), "w", encoding="utf-8") as f:
        f.write(content_paper)
    with open(os.path.join(dl_dir, "Neural_Style (2).pdf"), "w", encoding="utf-8") as f:
        f.write(content_paper)

    # Seed 1 keeper + 1 duplicate of bill
    with open(os.path.join(dl_dir, "Electricity_Bill.pdf"), "w", encoding="utf-8") as f:
        f.write(content_bill)
    with open(os.path.join(dl_dir, "Electricity_Bill_copy.pdf"), "w", encoding="utf-8") as f:
        f.write(content_bill)

    # Seed 1 unique file
    with open(os.path.join(dl_dir, "Unique_Notes.txt"), "w", encoding="utf-8") as f:
        f.write(content_unique)

    clusters_res = server.find_duplicate_clusters(server.BASE_STORAGE_DIR, "Download")
    assert_test(clusters_res.get("status") == "success", "find_duplicate_clusters succeeds")
    assert_test(clusters_res.get("total_clusters") == 2, f"Detected exactly 2 duplicate clusters (got {clusters_res.get('total_clusters')})")
    assert_test(clusters_res.get("total_duplicates") == 3, f"Detected exactly 3 redundant files (got {clusters_res.get('total_duplicates')})")
    assert_test(clusters_res.get("reclaimable_bytes") > 0, f"Reclaimable bytes calculated ({clusters_res.get('reclaimable_human')})")

    # Verify keeper in Paper cluster is Neural_Style.pdf
    paper_cluster = next((c for c in clusters_res["clusters"] if "neural" in c["keeper"]["filename"].lower()), None)
    assert_test(paper_cluster is not None, "Paper cluster found")
    assert_test(paper_cluster["keeper"]["filename"] == "Neural_Style.pdf", f"Preserves original keeper 'Neural_Style.pdf' (got '{paper_cluster['keeper']['filename']}')")
    assert_test(len(paper_cluster["duplicates"]) == 2, f"Paper cluster stages 2 redundant copies (got {len(paper_cluster['duplicates'])})")

    # -------------------------------------------------------------
    # 3. Dedicated Endpoint: GET & POST /scan_duplicates
    # -------------------------------------------------------------
    print("\n--- Test 3: Dedicated Endpoint (/scan_duplicates) ---")
    get_scan = requests.get(f"{BASE_URL}/scan_duplicates?target_folder=Download").json()
    assert_test(get_scan.get("status") == "success", "GET /scan_duplicates succeeds")
    assert_test(get_scan.get("total_clusters") == 2, f"GET reports 2 clusters (got {get_scan.get('total_clusters')})")

    post_scan = requests.post(f"{BASE_URL}/scan_duplicates", json={"target_folder": "Download"}).json()
    assert_test(post_scan.get("status") == "success", "POST /scan_duplicates succeeds")
    assert_test(post_scan.get("total_duplicates") == 3, f"POST reports 3 redundant duplicates (got {post_scan.get('total_duplicates')})")

    # -------------------------------------------------------------
    # 4. Prompt Intent: Read-Only Audit ("Find duplicate files")
    # -------------------------------------------------------------
    print("\n--- Test 4: Query Intent (Read-Only Duplicate Audit) ---")
    query_resp = requests.post(f"{BASE_URL}/propose_plan", json={
        "prompt": "Find duplicate files in Download",
        "target_folder": "Download"
    }).json()

    assert_test(query_resp.get("type") == "query_info", f"'Find duplicate files' returns type 'query_info' (got '{query_resp.get('type')}')")
    assert_test(len(query_resp.get("actions", [])) == 0, "Read-only query stages ZERO file mutations")
    msg = query_resp.get("message", "")
    assert_test("duplicate" in msg.lower(), "Query response message mentions duplicates")
    assert_test("neural_style.pdf" in msg.lower(), "Query cites original keeper file")
    assert_test("reclaimable storage" in msg.lower() or "reclaim" in msg.lower(), "Query reports reclaimable storage")

    # -------------------------------------------------------------
    # 5. Prompt Intent: Action Plan ("Clean duplicate downloads")
    # -------------------------------------------------------------
    print("\n--- Test 5: Action Plan Proposal ('Clean duplicate downloads') ---")
    plan_resp = requests.post(f"{BASE_URL}/propose_plan", json={
        "prompt": "Clean duplicate downloads",
        "target_folder": "Download"
    }).json()

    assert_test(plan_resp.get("type") == "plan", f"'Clean duplicate downloads' returns type 'plan' (got '{plan_resp.get('type')}')")
    actions = plan_resp.get("actions", [])
    assert_test(len(actions) == 3, f"Plan contains exactly 3 trash actions for the 3 duplicates (got {len(actions)})")

    # Verify all actions have type == 'trash' and badge == 'TRASH'
    all_trash = all(a.get("type") == "trash" for a in actions)
    assert_test(all_trash, "All proposed deduplication actions are of type 'trash'")
    all_trash_badge = all(a.get("badge") in ("TRASH", "DEDUPLICATION_TRASH") for a in actions)
    assert_test(all_trash_badge, "All actions annotated with TRASH badge")

    # Verify keepers are strictly preserved (NEVER present in actions)
    action_paths = [a.get("source") or a.get("path") for a in actions]
    assert_test("Download/Neural_Style.pdf" not in action_paths, "Keeper 'Neural_Style.pdf' is NOT trashed")
    assert_test("Download/Electricity_Bill.pdf" not in action_paths, "Keeper 'Electricity_Bill.pdf' is NOT trashed")
    assert_test("Download/Unique_Notes.txt" not in action_paths, "Unique document 'Unique_Notes.txt' is NOT touched")

    # Verify redundant copies ARE targeted
    assert_test("Download/Neural_Style (1).pdf" in action_paths, "Targets redundant copy 'Neural_Style (1).pdf'")
    assert_test("Download/Neural_Style (2).pdf" in action_paths, "Targets redundant copy 'Neural_Style (2).pdf'")
    assert_test("Download/Electricity_Bill_copy.pdf" in action_paths, "Targets redundant copy 'Electricity_Bill_copy.pdf'")

    # -------------------------------------------------------------
    # 6. Batch Execution & Verification of Kept vs Trashed Files
    # -------------------------------------------------------------
    print("\n--- Test 6: Batch Execution via /execute_plan ---")
    exec_resp = requests.post(f"{BASE_URL}/execute_plan", json=plan_resp["plan"]).json()
    assert_test(exec_resp.get("status") == "success", "Batch execution succeeds")
    assert_test(exec_resp.get("executed_actions") == 3, f"3 actions executed (got {exec_resp.get('executed_actions')})")
    batch_id = exec_resp.get("batch_id")
    assert_test(bool(batch_id), f"Returned valid SQLite WAL batch ID: {batch_id}")

    # Verify files on disk after execution
    assert_test(os.path.exists(os.path.join(dl_dir, "Neural_Style.pdf")), "Original keeper 'Neural_Style.pdf' remains intact in Download/")
    assert_test(os.path.exists(os.path.join(dl_dir, "Electricity_Bill.pdf")), "Original keeper 'Electricity_Bill.pdf' remains intact in Download/")
    assert_test(os.path.exists(os.path.join(dl_dir, "Unique_Notes.txt")), "Original unique file remains intact in Download/")

    assert_test(not os.path.exists(os.path.join(dl_dir, "Neural_Style (1).pdf")), "Redundant copy (1) removed from Download/")
    assert_test(not os.path.exists(os.path.join(dl_dir, "Neural_Style (2).pdf")), "Redundant copy (2) removed from Download/")
    assert_test(not os.path.exists(os.path.join(dl_dir, "Electricity_Bill_copy.pdf")), "Redundant copy (copy) removed from Download/")

    trash_files = os.listdir(server.TRASH_DIR)
    assert_test(len(trash_files) == 3, f"3 duplicate files safely stored in .agent_trash vault (found {len(trash_files)})")

    # -------------------------------------------------------------
    # 7. 1-Tap Atomic Rollback & State Restoration
    # -------------------------------------------------------------
    print("\n--- Test 7: 1-Tap Atomic Rollback via /rollback_batch ---")
    roll_resp = requests.post(f"{BASE_URL}/rollback_batch", json={"batch_id": batch_id}).json()
    assert_test(roll_resp.get("status") in ("rolled_back", "success"), f"Rollback succeeds (got '{roll_resp.get('status')}')")
    assert_test(roll_resp.get("reverted_actions") == 3, "3 actions reverted atomically")

    # Verify files restored on disk
    assert_test(os.path.exists(os.path.join(dl_dir, "Neural_Style (1).pdf")), "Rollback restored 'Neural_Style (1).pdf' to Download/")
    assert_test(os.path.exists(os.path.join(dl_dir, "Neural_Style (2).pdf")), "Rollback restored 'Neural_Style (2).pdf' to Download/")
    assert_test(os.path.exists(os.path.join(dl_dir, "Electricity_Bill_copy.pdf")), "Rollback restored 'Electricity_Bill_copy.pdf' to Download/")

    # Verify restored content matches exact original content
    with open(os.path.join(dl_dir, "Neural_Style (1).pdf"), "r", encoding="utf-8") as f:
        assert_test(f.read() == content_paper, "Restored file content matches original verbatim")

    # -------------------------------------------------------------
    # 8. Clean State: Prompting when ZERO Duplicates Exist
    # -------------------------------------------------------------
    print("\n--- Test 8: Clean State with Zero Duplicates ---")
    # Delete the redundant files to leave only unique documents
    os.remove(os.path.join(dl_dir, "Neural_Style (1).pdf"))
    os.remove(os.path.join(dl_dir, "Neural_Style (2).pdf"))
    os.remove(os.path.join(dl_dir, "Electricity_Bill_copy.pdf"))

    clean_resp = requests.post(f"{BASE_URL}/propose_plan", json={
        "prompt": "Clean duplicate downloads",
        "target_folder": "Download"
    }).json()

    assert_test(clean_resp.get("type") == "conversation", f"Zero duplicates returns 'conversation' (got '{clean_resp.get('type')}')")
    assert_test(len(clean_resp.get("actions", [])) == 0, "Zero actions staged when no duplicates exist")
    assert_test("no duplicate files detected" in clean_resp.get("message", "").lower(), "Reports friendly message: No duplicate files detected")

    print("\n" + "="*65)
    print(f" ALL {total} TESTS PASSED! ({passed}/{total})")
    print("="*65 + "\n")

if __name__ == "__main__":
    run_phase1_tests()
