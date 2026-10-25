# storage_copilot/storage/scanner.py
import os
import re
import time
import threading
from datetime import datetime, timedelta
from typing import Dict, Tuple, List, Any, Optional

from storage_copilot import config
from storage_copilot.security.path_resolver import resolve_safe_path
from storage_copilot.storage.content_extractor import extract_file_snippet
from storage_copilot.security.pii_sanitizer import sanitize_pii
def _publish_event(event_type: str, data: Dict[str, Any]) -> None:
    try:
        from storage_copilot.execution.event_bus import publish_event
        publish_event(event_type, data)
    except Exception:
        pass

# In-memory cache: (abs_path, mtime, max_chars) -> sanitized_snippet
_SNIPPET_CACHE: Dict[Tuple[str, float, int], str] = {}

# Maximum candidates hydrated per turn during lazy snippet extraction
MAX_HYDRATE_CANDIDATES = 50

# Maximum file size (5 MB) for automatic document snippet hydration
MAX_AUTO_HYDRATE_BYTES = 5 * 1024 * 1024

# Large archive threshold (10 MB) above which .zip/.apk inspection is skipped upfront
MAX_ARCHIVE_HYDRATE_BYTES = 10 * 1024 * 1024

# Media / binary extensions that never contain readable document text and should skip upfront extraction
BINARY_MEDIA_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".heic", ".svg",
    ".mp4", ".mkv", ".mov", ".avi", ".webm", ".3gp",
    ".mp3", ".m4a", ".wav", ".aac", ".ogg", ".opus", ".amr", ".flac",
    ".apk", ".xapk", ".apks", ".so", ".dex", ".bin", ".exe", ".iso"
}

# Readable document / text extensions eligible for snippet extraction
TEXT_DOC_EXTENSIONS = {
    ".pdf", ".docx", ".pptx", ".xlsx", ".txt", ".md", ".json",
    ".csv", ".log", ".xml", ".html", ".tex", ".cpp", ".py", ".vcf", ".ics", ".tmp"
}

# Cryptic filename regex (`DOC-*`, `WA*`, numeric ArXiv `1508*`, `document*`, `scan*`, `IMG_*`)
CRYPTIC_PREFIX_PATTERN = re.compile(
    r"^(?:doc[-_]|wa\d|whatsapp|1508\.|\d{4}\.\d{4,5}|document|scan|img[-_]|file[-_]|[a-f0-9]{12,})",
    re.IGNORECASE
)


def is_cryptic_filename(filename: str) -> bool:
    """Returns True if filename has a cryptic or generic prefix requiring content disambiguation."""
    fn_lower = (filename or "").lower()
    if CRYPTIC_PREFIX_PATTERN.search(fn_lower):
        return True
    if ".." in fn_lower or "wa0" in fn_lower:
        return True
    return False


def should_hydrate_file_upfront(
    filename: str,
    ext: str,
    size_bytes: int,
    exact_match_found: bool = False
) -> bool:
    """
    Smart Lazy Hydration Rule (Phase 4):
    - Skip obvious binary/media files (.png, .jpg, .mp4, .mp3, .apk, etc.) or .zip > 10 MB.
    - If an exact filename match already satisfies a specific user lookup, skip hydrating unrelated files
      unless the file itself is a cryptic WhatsApp/ArXiv document that may be part of a broader check.
    - Immediately hydrate cryptic/generic prefixes (`DOC-*`, `WA*`, `1508*`, `document*`, `scan*`, `IMG_*`)
      or small text/PDF/DOCX/code documents (<= 5 MB).
    """
    ext_lower = (ext or os.path.splitext(filename)[1]).lower()

    if ext_lower in BINARY_MEDIA_EXTENSIONS:
        return False

    if ext_lower in (".zip", ".rar", ".7z", ".tar", ".gz") and size_bytes > MAX_ARCHIVE_HYDRATE_BYTES:
        return False

    if is_cryptic_filename(filename):
        return size_bytes <= MAX_ARCHIVE_HYDRATE_BYTES

    if exact_match_found:
        return False

    if ext_lower in TEXT_DOC_EXTENSIONS and size_bytes <= MAX_AUTO_HYDRATE_BYTES:
        return True

    return False


