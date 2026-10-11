import os
import sys
sys.stdout.reconfigure(line_buffering=True)
import json
import tempfile
import threading
import requests
from werkzeug.serving import make_server

# Set up test environment
test_storage_dir = tempfile.mkdtemp(prefix="storage_accuracy_test_")
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

def run_tests():
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
    print(" Storage Copilot: Document Accuracy, PII Privacy & Search Test Suite")
    print("="*65)

    # 1. Fortified PII Sanitizer Tests
    print("\n--- Test 1: PII Sanitizer Fortification ---")
    # 1a. Passport numbers
    s1 = server.sanitize_pii("Applicant Passport No: PK9823412 issued in Islamabad.")
    assert_test("[REDACTED_PASSPORT]" in s1 and "PK9823412" not in s1, f"Redacts passport number: '{s1}'")

    # 1b. 13-digit unhyphenated CNIC
    s2 = server.sanitize_pii("Citizen CNIC is 3520112345671 for account verification.")
    assert_test("[REDACTED_CNIC]" in s2 and "3520112345671" not in s2, f"Redacts 13-digit raw CNIC: '{s2}'")

    # 1c. IBAN
    s3 = server.sanitize_pii("Beneficiary IBAN: PK36SCBL0000001123456701 at Standard Chartered.")
    assert_test("[REDACTED_IBAN]" in s3 and "PK36SCBL" not in s3, f"Redacts IBAN: '{s3}'")

    # 1d. Formal Names in administrative fields
    s4 = server.sanitize_pii("University Entry Pass: Student Name: Sumbal, Department: AI.")
    assert_test("[REDACTED_NAME]" in s4 and "Sumbal" not in s4, f"Redacts formal student name: '{s4}'")

    s5 = server.sanitize_pii("Student Fee Slip: Name: Fawad, Roll No: BSAI-190.")
    assert_test("[REDACTED_NAME]" in s5 and "Fawad" not in s5, f"Redacts formal voucher name: '{s5}'")

    s6 = server.sanitize_pii("Payment receipt for Ahmed, Department Library Fee.")
    assert_test("[REDACTED_NAME]" in s6 and "Ahmed" not in s6, f"Redacts 'receipt for' name: '{s6}'")

    # 2. Document Classification Caution & Accuracy Tests
    print("\n--- Test 2: Document Classification Caution & Accuracy ---")
    c_pass = server.classify_document("Sumbal pass.pdf", "University Entry Pass: Student Name: [REDACTED_NAME], Department: AI.")
    assert_test(c_pass["category"] == "IDENTITY_PASS", f"Sumbal pass classified as IDENTITY_PASS (got '{c_pass['category']}')")
    assert_test(c_pass["is_academic_study"] is False, "Sumbal pass has is_academic_study == False")

    c_kr = server.classify_document("DOC-20250413-WA0017..pdf", "Knowledge Representation and Reasoning Lecture 4: First-Order Predicate Logic, Ontologies. BSAI Semester 6.")
    assert_test(c_kr["category"] == "ACADEMIC_STUDY", f"WA0017 KR lecture classified as ACADEMIC_STUDY (got '{c_kr['category']}')")
    assert_test(c_kr["is_academic_study"] is True, "WA0017 has is_academic_study == True")
    assert_test("University/AI" in c_kr["target_dir"], f"WA0017 targets University/AI (got '{c_kr['target_dir']}')")

    c_bill = server.classify_document("DOC-20250413-WA0019..pdf", "Electricity Utility Bill Invoice for March 2025. Consumer ID: 948201. Amount Due: PKR 8,500.")
    assert_test(c_bill["category"] == "FINANCIAL_BILL", f"WA0019 bill classified as FINANCIAL_BILL (got '{c_bill['category']}')")
    assert_test(c_bill["is_academic_study"] is False, "WA0019 bill has is_academic_study == False")
    assert_test("Finances" in c_bill["target_dir"], f"WA0019 targets Finances (got '{c_bill['target_dir']}')")

    c_vouch = server.classify_document("BSAI-182_fee_voucher_fall.pdf", "Student Fee Voucher: Name: [REDACTED_NAME], Roll No: BSAI-182, Amount: PKR 45,000, Status: Paid.")
    assert_test(c_vouch["category"] == "ACADEMIC_VOUCHER", f"BSAI voucher classified as ACADEMIC_VOUCHER (got '{c_vouch['category']}')")
    assert_test(c_vouch["is_academic_study"] is False, "Fee voucher has is_academic_study == False")

    # 3. Seed Fixtures into Test Storage
    print("\n--- Test 3: Seed Demo Fixtures ---")
    s_resp = requests.post(f"{BASE_URL}/seed_fixtures").json()
    assert_test(s_resp.get("status") == "success", "Seed fixtures succeeded")

    # 4. Critical Bug Fix: "Organize all my University files in 1 place" MUST NOT touch Sumbal pass
    print("\n--- Test 4: Critical Fix: Organizing University Files Excludes Identity Pass ---")
    org_resp = requests.post(f"{BASE_URL}/propose_plan", json={
        "prompt": "Organize all my University files in 1 place",
        "target_folder": "Download"
    }).json()

    assert_test(org_resp.get("type") == "plan", f"Returns type 'plan' (got '{org_resp.get('type')}')")
    plan_acts = org_resp.get("actions", [])
    print(f"      Proposed Actions count: {len(plan_acts)}")
    for a in plan_acts:
        print(f"        {a.get('badge', a.get('type'))}: {a.get('source', a.get('path'))} -> {a.get('destination', '')}")

    # Verify Sumbal pass is NOT moved into University or Organized folder
    sumbal_moves = [a for a in plan_acts if "sumbal" in (a.get("source") or "").lower()]
    assert_test(len(sumbal_moves) == 0, "CRITICAL: 'Sumbal pass.pdf' is NOT organized into University study files!")

    # Verify academic lectures are included under Documents/University/AI/
    academic_moves = [a for a in plan_acts if "wa0017" in (a.get("source") or "").lower() or "wa0018" in (a.get("source") or "").lower() or "1508" in (a.get("source") or "").lower()]
    assert_test(len(academic_moves) >= 2, f"At least 2 academic study documents organized (found {len(academic_moves)})")
    for am in academic_moves:
        assert_test("university" in am.get("destination", "").lower(), f"Academic move routed to University folder: {am.get('destination')}")
        assert_test("organized/sumbal" not in am.get("destination", "").lower(), "No cross-pollution with peer files")

    # 5. Hybrid Search: Find Specific Documents by Name & Context
    print("\n--- Test 5: Hybrid Search - Locate Specific Document by Name ---")
    # 5a. "Find me Sumbal pass"
    f1 = requests.post(f"{BASE_URL}/propose_plan", json={"prompt": "Find me Sumbal pass", "target_folder": "Download"}).json()
    assert_test(f1.get("type") == "query_info", f"'Find me Sumbal pass' routes to QUERY_INFO (got '{f1.get('type')}')")
    assert_test(len(f1.get("actions", [])) == 0, "Zero mutations staged for lookup")
    msg_f1 = f1.get("message", "")
    assert_test("sumbal pass" in msg_f1.lower(), f"Lookup surfaces 'Sumbal pass.pdf' in message: {msg_f1[:140]}...")
    assert_test("identity" in msg_f1.lower() or "pass" in msg_f1.lower(), f"Identifies document as Identity/Entry Pass: {msg_f1[:140]}...")

    # 5b. "Where is the electricity bill"
    f2 = requests.post(f"{BASE_URL}/propose_plan", json={"prompt": "Where is the electricity bill", "target_folder": "Download"}).json()
    assert_test(f2.get("type") == "query_info", f"'Where is the electricity bill' routes to QUERY_INFO")
    msg_f2 = f2.get("message", "")
    assert_test("wa0019" in msg_f2.lower() or "bill" in msg_f2.lower() or "electricity" in msg_f2.lower(),
                f"Lookup identifies WA0019 electricity bill: {msg_f2[:140]}...")

    # 5c. "Find 1508 neural style paper"
    f3 = requests.post(f"{BASE_URL}/propose_plan", json={"prompt": "Find 1508 neural style paper", "target_folder": "Download"}).json()
    assert_test(f3.get("type") == "query_info", f"'Find 1508 neural style paper' routes to QUERY_INFO")
    msg_f3 = f3.get("message", "")
    assert_test("1508" in msg_f3 or "neural" in msg_f3.lower(), f"Lookup identifies neural style paper: {msg_f3[:140]}...")

    # 6. Execute Plan & Rollback
    print("\n--- Test 6: Safe Plan Execution & Rollback ---")
    exec_resp = requests.post(f"{BASE_URL}/execute_plan", json=org_resp["plan"]).json()
    assert_test(exec_resp.get("status") == "success", "Batch execution succeeds")
    batch_id = exec_resp.get("batch_id")

    roll_resp = requests.post(f"{BASE_URL}/rollback_batch", json={"batch_id": batch_id}).json()
    assert_test(roll_resp.get("status") in ("rolled_back", "success"), "Rollback succeeds")
    assert_test(roll_resp.get("reverted_actions") == exec_resp.get("executed_actions"), "All actions rolled back atomically")

    print("\n" + "="*65)
    print(f" ALL {total} TESTS PASSED! ({passed}/{total})")
    print("="*65 + "\n")

if __name__ == "__main__":
    try:
        run_tests()
        sys.exit(0)
    except Exception as e:
        print(f"\n[ERROR] Test suite failed: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
    finally:
        srv.shutdown()
