import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import json
from server import app

client = app.test_client()

prompts_test = [
    # (prompt, expected_type, min_actions, max_actions)
    ("What can we do to the files on my phone to organize in the vest way possible", "conversation", 0, 0),
    ("Sort all the other files like docs and pdfs in the download to their best suited places", "plan", 1, None),
    ("Yes organize all my uni files", "plan", 1, None),
]

print("=" * 80)
print("TEST 1: LOCAL OFFLINE INTELLIGENCE MODE (GEMINI_API_KEY = '')")
print("=" * 80)
os.environ["GEMINI_API_KEY"] = ""

all_passed_offline = True
for prompt, exp_type, min_acts, max_acts in prompts_test:
    res = client.post('/propose_plan', json={'prompt': prompt, 'target_folder': 'Download'})
    data = res.get_json()
    t = data.get('type')
    acts = len(data.get('actions', []))
    msg = data.get('message', '').replace('\n', ' ')[:90]

    type_ok = (t == exp_type)
    acts_ok = (acts >= min_acts) if max_acts is None else (min_acts <= acts <= max_acts)
    passed = type_ok and acts_ok
    if not passed:
        all_passed_offline = False

    status = "PASS" if passed else "FAIL"
    print(f"[{status}] \"{prompt}\"")
    print(f"       => Result: Type={t} (expected {exp_type}), Actions={acts}")
    print(f"       => Msg: {msg}...")
    print("-" * 80)

print(f"OFFLINE MODE: {'ALL PASSED' if all_passed_offline else 'SOME FAILED'}")
print()

# Test with Gemini live if key was originally loaded
from server import load_environment
load_environment()
online_key = os.getenv("GEMINI_API_KEY")

if online_key:
    print("=" * 80)
    print("TEST 2: ONLINE GEMINI LLM MODE")
    print("=" * 80)
    all_passed_online = True
    for prompt, exp_type, min_acts, max_acts in prompts_test:
        res = client.post('/propose_plan', json={'prompt': prompt, 'target_folder': 'Download'})
        data = res.get_json()
        t = data.get('type')
        acts = len(data.get('actions', []))
        msg = data.get('message', '').replace('\n', ' ')[:90]

        type_ok = (t == exp_type)
        acts_ok = (acts >= min_acts) if max_acts is None else (min_acts <= acts <= max_acts)
        passed = type_ok and acts_ok
        if not passed:
            all_passed_online = False

        status = "PASS" if passed else "FAIL"
        print(f"[{status}] \"{prompt}\"")
        print(f"       => Result: Type={t} (expected {exp_type}), Actions={acts}")
        print(f"       => Msg: {msg}...")
        print("-" * 80)
    print(f"ONLINE MODE: {'ALL PASSED' if all_passed_online else 'SOME FAILED'}")
    sys.exit(0 if (all_passed_offline and all_passed_online) else 1)
else:
    sys.exit(0 if all_passed_offline else 1)
