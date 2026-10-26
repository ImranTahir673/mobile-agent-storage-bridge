import os
import re
import hashlib
from collections import defaultdict
from datetime import datetime
from typing import Dict, Any, List, Set, Tuple, Optional

from storage_copilot import config
from storage_copilot.security.path_resolver import resolve_safe_path
from storage_copilot.storage.content_extractor import extract_file_snippet
from storage_copilot.security.pii_sanitizer import sanitize_pii

NEAR_DUPLICATE_EXTENSIONS = {
    ".pdf", ".docx", ".pptx", ".txt", ".md", ".csv", ".json", ".xml", ".html", ".tex", ".py", ".cpp"
}

TEMPORARY_EXTENSIONS = {".tmp", ".cache", ".bak", ".crdownload", ".part", ".temp"}


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


def compute_text_shingles(text: str, k: int = 3) -> Set[str]:
    """
    Extracts normalized character k-gram shingles from document text for Jaccard similarity.
    Normalizes punctuation and whitespace, generating character k-grams.
    """
    if not text:
        return set()
    # Strip synthetic wrapper prefixes like '[Word Doc: ...]'
    cleaned = re.sub(r"^\[(?:Word Doc|Presentation|Archive[^:]*):\s*", "", text.strip())
    cleaned = re.sub(r"\]$", "", cleaned)
    normalized = re.sub(r"[^a-z0-9]+", " ", cleaned.lower()).strip()
    if not normalized:
        return set()
    if len(normalized) < k:
        return {normalized}
    return {normalized[i:i + k] for i in range(len(normalized) - k + 1)}


def compute_jaccard_similarity(text_a: str, text_b: str, k: int = 3) -> float:
    """
    Computes 3-gram Jaccard similarity in [0.0, 1.0] between two text strings:
    J(A, B) = |S_A intersection S_B| / |S_A union S_B|.
    """
    if not text_a or not text_b:
        return 0.0
    shingles_a = compute_text_shingles(text_a, k=k)
    shingles_b = compute_text_shingles(text_b, k=k)
    if not shingles_a or not shingles_b:
        return 0.0
    intersection = len(shingles_a & shingles_b)
    union = len(shingles_a | shingles_b)
    if union == 0:
        return 0.0
    return round(intersection / union, 4)


def score_keeper_candidate(file_info: dict) -> float:
    """
    Evaluates which file in a duplicate cluster should be retained as the original keeper.
    Higher score indicates higher likelihood of being the original, non-redundant file.
    """
    score = 100.0
    filename = file_info["filename"]
    stem, _ = os.path.splitext(filename)
    stem_lower = stem.lower()

    # 1. Heavily penalize copy/duplicate/edit/draft suffixes
    if re.search(r"\(\d+\)$", stem):
        score -= 60.0
    if re.search(r"[-_ ](?:copy|edit|edited|draft|v\d+|\d+)$", stem_lower):
        score -= 40.0
    if any(w in stem_lower for w in ("duplicate", "copy", "edit", "draft")):
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


