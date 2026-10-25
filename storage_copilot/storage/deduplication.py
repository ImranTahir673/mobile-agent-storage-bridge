import os
import re
import hashlib
from collections import defaultdict
from datetime import datetime
from storage_copilot import config
from storage_copilot.security.path_resolver import resolve_safe_path

def calculate_file_hash(filepath: str) -> str:
    """Computes SHA256 checksum for file verification."""
    if not os.path.isfile(filepath):
        return ""
    h = hashlib.sha256()
    try:
        with open(filepath, "rb") as f:
            while chunk := f.read(65536):
                h.update(chunk)
        return h.hexdigest()
    except Exception:
        return ""

def score_keeper_candidate(file_info: dict) -> float:
    """
    Evaluates which file in a duplicate cluster should be retained as the original keeper.
    Higher score indicates higher likelihood of being the original, non-redundant file.
    """
    score = 100.0
    filename = file_info["filename"]
    stem, _ = os.path.splitext(filename)
    stem_lower = stem.lower()

    # 1. Heavily penalize copy/duplicate suffixes
    if re.search(r"\(\d+\)$", stem):
        score -= 60.0
    if re.search(r"[-_](?:copy|\d+)$", stem_lower):
        score -= 40.0
    if "duplicate" in stem_lower or "copy" in stem_lower:
        score -= 50.0

    # 2. Prefer clean canonical names over camera/whatsapp raw dumps
    if re.match(r"^(?:DOC-\d{8}-WA\d{4}|IMG-\d{8}-WA\d{4}|Screenshot_\d{8})", stem):
        score -= 20.0

    # 3. Prefer files already categorized under Documents over Download
    if "download" in file_info.get("path", "").lower():
        score -= 10.0

    # 4. Prefer older file creation/modification (original download)
    mtime = file_info.get("mtime", 0)
    if mtime > 0:
        score -= (mtime % 1000000) * 0.00001

    return score

def find_duplicate_clusters(base_dir: str, target_folder: str = "Download", recursive: bool = True) -> dict:
    """
    Phase 1 Core: Scans files within target_folder (or base_dir) to identify exact duplicate clusters via streaming SHA-256.
    Uses fast size pre-filtering to prevent unnecessary hashing on mobile.
    Selects the cleanest original copy as keeper and stages redundant copies for cleanup.
    """
    target_path = resolve_safe_path(target_folder) if target_folder else base_dir
    if not os.path.exists(target_path) or not os.path.isdir(target_path):
        return {
            "status": "success",
            "target_folder": target_folder,
            "total_clusters": 0,
            "total_duplicates": 0,
            "reclaimable_bytes": 0,
            "reclaimable_human": "0 B",
            "clusters": []
        }

    # Step 1: Collect non-hidden regular files grouped by size
    files_by_size = defaultdict(list)
    if recursive:
        for root, dirs, files in os.walk(target_path):
            dirs[:] = [d for d in dirs if not d.startswith(".") and d.lower() != "android" and d != ".agent_trash"]
            for fname in files:
                if fname.startswith("."):
                    continue
                abs_p = os.path.join(root, fname)
                try:
                    st = os.stat(abs_p)
                    size = st.st_size
                    if size > 0:
                        try:
                            rel_path = os.path.relpath(abs_p, base_dir).replace("\\", "/")
                        except Exception:
                            rel_path = fname

                        mtime_str = datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M")
                        files_by_size[size].append({
                            "filename": fname,
                            "path": rel_path,
                            "abs_path": abs_p,
                            "size_bytes": size,
                            "mtime": st.st_mtime,
                            "modified_date": mtime_str
                        })
                except Exception:
                    continue
    else:
        for entry in os.scandir(target_path):
            if entry.is_file() and not entry.name.startswith("."):
                try:
                    st = entry.stat()
                    size = st.st_size
                    if size > 0:
                        try:
                            rel_path = os.path.relpath(entry.path, base_dir).replace("\\", "/")
                        except Exception:
                            rel_path = entry.name

                        mtime_str = datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M")
                        files_by_size[size].append({
                            "filename": entry.name,
                            "path": rel_path,
                            "abs_path": entry.path,
                            "size_bytes": size,
                            "mtime": st.st_mtime,
                            "modified_date": mtime_str
                        })
                except Exception:
                    continue

    # Step 2: Stream SHA-256 only on files with identical non-zero sizes
    clusters_by_hash = defaultdict(list)
    for size, file_list in files_by_size.items():
        if len(file_list) >= 2:
            for f_info in file_list:
                f_hash = calculate_file_hash(f_info["abs_path"])
                if f_hash:
                    clusters_by_hash[f_hash].append(f_info)

    # Step 3: Filter true duplicate clusters (>= 2 files with identical hash)
    clusters = []
    total_duplicates = 0
    reclaimable_bytes = 0

    for f_hash, file_list in clusters_by_hash.items():
        if len(file_list) >= 2:
            sorted_files = sorted(file_list, key=score_keeper_candidate, reverse=True)
            keeper = sorted_files[0]
            keeper_dict = {
                "filename": keeper["filename"],
                "path": keeper["path"],
                "size_bytes": keeper["size_bytes"],
                "modified_date": keeper["modified_date"],
                "reason": "Original file with clean canonical naming"
            }

            redundant_files = []
            file_size = keeper["size_bytes"]
            for dup in sorted_files[1:]:
                reason = "Redundant copy"
                if re.search(r"\(\d+\)", dup["filename"]):
                    reason = "Redundant copy with numeric download suffix"
                elif "copy" in dup["filename"].lower():
                    reason = "Redundant copy duplicate"
                redundant_files.append({
                    "filename": dup["filename"],
                    "path": dup["path"],
                    "size_bytes": dup["size_bytes"],
                    "modified_date": dup["modified_date"],
                    "reason": reason
                })

            cluster_reclaimable = file_size * len(redundant_files)
            reclaimable_bytes += cluster_reclaimable
            total_duplicates += len(redundant_files)

            clusters.append({
                "hash": f_hash,
                "size_bytes": file_size,
                "size_human": config.format_bytes_human(file_size),
                "reclaimable_bytes": cluster_reclaimable,
                "reclaimable_human": config.format_bytes_human(cluster_reclaimable),
                "keeper": keeper_dict,
                "duplicates": redundant_files
            })

    return {
        "status": "success",
        "target_folder": target_folder,
        "total_clusters": len(clusters),
        "total_duplicates": total_duplicates,
        "reclaimable_bytes": reclaimable_bytes,
        "reclaimable_human": config.format_bytes_human(reclaimable_bytes),
        "clusters": clusters
    }
