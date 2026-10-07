import os
import sys
import time
import uuid
import tempfile
import argparse
import requests
import threading
from werkzeug.serving import make_server

# ==============================================================================
# Configuration & CLI Argument Parsing
# ==============================================================================
# Load .env if present
env_path = os.path.join(os.path.dirname(__file__), ".env")
if os.path.exists(env_path):
    with open(env_path) as f:
        for line in f:
            if line.strip() and not line.startswith("#") and "=" in line:
                k, v = line.strip().split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

def parse_cli_args():
    parser = argparse.ArgumentParser(
        description="Mobile Agent Storage Bridge: Phase 1 Batch & Rollback Verifier"
    )
    parser.add_argument(
        "--url",
        type=str,
        default=None,
        help="Target bridge URL to test (e.g. http://192.168.100.65:8080 or https://...trycloudflare.com)"
    )
    parser.add_argument(
        "--local",
        action="store_true",
        help="Force execution against an isolated local mock bridge on 127.0.0.1:8888"
    )
    return parser.parse_args()

def run_local_mock_server(storage_dir):
    """Spins up a local test instance of server.py pointing to a temporary storage directory."""
    os.environ["STORAGE_BASE_DIR"] = storage_dir
    os.environ["LEDGER_DB_PATH"] = os.path.join(storage_dir, ".ledger.db")

    import server
    server.BASE_DIR = os.path.realpath(storage_dir)
    server.TRASH_DIR = os.path.join(server.BASE_DIR, ".agent_trash")
    server.LEDGER_DB_PATH = os.path.join(server.BASE_DIR, ".ledger.db")
    os.makedirs(server.BASE_DIR, exist_ok=True)
    os.makedirs(server.TRASH_DIR, exist_ok=True)
    server.init_db()

    srv = make_server("127.0.0.1", 8888, server.app)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, "http://127.0.0.1:8888"

def perform_health_check(url: str, timeout_sec: int = 10) -> dict:
    """Robust health check with 10s timeout, validating status == 'running'."""
    endpoint = f"{url.rstrip('/')}/"
    try:
        resp = requests.get(endpoint, timeout=timeout_sec)
    except requests.exceptions.RequestException as exc:
        raise ConnectionError(f"Network transport error reaching {endpoint}: {exc}") from exc

    if resp.status_code != 200:
        raise ConnectionError(
            f"Health check failed with HTTP status {resp.status_code}.\n"
            f"  Response Body: {resp.text[:500]}"
        )

    try:
        data = resp.json()
    except Exception as exc:
        raise ValueError(f"Health endpoint did not return valid JSON: {exc}\nBody: {resp.text[:300]}") from exc

    if data.get("status") != "running":
        raise ValueError(f"Unexpected health status: expected 'running', got '{data.get('status')}'. Full data: {data}")

    return data

