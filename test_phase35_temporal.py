import os
import sys
sys.stdout.reconfigure(line_buffering=True)
import json
import tempfile
import threading
from datetime import datetime, timedelta
import requests
from werkzeug.serving import make_server

# Set up clean temporary test environment
test_storage_dir = tempfile.mkdtemp(prefix="storage_temporal_test_")
os.environ["STORAGE_BASE_DIR"] = test_storage_dir
os.environ["LEDGER_DB_PATH"] = os.path.join(test_storage_dir, ".ledger.db")

import server
server.BASE_STORAGE_DIR = os.path.realpath(test_storage_dir)
server.BASE_DIR = server.BASE_STORAGE_DIR
server.TRASH_DIR = os.path.join(server.BASE_DIR, ".agent_trash")
server.LEDGER_DB_PATH = os.path.join(server.BASE_DIR, ".ledger.db")
os.makedirs(server.BASE_DIR, exist_ok=True)
os.makedirs(server.TRASH_DIR, exist_ok=True)
download_dir = os.path.join(server.BASE_DIR, "Download")
os.makedirs(download_dir, exist_ok=True)
os.makedirs(os.path.join(server.BASE_DIR, "Documents"), exist_ok=True)
server.init_db()

# Start test server on dynamic free port
srv = make_server("127.0.0.1", 0, server.app)
thread = threading.Thread(target=srv.serve_forever, daemon=True)
thread.start()
BASE_URL = f"http://127.0.0.1:{srv.server_port}"

def seed_temporal_fixtures():
    now = datetime.now()
    fixtures = [
        ("today_lecture_notes.pdf", 0, "Artificial Intelligence Lecture Notes on Search Algorithms."),
        ("yesterday_fee_receipt.pdf", 1, "Tuition Fee Slip Receipt for Fall Semester Imran Tahir."),
        ("week_assignment_ai.pdf", 4, "Knowledge Representation Assignment 2 Heuristic Functions."),
        ("old_tax_archive_2025.zip", 45, "Archived financial records and tax documents from earlier this year."),
        ("very_old_statement_2024.pdf", 120, "Bank Account Statement and transaction summary from 2024.")
    ]
    for fname, days_ago, content in fixtures:
        fpath = os.path.join(download_dir, fname)
        with open(fpath, "w", encoding="utf-8") as f:
            f.write(content)
        target_time = (now - timedelta(days=days_ago)).timestamp()
        os.utime(fpath, (target_time, target_time))

