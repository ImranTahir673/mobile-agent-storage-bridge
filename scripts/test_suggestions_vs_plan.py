import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import json

from server import app

client = app.test_client()

test_cases = [
    ("Some suggestions for my fikes to organize", "conversation", 0),
    ("What do you recommend I do with my files?", "conversation", 0),
    ("How should I organize my documents?", "conversation", 0),
    ("Give me some ideas for organizing files", "conversation", 0),
    ("Organize all my university files in 1 place", "plan", None),
    ("Sort student vouchers", "plan", None),
    ("Find me Sumbal pass", "query_info", 0),
    ("Where is my degree pass or entry pass", "query_info", 0),
    ("Send an email to my professor", "conversation", 0)
]

print("=" * 80)
print("TESTING PROMPT UNDERSTANDING: SUGGESTIONS vs PLANS vs QUERIES")
print("=" * 80)

all_passed = True
for prompt, expected_type, expected_actions in test_cases:
    res = client.post('/propose_plan', json={'prompt': prompt, 'target_folder': 'Download'})
    data = res.get_json()
    resp_type = data.get('type')
    actions = len(data.get('actions', []))
    msg = data.get('message', '').replace('\n', ' ')[:100]

    pass_type = (resp_type == expected_type)
    pass_act = (expected_actions is None and actions > 0) or (expected_actions is not None and actions == expected_actions)
    status = "PASS" if (pass_type and pass_act) else "FAIL"
    if status == "FAIL":
        all_passed = False

    print(f"[{status}] Prompt: \"{prompt}\"")
    print(f"       => Result Type: {resp_type} (expected: {expected_type}) | Actions: {actions}")
    print(f"       => Reply: {msg}...")
    print("-" * 80)

print(f"OVERALL RESULT: {'ALL PASSED' if all_passed else 'SOME FAILED'}")
sys.exit(0 if all_passed else 1)