def get_cached_sanitized_snippet(file_path: str, mtime: float, max_chars: int = 1000) -> str:
    """Returns PII-sanitized file snippet using O(1) RAM cache when mtime is unchanged."""
    cache_key = (file_path, mtime, max_chars)
    if cache_key in _SNIPPET_CACHE:
        return _SNIPPET_CACHE[cache_key]

    raw_text = extract_file_snippet(file_path, max_chars)
    sanitized = sanitize_pii(raw_text)
    if len(_SNIPPET_CACHE) > 2500:
        _SNIPPET_CACHE.clear()
    _SNIPPET_CACHE[cache_key] = sanitized
    return sanitized


def hydrate_candidate_snippets(
    items: List[Dict[str, Any]],
    query_prompt: str = "",
    max_candidates: int = MAX_HYDRATE_CANDIDATES,
    max_chars: int = 1000
) -> int:
    """
    On-demand Stage 2 snippet hydration for candidate manifest items.
    Returns count of newly hydrated files in this pass.
    """
    if not items:
        return 0

    q_lower = (query_prompt or "").strip().lower()
    exact_match_items = []
    if len(q_lower) >= 4:
        for it in items:
            fn_l = (it.get("filename") or "").lower()
            stem_l = os.path.splitext(fn_l)[0]
            if len(stem_l) >= 4 and (fn_l in q_lower or stem_l in q_lower) and not is_cryptic_filename(fn_l):
                exact_match_items.append(it)

    exact_match_found = len(exact_match_items) > 0
    hydrated_count = 0

    # Priority 1: Exact matched items always get hydrated first
    for it in exact_match_items:
        if hydrated_count >= max_candidates:
            break
        if not it.get("snippet_hydrated"):
            snip = get_cached_sanitized_snippet(it["full_path"], it.get("mtime", 0.0), max_chars)
            it["snippet"] = snip
            it["text_snippet"] = snip if snip else "None (binary/image)"
            it["has_content"] = bool(snip)
            it["snippet_hydrated"] = True
            hydrated_count += 1

    # Priority 2: Smart hydration for cryptic filenames and eligible small documents (<= 5 MB)
    for it in items:
        if hydrated_count >= max_candidates:
            break
        if it.get("snippet_hydrated"):
            continue

        fn = it.get("filename") or ""
        ext = it.get("extension") or ""
        sz = it.get("size_bytes", 0)

        if should_hydrate_file_upfront(fn, ext, sz, exact_match_found=exact_match_found):
            snip = get_cached_sanitized_snippet(it["full_path"], it.get("mtime", 0.0), max_chars)
            it["snippet"] = snip
            it["text_snippet"] = snip if snip else "None (binary/image)"
            it["has_content"] = bool(snip)
            it["snippet_hydrated"] = True
            hydrated_count += 1

    return hydrated_count


class ManifestIndexManager:
    """
    Maintains an in-memory directory manifest index keyed by target_dir path,
    invalidated automatically whenever directory mtime or entry count changes.
    Supports background thread warm-up.
    """
    def __init__(self):
        self._lock = threading.Lock()
        self._index: Dict[str, Dict[str, Any]] = {}

    def _compute_dir_signature(self, target_dir: str) -> Tuple[float, int]:
        if not os.path.exists(target_dir) or not os.path.isdir(target_dir):
            return (0.0, 0)
        try:
            dir_mtime = os.stat(target_dir).st_mtime
            entries = list(os.scandir(target_dir))
            count = len(entries)
            max_entry_mtime = dir_mtime
            for e in entries:
                try:
                    em = e.stat().st_mtime
                    if em > max_entry_mtime:
                        max_entry_mtime = em
                except Exception:
                    continue
            return (round(max_entry_mtime, 3), count)
        except Exception:
            return (0.0, 0)

    def get_cached(self, target_dir: str) -> Optional[Dict[str, Any]]:
        sig = self._compute_dir_signature(target_dir)
        with self._lock:
            entry = self._index.get(target_dir)
            if entry and entry.get("signature") == sig:
                return entry.get("data")
        return None

    def get_Cached(self, target_dir: str) -> Optional[Dict[str, Any]]:
        """Alias for get_cached supporting test suites."""
        return self.get_cached(target_dir)

    def put_cached(self, target_dir: str, data: Dict[str, Any]) -> None:
        sig = self._compute_dir_signature(target_dir)
        with self._lock:
            self._index[target_dir] = {
                "signature": sig,
                "cached_at": time.time(),
                "data": data
            }

    def invalidate(self, target_dir: Optional[str] = None) -> None:
        with self._lock:
            if target_dir:
                self._index.pop(target_dir, None)
            else:
                self._index.clear()

    def warm_up_async(self, target_folder: str = "Download") -> threading.Thread:
        """Spawns a non-blocking background daemon thread to index target_folder."""
        def _worker():
            try:
                scan_directory_manifest(target_folder=target_folder, lazy_snippets=True)
            except Exception:
                pass

        t = threading.Thread(target=_worker, daemon=True)
        t.start()
        return t