def run_temporal_test_suite():
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
    print(" Storage Copilot Phase 3.5: Temporal & Date Engine Test Suite")
    print("="*65)

    # 1. Seed Temporal Fixtures
    print("\n--- Test 1: Seed Distinct Temporal Fixtures ---")
    seed_temporal_fixtures()
    files_seeded = os.listdir(download_dir)
    assert_test(len(files_seeded) >= 5, f"Seeded {len(files_seeded)} temporal fixture files")
    assert_test("today_lecture_notes.pdf" in files_seeded, "Includes today's file")
    assert_test("yesterday_fee_receipt.pdf" in files_seeded, "Includes yesterday's file")
    assert_test("very_old_statement_2024.pdf" in files_seeded, "Includes very old file (120d ago)")

    # 2. Query Recency: Latest Download
    print("\n--- Test 2: Recency Query ('What is my latest download?') ---")
    resp_latest = requests.post(f"{BASE_URL}/propose_plan", json={
        "prompt": "What is my latest download?",
        "target_folder": "Download"
    }).json()
    assert_test(resp_latest.get("type") == "query_info", f"Returns type 'query_info' (got '{resp_latest.get('type')}')")
    assert_test(len(resp_latest.get("actions", [])) == 0, "ZERO file mutations staged for recency query")
    msg_latest = resp_latest.get("message", "")
    assert_test("today_lecture_notes.pdf" in msg_latest, f"Cites 'today_lecture_notes.pdf' as latest download")

    # 3. Calendar Date Grounding: Today
    print("\n--- Test 3: Calendar Date Query ('Did I download anything today?') ---")
    resp_today = requests.post(f"{BASE_URL}/propose_plan", json={
        "prompt": "Did I download anything today?",
        "target_folder": "Download"
    }).json()
    assert_test(resp_today.get("type") == "query_info", f"Returns type 'query_info' (got '{resp_today.get('type')}')")
    assert_test(len(resp_today.get("actions", [])) == 0, "ZERO file mutations staged for date query")
    msg_today = resp_today.get("message", "")
    assert_test("today_lecture_notes" in msg_today or "today" in msg_today.lower(), "Accurately acknowledges today's download")

    # 4. Calendar Date Grounding: Yesterday
    print("\n--- Test 4: Calendar Date Query ('What files did I receive yesterday?') ---")
    resp_yesterday = requests.post(f"{BASE_URL}/propose_plan", json={
        "prompt": "What files did I receive yesterday?",
        "target_folder": "Download"
    }).json()
    assert_test(resp_yesterday.get("type") == "query_info", f"Returns type 'query_info' (got '{resp_yesterday.get('type')}')")
    assert_test(len(resp_yesterday.get("actions", [])) == 0, "ZERO file mutations staged")
    msg_yesterday = resp_yesterday.get("message", "")
    assert_test("yesterday_fee_receipt.pdf" in msg_yesterday, "Accurately cites 'yesterday_fee_receipt.pdf'")

    # 5. Temporal Recency: Oldest Files
    print("\n--- Test 5: Recency Query ('What are my oldest files?') ---")
    resp_oldest = requests.post(f"{BASE_URL}/propose_plan", json={
        "prompt": "What are my oldest files?",
        "target_folder": "Download"
    }).json()
    assert_test(resp_oldest.get("type") == "query_info", f"Returns type 'query_info' (got '{resp_oldest.get('type')}')")
    assert_test(len(resp_oldest.get("actions", [])) == 0, "ZERO file mutations staged")
    msg_oldest = resp_oldest.get("message", "")
    assert_test("very_old_statement_2024.pdf" in msg_oldest or "old_tax_archive_2025.zip" in msg_oldest,
                "Identifies 2024/2025 old files as oldest")

    # 6. Temporal Action Directives: Lifecycle Archiving
    print("\n--- Test 6: Temporal Action Directive ('Move files older than 30 days to Documents/Archive/') ---")
    resp_archive = requests.post(f"{BASE_URL}/propose_plan", json={
        "prompt": "Move files older than 30 days to Documents/Archive/",
        "target_folder": "Download"
    }).json()
    assert_test(resp_archive.get("type") == "plan", f"Returns type 'plan' (got '{resp_archive.get('type')}')")
    actions = resp_archive.get("actions", [])
    assert_test(len(actions) >= 2, f"Staged {len(actions)} actions for files older than 30 days")
    sources = [a.get("source", "").lower() for a in actions]
    assert_test(any("old_tax_archive_2025.zip" in s for s in sources), "Includes old_tax_archive_2025.zip")
    assert_test(any("very_old_statement_2024.pdf" in s for s in sources), "Includes very_old_statement_2024.pdf")
    # Verify newer files are NOT touched
    assert_test(not any("today_lecture_notes.pdf" in s for s in sources), "Preserves today's lecture notes in Download")
    assert_test(not any("yesterday_fee_receipt.pdf" in s for s in sources), "Preserves yesterday's receipt in Download")

    # 7. Execute Lifecycle Archiving Plan & 1-Tap Rollback
    print("\n--- Test 7: Execute Lifecycle Archiving Plan and Verify Rollback ---")
    exec_resp = requests.post(f"{BASE_URL}/execute_plan", json=resp_archive["plan"]).json()
    assert_test(exec_resp.get("status") == "success", "Archiving execution succeeds")
    batch_id = exec_resp.get("batch_id")
    assert_test(bool(batch_id), f"Batch ID returned: {batch_id}")

    # Verify destination files exist on disk
    archive_dir = os.path.join(server.BASE_DIR, "Documents", "Archive")
    assert_test(os.path.exists(archive_dir), "Documents/Archive/ directory created")

    # Rollback via SQLite WAL ledger
    roll_resp = requests.post(f"{BASE_URL}/rollback_batch", json={"batch_id": batch_id}).json()
    assert_test(roll_resp.get("status") in ("rolled_back", "success"), f"Rollback succeeds: {roll_resp.get('status')}")
    assert_test(roll_resp.get("reverted_actions") == exec_resp.get("executed_actions"),
                f"All {roll_resp.get('reverted_actions')} lifecycle actions reverted")

    # Verify restored back in Download
    assert_test(os.path.exists(os.path.join(download_dir, "old_tax_archive_2025.zip")), "old_tax_archive_2025.zip restored to Download/")
    assert_test(os.path.exists(os.path.join(download_dir, "very_old_statement_2024.pdf")), "very_old_statement_2024.pdf restored to Download/")

    print("\n" + "="*65)
    print(f" ALL {total} TEMPORAL ENGINE TESTS PASSED! ({passed}/{total})")
    print("="*65 + "\n")

if __name__ == "__main__":
    try:
        run_temporal_test_suite()
        sys.exit(0)
    except Exception as e:
        print(f"\n[ERROR] Temporal test suite failed: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
    finally:
        srv.shutdown()