def find_near_duplicate_clusters(
    candidates: List[Dict[str, Any]],
    threshold: float = 0.85,
    k: int = 3
) -> List[Dict[str, Any]]:
    """
    Phase 6 Core: Identifies near-duplicate document clusters among candidate files
    using 3-gram Jaccard similarity (default threshold >= 0.85).
    Only compares files that have non-empty text snippets and were not already grouped as exact SHA-256 duplicates.
    """
    eligible = []
    for f_info in candidates:
        ext = os.path.splitext(f_info["filename"])[1].lower()
        if ext not in NEAR_DUPLICATE_EXTENSIONS:
            continue
        snip = f_info.get("snippet")
        if snip is None:
            raw_txt = extract_file_snippet(f_info["abs_path"], 2000)
            snip = sanitize_pii(raw_txt)
        if snip and len(snip.strip()) >= 20 and snip != "None (binary/image)":
            item_copy = dict(f_info)
            item_copy["snippet"] = snip
            item_copy["shingles"] = compute_text_shingles(snip, k=k)
            if len(item_copy["shingles"]) >= 3:
                eligible.append(item_copy)

    visited: Set[str] = set()
    near_clusters: List[Dict[str, Any]] = []

    for i in range(len(eligible)):
        base_item = eligible[i]
        if base_item["abs_path"] in visited:
            continue

        group = [base_item]
        sim_scores: Dict[str, float] = {}

        for j in range(i + 1, len(eligible)):
            other_item = eligible[j]
            if other_item["abs_path"] in visited:
                continue

            s_a = base_item["shingles"]
            s_b = other_item["shingles"]
            inter = len(s_a & s_b)
            union = len(s_a | s_b)
            sim = round(inter / union, 4) if union > 0 else 0.0

            if sim >= threshold:
                group.append(other_item)
                sim_scores[other_item["abs_path"]] = sim

        if len(group) >= 2:
            for member in group:
                visited.add(member["abs_path"])

            sorted_group = sorted(group, key=score_keeper_candidate, reverse=True)
            keeper = sorted_group[0]
            keeper_dict = {
                "filename": keeper["filename"],
                "path": keeper["path"],
                "size_bytes": keeper["size_bytes"],
                "modified_date": keeper["modified_date"],
                "reason": "Original canonical document (Near-Duplicate Keeper)"
            }

            redundant_files = []
            cluster_reclaimable = 0
            max_sim = 0.0

            for dup in sorted_group[1:]:
                sim_val = sim_scores.get(dup["abs_path"])
                if sim_val is None:
                    sim_val = compute_jaccard_similarity(keeper["snippet"], dup["snippet"], k=k)
                max_sim = max(max_sim, sim_val)
                cluster_reclaimable += dup["size_bytes"]
                redundant_files.append({
                    "filename": dup["filename"],
                    "path": dup["path"],
                    "size_bytes": dup["size_bytes"],
                    "modified_date": dup["modified_date"],
                    "similarity": sim_val,
                    "match_type": "NEAR_JACCARD",
                    "reason": f"Near-duplicate revision ({int(sim_val * 100)}% 3-gram Jaccard match)"
                })

            near_clusters.append({
                "hash": f"jaccard_3gram_{hashlib.md5(keeper['path'].encode('utf-8')).hexdigest()[:12]}",
                "match_type": "NEAR_JACCARD",
                "similarity": max_sim,
                "size_bytes": keeper["size_bytes"],
                "size_human": config.format_bytes_human(keeper["size_bytes"]),
                "reclaimable_bytes": cluster_reclaimable,
                "reclaimable_human": config.format_bytes_human(cluster_reclaimable),
                "keeper": keeper_dict,
                "duplicates": redundant_files
            })

    return near_clusters


