import os
import sys
import json
from datetime import datetime

# Test the unified prompt generation and execution
from server import (
    BASE_STORAGE_DIR,
    get_user_profile_path,
    extract_file_snippet,
    sanitize_pii,
    call_gemini_json,
    find_duplicate_clusters,
    ensure_unique_plan_destinations,
    format_plan_badges
)

def run_unified_test(prompt, history=None):
    if history is None:
        history = []
    target_folder = "Download"
    
    # 1. Reconnaissance
    target_dir = os.path.join(BASE_STORAGE_DIR, target_folder)
    os.makedirs(target_dir, exist_ok=True)
    
    direct_files = []
    subfolders = []
    for entry in os.scandir(target_dir):
        if entry.is_dir() and not entry.name.startswith("."):
            try:
                cnt = len([f for f in os.scandir(entry.path) if not f.name.startswith(".")])
                subfolders.append(f"{entry.name} ({cnt} items)")
            except Exception:
                subfolders.append(entry.name)
        elif entry.is_file() and not entry.name.startswith("."):
            size_kb = round(entry.stat().st_size / 1024, 1)
            raw_text = extract_file_snippet(entry.path, 300)
            snip = sanitize_pii(raw_text)
            direct_files.append({
                "filename": entry.name,
                "path": f"{target_folder}/{entry.name}",
                "size_kb": size_kb,
                "snippet": snip if snip else "None (binary/image)"
            })
            
    p_path = get_user_profile_path()
    profile = {}
    if os.path.exists(p_path):
        with open(p_path, "r", encoding="utf-8") as f:
            profile = json.load(f)
            
    user_id = profile.get("user_identity", {})
    owner_name = user_id.get("primary_name", "Imran Tahir")
    identifiers = user_id.get("identifiers", ["BSAI-182", "182"])
    peers = profile.get("known_peers", [])
    peer_names = [p.get("name") for p in peers if p.get("name")] or ["Fawad", "Sumbal", "Ahmed", "Yousaf"]
    peer_matrix_lines = "\n".join(
        f"- Peer '{p.get('name')}': designated folder '{p.get('designated_folder', f'Documents/Peers/{p.get('name')}')}'"
        for p in peers
    )
    
    # Dialogue context
    context_lines = []
    for turn in history[-4:]:
        role = (turn.get("role") or "user").upper()
        content = (turn.get("content") or turn.get("text") or turn.get("message") or "").strip()
        if content:
            context_lines.append(f"{role}: {content[:300].replace(chr(10), ' ')}")
    dialogue_block = "\n".join(context_lines) if context_lines else "None (New conversation)"
    
    system_prompt = f"""You are Storage Copilot, an elite on-device AI agent running locally via Termux on Android for {owner_name} (Roll/ID: {identifiers}, Major: Artificial Intelligence).
Storage Root: /storage/emulated/0 (Downloads, Documents).
Known Peers: {", ".join(peer_names)}.
Peer Routing:
{peer_matrix_lines}

RECENT CONVERSATION HISTORY:
{dialogue_block}

LIVE DIRECTORY STATUS FOR '{target_folder}/':
- Top-level subfolders: {", ".join(subfolders) if subfolders else "None"}
- Direct files on disk ({len(direct_files)} files):
{json.dumps(direct_files[:60], indent=2)}

INSTRUCTIONS & UNIFIED CONTRACT:
Analyze the user's operational request and dialogue history with full, direct access to the live files manifest above.
Determine the response type natively:

1. "query_info" (SEARCH / LOOKUP / INSPECT / LIST):
   - Triggered when the user asks to find, list, search, view, check, or inspect documents (e.g. "find Java book", "list lectures", "find receipts", "what is inside Download", "what files are here?").
   - Directly inspect the real files manifest above. Cite matching documents with bold titles, paths in code spans, sizes, and previews.
   - Set "actions": []

2. "conversation" (ADVICE / GREETINGS / CAPABILITIES / OUT-OF-SCOPE):
   - Triggered for greetings ("Hi"), suggestions/advice ("What suggestions do you have for my files?", "How can I organize?"), capabilities ("What can you do?"), or out-of-scope tasks (emails, web scraping).
   - Provide articulate, intelligent guidance grounded in their actual files on disk.
   - If out-of-scope, politely state local storage boundaries.
   - Set "actions": []

3. "plan" (ACTIONS / CONFIRMATIONS / ORGANIZING):
   - Triggered when the user commands an action ("Clean Download Names", "Sort student vouchers", "Separate peer documents") OR confirms a previous suggestion ("Yes", "Proceed", "Do it").
   - Synthesize move/rename/trash actions referencing exact filenames from the manifest.
   - Follow academic taxonomy: lectures/notes to Documents/University/<Subject>/, personal vouchers to Documents/University/Vouchers/, peer documents to Documents/Peers/<Peer>/, bills to Documents/Finances/.
   - Derive clean, descriptive filenames from content (never generic 'Document_2025-04-13.pdf').
   - Ensure distinct destinations.

OUTPUT SCHEMA (Strict JSON):
{{
  "type": "conversation" | "query_info" | "plan",
  "message": "<Articulate markdown response>",
  "actions": [
    {{
      "action_id": "step-1",
      "type": "move" | "trash" | "make_dir",
      "source": "<exact filename from manifest>",
      "destination": "Documents/...",
      "rationale": "...",
      "badge": "RENAME" | "FILE_MOVE" | "TRASH"
    }}
  ],
  "quick_chips": [ ... 3-4 contextual suggestions ... ]
}}
"""

    resp = call_gemini_json(system_prompt, prompt)
    return resp

if __name__ == "__main__":
    test_queries = [
        "What suggestions do you have for my files?",
        "Also can you find me a book for Java Language in the downloads?",
        "List university lectures",
        "Find receipts"
    ]
    for q in test_queries:
        print(f"\n>>> TESTING: '{q}'")
        res = run_unified_test(q)
        print(f"Result type: {res.get('type')}")
        print(f"Message preview: {res.get('message', '')[:120]}...")
        print(f"Actions count: {len(res.get('actions', []))}")
