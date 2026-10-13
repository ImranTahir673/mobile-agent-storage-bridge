import os
import sys
import json
from datetime import datetime, timedelta

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from server import call_gemini_json, sanitize_pii, extract_file_snippet

test_dir = os.path.join(os.path.dirname(__file__), "temporal_test_fixtures")
now_dt = datetime.now()
now_str = now_dt.strftime("%A, %B %d, %Y at %I:%M %p")
today_date = now_dt.strftime("%Y-%m-%d")

# 1. Build rich temporal manifest
manifest = []
for entry in os.scandir(test_dir):
    if entry.is_file():
        stat = entry.stat()
        mtime = stat.st_mtime
        mtime_dt = datetime.fromtimestamp(mtime)
        mtime_str = mtime_dt.strftime("%Y-%m-%d %H:%M")
        date_str = mtime_dt.strftime("%Y-%m-%d")
        
        age_seconds = (now_dt - mtime_dt).total_seconds()
        age_days = round(age_seconds / 86400, 1)
        
        if mtime_dt.date() == now_dt.date():
            rel_age = "Today"
        elif mtime_dt.date() == (now_dt.date() - timedelta(days=1)):
            rel_age = "Yesterday"
        elif age_days < 7:
            rel_age = f"{int(age_days)} days ago"
        elif age_days < 30:
            rel_age = f"{max(1, int(age_days / 7))} weeks ago"
        elif age_days < 365:
            rel_age = f"{max(1, int(age_days / 30))} months ago"
        else:
            rel_age = f"{max(1, int(age_days / 365))} years ago"
            
        manifest.append({
            "filename": entry.name,
            "path": f"Download/{entry.name}",
            "size_kb": round(stat.st_size / 1024, 1),
            "modified_date": mtime_str,
            "relative_age": rel_age,
            "age_days": age_days,
            "mtime": mtime,
            "snippet": entry.name
        })

# Sort newest first by default
manifest_sorted_newest = sorted(manifest, key=lambda x: x["mtime"], reverse=True)
manifest_sorted_oldest = sorted(manifest, key=lambda x: x["mtime"], reverse=False)

def query_temporal_copilot(user_query):
    # Detect oldest vs newest preference
    is_oldest_req = any(w in user_query.lower() for w in ("oldest", "earliest", "first downloaded"))
    active_manifest = manifest_sorted_oldest if is_oldest_req else manifest_sorted_newest
    
    # Compute folder temporal stats
    today_files = [f["filename"] for f in manifest if f["relative_age"] == "Today"]
    yesterday_files = [f["filename"] for f in manifest if f["relative_age"] == "Yesterday"]
    week_files = [f["filename"] for f in manifest if f["age_days"] <= 7]
    old_files = [f["filename"] for f in manifest if f["age_days"] >= 30]
    newest_f = manifest_sorted_newest[0]["filename"] if manifest_sorted_newest else "None"
    oldest_f = manifest_sorted_oldest[0]["filename"] if manifest_sorted_oldest else "None"
    
    system_prompt = f"""You are Storage Copilot, an elite on-device AI agent running locally via Termux on Android for Imran Tahir (Roll/ID: ['BSAI-182'], Major: Artificial Intelligence).
Current Device Time: {now_str} (Today's Date: {today_date}).
Storage Root: /storage/emulated/0 (Downloads, Documents).

TEMPORAL DIRECTORY AUDIT STATS (for target folder 'Download/'):
- Files downloaded Today ({today_date}): {len(today_files)} files ({', '.join(today_files) if today_files else 'None'})
- Files downloaded Yesterday: {len(yesterday_files)} files ({', '.join(yesterday_files) if yesterday_files else 'None'})
- Files from past 7 days: {len(week_files)} files
- Files older than 30 days: {len(old_files)} files ({', '.join(old_files) if old_files else 'None'})
- Single Most Recent File: {newest_f}
- Single Oldest File: {oldest_f}

FILES MANIFEST (Sorted {'oldest first' if is_oldest_req else 'newest first'}):
{json.dumps([{k: v for k, v in f.items() if k != 'mtime'} for f in active_manifest], indent=2)}

INSTRUCTIONS & REASONING RULES:
1. "query_info" (TEMPORAL SEARCH / DATES / RECENCY / TIME RANGES):
   - If user asks about recency (e.g. "latest files", "newest file", "oldest files"):
     * Cite the exact matching files with bold names, paths in code spans, sizes, and exact modified timestamps/relative age.
   - If user asks about exact dates or durations (e.g. "files from today", "yesterday", "last 7 days", "older than 30 days"):
     * Use the temporal stats and `relative_age`/`age_days` from the manifest to accurately answer.
   - Set "actions": []

2. "plan" (TEMPORAL ACTIONS / ARCHIVING / LIFECYCLE CLEANUP):
   - If user commands an action based on age or dates (e.g. "archive files older than 30 days", "sort today's downloads"):
     * Target EXACTLY the files matching that age criterion.
     * Staged move/archive/trash actions referencing exact filenames.
     * Set "type": "plan".

Output Schema (Strict JSON):
{{
  "type": "query_info" | "plan" | "conversation",
  "message": "<Articulate markdown response>",
  "actions": [ ... ],
  "quick_chips": [ ... ]
}}
"""

    resp = call_gemini_json(system_prompt, user_query)
    return resp

if __name__ == "__main__":
    queries = [
        "What is my latest download?",
        "Did I download anything today?",
        "What files did I receive yesterday?",
        "Show files from the past 7 days",
        "What are my oldest files?",
        "Move files older than 30 days to Documents/Archive/"
    ]
    for q in queries:
        print(f"\n==========================================")
        print(f"QUERY: '{q}'")
        res = query_temporal_copilot(q)
        print(f"Type: {res.get('type')}")
        print(f"Message: {res.get('message')}")
        if res.get("actions"):
            print(f"Staged Actions ({len(res.get('actions'))}):")
            for a in res.get("actions"):
                print(f"  -> {a.get('type')} {a.get('source')} => {a.get('destination')}")
