import requests
import json

url = "http://192.168.100.65:8080"
print("=== 1. SERVER HEALTH & VERSION ===")
try:
    r = requests.get(f"{url}/health", timeout=5)
    print(json.dumps(r.json(), indent=2))
except Exception as e:
    print(f"Failed to get health: {e}")

print("\n=== 2. DOWNLOAD FOLDER CONTENTS ON PHONE ===")
try:
    r = requests.post(f"{url}/list_files", json={"path": "Download"}, timeout=5)
    data = r.json()
    items = data.get("items", [])
    print(f"Total items in Download: {len(items)}")
    files_only = [f for f in items if not f.get("is_dir")]
    dirs_only = [f for f in items if f.get("is_dir")]
    print(f"  Files: {len(files_only)}, Subdirectories: {len(dirs_only)}")
    print("\nSample files in phone's Download directory:")
    for f in files_only[:35]:
        sz = round(f.get("size_bytes", 0)/1024, 1)
        print(f"  - {f.get('name')} ({sz} KB)")
except Exception as e:
    print(f"Failed to list Download: {e}")

print("\n=== 3. DOCUMENTS FOLDER CONTENTS ON PHONE ===")
try:
    r = requests.post(f"{url}/list_files", json={"path": "Documents"}, timeout=5)
    data = r.json()
    items = data.get("items", [])
    print(f"Total items in Documents: {len(items)}")
    for f in items:
        kind = "DIR " if f.get("is_dir") else "FILE"
        sz = round(f.get("size_bytes", 0)/1024, 1)
        print(f"  [{kind}] {f.get('name')} ({sz} KB)")
except Exception as e:
    print(f"Failed to list Documents: {e}")