def find_duplicate_clusters(
    base_dir: str,
    target_folder: str = "Download",
    recursive: bool = True,
    include_near: bool = False,
    near_threshold: float = 0.85
) -> dict:
    """
    Scans files within target_folder (or base_dir) to identify:
    1. Exact duplicate clusters via streaming SHA-256 (fast size pre-filtering).
    2. Optional near-duplicate clusters via 3-gram Jaccard similarity (threshold >= 0.85) when include_near=True.
    Selects the cleanest original copy as keeper and stages redundant copies for cleanup.
    """
    target_path = resolve_safe_path(target_folder) if target_folder else base_dir
    if not os.path.exists(target_path) or not os.path.isdir(target_path):
        return {
            "status": "success",
            "target_folder": target_folder,
            "total_clusters": 0,
            "total_duplicates": 0,
            "exact_clusters_count": 0,
            "near_clusters_count": 0,
            "reclaimable_bytes": 0,
            "reclaimable_human": "0 B",
            "clusters": []
        }

    all_scanned_files: List[Dict[str, Any]] = []
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
                        f_entry = {
                            "filename": fname,
                            "path": rel_path,
                            "abs_path": abs_p,
                            "size_bytes": size,
                            "mtime": st.st_mtime,
                            "modified_date": mtime_str
                        }
                        files_by_size[size].append(f_entry)
                        all_scanned_files.append(f_entry)
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
                        f_entry = {
                            "filename": entry.name,
                            "path": rel_path,
                            "abs_path": entry.path,
                            "size_bytes": size,
                            "mtime": st.st_mtime,
                            "modified_date": mtime_str
                        }
                        files_by_size[size].append(f_entry)
                        all_scanned_files.append(f_entry)
                except Exception:
                    continue

    # Pass 1: Stream SHA-256 only on files with identical non-zero sizes
    clusters_by_hash = defaultdict(list)
    for size, file_list in files_by_size.items():
        if len(file_list) >= 2:
            for f_info in file_list:
                f_hash = calculate_file_hash(f_info["abs_path"])
                if f_hash:
                    clusters_by_hash[f_hash].append(f_info)

    clusters = []
    total_duplicates = 0
    reclaimable_bytes = 0
    exact_redundant_paths: Set[str] = set()

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
                exact_redundant_paths.add(dup["abs_path"])
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
                    "similarity": 1.0,
                    "match_type": "EXACT_SHA256",
                    "reason": reason
                })

            cluster_reclaimable = file_size * len(redundant_files)
            reclaimable_bytes += cluster_reclaimable
            total_duplicates += len(redundant_files)

            clusters.append({
                "hash": f_hash,
                "match_type": "EXACT_SHA256",
                "similarity": 1.0,
                "size_bytes": file_size,
                "size_human": config.format_bytes_human(file_size),
                "reclaimable_bytes": cluster_reclaimable,
                "reclaimable_human": config.format_bytes_human(cluster_reclaimable),
                "keeper": keeper_dict,
                "duplicates": redundant_files
            })

    exact_clusters_count = len(clusters)
    near_clusters_count = 0

    # Pass 2: 3-Gram Jaccard Near-Duplicate Detection (when include_near=True)
    if include_near:
        remaining_candidates = [f for f in all_scanned_files if f["abs_path"] not in exact_redundant_paths]
        near_clusters = find_near_duplicate_clusters(remaining_candidates, threshold=near_threshold)
        near_clusters_count = len(near_clusters)
        for nc in near_clusters:
            clusters.append(nc)
            total_duplicates += len(nc["duplicates"])
            reclaimable_bytes += nc["reclaimable_bytes"]

    return {
        "status": "success",
        "target_folder": target_folder,
        "total_clusters": len(clusters),
        "exact_clusters_count": exact_clusters_count,
        "near_clusters_count": near_clusters_count,
        "total_duplicates": total_duplicates,
        "reclaimable_bytes": reclaimable_bytes,
        "reclaimable_human": config.format_bytes_human(reclaimable_bytes),
        "clusters": clusters
    }


