import os
import sys
import json

from scratch.test_unified_comprehensive import run_unified_test

print("\n>>> TESTING PLAN GENERATION: 'Clean Download Names'")
res_plan = run_unified_test("Clean Download Names")
print(f"Result type: {res_plan.get('type')}")
print(f"Message preview: {res_plan.get('message', '')[:120]}...")
print(f"Actions count: {len(res_plan.get('actions', []))}")
for a in res_plan.get("actions", [])[:5]:
    print("  ->", a.get("type"), a.get("source"), "=>", a.get("destination"))

print("\n>>> TESTING MULTI-TURN AFFIRMATION: 'Yes'")
history = [
    {"role": "user", "content": "What suggestions do you have for my files?"},
    {"role": "assistant", "content": "I recommend we separate peer documents for Fawad and Sumbal into Documents/Peers/."}
]
res_yes = run_unified_test("Yes", history=history)
print(f"Result type: {res_yes.get('type')}")
print(f"Message preview: {res_yes.get('message', '')[:120]}...")
print(f"Actions count: {len(res_yes.get('actions', []))}")
for a in res_yes.get("actions", [])[:5]:
    print("  ->", a.get("type"), a.get("source"), "=>", a.get("destination"))
