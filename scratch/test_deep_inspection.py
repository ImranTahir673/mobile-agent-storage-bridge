import os
import json
from server import (
    BASE_STORAGE_DIR,
    extract_file_snippet,
    sanitize_pii,
    call_gemini_json,
    get_user_profile_path
)

# Test Deep Inspection for Neural Style Paper and Java Book
target_folder = "Download"
target_dir = os.path.join(BASE_STORAGE_DIR, target_folder)

# Find neural style paper or java book
candidates = []
for entry in os.scandir(target_dir):
    if entry.is_file() and not entry.name.startswith("."):
        candidates.append(entry)

print(f"Total candidates in {target_folder}: {len(candidates)}")

# Pick neural style paper or first available PDF
test_file = None
for c in candidates:
    if "neural" in c.name.lower() or "1508" in c.name.lower():
        test_file = c
        break
if not test_file and candidates:
    test_file = candidates[0]

print(f"Selected test file: {test_file.name}")

# Extract deep snippet (up to 5,000 chars)
deep_raw = extract_file_snippet(test_file.path, 5000)
deep_sanitized = sanitize_pii(deep_raw)
print(f"Deep snippet length: {len(deep_sanitized)} characters")
print(f"Preview of deep snippet:\n{deep_sanitized[:300]}...\n")

# Run Gemini query asking about specific content
user_query = f"Can you summarize the main findings and methodology of {test_file.name}?"

prompt = f"""You are Storage Copilot, an articulate on-device AI agent for Imran Tahir (BSAI-182).
Target File: {test_file.name}

DEEP DOCUMENT CONTENT (Full text extract with PII sanitized):
{deep_sanitized}

INSTRUCTIONS:
Answer the user's question directly, citing the specific concepts, methodology, and details from the text above.

Output JSON:
{{
  "type": "query_info",
  "message": "<Articulate markdown summary based on the text>",
  "actions": [],
  "quick_chips": ["Move to Documents/University/AI", "Find related papers", "Sort other files"]
}}
"""

resp = call_gemini_json(prompt, user_query)
print("Gemini Response:")
print(json.dumps(resp, indent=2))
