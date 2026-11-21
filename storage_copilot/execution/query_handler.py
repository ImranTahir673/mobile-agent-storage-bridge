import os
import re
import json
from typing import Dict, Any, List, Optional

from storage_copilot import config
from storage_copilot.execution.common import _load_profile_context
from storage_copilot.security.pii_sanitizer import sanitize_pii
from storage_copilot.storage.scanner import scan_directory_manifest, resolve_target_directory
from storage_copilot.storage.content_extractor import extract_file_snippet, inspect_file_health
from storage_copilot.storage.deduplication import find_duplicate_clusters, find_forgotten_files
from storage_copilot.intelligence.session_context import session_context_store
from storage_copilot.intelligence.document_classifier import classify_document, parse_query_intent
from storage_copilot.intelligence.search_engine import score_file_relevance, compute_query_relevance_score
from storage_copilot.intelligence.gemini_client import (
    call_gemini_text,
    is_gemini_available,
    get_last_fallback_info
)
from storage_copilot.advice.advisor import (
    FORMAT_CATEGORIES,
    get_unorganized_download_files
)


def handle_query_info_turn(
    prompt: str,
    classification: Dict[str, Any],
    target_folder: str = "Download",
    conversation_history: Optional[List[Dict[str, Any]]] = None
) -> Dict[str, Any]:
    """
    Tier 2 Query Info Handler:
    - Performs fast, read-only reconnaissance across Download/ and Documents/ subtrees.
    - Ranks results with grounded relevance scoring, snippets, and match reasons.
    - Strictly enforces honest-empty: returns matches == [] when 0 candidates score >= 35.0.
    - Flags damaged / corrupt files with explicit user notes and quarantine quick chips.
    """
    ctx = _load_profile_context()
    owner_name = ctx["owner_name"]
    sub_intent = classification.get("sub_intent", "")
    raw_prompt = classification.get("raw_prompt", prompt)
    normalized_prompt = classification.get("normalized_prompt", prompt)
    lower_raw = raw_prompt.lower()
    lower_norm = normalized_prompt.lower()
    fb_diag = get_last_fallback_info()

    # 1a. Phase 6 Forgotten Files Audit
    if sub_intent == "forgotten_audit":
        days_match = re.search(r"(\d+)\s*(?:day|days)", lower_norm)
        stale_days = float(days_match.group(1)) if days_match else 90.0
        forg_res = find_forgotten_files(config.BASE_STORAGE_DIR, target_folder=target_folder, stale_days=stale_days)
        session_context_store.record_forgotten_audit(target_folder, forg_res, stale_days=stale_days)
        if forg_res["total_forgotten"] == 0:
            msg = (
                f"✨ **No forgotten or stale files detected in `{target_folder}/`!**\n\n"
                f"Scanned for downloads older than {int(stale_days)} days, temporary cache artifacts, and orphaned screenshots."
            )
            return {
                "type": "query_info",
                "intent": "QUERY_INFO",
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
                "badges": ["Forgotten Files", "Read-Only"],
                "forgotten_summary": forg_res,
                "quick_chips": [
                    "📋 Sort Student Vouchers",
                    "👥 Separate Peer Documents",
                    "🧹 Clean Duplicates"
                ]
            }

        lines = [
            f"### 🕰️ Forgotten Files Audit for `{target_folder}/`",
            f"Identified **{forg_res['total_forgotten']} forgotten or ephemeral file(s)** occupying **`{forg_res['reclaimable_human']}`**:\n",
            f"- 📦 **Stale Downloads ($\\\\ge {int(stale_days)}$ days):** {len(forg_res['stale_downloads'])} file(s)",
            f"- 🗑️ **Temporary Cache Artifacts:** {len(forg_res['temporary_files'])} file(s)",
            f"- 🖼️ **Orphaned Screenshots:** {len(forg_res['orphaned_screenshots'])} file(s)\n"
        ]
        for item in forg_res["items"][:15]:
            lines.append(f"- **{item['filename']}** (`{item['path']}`, {item['size_human']}) — *{item['reason']}*")
        lines.append("\n💡 *To safely sweep these into `.agent_trash` with 1-tap rollback, say **'Clean forgotten files'**.*")
        msg = "\n".join(lines)
        return {
            "type": "query_info",
            "intent": "QUERY_INFO",
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
            "badges": ["Forgotten Files", "Read-Only"],
            "forgotten_summary": forg_res,
            "quick_chips": [
                "🗑️ Clean forgotten files",
                "🧹 Clean duplicate downloads",
                "📋 Sort Student Vouchers"
            ]
        }

    # 1b. Deduplication Audit (Exact SHA-256 & Near-Duplicate Jaccard)
    if sub_intent in ("deduplicate_audit", "deduplicate_near_audit"):
        include_near = (sub_intent == "deduplicate_near_audit") or bool(classification.get("include_near"))
        dup_res = find_duplicate_clusters(config.BASE_STORAGE_DIR, target_folder, include_near=include_near)
        session_context_store.record_duplicate_audit(target_folder, dup_res, include_near_requested=include_near)
        if dup_res["total_duplicates"] == 0:
            msg = (
                f"✨ **No duplicate files detected in `{target_folder}/`!**\n\n"
                "All documents possess unique signatures. No redundant copies were found."
            )
            return {
                "type": "query_info",
                "intent": "QUERY_INFO",
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
                "badges": ["Search", "Read-Only"],
                "duplicate_summary": dup_res,
                "quick_chips": [
                    "📋 Sort Student Vouchers",
                    "👥 Separate Peer Documents",
                    "🏷️ Clean Download Names",
                    "🗑️ Cleanup Old Screenshots"
                ]
            }

        lines = [
            f"### 🧹 Duplicate File Audit for `{target_folder}/`",
            f"Found **{dup_res['total_duplicates']} redundant duplicate file(s)** across **{dup_res['total_clusters']} group(s)**.",
            f"**Total Reclaimable Storage:** `{dup_res['reclaimable_human']}`\n",
            "#### Detected Groups:"
        ]
        for idx, cluster in enumerate(dup_res["clusters"], 1):
            k = cluster["keeper"]
            m_type = cluster.get("match_type", "EXACT_SHA256")
            badge_str = "Exact SHA-256" if m_type == "EXACT_SHA256" else f"Near-Duplicate ({int(cluster.get('similarity', 0.85) * 100)}% Jaccard)"
            lines.append(f"**Group {idx} [{badge_str}]** (Size: {cluster['size_human']} each, Reclaimable: {cluster['reclaimable_human']}):")
            lines.append(f"- 🛡️ **Preserving Keeper:** `{k['path']}` *({k['reason']})*")
            for d in cluster["duplicates"]:
                lines.append(f"- 🗑️ **Redundant Copy:** `{d['path']}` *({d['reason']})*")
            lines.append("")
        lines.append("💡 *To clean up these redundant copies and reclaim space, say **'Clean duplicate downloads'** or tap **Clean Duplicates** below.*")
        msg = "\n".join(lines)
        return {
            "type": "query_info",
            "intent": "QUERY_INFO",
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
            "badges": ["Search", "Read-Only"],
            "query_results": dup_res["clusters"],
            "duplicate_summary": dup_res,
            "quick_chips": [
                "🧹 Clean duplicate downloads",
                "📋 Sort Student Vouchers",
                "👥 Separate Peer Documents",
                "🏷️ Clean Download Names"
            ]
        }

    # 2. View Organized Folders
    if sub_intent == "view_organized_folders":
        target_dir = resolve_target_directory(target_folder)
        dl_subfolders = []
        if os.path.exists(target_dir) and os.path.isdir(target_dir):
            for entry in os.scandir(target_dir):
                if entry.is_dir() and not entry.name.startswith("."):
                    cnt = sum(1 for e in os.scandir(entry.path) if e.is_file() and not e.name.startswith("."))
                    dl_subfolders.append((entry.name, cnt))

        doc_dir = os.path.join(config.BASE_STORAGE_DIR, "Documents")
        doc_subfolders = []
        if os.path.exists(doc_dir) and os.path.isdir(doc_dir):
            for root, dirs, files_in_dir in os.walk(doc_dir):
                dirs[:] = [d for d in dirs if not d.startswith(".")]
                rel_root = os.path.relpath(root, config.BASE_STORAGE_DIR).replace("\\", "/")
                file_count = sum(1 for f in files_in_dir if not f.startswith("."))
                if file_count > 0:
                    doc_subfolders.append((rel_root, file_count))

        unorg_files = get_unorganized_download_files(target_dir)
        lines = [
            f"### 📂 Organized Folders Status for {owner_name}\n",
            "Here is the verified status of organized folders across your storage:\n"
        ]
        if dl_subfolders:
            lines.append(f"#### 📥 `{target_folder}/` Format Subfolders:")
            for sname, scnt in dl_subfolders:
                cat = FORMAT_CATEGORIES.get(sname, {"emoji": "📁"})
                lines.append(f"- {cat.get('emoji', '📁')} `{target_folder}/{sname}/`: **{scnt} files**")
            lines.append("")
        else:
            lines.append(f"#### 📥 `{target_folder}/` Subfolders:\n- *No format subfolders created yet.*")

        if doc_subfolders:
            lines.append("#### 📁 `Documents/` Academic & Personal Taxonomy:")
            for proot, pcnt in doc_subfolders[:15]:
                lines.append(f"- 📂 `{proot}/`: **{pcnt} files**")
            lines.append("")

        if unorg_files:
            lines.append(f"📊 **Unorganized files remaining in `{target_folder}/` root:** **{len(unorg_files)} files**.")
            lines.append("💡 *Say **'Sort all the file types together in the downloads'** to group them into dedicated subfolders.*")
        else:
            lines.append(f"✨ **All files in `{target_folder}/` are organized!** No loose unorganized files remain.")

        msg = "\n".join(lines)
        chips = ["⏩ Process Next Batch", "🧹 Clean Duplicates", "📋 Sort Student Vouchers"] if unorg_files else ["🧹 Clean Duplicates", "📋 Sort Student Vouchers", "👥 Separate Peer Documents"]
        return {
            "type": "query_info",
            "intent": "QUERY_INFO",
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
            "badges": ["Search", "Read-Only"],
            "quick_chips": chips
        }

    scan_data = scan_directory_manifest(target_folder=target_folder, query_prompt=raw_prompt)
    manifest = list(scan_data["manifest"])

    # Expand search scope to include Documents/ subtree when searching Download (and vice versa)
    other_folder = "Documents" if target_folder.lower() == "download" else "Download"
    other_dir = os.path.join(config.BASE_STORAGE_DIR, other_folder)
    if os.path.exists(other_dir) and os.path.isdir(other_dir):
        other_scan = scan_directory_manifest(target_folder=other_folder, query_prompt=raw_prompt)
        existing_paths = {m["full_path"] for m in manifest}
        for itm in other_scan.get("manifest", []):
            if itm["full_path"] not in existing_paths:
                manifest.append(itm)
                existing_paths.add(itm["full_path"])

    # Inspect for damaged/corrupted files in storage
    corrupt_files = []
    for itm in manifest:
        health = inspect_file_health(itm.get("full_path", ""))
        if health.get("is_corrupt"):
            corrupt_files.append({"name": itm["name"], "path": itm["path"], "reason": health.get("reason", "Corrupt file")})

    parsed_q = parse_query_intent(normalized_prompt, default_folder=target_folder)

    # Temporal detection for <MonthName> <YYYY> or <YYYY-MM> (e.g. "October 2026" -> "2026-10")
    months_map = {
        "january": "01", "jan": "01",
        "february": "02", "feb": "02",
        "march": "03", "mar": "03",
        "april": "04", "apr": "04",
        "may": "05",
        "june": "06", "jun": "06",
        "july": "07", "jul": "07",
        "august": "08", "aug": "08",
        "september": "09", "sep": "09", "sept": "09",
        "october": "10", "oct": "10",
        "november": "11", "nov": "11",
        "december": "12", "dec": "12"
    }
    expected_ym = None
    ym_match = re.search(r"\b(\d{4})-(0[1-9]|1[0-2])\b", raw_prompt)
    if ym_match:
        expected_ym = f"{ym_match.group(1)}-{ym_match.group(2)}"
    else:
        m_regex = r"(?:january|february|march|april|may|june|july|august|september|october|november|december|jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec)"
        m1 = re.search(rf"\b({m_regex})\s+(\d{{4}})\b", raw_prompt, re.I)
        m2 = re.search(rf"\b(\d{{4}})\s+({m_regex})\b", raw_prompt, re.I)
        if m1:
            expected_ym = f"{m1.group(2)}-{months_map[m1.group(1).lower()]}"
        elif m2:
            expected_ym = f"{m2.group(1)}-{months_map[m2.group(2).lower()]}"

    is_oldest_req = any(w in lower_raw or w in lower_norm for w in ("oldest", "earliest", "first downloaded", "longest ago"))
    is_latest_req = any(w in lower_raw or w in lower_norm for w in ("latest", "newest", "most recent"))
    is_directory_inventory = parsed_q.get("is_directory_listing") or any(
        k in lower_norm for k in ("what is inside", "what's inside", "whats inside", "list of all the files", "list all files")
    )

    for item in manifest:
        rel_info = score_file_relevance(item, parsed_q, item.get("snippet", ""))
        item["relevance_score"] = rel_info["score"]
        item["is_relevant"] = rel_info["is_relevant"]
        item["match_reasons"] = rel_info["match_reasons"]
        item["query_relevance"] = compute_query_relevance_score(raw_prompt, item)

    sorted_by_newest = sorted(manifest, key=lambda x: x.get("mtime", 0.0), reverse=True)

    matched_items = []
    if expected_ym:
        matched_items = [
            f for f in manifest
            if (f.get("modified_date") or "").startswith(expected_ym)
            or (f.get("date_str") or "").startswith(expected_ym)
            or (f.get("modified_str") or "").startswith(expected_ym)
        ]
        if parsed_q.get("target_tokens"):
            kw_matched = [f for f in matched_items if f.get("is_relevant")]
            if kw_matched:
                matched_items = kw_matched
        matched_items.sort(key=lambda x: (x.get("relevance_score", 0.0), x.get("mtime", 0.0)), reverse=True)
    elif is_directory_inventory and not parsed_q.get("target_tokens"):
        if parsed_q.get("folder_constraint"):
            fc = parsed_q["folder_constraint"].lower()
            matched_items = [
                f for f in sorted_by_newest
                if f.get("path", "").lower().startswith(fc + "/") or (fc == "download" and not f.get("path", "").lower().startswith("documents/"))
            ]
        else:
            matched_items = sorted_by_newest
    elif is_oldest_req:
        matched_items = list(reversed(sorted_by_newest[-10:])) if sorted_by_newest else []
    elif is_latest_req:
        matched_items = sorted_by_newest[:10]
    elif "today" in lower_norm:
        matched_items = [f for f in manifest if f.get("relative_age") == "Today"]
    elif "yesterday" in lower_norm:
        matched_items = [f for f in manifest if f.get("relative_age") == "Yesterday"]
    elif any(w in lower_norm for w in ("receipt", "receipts", "voucher", "vouchers", "fee", "bill", "bills", "invoice")):
        matched_items = [
            f for f in manifest
            if any(re.search(rf"\b{re.escape(k)}\b", f["name"].lower()) or re.search(rf"\b{re.escape(k)}\b", (f.get("snippet") or "").lower()) for k in ("receipt", "voucher", "bill", "invoice", "fee", "challan"))
        ]
    elif any(w in lower_norm for w in ("lecture", "lectures", "notes", "assignment", "academic", "university")):
        matched_items = [
            f for f in manifest
            if classify_document(f["name"], f.get("snippet") or "", user_profile=ctx["profile"])["is_academic_study"]
        ]
    else:
        # Strictly enforce honest-empty: only include candidates scoring >= 35.0
        matched_items = [f for f in manifest if f.get("is_relevant")]
        matched_items.sort(key=lambda x: (x.get("relevance_score", 0.0), x.get("mtime", 0.0)), reverse=True)

    # Build structured ranked match objects with snippets and match reasons (Phase 9.x P0 #6)
    ranked_matches = []
    for idx, m in enumerate(matched_items[:15], 1):
        doc_class = classify_document(m["name"], m.get("snippet", ""), user_profile=ctx["profile"])
        type_label = doc_class.get("type_label", "Document")
        reasons = m.get("match_reasons") or []
        if not reasons:
            if m.get("is_relevant"):
                reasons = ["Query relevance match"]
            else:
                reasons = ["Indexed file in storage"]
        ranked_matches.append({
            "rank": idx,
            "name": m["name"],
            "path": m["path"],
            "size_kb": m["size_kb"],
            "relevance_score": round(m.get("relevance_score", 0.0), 2),
            "match_reasons": reasons,
            "category": type_label,
            "snippet": (m.get("snippet") or "")[:250],
            "modified_date": m.get("modified_date") or m.get("relative_age", "Unknown")
        })

    deep_context_block = ""
    if matched_items:
        top_item = matched_items[0]
        try:
            raw_deep = extract_file_snippet(top_item["full_path"], 12000)
            sanitized_deep = sanitize_pii(raw_deep)
            if sanitized_deep:
                deep_context_block = f"\nDEEP SNIPPET FOR TOP MATCH '{top_item['name']}':\n{sanitized_deep[:3000]}\n"
        except Exception:
            pass

    # Optional Damaged Files Warning Notice
    damaged_note = ""
    if corrupt_files:
        c_names = ", ".join(f"`{c['name']}`" for c in corrupt_files[:3])
        damaged_note = f"\n\n⚠️ **Damaged files detected:** Found **{len(corrupt_files)} corrupted or broken file(s)** ({c_names}). Say **'Quarantine damaged files'** to safely isolate them into `Documents/Review/Damaged/`."

    if matched_items and is_gemini_available():
        top_slice = ranked_matches
        sys_prompt = f"""You are Storage Copilot answering a READ-ONLY file lookup for {owner_name}.
Matched Files in '{target_folder}/':
{json.dumps(top_slice, indent=2)}
{deep_context_block}

Rules:
- Cite the matched filenames in bold, their paths in code spans, sizes, document categories, and relevant snippet details.
- NEVER output robotic debug strings like 'Match: Filename matches:'.
- Keep the summary clear, grounded, and helpful.
"""
        llm_msg = call_gemini_text(sys_prompt, raw_prompt)
        if llm_msg:
            return {
                "type": "query_info",
                "intent": "QUERY_INFO",
                "message": llm_msg + damaged_note,
                "analysis": llm_msg,
                "matches": top_slice,
                "badges": ["Search", "Read-Only"],
                "version": config.APP_VERSION,
                "model_source": "gemini-flash",
                "is_llm": True,
                "degraded_fallback": False,
                "fallback_reason": None,
                "fallback_notice": None,
                "actions": [],
                "plan": [],
                "quick_chips": [
                    "📋 Sort Student Vouchers",
                    "👥 Separate Peer Documents",
                    "🏷️ Clean Download Names",
                    "🧹 Clean Duplicates"
                ]
            }

    if matched_items:
        if is_directory_inventory and not parsed_q.get("target_tokens"):
            lines = [f"### 📂 Directory Inventory for `{target_folder}/` ({len(matched_items)} files)\n"]
        else:
            lines = [f"Found **{len(matched_items)} matching document(s)** across storage:\n"]

        for rm in ranked_matches:
            snip_preview = rm["snippet"].strip().replace("\n", " ")[:140]
            rel_pct = int(rm["relevance_score"] * 100) if rm["relevance_score"] <= 1.0 else int(rm["relevance_score"])
            detail_str = f" — *{rm['category']}* (Relevance: {rel_pct}%)"
            if rm["match_reasons"]:
                detail_str += f" [{', '.join(rm['match_reasons'][:2])}]"
            if snip_preview and snip_preview != "None (binary/image)":
                detail_str += f"\n  > \"{snip_preview}\""
            lines.append(
                f"{rm['rank']}. **{rm['name']}** (`{rm['path']}`, {rm['size_kb']} KB, {rm['modified_date']}){detail_str}"
            )
        msg = "\n".join(lines) + damaged_note
    else:
        if expected_ym:
            msg = f"No files were found from **{expected_ym}** in `{target_folder}/` ({len(manifest)} total files indexed)."
        elif "today" in lower_norm:
            msg = f"No files were downloaded **today** in `{target_folder}/` ({len(manifest)} total files indexed)."
        elif "yesterday" in lower_norm:
            msg = f"No files were received **yesterday** in `{target_folder}/` ({len(manifest)} total files indexed)."
        else:
            msg = f"I scanned `{target_folder}/` ({len(manifest)} files indexed) but could not locate documents matching '{raw_prompt}'."
        msg += damaged_note

    return {
        "type": "query_info",
        "intent": "QUERY_INFO",
        "message": msg,
        "analysis": msg,
        "matches": ranked_matches,
        "badges": ["Search", "Read-Only"],
        "version": config.APP_VERSION,
        "model_source": "offline-heuristic",
        "is_llm": False,
        "degraded_fallback": True,
        "fallback_reason": fb_diag["fallback_reason"],
        "fallback_notice": fb_diag["fallback_notice"],
        "actions": [],
        "plan": [],
        "quick_chips": [
            "📋 Sort Student Vouchers",
            "👥 Separate Peer Documents",
            "🏷️ Clean Download Names",
            "🧹 Clean Duplicates"
        ]
    }