manifest_index_manager = ManifestIndexManager()


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


def scan_directory_manifest(
    target_folder: str = "Download",
    max_files: int = 1000,
    lazy_snippets: bool = True,
    query_prompt: str = ""
) -> Dict[str, Any]:
    """
    Two-Stage Manifest Scanner (Phase 4):
    - Stage 1: Fast POSIX metadata scan (or instant return from ManifestIndexManager if directory mtime is unchanged).
    - Stage 2: Smart Lazy Snippet Hydration for up to MAX_HYDRATE_CANDIDATES=50 eligible files.
    - Emits `scan_progress` SSE telemetry via EventBus.
    """
    base_dir = config.BASE_STORAGE_DIR
    target_dir = resolve_target_directory(target_folder)
    now_dt = datetime.now()

    cached_data = manifest_index_manager.get_cached(target_dir)
    if cached_data is not None:
        return cached_data

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

    total_entries = min(len(scanned_entries), max_files)
    _publish_event("scan_progress", {
        "phase": "scanning_metadata",
        "target_folder": target_folder,
        "total_files": total_entries
    })

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
                rel_age = f"{max(1, int(age_days / 30))} months ago" if age_days < 365 else f"{max(1, int(age_days / 365))} years ago"
        except Exception:
            file_size = 0
            mtime = 0.0
            mtime_str = ""
            date_str = ""
            rel_age = "Unknown"
            age_days = 9999.0

        try:
            rel_path = os.path.relpath(entry.path, base_dir).replace("\\", "/")
        except Exception:
            rel_path = f"{target_folder}/{filename}"

        # Check if snippet is ALREADY in RAM cache from an earlier scan (O(1) instant lookup)
        cache_key = (entry.path, mtime, 1000)
        already_cached = cache_key in _SNIPPET_CACHE
        cached_snip = _SNIPPET_CACHE.get(cache_key, "") if already_cached else ""

        if not lazy_snippets and not already_cached:
            cached_snip = get_cached_sanitized_snippet(entry.path, mtime, 1000)
            already_cached = True

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
            "snippet": cached_snip,
            "text_snippet": cached_snip if cached_snip else "None (binary/image)",
            "has_content": bool(cached_snip),
            "snippet_hydrated": already_cached
        }
        manifest.append(item)
        manifest_lookup[rel_path.lower()] = item
        manifest_lookup[filename.lower()] = item

    # Stage 2: Smart Lazy Hydration for eligible candidate files (<= MAX_HYDRATE_CANDIDATES)
    hydrated_now = 0
    if lazy_snippets:
        hydrated_now = hydrate_candidate_snippets(
            manifest,
            query_prompt=query_prompt,
            max_candidates=MAX_HYDRATE_CANDIDATES,
            max_chars=1000
        )

    result_data = {
        "target_dir": target_dir,
        "subfolders": subfolders,
        "manifest": manifest,
        "manifest_lookup": manifest_lookup,
        "now_dt": now_dt,
        "hydrated_count": hydrated_now
    }
    manifest_index_manager.put_cached(target_dir, result_data)

    _publish_event("scan_progress", {
        "phase": "scan_complete",
        "target_folder": target_folder,
        "total_files": len(manifest),
        "hydrated_snippets": hydrated_now
    })
    return result_data


def collect_storage_files(base_dir: str, target_folder: str = "Download", max_files: int = 1500) -> list:
    """
    Collects real files across device storage (target folder and Documents subtree).
    Uses smart lazy hydration criteria to avoid expensive extraction on large binary/media files.
    """
    collected = []
    seen_paths = set()
    now_dt = datetime.now()
    hydrated_count = 0

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

                    sanitized = ""
                    if hydrated_count < MAX_HYDRATE_CANDIDATES and should_hydrate_file_upfront(f, ext, fsize):
                        sanitized = get_cached_sanitized_snippet(full_p, mtime, 400)
                        hydrated_count += 1

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
