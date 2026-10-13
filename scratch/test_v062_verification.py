import json
import os
import sys

# Add root directory to sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import app, is_format_sorting_query, APP_VERSION, SERVER_BOOT_ID

def run_tests():
    client = app.test_client()

    print("\n--- TEST 1: Health & Build ID Check ---")
    resp = client.get("/health")
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"
    data = resp.get_json()
    assert data["version"] == "v0.6.2-live", f"Expected v0.6.2-live, got {data['version']}"
    assert "build_id" in data, "Expected build_id in health response"
    print(f"✓ Health OK: Version={data['version']}, BuildID={data['build_id']}")

    print("\n--- TEST 2: Query Intent Matching ---")
    user_prompt = "Lets arrange all the same format files in 1 place . From the downloads. And put them inside the folders inside Dowlands."
    assert is_format_sorting_query(user_prompt) is True, "Expected user_prompt to match format sorting intent"
    print("✓ Intent Matching OK: Prompt accurately classified as format sorting")

    print("\n--- TEST 3: User Prompt -> Format Sorting Proposal Plan ---")
    resp = client.post("/propose_plan", json={
        "prompt": user_prompt,
        "target_folder": "Download",
        "conversation_history": []
    })
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"
    p_data = resp.get_json()
    assert p_data["model_source"] == "deterministic-engine", f"Expected deterministic-engine, got {p_data.get('model_source')}"
    assert p_data["is_llm"] is False, "Expected is_llm to be False"
    assert p_data.get("format_sorting_active") is True or "breakdown" in p_data.get("message", "").lower() or "format" in p_data.get("message", "").lower(), "Expected format sorting active"
    print(f"✓ Format Sorting Proposal OK: Model Source={p_data['model_source']}, is_llm={p_data['is_llm']}")
    print(f"  Snippet: {p_data['message'][:180]}...")

    print("\n--- TEST 4: Confirmation -> Batch Staging Execution ---")
    resp = client.post("/propose_plan", json={
        "prompt": "Yes, proceed with moving these files",
        "target_folder": "Download",
        "conversation_history": [
            {"role": "user", "content": user_prompt},
            {"role": "assistant", "content": p_data["message"]}
        ]
    })
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"
    b_data = resp.get_json()
    assert b_data["type"] == "plan", f"Expected type=plan, got {b_data.get('type')}"
    assert len(b_data["actions"]) > 0, "Expected staged actions for batch"
    assert b_data["model_source"] == "deterministic-engine", f"Expected deterministic-engine, got {b_data.get('model_source')}"
    assert "batch_info" in b_data, "Expected batch_info in response"
    print(f"✓ Batch Staging OK: Staged {len(b_data['actions'])} actions. Target: {b_data['actions'][0]['destination']}")

    print("\n--- TEST 5: API Status Quota Reporting ---")
    resp = client.get("/api_status")
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"
    api_data = resp.get_json()
    assert "quota_limited" in api_data, "Expected quota_limited in api_status"
    print(f"✓ API Status OK: Configured={api_data['gemini_configured']}, QuotaLimited={api_data['quota_limited']}")

    print("\n🎉 ALL TESTS PASSED SUCCESSFULLY! 100% VERIFIED!\n")

if __name__ == "__main__":
    run_tests()
