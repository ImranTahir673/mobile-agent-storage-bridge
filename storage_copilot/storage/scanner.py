import os
from datetime import datetime
from storage_copilot.storage.content_extractor import extract_file_snippet
from storage_copilot.security.pii_sanitizer import sanitize_pii

def collect_storage_files(base_dir: str, target_folder: str = "Download", max_files: int = 1500) -> list:
    """
    Collects real files across device storage (target folder and Documents subtree) without arbitrary low caps.
    Extracts modification dates, sizes, and up to 600 chars of text with PII redaction for inspection and query answering.
    """
    collected = []
    seen_paths = set()
    now_dt = datetime.now()

    search_dirs = [
        os.path.join(base_dir, target_folder),
        os.path.join(base_dir, "Documents"),
        os.path.join(base_dir, "Download"),
    ]

    for s_dir in search_dirs:
        if not os.path.exists(s_dir) or not os.path.isdir(s_dir):
            continue
        try:
            for root, dirs, files in os.walk(s_dir):
                # Omit internal protected trees from query inspection
                dirs[:] = [d for d in dirs if not d.startswith(".") and d.lower() != "android"]
                for f in files:
                    if f.startswith("."):
                        continue
                    full_p = os.path.join(root, f)
                    if full_p in seen_paths:
                        continue
                    seen_paths.add(full_p)

                    try:
                        fstat = os.stat(full_p)
                        fsize = fstat.st_size
                        mtime = fstat.st_mtime
                        mtime_dt = datetime.fromtimestamp(mtime)
                        is_today = (mtime_dt.date() == now_dt.date())
                        modified_str = mtime_dt.strftime("%Y-%m-%d %H:%M")
                        date_str = mtime_dt.strftime("%Y-%m-%d")
                    except Exception:
                        fsize = 0
                        mtime = 0
                        is_today = False
                        modified_str = ""
                        date_str = ""

                    ext = os.path.splitext(f)[1].lower()
                    rel_p = os.path.relpath(full_p, base_dir).replace("\\", "/")

                    raw_text = extract_file_snippet(full_p, 400)
                    sanitized = sanitize_pii(raw_text)
                    collected.append({
                        "filename": f,
                        "name": f,
                        "path": rel_p,
                        "full_path": full_p,
                        "extension": ext,
                        "size_kb": round(fsize / 1024, 1),
                        "size_bytes": fsize,
                        "modified_timestamp": mtime,
                        "modified_str": modified_str,
                        "date_str": date_str,
                        "is_today": is_today,
                        "snippet": sanitized,
                        "text_snippet": sanitized if sanitized else "None (binary/image)",
                        "has_content": bool(sanitized)
                    })
                    if len(collected) >= max_files:
                        return collected
        except Exception:
            continue
    return collected
