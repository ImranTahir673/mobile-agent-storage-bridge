import os
import sys
sys.stdout.reconfigure(line_buffering=True)
import json
import tempfile
import threading
import requests
from werkzeug.serving import make_server

# Set up test environment
test_storage_dir = tempfile.mkdtemp(prefix="storage_v06_test_")
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

def test_suite():
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

    print("\n" + "="*60)
    print(" Storage Copilot v0.6-live Comprehensive Test Suite")
    print("="*60)

    # 1. Health and Version Check
    print("\n--- Test 1: Health Endpoint & Version Verification ---")
    h_resp = requests.get(f"{BASE_URL}/health").json()
    assert_test(h_resp.get("status") == "running", "Health status is 'running'")
    assert_test(h_resp.get("version") in ("v0.6-live", "v0.6.2-live"), f"Version starts with 'v0.6' (got '{h_resp.get('version')}')")
    assert_test("v0.6" in h_resp.get("engine", ""), f"Engine reports v0.6: '{h_resp.get('engine')}'")

    # 2. Seed Fixtures
    print("\n--- Test 2: Seed Demo Fixtures ---")
    s_resp = requests.post(f"{BASE_URL}/seed_fixtures").json()
    assert_test(s_resp.get("status") == "success", "seed_fixtures succeeds")
    assert_test("DOC-20250413-WA0017..pdf" in s_resp.get("files", []), "Includes WhatsApp KR lecture fixture")
    assert_test("DOC-20250413-WA0018..pdf" in s_resp.get("files", []), "Includes WhatsApp AI assignment fixture")
    assert_test("DOC-20250413-WA0019..pdf" in s_resp.get("files", []), "Includes WhatsApp Utility bill fixture")

    # 3. Three-Tier Intent Classifier - TIER 1: CONVERSATION
    print("\n--- Test 3: Tier 1 CONVERSATION Intent (No Canned Template) ---")
    # 3a. Greeting
    c_resp1 = requests.post(f"{BASE_URL}/propose_plan", json={"prompt": "Hii", "target_folder": "Download"}).json()
    assert_test(c_resp1.get("type") == "conversation", f"Greeting returns type 'conversation' (got {c_resp1.get('type')})")
    assert_test(len(c_resp1.get("actions", [])) == 0, "No file mutation actions staged for greeting")
    assert_test(len(c_resp1.get("message", "")) > 10, "Has non-empty message")
    assert_test(c_resp1.get("version") in ("v0.6-live", "v0.6.2-live"), f"Returns version starts with v0.6 (got '{c_resp1.get('version')}')")

    # 3b. "What else"
    c_resp2 = requests.post(f"{BASE_URL}/propose_plan", json={"prompt": "What else", "target_folder": "Download"}).json()
    assert_test(c_resp2.get("type") == "conversation", "'What else' returns type 'conversation'")
    assert_test("👋 Hello Imran Tahir! I am Storage Copilot, running locally on your Android device via Termux" not in c_resp2.get("message", ""),
                "'What else' does NOT return the old static canned greeting template!")
    assert_test(len(c_resp2.get("actions", [])) == 0, "No file mutation actions staged for 'What else'")
    print(f"      'What else' message preview: {c_resp2.get('message')[:120]}...")

    # 3c. Capabilities inquiry
    c_resp3 = requests.post(f"{BASE_URL}/propose_plan", json={"prompt": "what can you do?", "target_folder": "Download"}).json()
    assert_test(c_resp3.get("type") == "conversation", "'what can you do?' returns type 'conversation'")
    assert_test(len(c_resp3.get("actions", [])) == 0, "No actions staged for capabilities inquiry")

    # 4. Three-Tier Intent Classifier - TIER 2: QUERY_INFO
    print("\n--- Test 4: Tier 2 QUERY_INFO Intent (Read-Only Inspection) ---")
    # 4a. "List university lectures"
    q_resp1 = requests.post(f"{BASE_URL}/propose_plan", json={"prompt": "List university lectures", "target_folder": "Download"}).json()
    assert_test(q_resp1.get("type") == "query_info", f"'List university lectures' returns type 'query_info' (got {q_resp1.get('type')})")
    assert_test(len(q_resp1.get("actions", [])) == 0, "ZERO file mutation actions staged for read-only query")
    msg1 = q_resp1.get("message", "")
    assert_test("Knowledge Representation" in msg1 or "neural" in msg1.lower() or "lecture" in msg1.lower(),
                f"Informational summary cites academic content (preview: {msg1[:150]}...)")

    # 4b. "Find receipts"
    q_resp2 = requests.post(f"{BASE_URL}/propose_plan", json={"prompt": "Find receipts", "target_folder": "Download"}).json()
    assert_test(q_resp2.get("type") == "query_info", f"'Find receipts' returns type 'query_info' (got {q_resp2.get('type')})")
    assert_test(len(q_resp2.get("actions", [])) == 0, "ZERO file mutation actions staged for 'Find receipts'")
    msg2 = q_resp2.get("message", "")
    assert_test("receipt" in msg2.lower() or "fee" in msg2.lower() or "bill" in msg2.lower() or "voucher" in msg2.lower(),
                f"Summary cites receipts/financial documents (preview: {msg2[:150]}...)")

    # 4c. "What is inside Download?"
    q_resp3 = requests.post(f"{BASE_URL}/propose_plan", json={"prompt": "What is inside Download?", "target_folder": "Download"}).json()
    assert_test(q_resp3.get("type") == "query_info", "'What is inside Download?' returns type 'query_info'")
    assert_test(len(q_resp3.get("actions", [])) == 0, "ZERO file mutation actions staged for inspection")

    # 5. Three-Tier Intent Classifier - TIER 3: FILE_OPERATION & Semantic Disambiguation
    print("\n--- Test 5: Tier 3 FILE_OPERATION & Semantic Disambiguation ---")
    # 5a. Clean Download Names
    plan_resp = requests.post(f"{BASE_URL}/propose_plan", json={"prompt": "Clean Download Names", "target_folder": "Download"}).json()
    assert_test(plan_resp.get("type") == "plan", f"'Clean Download Names' returns type 'plan' (got {plan_resp.get('type')})")
    actions = plan_resp.get("actions", [])
    assert_test(len(actions) > 0, f"Synthesized action plan with {len(actions)} actions")

    # Check destinations
    destinations = [a.get("destination") for a in actions if a.get("type") == "move"]
    print(f"      Proposed Move Destinations ({len(destinations)}):")
    for d in destinations:
        print(f"        -> {d}")

    # Check academic taxonomy priority:
    # Any move for DOC-20250413-WA0017 (Knowledge Representation) must be in Documents/University/<Subject>/, NOT Documents/Finances/!
    kr_moves = [a for a in actions if "wa0017" in (a.get("source") or "").lower()]
    if kr_moves:
        kr_dest = kr_moves[0].get("destination", "")
        assert_test("university" in kr_dest.lower() and "finances" not in kr_dest.lower(),
                    f"Academic document WA0017 prioritized under Documents/University/ (got '{kr_dest}')")
        assert_test("Document_2025-04-13.pdf" not in kr_dest,
                    f"Academic document WA0017 does NOT get generic 'Document_2025-04-13.pdf' title (got '{kr_dest}')")

    # Check bill document (WA0019) routes to Finances
    bill_moves = [a for a in actions if "wa0019" in (a.get("source") or "").lower()]
    if bill_moves:
        bill_dest = bill_moves[0].get("destination", "")
        assert_test("finances" in bill_dest.lower(), f"Utility bill document WA0019 routes to Documents/Finances (got '{bill_dest}')")

    # Check NO duplicate destination paths in a single batch
    lower_dests = [d.lower() for d in destinations if d]
    has_duplicates = len(lower_dests) != len(set(lower_dests))
    assert_test(not has_duplicates, "CRITICAL: Strictly ZERO duplicate destination paths in the batch!")

    # 6. Plan Execution & 1-Tap Rollback
    print("\n--- Test 6: Plan Execution and 1-Tap Rollback ---")
    exec_resp = requests.post(f"{BASE_URL}/execute_plan", json=plan_resp["plan"]).json()
    assert_test(exec_resp.get("status") == "success", "Batch execution succeeds")
    batch_id = exec_resp.get("batch_id")
    assert_test(bool(batch_id), f"Batch ID returned: {batch_id}")

    # Verify rollback
    roll_resp = requests.post(f"{BASE_URL}/rollback_batch", json={"batch_id": batch_id}).json()
    assert_test(roll_resp.get("status") in ("rolled_back", "success"), f"Batch rollback succeeds (got '{roll_resp.get('status')}')")
    assert_test(roll_resp.get("reverted_actions") == exec_resp.get("executed_actions"),
                f"All {roll_resp.get('reverted_actions')} actions reverted via SQLite WAL ledger")

    print("\n" + "="*60)
    print(f" ALL {total} TESTS PASSED! ({passed}/{total})")
    print("="*60 + "\n")

if __name__ == "__main__":
    try:
        test_suite()
        sys.exit(0)
    except Exception as e:
        print(f"\n[ERROR] Test suite failed: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
    finally:
        srv.shutdown()