def find_forgotten_files(
    base_dir: str,
    target_folder: str = "Download",
    stale_days: float = 90.0,
    recursive: bool = True
) -> Dict[str, Any]:
    """
    Phase 6 "Forgotten Files Finder":
    Scans target_folder to identify forgotten storage bloat across 3 distinct categories:
    1. `temporary_files`: .tmp, .cache, .bak, .crdownload, .part, or temp_* files.
    2. `orphaned_screenshots`: Screenshot_* or Screen_Shot_* captures left in Download/.
    3. `stale_downloads`: Untouched downloads older than `stale_days` (default 90 days).
    """
    target_path = resolve_safe_path(target_folder) if target_folder else base_dir
    if not os.path.exists(target_path) or not os.path.isdir(target_path):
        return {
            "status": "success",
            "target_folder": target_folder,
            "stale_days_threshold": stale_days,
            "total_forgotten": 0,
            "reclaimable_bytes": 0,
            "reclaimable_human": "0 B",
            "stale_downloads": [],
            "temporary_files": [],
            "orphaned_screenshots": [],
            "items": []
        }

    now_dt = datetime.now()
    stale_downloads: List[Dict[str, Any]] = []
    temporary_files: List[Dict[str, Any]] = []
    orphaned_screenshots: List[Dict[str, Any]] = []
    all_forgotten: List[Dict[str, Any]] = []
    reclaimable_bytes = 0

    def _inspect_entry(abs_p: str, fname: str):
        nonlocal reclaimable_bytes
        if fname.startswith("."):
            return
        try:
            st = os.stat(abs_p)
            size = st.st_size
            mtime = st.st_mtime
            mtime_dt = datetime.fromtimestamp(mtime)
            age_days = round(max(0.0, (now_dt - mtime_dt).total_seconds() / 86400.0), 1)
            mtime_str = mtime_dt.strftime("%Y-%m-%d %H:%M")
        except Exception:
            return

        try:
            rel_path = os.path.relpath(abs_p, base_dir).replace("\\", "/")
        except Exception:
            rel_path = fname

        fn_lower = fname.lower()
        ext = os.path.splitext(fn_lower)[1]

        is_temp = (
            ext in TEMPORARY_EXTENSIONS
            or fn_lower.startswith(("temp_", "tmp_", "~$"))
            or fn_lower.endswith((".tmp", ".cache", ".bak", ".old"))
        )
        is_screenshot = (
            "screenshot" in fn_lower
            or "screen_shot" in fn_lower
            or fn_lower.startswith("screen_")
        )
        is_stale = age_days >= stale_days

        if not (is_temp or is_screenshot or is_stale):
            return

        if is_temp:
            cat = "TEMPORARY_FILE"
            reason = f"Temporary cache/sync artifact ({ext or 'temp'})"
            suggested_action = "trash"
            suggested_dest = f".agent_trash/{fname}"
        elif is_screenshot:
            cat = "ORPHANED_SCREENSHOT"
            reason = f"Orphaned screen capture in {target_folder}/ ({age_days} days old)"
            suggested_action = "trash"
            suggested_dest = f".agent_trash/{fname}"
        else:
            cat = "STALE_DOWNLOAD"
            reason = f"Stale untouched file ({int(age_days)} days old >= {int(stale_days)}d threshold)"
            suggested_action = "trash"
            suggested_dest = f".agent_trash/{fname}"

        item_dict = {
            "filename": fname,
            "path": rel_path,
            "size_bytes": size,
            "size_human": config.format_bytes_human(size),
            "age_days": age_days,
            "modified_date": mtime_str,
            "category": cat,
            "reason": reason,
            "suggested_action": suggested_action,
            "suggested_destination": suggested_dest
        }

        if is_temp:
            temporary_files.append(item_dict)
        elif is_screenshot:
            orphaned_screenshots.append(item_dict)
        else:
            stale_downloads.append(item_dict)

        all_forgotten.append(item_dict)
        reclaimable_bytes += size

    if recursive:
        for root, dirs, files in os.walk(target_path):
            dirs[:] = [d for d in dirs if not d.startswith(".") and d.lower() != "android" and d != ".agent_trash"]
            for fname in files:
                _inspect_entry(os.path.join(root, fname), fname)
    else:
        for entry in os.scandir(target_path):
            if entry.is_file():
                _inspect_entry(entry.path, entry.name)

    return {
        "status": "success",
        "target_folder": target_folder,
        "stale_days_threshold": stale_days,
        "total_forgotten": len(all_forgotten),
        "reclaimable_bytes": reclaimable_bytes,
        "reclaimable_human": config.format_bytes_human(reclaimable_bytes),
        "stale_downloads": stale_downloads,
        "temporary_files": temporary_files,
        "orphaned_screenshots": orphaned_screenshots,
        "items": all_forgotten
    }