# ==============================================================================
# Main Verification Flow
# ==============================================================================
def main():
    args = parse_cli_args()

    print("=" * 70)
    print(" Mobile Agent Storage Bridge: Phase 1 Batch & Rollback Verifier")
    print("=" * 70)

    local_srv = None
    temp_dir_obj = None
    storage_dir = None

    # 1. Determine Target URL and Enforce Mode
    if args.local:
        print("[*] CLI flag '--local' explicitly specified.")
        print("[*] Initializing isolated local mock bridge with sandbox storage...")
        temp_dir_obj = tempfile.TemporaryDirectory(prefix="bridge_test_")
        storage_dir = os.path.realpath(temp_dir_obj.name)
        local_srv, server_url = run_local_mock_server(storage_dir)
        time.sleep(0.5)
        print(f"[*] Local mock server running at: {server_url} (Sandbox: {storage_dir})")
    elif args.url:
        server_url = args.url.rstrip("/")
        print(f"[*] Remote target URL explicitly specified: {server_url}")
        print("[*] Verifying remote bridge health (timeout: 10s)...")
        try:
            health = perform_health_check(server_url, timeout_sec=10)
            print(f"  ✓ Remote bridge online: engine='{health.get('engine')}', base='{health.get('base_dir')}', status='{health.get('status')}'")
        except Exception as err:
            print(f"\n[FATAL ERROR] Failed to connect to remote target '{server_url}':")
            print(f"  {err}\n")
            print("Aborting without falling back to mock server. Please verify device IP/tunnel.")
            sys.exit(1)
    else:
        # Fall back to PHONE_URL from .env if defined
        env_url = os.getenv("PHONE_URL")
        if env_url:
            server_url = env_url.rstrip("/")
            print(f"[*] No --url or --local flag specified. Using PHONE_URL from environment: {server_url}")
            print("[*] Verifying remote bridge health (timeout: 10s)...")
            try:
                health = perform_health_check(server_url, timeout_sec=10)
                print(f"  ✓ Bridge online: engine='{health.get('engine')}', base='{health.get('base_dir')}', status='{health.get('status')}'")
            except Exception as err:
                print(f"\n[FATAL ERROR] Target '{server_url}' is unreachable:")
                print(f"  {err}\n")
                print("Hint: Pass '--url <URL>' to specify target, or pass '--local' to test against local sandbox.")
                sys.exit(1)
        else:
            print("[FATAL ERROR] Neither --url nor --local was provided, and no PHONE_URL configured in .env.")
            print("Usage: python test_batch.py --url <TARGET_URL> | --local")
            sys.exit(1)

    try:
        # 2. Remote Seeding / Fixture Setup
        print("\n[Step 1] Preparing 3 test fixtures for reorganization...")
        test_files = {}

        if storage_dir:
            # Local mock mode: direct file writes into sandbox
            sample_specs = {
                "Download/sample_paper_1.pdf": "Content of arXiv:2301.00001 Deep Learning Foundations.",
                "Download/sample_notes_2.txt": "Notes from engineering sync regarding system architecture.",
                "Download/sample_junk_3.tmp": "Ephemeral temporary download receipt."
            }
            for rel, content in sample_specs.items():
                p = os.path.join(storage_dir, rel)
                os.makedirs(os.path.dirname(p), exist_ok=True)
                with open(p, "w", encoding="utf-8") as f:
                    f.write(content)
            test_files = sample_specs
            print(f"  ✓ Created 3 synthetic test files in local sandbox.")
        else:
            # Remote mode: Try creating files via bridge POST /write_file API
            remote_sample_specs = {
                "Download/bridge_test_paper_1.pdf": "Synthetic research paper fixture for bridge batch testing.",
                "Download/bridge_test_notes_2.txt": "Synthetic system architecture notes fixture.",
                "Download/bridge_test_junk_3.tmp": "Synthetic temporary receipt for soft-delete testing."
            }
            can_write_remote = True
            for rel, content in remote_sample_specs.items():
                try:
                    w_res = requests.post(f"{server_url}/write_file", json={"path": rel, "content": content}, timeout=10)
                    if w_res.status_code != 200:
                        can_write_remote = False
                        break
                except Exception:
                    can_write_remote = False
                    break

            if can_write_remote:
                test_files = remote_sample_specs
                print("  ✓ Created 3 isolated test files via bridge POST /write_file.")
            else:
                # If /write_file is unavailable on remote, pick 3 existing safe files from Download/
                print("  [*] Bridge does not support /write_file yet; selecting 3 existing fixtures from 'Download/'...")
                list_res = requests.post(f"{server_url}/list_files", json={"path": "Download"}, timeout=10).json()
                items = [it for it in list_res.get("items", []) if not it.get("is_dir") and it.get("size_bytes", 0) > 0]
                if len(items) < 3:
                    print(f"[FATAL ERROR] 'Download' folder has only {len(items)} files; need at least 3 to run batch test.")
                    sys.exit(1)

                selected = items[:3]
                test_files = {
                    f"Download/{selected[0]['name']}": None,
                    f"Download/{selected[1]['name']}": None,
                    f"Download/{selected[2]['name']}": None,
                }
                print(f"  ✓ Selected safe remote fixtures:")
                for fn in test_files.keys():
                    print(f"      - {fn}")

        fixture_keys = list(test_files.keys())
        src_1, src_2, src_3 = fixture_keys[0], fixture_keys[1], fixture_keys[2]
        dest_1 = f"Documents/Organized_Batch/{os.path.basename(src_1)}"
        dest_2 = f"Documents/Organized_Batch/{os.path.basename(src_2)}"

        # 3. Dry-Run Simulation
        plan_id = str(uuid.uuid4())
        action_plan = {
            "plan_id": plan_id,
            "version": "1.0",
            "description": "3-file reorganization test plan (Phase 1 verification)",
            "dry_run": True,
            "collision_strategy": "RENAME_NUMERIC",
            "actions": [
                {
                    "action_id": "step-1",
                    "type": "make_dir",
                    "path": "Documents/Organized_Batch"
                },
                {
                    "action_id": "step-2",
                    "type": "move",
                    "source": src_1,
                    "destination": dest_1
                },
                {
                    "action_id": "step-3",
                    "type": "move",
                    "source": src_2,
                    "destination": dest_2
                },
                {
                    "action_id": "step-4",
                    "type": "trash",
                    "path": src_3
                }
            ]
        }

        print("\n[Step 2] Testing dry_run simulation against POST /execute_plan...")
        dry_res = requests.post(f"{server_url}/execute_plan", json=action_plan, timeout=10).json()
        assert dry_res.get("dry_run") is True, f"Expected dry_run True, got: {dry_res}"
        assert dry_res.get("actions_validated") == 4, f"Expected 4 validated actions, got: {dry_res}"
        print(f"  ✓ Dry-run validated {dry_res['actions_validated']} actions with zero mutations.")

        # 4. Live Action Plan Execution
        print(f"\n[Step 3] Executing Action Plan (Batch ID: {plan_id})...")
        action_plan["dry_run"] = False
        exec_res = requests.post(f"{server_url}/execute_plan", json=action_plan, timeout=15).json()
        assert exec_res.get("status") == "success", f"Execution failed: {exec_res}"
        assert exec_res.get("executed_actions") == 4, f"Expected 4 executed actions, got: {exec_res}"
        print(f"  ✓ Batch executed and pre-logged to SQLite successfully!")

        # 5. Verify Mutated State
        print("\n[Step 4] Verifying mutated storage state on bridge...")
        list_docs = requests.post(f"{server_url}/list_files", json={"path": "Documents/Organized_Batch"}, timeout=10).json()
        doc_names = [item["name"] for item in list_docs.get("items", [])]
        print(f"  * Items in 'Documents/Organized_Batch': {doc_names}")
        assert os.path.basename(dest_1) in doc_names, f"{dest_1} not found in destination!"
        assert os.path.basename(dest_2) in doc_names, f"{dest_2} not found in destination!"

        list_down = requests.post(f"{server_url}/list_files", json={"path": "Download"}, timeout=10).json()
        down_names = [item["name"] for item in list_down.get("items", [])]
        assert os.path.basename(src_1) not in down_names, f"{src_1} should have been moved!"
        assert os.path.basename(src_2) not in down_names, f"{src_2} should have been moved!"
        assert os.path.basename(src_3) not in down_names, f"{src_3} should have been trashed!"
        print("  ✓ State verification PASSED: files moved and soft-deleted correctly.")

        # 5.5 Phase 2.6: Test Deterministic Ledger Lookup (/lookup_history)
        print("\n[Step 4.5] Verifying Phase 2.6 Deterministic Ledger Lookup (/lookup_history)...")
        # Lookup moved file
        lookup_res1 = requests.post(f"{server_url}/lookup_history", json={"query": "sample_paper_1.pdf"}, timeout=10).json()
        assert lookup_res1.get("status") == "success", f"Lookup failed: {lookup_res1}"
        assert lookup_res1.get("matches_count", 0) >= 1, f"Expected match for sample_paper_1.pdf: {lookup_res1}"
        match1 = lookup_res1["matches"][0]
        assert match1["plan_id"] == plan_id, f"Plan ID mismatch: {match1}"
        assert match1["is_trashed"] is False, f"Expected is_trashed=False for moved file: {match1}"
        print(f"  ✓ Moved file lookup PASSED: {match1['source_path']} -> {match1['destination_path']} [Plan: {match1['plan_id']}]")

        # Lookup soft-deleted / trashed file
        lookup_res2 = requests.post(f"{server_url}/lookup_history", json={"query": "sample_junk_3.tmp"}, timeout=10).json()
        assert lookup_res2.get("status") == "success", f"Lookup failed: {lookup_res2}"
        assert lookup_res2.get("matches_count", 0) >= 1, f"Expected match for sample_junk_3.tmp: {lookup_res2}"
        match2 = lookup_res2["matches"][0]
        assert match2["is_trashed"] is True, f"Expected is_trashed=True for trashed file: {match2}"
        print(f"  ✓ Trashed file lookup PASSED: is_trashed=True correctly identified.")

        # 5.6 Phase 2.7: Test Device-Local User Profile Endpoint (/user_profile)
        print("\n[Step 4.6] Verifying Phase 2.7 User Profile Endpoint (/user_profile)...")
        profile_res = requests.get(f"{server_url}/user_profile", timeout=10).json()
        assert profile_res.get("status") in ("success", "default"), f"Failed getting user profile: {profile_res}"
        profile_data = profile_res.get("profile", {})
        assert "user_identity" in profile_data, f"Missing user_identity in profile: {profile_data}"
        assert "routing_rules" in profile_data, f"Missing routing_rules in profile: {profile_data}"
        print(f"  ✓ User profile verified: owner='{profile_data['user_identity'].get('primary_name')}', rules={list(profile_data['routing_rules'].keys())}")

        # 6. Trigger Rollback
        print(f"\n[Step 5] Triggering POST /rollback_batch for Batch {plan_id}...")
        rollback_res = requests.post(f"{server_url}/rollback_batch", json={"batch_id": plan_id}, timeout=15).json()
        assert rollback_res.get("status") == "rolled_back", f"Rollback failed: {rollback_res}"
        assert rollback_res.get("reverted_actions") == 4, f"Expected 4 reverted actions, got: {rollback_res}"
        print(f"  ✓ Rollback completed successfully! Reverted operations: {rollback_res['reverted_actions']}")

        # 7. Verify Restored State
        print("\n[Step 6] Verifying restored storage state after rollback...")
        list_down_after = requests.post(f"{server_url}/list_files", json={"path": "Download"}, timeout=10).json()
        restored_names = [item["name"] for item in list_down_after.get("items", [])]
        assert os.path.basename(src_1) in restored_names, f"{src_1} was not restored!"
        assert os.path.basename(src_2) in restored_names, f"{src_2} was not restored!"
        assert os.path.basename(src_3) in restored_names, f"{src_3} was not untrashed!"

        check_docs = requests.post(f"{server_url}/list_files", json={"path": "Documents/Organized_Batch"}, timeout=10)
        assert check_docs.status_code == 404, f"Directory Documents/Organized_Batch should have been removed on rollback!"
        print("  ✓ Destination directory cleanly removed.")
        print("  ✓ All 3 files restored to their exact original locations.")

        # Verify lookup reflects rollback status
        lookup_post_rollback = requests.post(f"{server_url}/lookup_history", json={"query": "sample_paper_1.pdf"}, timeout=10).json()
        assert lookup_post_rollback["matches"][0]["status"] == "REVERTED", f"Expected REVERTED status after rollback: {lookup_post_rollback}"
        print("  ✓ Historical lookup reflects REVERTED status accurately post-rollback.")

        # 8. Check Integrity if synthetic files were used
        if storage_dir:
            print("\n[Step 7] Checking file content integrity...")
            for rel, expected in test_files.items():
                p = os.path.join(storage_dir, rel)
                with open(p, "r", encoding="utf-8") as f:
                    actual = f.read()
                assert actual == expected, f"File {rel} content mismatch after rollback!"
            print("  ✓ 100% byte-for-byte content integrity verified!")

        print("\n" + "=" * 70)
        print(" ALL VERIFICATION CHECKS PASSED: Phase 1 Batch & Rollback OK!")
        print("=" * 70)

    finally:
        if local_srv:
            local_srv.shutdown()
        if temp_dir_obj:
            temp_dir_obj.cleanup()

if __name__ == "__main__":
    main()
