import os
import re
import json
import uuid
from collections import defaultdict
from typing import Dict, Any, List, Optional

from storage_copilot import config
from storage_copilot.execution.common import _load_profile_context
from storage_copilot.storage.scanner import scan_directory_manifest, resolve_target_directory
from storage_copilot.storage.content_extractor import inspect_file_health
from storage_copilot.storage.deduplication import find_duplicate_clusters, find_forgotten_files
from storage_copilot.storage.collision_resolver import ensure_unique_plan_destinations
from storage_copilot.storage.namer import ContentDerivedNamer, extract_course_code
from storage_copilot.intelligence.session_context import session_context_store
from storage_copilot.intelligence.document_classifier import classify_document
from storage_copilot.intelligence.gemini_client import (
    call_gemini_json,
    is_gemini_available,
    get_last_fallback_info
)
from storage_copilot.execution.plan_builder import format_plan_badges
from storage_copilot.advice.advisor import (
    get_format_category,
    get_unorganized_download_files
)


def handle_file_operation_turn(
    prompt: str,
    classification: Dict[str, Any],
    target_folder: str = "Download",
    conversation_history: Optional[List[Dict[str, Any]]] = None
) -> Dict[str, Any]:
    """
    Tier 3 File Operation Handler:
    - Synthesizes declarative JSON Action Plans with 1-Tap Undo guarantees.
    - Supports Forgotten Files cleanup, Exact & Near Deduplication, Format Batching,
      Corrupted File Quarantine (Documents/Review/Damaged/), and Academic Organization.
    - Enforces zero-match guards (returns No-Op conversation turns when 0 files match).
    - Sets degraded_fallback, fallback_reason, and fallback_notice on offline execution.
    """
    ctx = _load_profile_context()
    owner_name = ctx["owner_name"]
    peer_names = ctx["peer_names"]
    user_profile = ctx["profile"]
    namer = ContentDerivedNamer(user_profile=user_profile)

    sub_intent = classification.get("sub_intent", "")
    raw_prompt = classification.get("raw_prompt", prompt)
    resolved_goal = classification.get("resolved_goal") or classification.get("normalized_prompt") or raw_prompt
    lower_p = resolved_goal.lower()
    fb_diag = get_last_fallback_info()

    # 1a. Phase 6 Forgotten Files Clean Sweep
    if sub_intent == "forgotten_clean":
        days_match = re.search(r"(\d+)\s*(?:day|days)", lower_p)
        stale_days = float(days_match.group(1)) if days_match else 90.0
        forg_res = find_forgotten_files(config.BASE_STORAGE_DIR, target_folder=target_folder, stale_days=stale_days)
        if forg_res["total_forgotten"] == 0:
            msg = f"✨ **No forgotten or stale files found in `{target_folder}/` to clean!**"
            return {
                "type": "conversation",
                "intent": "FILE_OPERATION",
                "message": msg,
                "analysis": msg,
                "version": config.APP_VERSION,
                "model_source": "deterministic-engine",
                "is_llm": False,
                "degraded_fallback": True,
                "fallback_reason": fb_diag["fallback_reason"],
                "fallback_notice": fb_diag["fallback_notice"],
                "actions": [],
                "plan": [],
                "badges": ["Forgotten Files", "Clean"],
                "forgotten_summary": forg_res,
                "quick_chips": [
                    "📋 Sort Student Vouchers",
                    "👥 Separate Peer Documents",
                    "🧹 Clean Duplicates"
                ]
            }

        actions = []
        for idx, item in enumerate(forg_res["items"], 1):
            actions.append({
                "action_id": f"step-{idx}",
                "type": "trash",
                "badge": "TRASH",
                "badge_label": "Soft Delete",
                "source": item["path"],
                "path": item["path"],
                "destination": item["suggested_destination"],
                "rationale": f"{item['reason']} ({item['size_human']} reclaimed)",
                "ai_inspected": False
            })
        plan_id = str(uuid.uuid4())
        plan_desc = f"Forgotten Files Cleanup: Soft-delete {len(actions)} stale/temporary file(s) ({forg_res['reclaimable_human']})"
        msg = (
            f"Identified **{len(actions)} forgotten file(s)** ({len(forg_res['stale_downloads'])} stale downloads, "
            f"{len(forg_res['temporary_files'])} temp files, {len(forg_res['orphaned_screenshots'])} orphaned screenshots) "
            f"totalling **`{forg_res['reclaimable_human']}`**.\n\n"
            "Tap **Execute Action Plan** to move them safely to `.agent_trash` with 1-tap rollback."
        )
        return {
            "type": "plan",
            "intent": "FILE_OPERATION",
            "message": msg,
            "analysis": msg,
            "plan": {
                "plan_id": plan_id,
                "description": plan_desc,
                "actions": actions
            },
            "actions": actions,
            "badges": ["Action Plan", f"{len(actions)} Trash"],
            "forgotten_summary": forg_res,
            "version": config.APP_VERSION,
            "model_source": "deterministic-engine",
            "is_llm": False,
            "degraded_fallback": True,
            "fallback_reason": fb_diag["fallback_reason"],
            "fallback_notice": fb_diag["fallback_notice"],
            "quick_chips": [
                "📋 Sort Student Vouchers",
                "👥 Separate Peer Documents",
                "🏷️ Clean Download Names"
            ]
        }

    # 1b. Deduplication Clean Sweep (Exact SHA-256 & Near-Duplicate Jaccard)
    if sub_intent in ("deduplicate_clean", "deduplicate_near_clean"):
        session_ctx = session_context_store.get_context(target_folder)
        include_near = (
            sub_intent == "deduplicate_near_clean"
            or bool(classification.get("include_near"))
            or (bool(session_ctx.get("include_near")) and int(session_ctx.get("near_duplicates_count", 0)) > 0 and int(session_ctx.get("exact_duplicates_count", 0)) == 0)
        )
        dup_res = find_duplicate_clusters(config.BASE_STORAGE_DIR, target_folder, include_near=include_near)
        if dup_res["total_duplicates"] == 0 and not include_near:
            near_res = find_duplicate_clusters(config.BASE_STORAGE_DIR, target_folder, include_near=True)
            if near_res["total_duplicates"] > 0:
                msg = (
                    f"✨ **No exact duplicate files detected in `{target_folder}/`!**\n\n"
                    f"However, **{near_res['total_duplicates']} near-duplicate document(s)** with similar content (3-gram Jaccard $\\ge 85\\%$) were found.\n\n"
                    "💡 *To clean these revised copies, say **'Clean near duplicate downloads'** or tap the button below.*"
                )
                return {
                    "type": "conversation",
                    "intent": "FILE_OPERATION",
                    "message": msg,
                    "analysis": msg,
                    "version": config.APP_VERSION,
                    "model_source": "deterministic-engine",
                    "is_llm": False,
                    "degraded_fallback": True,
                    "fallback_reason": fb_diag["fallback_reason"],
                    "fallback_notice": fb_diag["fallback_notice"],
                    "actions": [],
                    "plan": [],
                    "badges": ["Deduplication", "Near-Duplicates Available"],
                    "duplicate_summary": near_res,
                    "quick_chips": [
                        "🧹 Clean near duplicate downloads",
                        "📋 Sort Student Vouchers",
                        "👥 Separate Peer Documents"
                    ]
                }

        if dup_res["total_duplicates"] == 0:
            msg = (
                f"✨ **No duplicate files detected in `{target_folder}/`!**\n\n"
                "All documents possess unique cryptographic signatures. No redundant copies were found to clean."
            )
            return {
                "type": "conversation",
                "intent": "FILE_OPERATION",
                "message": msg,
                "analysis": msg,
                "version": config.APP_VERSION,
                "model_source": "deterministic-engine",
                "is_llm": False,
                "degraded_fallback": True,
                "fallback_reason": fb_diag["fallback_reason"],
                "fallback_notice": fb_diag["fallback_notice"],
                "actions": [],
                "plan": [],
                "badges": ["Deduplication", "Clean"],
                "duplicate_summary": dup_res,
                "quick_chips": [
                    "📋 Sort Student Vouchers",
                    "👥 Separate Peer Documents",
                    "🏷️ Clean Download Names",
                    "🗑️ Cleanup Old Screenshots"
                ]
            }

        actions = []
        for cluster in dup_res["clusters"]:
            keeper = cluster["keeper"]
            m_type = cluster.get("match_type", "EXACT_SHA256")
            is_near_cluster = (m_type == "NEAR_JACCARD")
            badge_label = "Near-Duplicate Soft Delete" if is_near_cluster else "Soft Delete"
            for dup in cluster["duplicates"]:
                rat = (
                    f"Near-duplicate ({int(cluster.get('similarity', 0.85) * 100)}% Jaccard) of '{keeper['filename']}'"
                    if is_near_cluster
                    else f"{dup.get('reason', 'Redundant duplicate')} of '{keeper['filename']}'"
                )
                actions.append({
                    "action_id": f"step-{len(actions) + 1}",
                    "type": "trash",
                    "badge": "TRASH",
                    "badge_label": badge_label,
                    "source": dup["path"],
                    "path": dup["path"],
                    "destination": f".agent_trash/{dup['filename']}",
                    "rationale": rat,
                    "ai_inspected": True,
                    "inspected_preview": f"Matched against keeper {keeper['filename']}"
                })
        plan_id = str(uuid.uuid4())
        plan_desc = f"Deduplication: Clean {len(actions)} redundant duplicate file(s) ({dup_res['reclaimable_human']})"
        msg = (
            f"Identified **{len(actions)} redundant duplicate file(s)** across {dup_res['total_clusters']} group(s). "
            f"Cleaning them will reclaim **{dup_res['reclaimable_human']}** of storage while safely preserving all original canonical files.\n\n"
            "Review the proposed removals below. Tap **Execute Action Plan** to move redundant copies to `.agent_trash` (with 1-tap rollback at any time)."
        )
        return {
            "type": "plan",
            "intent": "FILE_OPERATION",
            "message": msg,
            "analysis": msg,
            "plan": {
                "plan_id": plan_id,
                "description": plan_desc,
                "actions": actions
            },
            "actions": actions,
            "badges": ["Action Plan", f"{len(actions)} Trash"],
            "duplicate_summary": dup_res,
            "version": config.APP_VERSION,
            "model_source": "deterministic-engine",
            "is_llm": False,
            "degraded_fallback": True,
            "fallback_reason": fb_diag["fallback_reason"],
            "fallback_notice": fb_diag["fallback_notice"],
            "quick_chips": [
                "📋 Sort Student Vouchers",
                "👥 Separate Peer Documents",
                "🏷️ Clean Download Names"
            ]
        }

    # 1c. Corrupted / Damaged Files Quarantine
    is_damage_quarantine_prompt = sub_intent == "damaged_quarantine" or any(w in lower_p for w in ("damaged", "corrupt", "quarantine"))
    if is_damage_quarantine_prompt:
        scan_data = scan_directory_manifest(target_folder=target_folder, query_prompt=raw_prompt)
        manifest = list(scan_data["manifest"])

        # FIX 6 (P1): Expand quarantine scope to match damaged-notice scope across Download/ and Documents/
        other_folder = "Documents" if target_folder.lower() == "download" else "Download"
        other_dir = os.path.join(config.BASE_STORAGE_DIR, other_folder)
        if os.path.exists(other_dir) and os.path.isdir(other_dir):
            other_scan = scan_directory_manifest(target_folder=other_folder, query_prompt=raw_prompt)
            existing_paths = {m["full_path"] for m in manifest}
            for itm in other_scan.get("manifest", []):
                if itm["full_path"] not in existing_paths:
                    manifest.append(itm)
                    existing_paths.add(itm["full_path"])

        damaged_files = []
        for itm in manifest:
            f_rel = itm.get("path", "").replace("\\", "/")
            if f_rel.startswith("Documents/Review/Damaged"):
                continue
            health = inspect_file_health(itm.get("full_path", ""))
            if health.get("is_corrupt"):
                damaged_files.append((itm, health.get("reason", "Corrupted file structure")))

        if not damaged_files:
            msg = f"✨ **No corrupted or damaged files detected in `{target_folder}/`!**"
            return {
                "type": "conversation",
                "intent": "FILE_OPERATION",
                "message": msg,
                "analysis": msg,
                "version": config.APP_VERSION,
                "model_source": "deterministic-engine",
                "is_llm": False,
                "degraded_fallback": True,
                "fallback_reason": fb_diag["fallback_reason"],
                "fallback_notice": fb_diag["fallback_notice"],
                "actions": [],
                "plan": [],
                "badges": ["Integrity Check", "Clean"],
                "quick_chips": [
                    "📋 Sort Student Vouchers",
                    "👥 Separate Peer Documents",
                    "🧹 Clean Duplicates"
                ]
            }

        actions = []
        for idx, (itm, reason) in enumerate(damaged_files, 1):
            dest_rel = f"Documents/Review/Damaged/{itm['name']}"
            actions.append({
                "action_id": f"step-{idx}",
                "type": "move",
                "badge": "CORRUPT_QUARANTINE",
                "badge_label": "Quarantine",
                "source": itm["path"],
                "path": itm["path"],
                "destination": dest_rel,
                "rationale": f"Corrupted file quarantine: {reason}",
                "ai_inspected": False
            })

        plan_id = str(uuid.uuid4())
        plan_desc = f"Quarantine Damaged Files: Move {len(actions)} damaged file(s) to Documents/Review/Damaged/"
        msg = (
            f"Identified **{len(actions)} damaged or corrupted file(s)** in `{target_folder}/`.\n\n"
            f"Tap **Execute Action Plan** to safely isolate them into `Documents/Review/Damaged/`."
        )
        return {
            "type": "plan",
            "intent": "FILE_OPERATION",
            "message": msg,
            "analysis": msg,
            "plan": {
                "plan_id": plan_id,
                "description": plan_desc,
                "actions": actions
            },
            "actions": actions,
            "badges": ["Action Plan", f"{len(actions)} Quarantine"],
            "version": config.APP_VERSION,
            "model_source": "deterministic-engine",
            "is_llm": False,
            "degraded_fallback": True,
            "fallback_reason": fb_diag["fallback_reason"],
            "fallback_notice": fb_diag["fallback_notice"],
            "quick_chips": [
                "📋 Sort Student Vouchers",
                "👥 Separate Peer Documents",
                "🧹 Clean Duplicates"
            ]
        }

    # 2. Format Batch Execution
    if sub_intent == "format_batch_execute":
        target_dir = resolve_target_directory(target_folder)
        unorg_files = get_unorganized_download_files(target_dir)

        # FIX 1 (P0): Honor file-type constraint if specified in prompt
        gather_exts = None
        if re.search(r"\b(?:pdfs?|\.pdf)\b", lower_p):
            gather_exts = {".pdf"}
        elif re.search(r"\b(?:apks?|apps?|\.apk|\.xapk)\b", lower_p):
            gather_exts = {".apk", ".xapk", ".apks"}
        elif re.search(r"\b(?:images?|photos?|pictures?|\.png|\.jpg|\.jpeg|\.webp|\.gif)\b", lower_p):
            gather_exts = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
        elif re.search(r"\b(?:videos?|movies?|\.mp4|\.mkv|\.avi|\.mov)\b", lower_p):
            gather_exts = {".mp4", ".mkv", ".avi", ".mov"}
        elif re.search(r"\b(?:audios?|music|songs?|\.mp3|\.wav|\.m4a|\.flac)\b", lower_p):
            gather_exts = {".mp3", ".wav", ".m4a", ".flac"}
        elif re.search(r"\b(?:docs?|documents?|\.docx?|\.doc|\.txt)\b", lower_p) and not any(k in lower_p for k in ("voucher", "receipt", "bill", "archive")):
            gather_exts = {".doc", ".docx", ".pdf", ".txt"}
        elif re.search(r"\b(?:zips?|archives?|\.zip|\.rar|\.7z|\.tar|\.gz)\b", lower_p):
            gather_exts = {".zip", ".rar", ".7z", ".tar", ".gz"}
        elif re.search(r"\b(?:scripts?|code|programs?|\.py|\.js|\.html|\.css)\b", lower_p):
            gather_exts = {".py", ".js", ".html", ".css"}

        if gather_exts:
            unorg_files = [f for f in unorg_files if os.path.splitext(f.name)[1].lower() in gather_exts]

        if not unorg_files:
            msg = f"✨ **All files in `{target_folder}/` are already organized!**\n\nNo loose unorganized files matching your criteria remain."
            return {
                "type": "conversation",
                "intent": "FILE_OPERATION",
                "message": msg,
                "analysis": msg,
                "version": config.APP_VERSION,
                "model_source": "deterministic-engine",
                "is_llm": False,
                "degraded_fallback": True,
                "fallback_reason": fb_diag["fallback_reason"],
                "fallback_notice": fb_diag["fallback_notice"],
                "actions": [],
                "plan": [],
                "badges": ["Format Sort", "Complete"],
                "quick_chips": [
                    "View organized folders",
                    "🧹 Clean Duplicates",
                    "📋 Sort Student Vouchers"
                ]
            }

        # FIX 1 (P0): Strict 20-action batch cap
        batch_size = min(20, getattr(config, "DEFAULT_MAX_ACTIONS_PER_BATCH", 20))
        batch_slice = unorg_files[:batch_size]
        remaining = len(unorg_files) - len(batch_slice)
        actions = []
        for idx, f in enumerate(batch_slice, 1):
            cat = get_format_category(f.name)
            dest_folder = cat["folder"]
            actions.append({
                "action_id": f"step-{idx}",
                "type": "move",
                "badge": "FILE_MOVE",
                "badge_label": f"Move to {dest_folder}",
                "source": f"{target_folder}/{f.name}",
                "destination": f"{target_folder}/{dest_folder}/{f.name}",
                "rationale": f"Sort {cat['label']} into dedicated {target_folder}/{dest_folder}/ subfolder",
                "ai_inspected": False
            })

        # FIX 1 (P0): Present breakdown table first in plan message
        cat_counts = defaultdict(int)
        for f in batch_slice:
            cat = get_format_category(f.name)
            cat_counts[cat["folder"]] += 1
        rows = []
        for cat_name, info in FORMAT_CATEGORIES.items():
            cnt = cat_counts.get(info["folder"], 0)
            if cnt > 0:
                rows.append(f"| {info['emoji']} **{info['label']}** | `{target_folder}/{info['folder']}/` | **{cnt} files** |")
        if cat_counts.get("Other", 0) > 0:
            rows.append(f"| 📁 **Other Files** | `{target_folder}/Other/` | **{cat_counts['Other']} files** |")
        table_md = "\n".join(rows)

        plan_id = str(uuid.uuid4())
        batch_info = {
            "batch_size": len(actions),
            "remaining_files": remaining,
            "total_unorganized": len(unorg_files),
            "target_folder": target_folder,
            "mode": "format_sorting"
        }
        plan_desc = f"Format Sorting: Batch of {len(actions)} files into {target_folder}/ subfolders ({remaining} remaining)"
        msg = (
            f"### 📊 Format Breakdown\n{table_md}\n\n"
            f"Staging **Batch of {len(actions)} files** to move into dedicated format folders (`{target_folder}/PDFs/`, `{target_folder}/APKs/`, etc.).\n\n"
            f"- **Remaining files after this batch:** **{remaining} files**\n\n"
            "Review the proposed moves below and tap **Approve Selected** to execute."
        )
        return {
            "type": "plan",
            "intent": "FILE_OPERATION",
            "message": msg,
            "analysis": msg,
            "plan": {
                "plan_id": plan_id,
                "description": plan_desc,
                "actions": actions,
                "batch_info": batch_info
            },
            "actions": actions,
            "batch_info": batch_info,
            "badges": ["Action Plan", f"{len(actions)} Moves"],
            "version": config.APP_VERSION,
            "model_source": "deterministic-engine",
            "is_llm": False,
            "degraded_fallback": True,
            "fallback_reason": fb_diag["fallback_reason"],
            "fallback_notice": fb_diag["fallback_notice"],
            "quick_chips": [
                "⏩ Process Next Batch",
                "View organized folders",
                "🧹 Clean Duplicates"
            ]
        }

    scan_data = scan_directory_manifest(target_folder=target_folder, query_prompt=raw_prompt)
    manifest = scan_data["manifest"]
    manifest_lookup = scan_data["manifest_lookup"]

    # Gather Extension & Category Pre-Filters (P0 #2 & #7)
    gather_exts = None
    if re.search(r"\b(?:gather|collect|move|sort|organize|group)\s+(?:all\s+)?pdfs?\b|\bonly\s+pdfs?\b|\ball\s+pdfs?\b|\bpdfs?\s+only\b|\b(?:gather|collect)\s+pdfs?\b", lower_p):
        gather_exts = {".pdf"}
    elif re.search(r"\b(?:gather|collect|move|sort|organize|group)\s+(?:all\s+)?apks?\b|\bonly\s+apks?\b|\ball\s+apks?\b|\bapks?\s+only\b|\b(?:gather|collect)\s+apks?\b", lower_p):
        gather_exts = {".apk", ".xapk", ".apks"}
    elif re.search(r"\b(?:gather|collect|move|sort|organize|group)\s+(?:all\s+)?(?:images?|photos?)\b|\bonly\s+(?:images?|photos?)\b|\ball\s+(?:images?|photos?)\b|\b(?:gather|collect)\s+(?:images?|photos?)\b", lower_p):
        gather_exts = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
    elif re.search(r"\b(?:gather|collect|move|sort|organize|group)\s+(?:all\s+)?(?:videos?|movies?)\b|\bonly\s+(?:videos?|movies?)\b|\ball\s+(?:videos?|movies?)\b", lower_p):
        gather_exts = {".mp4", ".mkv", ".avi", ".mov"}
    elif re.search(r"\b(?:gather|collect|move|sort|organize|group)\s+(?:all\s+)?(?:audios?|music)\b|\bonly\s+(?:audios?|music)\b|\ball\s+(?:audios?|music)\b", lower_p):
        gather_exts = {".mp3", ".wav", ".m4a", ".flac"}
    elif re.search(r"\b(?:gather|collect|move|sort|organize|group)\s+(?:all\s+)?(?:docs?|documents?)\b|\bonly\s+docx?\b", lower_p) and not any(k in lower_p for k in ("voucher", "receipt", "bill", "archive")):
        gather_exts = {".doc", ".docx", ".pdf", ".txt"}
    elif re.search(r"\b(?:gather|collect|move|sort|organize|group)\s+(?:all\s+)?(?:zips?|archives?)\b|\bonly\s+zips?\b|\ball\s+zips?\b", lower_p):
        gather_exts = {".zip", ".rar", ".7z", ".tar", ".gz"}
    elif re.search(r"\b(?:gather|collect|move|sort|organize|group)\s+(?:all\s+)?(?:scripts?|code|py)\b", lower_p):
        gather_exts = {".py", ".js", ".html", ".css"}

    gather_cat = None
    if re.search(r"\b(?:gather|collect|organize|find)\s+(?:my\s+|all\s+)?(?:receipts?|invoices?)\b", lower_p) and not any(k in lower_p for k in ("voucher", "fee", "challan")):
        gather_cat = "RECEIPTS"
    elif re.search(r"\b(?:gather|collect|organize|find)\s+(?:my\s+|all\s+)?(?:certificates?|certifications?)\b", lower_p):
        gather_cat = "CERTIFICATES"

    if gather_exts:
        manifest = [item for item in manifest if os.path.splitext(item.get("name", ""))[1].lower() in gather_exts]
    if gather_cat == "RECEIPTS":
        manifest = [
            item for item in manifest
            if re.search(r"\b(?:receipt|invoice)\b", item.get("name", "").lower())
            or re.search(r"\b(?:receipt|invoice)\b", (item.get("snippet") or "").lower())
        ]
    elif gather_cat == "CERTIFICATES":
        manifest = [
            item for item in manifest
            if re.search(r"\b(?:certificate|certification)\b", item.get("name", "").lower())
            or re.search(r"\b(?:certificate|certification)\b", (item.get("snippet") or "").lower())
        ]

    if (gather_exts or gather_cat) and not manifest:
        constraint_str = list(gather_exts)[0] if gather_exts else (gather_cat or "matching")
        msg = f"ℹ️ No eligible documents found in `{target_folder}/` matching {constraint_str} (\"{raw_prompt}\").\n\nZero matching files exist to organize."
        return {
            "type": "conversation",
            "intent": "FILE_OPERATION",
            "message": msg,
            "analysis": msg,
            "version": config.APP_VERSION,
            "model_source": "offline-heuristic",
            "is_llm": False,
            "degraded_fallback": True,
            "fallback_reason": fb_diag["fallback_reason"],
            "fallback_notice": fb_diag["fallback_notice"],
            "actions": [],
            "plan": [],
            "badges": ["No-Op"],
            "quick_chips": [
                "📋 Sort Student Vouchers",
                "👥 Separate Peer Documents",
                "🏷️ Clean Download Names",
                "🗑️ Cleanup Old Screenshots"
            ]
        }

    use_deterministic_first = any(k in lower_p for k in (
        "university", "academic", "voucher", "fee", "challan", "peer", "separate",
        "clean download names", "older than", "days old", "screenshot", "cleanup"
    ))

    if not use_deterministic_first and is_gemini_available():
        compact_manifest = [
            {
                "name": f["filename"],
                "path": f["path"],
                "size_kb": f["size_kb"],
                "date": f["modified_date"],
                "age": f["relative_age"],
                "snippet": (f.get("snippet") or "")[:200]
            }
            for f in manifest[:60]
            if not inspect_file_health(f.get("full_path", "")).get("is_corrupt")
        ]
        sys_prompt = f"""You are Storage Copilot generating a safe JSON Action Plan for {owner_name}.
Known Peers: {', '.join(peer_names)}.
Peer Routing:
{ctx['peer_matrix_lines']}
Candidate Files in '{target_folder}/':
{json.dumps(compact_manifest, separators=(',', ':'))}

Rules:
- Academic study materials -> Documents/University/<Subject>/
- Personal tuition vouchers -> Documents/University/Vouchers/
- Peer documents -> Documents/Peers/<PeerName>/
- Utility bills -> Documents/Finances/
- NEVER touch Identity Passes ('Sumbal pass.pdf', 'Yousaf Pass.pdf') when organizing university study materials!
- Never use generic 'Document_2025-04-13.pdf' names.
- Never use placeholder folder names like 'Peer_Name'; if peer cannot be identified, route to Documents/Review/.
Return JSON:
{{
  "type": "plan",
  "message": "...",
  "actions": [{{"action_id": "step-1", "type": "move"|"trash"|"make_dir", "source": "...", "destination": "...", "rationale": "..."}}]
}}
"""
        gemini_resp = call_gemini_json(sys_prompt, f"Goal: {resolved_goal}")
        if gemini_resp and isinstance(gemini_resp, dict) and gemini_resp.get("actions"):
            raw_actions = gemini_resp.get("actions") or []
            manifest_paths = {m["path"].lower(): m["path"] for m in manifest}
            manifest_names = {m["filename"].lower(): m["path"] for m in manifest}
            validated = []
            for act in raw_actions:
                act_type = act.get("type")
                if act_type == "make_dir":
                    p_path = act.get("path", "")
                    if any(ph in p_path for ph in ("Peer_Name", "<PeerName>", "<Peer_Name>", "<Subject>", "<Category>", "PeerName")):
                        act["path"] = "Documents/Review"
                    validated.append(act)
                elif act_type in ("move", "copy", "trash"):
                    src = act.get("source") or act.get("path") or ""
                    matched_src = manifest_paths.get(src.lower()) or manifest_names.get(os.path.basename(src).lower())
                    if matched_src:
                        # FIX 1 (P0): Honor gather_exts constraint in LLM actions
                        if gather_exts:
                            src_ext = os.path.splitext(matched_src)[1].lower()
                            if src_ext not in gather_exts:
                                continue
                        full_s = os.path.join(config.BASE_STORAGE_DIR, matched_src)
                        # FIX 7: Never route corrupt files in organise plans
                        if inspect_file_health(full_s).get("is_corrupt"):
                            continue
                        dst = act.get("destination", "")
                        if any(ph in dst for ph in ("Peer_Name", "<PeerName>", "<Peer_Name>", "<Subject>", "<Category>", "PeerName")):
                            act["destination"] = f"Documents/Review/{os.path.basename(dst)}"
                        if act_type in ("move", "copy"):
                            act["source"] = matched_src
                        else:
                            act["path"] = matched_src
                        validated.append(act)

            if validated:
                # FIX 1 (P0): Strict 20-action batch cap on LLM generated plan
                MAX_BATCH_ACTIONS = getattr(config, "DEFAULT_MAX_ACTIONS_PER_BATCH", 20)
                file_acts = [a for a in validated if a.get("type") in ("move", "copy", "trash", "delete")]
                rem_batch_count = 0
                if len(file_acts) > MAX_BATCH_ACTIONS:
                    allowed_sources = {a.get("source") or a.get("path") for a in file_acts[:MAX_BATCH_ACTIONS]}
                    rem_batch_count = len(file_acts) - MAX_BATCH_ACTIONS
                    capped_actions = []
                    for a in validated:
                        if a.get("type") == "make_dir":
                            capped_actions.append(a)
                        elif (a.get("source") or a.get("path")) in allowed_sources:
                            capped_actions.append(a)
                    validated = capped_actions

                validated = ensure_unique_plan_destinations(validated)
                formatted_actions = format_plan_badges(validated, peer_names, manifest_lookup)
                plan_id = str(uuid.uuid4())
                resp_msg = gemini_resp.get("message") or f"Synthesized Action Plan for: {resolved_goal}"

                # FIX 1 (P0): Present breakdown table first in plan message
                cat_counts = defaultdict(int)
                for a in [act for act in formatted_actions if act.get("type") in ("move", "copy", "trash")]:
                    ext = os.path.splitext(a.get("source") or a.get("path") or "")[1].upper().replace(".", "")
                    cat_counts[ext or "Files"] += 1
                rows = [f"| 📄 **{ext}** | **{cnt} files** |" for ext, cnt in cat_counts.items()]
                breakdown_md = "### 📊 File Breakdown\n" + "\n".join(rows) + "\n\n" if rows else ""
                batch_note = f" (showing first {MAX_BATCH_ACTIONS} files, {rem_batch_count} remaining in next batch)" if rem_batch_count > 0 else ""
                full_msg = f"{breakdown_md}{resp_msg}{batch_note}"

                return {
                    "type": "plan",
                    "intent": "FILE_OPERATION",
                    "message": full_msg,
                    "analysis": full_msg,
                    "version": config.APP_VERSION,
                    "model_source": gemini_resp.get("_model_used", "gemini-2.5-flash"),
                    "is_llm": True,
                    "degraded_fallback": False,
                    "fallback_reason": None,
                    "fallback_notice": None,
                    "actions": formatted_actions,
                    "badges": ["Action Plan", f"{len(formatted_actions)} Actions"],
                    "plan": {
                        "plan_id": plan_id,
                        "version": "1.0",
                        "description": full_msg,
                        "collision_strategy": "RENAME_NUMERIC",
                        "actions": formatted_actions
                    },
                    "quick_chips": [
                        "📋 Sort Student Vouchers",
                        "👥 Separate Peer Documents",
                        "🏷️ Clean Download Names",
                        "🧹 Clean Duplicates"
                    ]
                }

    # Deterministic Grounded Rule Engine
    plan_actions = []
    plan_desc = ""
    created_dirs = set()
    step_idx = 1

    if "voucher" in lower_p or "fee" in lower_p or "challan" in lower_p:
        plan_desc = "Organize student fee vouchers into academic and peer directories"
        for item in manifest:
            fn = item["name"].lower()
            snip = item["snippet"].lower()
            if not any(k in fn or k in snip for k in ("voucher", "fee", "challan", "slip")):
                continue

            peer_match = next((p for p in peer_names if p.lower() in fn or p.lower() in snip), None)
            if peer_match:
                peer_dir = f"Documents/Peers/{peer_match}"
                if peer_dir not in created_dirs:
                    plan_actions.append({"action_id": f"step-{step_idx}", "type": "make_dir", "path": peer_dir})
                    created_dirs.add(peer_dir)
                    step_idx += 1
                plan_actions.append({
                    "action_id": f"step-{step_idx}",
                    "type": "move",
                    "source": item["path"],
                    "destination": f"{peer_dir}/{item['name']}",
                    "peer_name": peer_match,
                    "rationale": f"Routed to peer folder for {peer_match} to avoid polluting personal storage"
                })
                step_idx += 1
            else:
                target_d = "Documents/University/Vouchers"
                if target_d not in created_dirs:
                    plan_actions.append({"action_id": f"step-{step_idx}", "type": "make_dir", "path": target_d})
                    created_dirs.add(target_d)
                    step_idx += 1
                plan_actions.append({
                    "action_id": f"step-{step_idx}",
                    "type": "move",
                    "source": item["path"],
                    "destination": f"{target_d}/{item['name']}",
                    "rationale": f"Personal voucher for {owner_name} filed under University Vouchers"
                })
                step_idx += 1

    elif "peer" in lower_p or "separate" in lower_p:
        plan_desc = f"Separate peer documents from {owner_name}'s personal storage"
        for item in manifest:
            fn = item["name"].lower()
            snip = item["snippet"].lower()
            peer_match = next((p for p in peer_names if p.lower() in fn or p.lower() in snip), None)
            if peer_match:
                peer_dir = f"Documents/Peers/{peer_match}"
                if peer_dir not in created_dirs:
                    plan_actions.append({"action_id": f"step-{step_idx}", "type": "make_dir", "path": peer_dir})
                    created_dirs.add(peer_dir)
                    step_idx += 1
                plan_actions.append({
                    "action_id": f"step-{step_idx}",
                    "type": "move",
                    "source": item["path"],
                    "destination": f"{peer_dir}/{item['name']}",
                    "peer_name": peer_match,
                    "rationale": f"Isolated document for peer {peer_match}"
                })
                step_idx += 1

    elif "clean" in lower_p and ("name" in lower_p or "download" in lower_p):
        plan_desc = "Standardize cryptic download filenames into structured folders"
        for item in manifest:
            fn = item["name"]
            if fn.endswith((".tmp", ".log")):
                continue
            if re.search(r"^(?:DOC-|\d{4}\.\d{4,5}|IMG_|WhatsApp|Screenshot|[a-f0-9]{16,})", fn, re.I) or ".." in fn or "wa0" in fn.lower():
                snip = item["snippet"] or ""
                snip_lower = snip.lower()

                if any(k in snip_lower for k in ("knowledge representation", "artificial intelligence", "lecture", "course", "assignment", "semester", "bsai", "lab ", "chapter ")):
                    subject = extract_course_code(snip, fn)
                    target_d = f"Documents/University/{subject}"
                    clean_title = namer.derive_academic_title(fn, snip, concise_mode=False)
                elif "1508" in fn or "neural" in snip_lower or "arxiv" in snip_lower:
                    target_d = "Documents/University/AI"
                    clean_title = namer.derive_academic_title(fn, snip, concise_mode=False)
                elif any(p_name.lower() in snip_lower or p_name.lower() in fn.lower() for p_name in peer_names):
                    p_found = next(p for p in peer_names if p.lower() in snip_lower or p.lower() in fn.lower())
                    target_d = f"Documents/Peers/{p_found}"
                    clean_title = namer.derive_peer_title(fn, p_found, snip, standardize_cryptic=True)
                elif "voucher" in snip_lower or "fee" in snip_lower:
                    target_d = "Documents/University/Vouchers"
                    clean_title = namer.derive_voucher_title(fn, snip, standardize_cryptic=True)
                elif "bill" in snip_lower or "invoice" in snip_lower or "utility" in snip_lower or "electricity" in snip_lower:
                    target_d = "Documents/Finances"
                    clean_title = namer.derive_financial_bill_title(fn, snip, concise_invoice=True)
                else:
                    target_d = "Documents/University/AI"
                    clean_title = namer.derive_academic_title(fn, snip, concise_mode=False)

                if target_d not in created_dirs:
                    plan_actions.append({"action_id": f"step-{step_idx}", "type": "make_dir", "path": target_d})
                    created_dirs.add(target_d)
                    step_idx += 1
                plan_actions.append({
                    "action_id": f"step-{step_idx}",
                    "type": "move",
                    "source": item["path"],
                    "destination": f"{target_d}/{clean_title}",
                    "badge": "RENAME",
                    "rationale": f"Semantically organized '{fn}' into {target_d} based on inspected content"
                })
                step_idx += 1

    elif any(k in lower_p for k in ("older than", "days old", "archive")) and any(v in lower_p for v in ("move", "archive", "transfer", "relocate")):
        days_match = re.search(r"(\d+)\s*(?:day|days)", lower_p)
        min_age_days = float(days_match.group(1)) if days_match else 30.0
        plan_desc = f"Archive files older than {int(min_age_days)} days into Documents/Archive/"
        target_d = "Documents/Archive"
        for item in manifest:
            if item.get("age_days", 0) >= min_age_days and not item["name"].startswith("."):
                if target_d not in created_dirs:
                    plan_actions.append({"action_id": f"step-{step_idx}", "type": "make_dir", "path": target_d})
                    created_dirs.add(target_d)
                    step_idx += 1
                plan_actions.append({
                    "action_id": f"step-{step_idx}",
                    "type": "move",
                    "source": item["path"],
                    "destination": f"{target_d}/{item['name']}",
                    "badge": "FILE_MOVE",
                    "rationale": f"Archived older file ({item.get('relative_age', 'older than threshold')}) to {target_d}"
                })
                step_idx += 1

    elif "screenshot" in lower_p or "cleanup" in lower_p or "trash" in lower_p:
        plan_desc = "Safely soft-delete obsolete screenshots and sync cache files to .agent_trash"
        for item in manifest:
            fn = item["name"].lower()
            if "screenshot" in fn or fn.endswith((".tmp", ".cache", ".bak")):
                plan_actions.append({
                    "action_id": f"step-{step_idx}",
                    "type": "trash",
                    "path": item["path"],
                    "rationale": "Temporary/screenshot file queued for soft-delete with rollback"
                })
                step_idx += 1

    elif any(re.search(rf"\b{re.escape(k)}\b", lower_p) for k in (
        "university", "academic", "lecture", "course", "study", "uni",
        "college", "campus", "school", "varsity", "homework", "hw", "slides", "notes", "assignment"
    )):
        plan_desc = f"Organize academic university course materials for {owner_name}"
        for item in manifest:
            doc_class = classify_document(item["name"], item["snippet"], user_profile=user_profile)
            if doc_class["is_academic_study"]:
                target_d = doc_class["target_dir"]
                clean_title = doc_class["suggested_title"]
                if target_d not in created_dirs:
                    plan_actions.append({"action_id": f"step-{step_idx}", "type": "make_dir", "path": target_d})
                    created_dirs.add(target_d)
                    step_idx += 1
                plan_actions.append({
                    "action_id": f"step-{step_idx}",
                    "type": "move",
                    "source": item["path"],
                    "destination": f"{target_d}/{clean_title}",
                    "badge": "RENAME" if clean_title != item["name"] else "FILE_MOVE",
                    "rationale": f"Organized academic study document into {target_d} based on inspected content",
                    "ai_inspected": True,
                    "inspected_preview": (item["snippet"] or "")[:100]
                })
                step_idx += 1

    else:
        # General organization
        detected_exts = set()
        if gather_exts:
            detected_exts.update(gather_exts)
        for ext in (".pdf", ".docx", ".pptx", ".xlsx", ".txt", ".png", ".jpg", ".jpeg", ".mp4", ".mp3", ".zip", ".py", ".apk"):
            ext_token = ext[1:]
            if ext in lower_p or f"{ext_token}s" in lower_p or f"{ext_token} files" in lower_p or re.search(rf"\b{ext_token}s?\b", lower_p):
                detected_exts.add(ext)

        target_items = [
            item for item in manifest
            if not detected_exts or item.get("extension", "").lower() in detected_exts
        ]

        if not target_items and detected_exts:
            plan_actions = []
        else:
            plan_desc = f"Sort and organize candidate documents in '{target_folder}' into structured taxonomy"
            for item in target_items:
                fn = item["name"]
                snip = item["snippet"] or ""
                if fn.startswith("."):
                    continue

                health = inspect_file_health(item.get("full_path", ""))
                if health.get("is_corrupt"):
                    # FIX 7 (P2): Files flagged corrupt must never appear in organise plans! They go to quarantine, not organise.
                    continue
                else:
                    matched_peer = next((p for p in peer_names if p.lower() in fn.lower() or p.lower() in snip.lower()), None)
                    if matched_peer:
                        target_d = f"Documents/Peers/{matched_peer}"
                        clean_title = fn
                        rationale = f"Routed to peer folder for {matched_peer} to protect personal storage"
                    else:
                        doc_class = classify_document(fn, snip, user_profile=user_profile)
                        target_d = doc_class["target_dir"]
                        clean_title = doc_class["suggested_title"]
                        if doc_class["category"] == "TEMPORARY_TRASH":
                            plan_actions.append({
                                "action_id": f"step-{step_idx}",
                                "type": "trash",
                                "path": item["path"],
                                "badge": "TRASH",
                                "rationale": "Temporary/screenshot file queued for soft-delete with rollback"
                            })
                            step_idx += 1
                            continue
                        rationale = f"Semantically categorized as {doc_class['type_label']} into {target_d}"

                if target_d not in created_dirs:
                    plan_actions.append({"action_id": f"step-{step_idx}", "type": "make_dir", "path": target_d})
                    created_dirs.add(target_d)
                    step_idx += 1

                plan_actions.append({
                    "action_id": f"step-{step_idx}",
                    "type": "move",
                    "source": item["path"],
                    "destination": f"{target_d}/{clean_title}",
                    "badge": "RENAME" if clean_title != fn else "FILE_MOVE",
                    "rationale": rationale,
                    "ai_inspected": True,
                    "inspected_preview": snip[:100]
                })
                step_idx += 1

    # Ensure at least one actual file mutation action exists
    file_actions = [
        a for a in plan_actions
        if a.get("type") in ("move", "copy", "trash", "delete")
    ]
    if not file_actions:
        msg = (
            f"ℹ️ No eligible documents found in `{target_folder}/` matching \"{raw_prompt}\".\n\n"
            f"Inspected {len(manifest)} real files on disk. None required modification under this criteria."
        )
        return {
            "type": "conversation",
            "intent": "FILE_OPERATION",
            "message": msg,
            "analysis": msg,
            "version": config.APP_VERSION,
            "model_source": "offline-heuristic",
            "is_llm": False,
            "degraded_fallback": True,
            "fallback_reason": fb_diag["fallback_reason"],
            "fallback_notice": fb_diag["fallback_notice"],
            "actions": [],
            "plan": [],
            "badges": ["No-Op"],
            "quick_chips": [
                "📋 Sort Student Vouchers",
                "👥 Separate Peer Documents",
                "🏷️ Clean Download Names",
                "🗑️ Cleanup Old Screenshots"
            ]
        }

    # FIX 1 (P0): Enforce 20-action batch cap
    MAX_BATCH_ACTIONS = getattr(config, "DEFAULT_MAX_ACTIONS_PER_BATCH", 20)
    file_acts = [a for a in plan_actions if a.get("type") in ("move", "copy", "trash", "delete")]
    rem_batch_count = 0
    if len(file_acts) > MAX_BATCH_ACTIONS:
        allowed_sources = {a.get("source") or a.get("path") for a in file_acts[:MAX_BATCH_ACTIONS]}
        rem_batch_count = len(file_acts) - MAX_BATCH_ACTIONS
        capped_actions = []
        for a in plan_actions:
            if a.get("type") == "make_dir":
                capped_actions.append(a)
            elif (a.get("source") or a.get("path")) in allowed_sources:
                capped_actions.append(a)
        plan_actions = capped_actions

    # FIX 7 (P2): Sanitize any placeholder/template folder names in final plan actions
    sanitized_actions = []
    for a in plan_actions:
        dst = a.get("destination") or a.get("path") or ""
        if any(ph in dst for ph in ("Peer_Name", "<PeerName>", "<Peer_Name>", "<Subject>", "<Category>", "PeerName")):
            cleaned_dest = re.sub(r"Documents/Peers/(?:Peer_Name|<PeerName>|<Peer_Name>|PeerName)/?", "Documents/Review/", dst)
            if not cleaned_dest.startswith("Documents/Review"):
                cleaned_dest = f"Documents/Review/{os.path.basename(dst)}"
            if a.get("destination"):
                a["destination"] = cleaned_dest
            if a.get("path") and a.get("type") == "make_dir":
                a["path"] = os.path.dirname(cleaned_dest) or "Documents/Review"
        sanitized_actions.append(a)
    plan_actions = sanitized_actions

    plan_actions = ensure_unique_plan_destinations(plan_actions)
    formatted_actions = format_plan_badges(plan_actions, peer_names, manifest_lookup)
    plan_id = str(uuid.uuid4())
    action_plan = {
        "plan_id": plan_id,
        "version": "1.0",
        "description": plan_desc,
        "collision_strategy": "RENAME_NUMERIC",
        "actions": formatted_actions
    }

    # FIX 1 (P0): Present breakdown table first in plan message
    cat_counts = defaultdict(int)
    for a in [act for act in formatted_actions if act.get("type") in ("move", "copy", "trash")]:
        ext = os.path.splitext(a.get("source") or a.get("path") or "")[1].upper().replace(".", "")
        cat_counts[ext or "Files"] += 1
    rows = [f"| 📄 **{ext}** | **{cnt} files** |" for ext, cnt in cat_counts.items()]
    breakdown_md = "### 📊 File Breakdown\n" + "\n".join(rows) + "\n\n" if rows else ""
    batch_note = f" (showing first {MAX_BATCH_ACTIONS} files, {rem_batch_count} remaining in next batch)" if rem_batch_count > 0 else ""
    msg = f"{breakdown_md}I've inspected your storage ({len(manifest)} real files on disk) and synthesized an Action Plan for: \"{plan_desc}\"{batch_note}"
    return {
        "type": "plan",
        "intent": "FILE_OPERATION",
        "message": msg,
        "analysis": msg,
        "version": config.APP_VERSION,
        "model_source": "offline-heuristic",
        "is_llm": False,
        "degraded_fallback": True,
        "fallback_reason": fb_diag["fallback_reason"],
        "fallback_notice": fb_diag["fallback_notice"],
        "actions": formatted_actions,
        "plan": action_plan,
        "badges": ["Action Plan", f"{len(formatted_actions)} Actions"],
        "quick_chips": [
            "⏩ Process Next Batch",
            "👥 Separate Peer Documents",
            "📋 Sort Student Vouchers",
            "🧹 Clean Duplicates"
        ]
    }
