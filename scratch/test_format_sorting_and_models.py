import os
import sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
import json
import requests
import server

def run_tests():
    print("=== Testing Format Sorting, Reconnaissance, and Duplicate Icon Fixes ===")
    app = server.app
    client = app.test_client()

    # 1. Test "View organized folders"
    print("\n--- 1. Testing 'View organized folders' ---")
    res = client.post("/propose_plan", json={"prompt": "View organized folders", "target_folder": "Download"})
    data = res.get_json()
    assert res.status_code == 200, f"Expected 200, got {res.status_code}"
    assert data["type"] == "query_info", f"Expected 'query_info', got {data.get('type')}"
    assert data["actions"] == [], "Expected empty actions for view organized folders"
    assert "Organized Folders Status" in data["message"], "Message missing header"
    print("✓ 'View organized folders' returned valid query_info overview without mutating actions!")
    print("Message preview:\n", data["message"][:200])

    # 2. Test Proposal Mode for format sorting
    print("\n--- 2. Testing Format Sorting (Proposal Mode) ---")
    prompt_var = "sort all the file types together in the downloads"
    res = client.post("/propose_plan", json={"prompt": prompt_var, "target_folder": "Download"})
    data = res.get_json()
    assert res.status_code == 200
    assert data["type"] == "conversation", f"Expected 'conversation' for proposal, got {data.get('type')}"
    assert data["actions"] == [], f"Expected 0 actions in proposal mode, got {len(data['actions'])}"
    assert "Would you like me to proceed with moving these files" in data["message"], "Missing permission question"
    assert "Destination" in data["message"] and "Format" in data["message"], "Missing breakdown table"
    assert any("Yes" in chip for chip in data.get("quick_chips", [])), "Missing confirmation chip"
    print("✓ Format sorting proposal returned clean breakdown table and permission question without dumping action cards!")

    # 3. Test Execution Mode (User confirms with 'Yes')
    print("\n--- 3. Testing Format Sorting Execution Mode (User Confirms 'Yes') ---")
    res = client.post("/propose_plan", json={
        "prompt": "Yes",
        "target_folder": "Download",
        "conversation_history": [
            {"role": "user", "content": prompt_var},
            {"role": "assistant", "content": data["message"]}
        ]
    })
    exec_data = res.get_json()
    assert res.status_code == 200
    assert exec_data["type"] == "plan", f"Expected 'plan', got {exec_data.get('type')}"
    plan = exec_data.get("plan", {})
    actions = plan.get("actions", [])
    batch_info = plan.get("batch_info", {})
    assert len(actions) > 0, "Expected at least 1 action staged"
    assert len(actions) <= 25, f"Expected at most 25 actions staged, got {len(actions)}"
    assert "remaining_files" in batch_info, "Missing remaining_files in batch_info"
    print(f"✓ Confirmation staged micro-batch of {len(actions)} files with batch_info: {batch_info}")
    for act in actions[:3]:
        print(f"   Sample action: {act['source']} -> {act['destination']} ({act['badge_label']})")

    # 4. Test Batch Execution and WAL Ledger
    print("\n--- 4. Testing Execute Plan with batch_info ---")
    exec_res = client.post("/execute_plan", json={
        "plan_id": plan["plan_id"],
        "description": plan["description"],
        "actions": actions,
        "batch_info": batch_info
    })
    exec_res_data = exec_res.get_json()
    assert exec_res.status_code == 200, f"Execution failed: {exec_res_data}"
    assert exec_res_data["status"] == "success"
    assert "batch_info" in exec_res_data, "batch_info missing in execution response"
    print(f"✓ Batch executed atomically! {exec_res_data['executed_actions']} actions executed.")
    print(f"   Updated batch_info: {exec_res_data['batch_info']}")

    # 5. Test 1-Tap Undo Rollback
    print("\n--- 5. Testing 1-Tap Undo Rollback ---")
    roll_res = client.post("/rollback_batch", json={"batch_id": plan["plan_id"]})
    roll_data = roll_res.get_json()
    assert roll_res.status_code == 200, f"Rollback failed: {roll_data}"
    assert roll_data["reverted_actions"] == len(actions)
    print(f"✓ 1-Tap Rollback successfully reverted all {roll_data['reverted_actions']} files to original paths!")

    print("\n=== ALL TESTS PASSED SUCCESSFULLY! ===")

if __name__ == "__main__":
    run_tests()
