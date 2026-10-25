# storage_copilot/storage/scanner.py
import os
from datetime import datetime, timedelta
from typing import Dict, Tuple, List, Any

from storage_copilot import config
from storage_copilot.security.path_resolver import resolve_safe_path
from storage_copilot.storage.content_extractor import extract_file_snippet
from storage_copilot.security.pii_sanitizer import sanitize_pii

# In-memory cache: (abs_path, mtime, max_chars) -> sanitized_snippet
_SNIPPET_CACHE: Dict[Tuple[str, float, int], str] = {}


def get_cached_sanitized_snippet(file_path: str, mtime: float, max_chars: int = 1000) -> str:
    """Returns PII-sanitized file snippet using O(1) RAM cache when mtime is unchanged."""
    cache_key = (file_path, mtime, max_chars)
    if cache_key in _SNIPPET_CACHE:
        return _SNIPPET_CACHE[cache_key]

    raw_text = extract_file_snippet(file_path, max_chars)
    sanitized = sanitize_pii(raw_text)
    if len(_SNIPPET_CACHE) > 2000:
        _SNIPPET_CACHE.clear()
    _SNIPPET_CACHE[cache_key] = sanitized
    return sanitized


def resolve_target_directory(target_folder: str = "Download") -> str:
    """Safely resolves target_folder relative to current config.BASE_STORAGE_DIR."""
    base_dir = config.BASE_STORAGE_DIR
    dir_candidates = [
        target_folder,
        target_folder.lower(),
        target_folder.capitalize(),
        os.path.join(base_dir, target_folder),
        os.path.join(base_dir, target_folder.lower()),
    ]
    for c in dir_candidates:
        try:
            p = resolve_safe_path(c) if not os.path.isabs(c) else c
            if os.path.exists(p) and os.path.isdir(p):
                return p
        except Exception:
            continue

    fallback = os.path.join(base_dir, target_folder)
    os.makedirs(fallback, exist_ok=True)
    return fallback


def scan_directory_manifest(target_folder: str = "Download", max_files: int = 1000) -> Dict[str, Any]:
    """
    Scans target_folder (including 1st-level subfolders), computes temporal metadata,
    and extracts cached PII-sanitized snippets.
    Never called on pure CONVERSATION turns unless format breakdown is requested.
    """
    base_dir = config.BASE_STORAGE_DIR
    target_dir = resolve_target_directory(target_folder)
    now_dt = datetime.now()

    scanned_entries = []
    subfolders = []
    if os.path.exists(target_dir) and os.path.isdir(target_dir):
        for entry in os.scandir(target_dir):
            if entry.is_dir() and not entry.name.startswith("."):
                cnt = 0
                try:
                    for sub_entry in os.scandir(entry.path):
                        if sub_entry.is_file() and not sub_entry.name.startswith("."):
                            cnt += 1
                            scanned_entries.append(sub_entry)
                    subfolders.append(f"{entry.name} ({cnt} items)")
                except Exception:
                    subfolders.append(entry.name)
            elif entry.is_file() and not entry.name.startswith("."):
                scanned_entries.append(entry)

    manifest: List[Dict[str, Any]] = []
    manifest_lookup: Dict[str, Dict[str, Any]] = {}

    for entry in scanned_entries[:max_files]:
        filename = entry.name
        ext = os.path.splitext(filename)[1].lower()
        try:
            st = entry.stat()
            file_size = st.st_size
            mtime = st.st_mtime
            mtime_dt = datetime.fromtimestamp(mtime)
            mtime_str = mtime_dt.strftime("%Y-%m-%d %H:%M")
            date_str = mtime_dt.strftime("%Y-%m-%d")

            age_seconds = (now_dt - mtime_dt).total_seconds()
            age_days = round(max(0.0, age_seconds / 86400.0), 1)

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
        except Exception:
            file_size = 0
            mtime = 0.0
            mtime_str = ""
            date_str = ""
            rel_age = "Unknown"
            age_days = 9999.0

        sanitized_snippet = get_cached_sanitized_snippet(entry.path, mtime, 1000)

        try:
            rel_path = os.path.relpath(entry.path, base_dir).replace("\\", "/")
        except Exception:
            rel_path = f"{target_folder}/{filename}"

        item = {
            "filename": filename,
            "name": filename,
            "path": rel_path,
            "full_path": entry.path,
            "extension": ext,
            "size_kb": round(file_size / 1024, 1),
            "size_bytes": file_size,
            "modified_date": mtime_str,
            "date_str": date_str,
            "relative_age": rel_age,
            "age_days": age_days,
            "mtime": mtime,
            "snippet": sanitized_snippet,
            "text_snippet": sanitized_snippet if sanitized_snippet else "None (binary/image)",
            "has_content": bool(sanitized_snippet)
        }
        manifest.append(item)
        manifest_lookup[rel_path.lower()] = item
        manifest_lookup[filename.lower()] = item

    return {
        "target_dir": target_dir,
        "subfolders": subfolders,
        "manifest": manifest,
        "manifest_lookup": manifest_lookup,
        "now_dt": now_dt
    }


def collect_storage_files(base_dir: str, target_folder: str = "Download", max_files: int = 1500) -> list:
    """
    Collects real files across device storage (target folder and Documents subtree).
    Uses cached snippet extraction for fast performance.
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
                        mtime = 0.0
                        is_today = False
                        modified_str = ""
                        date_str = ""

                    ext = os.path.splitext(f)[1].lower()
                    rel_p = os.path.relpath(full_p, base_dir).replace("\\", "/")

                    sanitized = get_cached_sanitized_snippet(full_p, mtime, 400)
                    collected.append({
                        "filename": f,
                        "name": f,
                        "path": rel_p,
                        "full_path": full_p,
                        "extension": ext,
                        "size_kb": round(fsize / 1024, 1),
                        "size_bytes": fsize,
                        "modified_timestamp": mtime,
                        "modified_date": modified_str,
                        "modified_str": modified_str,
                        "date_str": date_str,
                        "is_today": is_today,
                        "mtime": mtime,
                        "snippet": sanitized,
                        "text_snippet": sanitized if sanitized else "None (binary/image)",
                        "has_content": bool(sanitized)
                    })
                    if len(collected) >= max_files:
                        return collected
        except Exception:
            continue
    return collected
