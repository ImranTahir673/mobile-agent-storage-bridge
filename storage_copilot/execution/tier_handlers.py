import os
import re
import json
import uuid
from collections import defaultdict
from typing import Dict, Any, List, Optional

from storage_copilot import config
from storage_copilot.security.pii_sanitizer import sanitize_pii
from storage_copilot.storage.scanner import (
    scan_directory_manifest,
    resolve_target_directory
)
from storage_copilot.storage.content_extractor import extract_file_snippet
from storage_copilot.storage.deduplication import (
    find_duplicate_clusters,
    find_forgotten_files
)
from storage_copilot.storage.collision_resolver import ensure_unique_plan_destinations
from storage_copilot.storage.namer import ContentDerivedNamer, extract_course_code
from storage_copilot.intelligence.gemini_client import call_gemini_json, call_gemini_text
from storage_copilot.intelligence.document_classifier import classify_document, parse_query_intent
from storage_copilot.intelligence.search_engine import (
    score_file_relevance,
    compute_query_relevance_score
)
from storage_copilot.execution.plan_builder import format_plan_badges
from storage_copilot.advice.advisor import (
    FORMAT_CATEGORIES,
    get_format_category,
    get_unorganized_download_files,
    generate_dynamic_storage_advice
)
from storage_copilot.api.profile_routes import get_user_profile_path


def _load_profile_context() -> Dict[str, Any]:
    p_path = get_user_profile_path()
    profile = {}
    if os.path.exists(p_path):
        try:
            with open(p_path, "r", encoding="utf-8") as pf:
                profile = json.load(pf)
        except Exception:
            pass

    user_id = profile.get("user_identity", {})
    owner_name = user_id.get("primary_name", "Imran Tahir")
    identifiers = user_id.get("identifiers", ["BSAI-182", "182"])
    peers = profile.get("known_peers", [])
    peer_names = [p.get("name") for p in peers if p.get("name")] or ["Fawad", "Sumbal", "Ahmed", "Yousaf"]
    peer_matrix_lines = "\n".join(
        f"- Peer '{p.get('name')}': designated folder '{p.get('designated_folder', f'Documents/Peers/{p.get('name')}')}'"
        for p in peers
    ) if peers else "\n".join(f"- Peer '{p}': designated folder 'Documents/Peers/{p}'" for p in peer_names)

    return {
        "profile": profile,
        "owner_name": owner_name,
        "identifiers": identifiers,
        "peer_names": peer_names,
        "peer_matrix_lines": peer_matrix_lines
    }


def _format_conversation_history(conversation_history: Optional[List[Dict[str, Any]]]) -> str:
    if not conversation_history:
        return "None (New conversation)"
    lines = []
    for turn in conversation_history[-4:]:
        role = (turn.get("role") or "user").upper()
        content = (turn.get("content") or turn.get("text") or turn.get("message") or "").strip()
        if content:
            lines.append(f"{role}: {content[:300].replace(chr(10), ' ')}")
    return "\n".join(lines) if lines else "None (New conversation)"


# =============================================================================
# TIER 1: CONVERSATION HANDLER (Zero Deep File Scans, Zero Mutations)
# =============================================================================
def handle_conversation_turn(
    prompt: str,
    classification: Dict[str, Any],
    target_folder: str = "Download",
    conversation_history: Optional[List[Dict[str, Any]]] = None
) -> Dict[str, Any]:
    ctx = _load_profile_context()
    owner_name = ctx["owner_name"]
    peer_names = ctx["peer_names"]
    sub_intent = classification.get("sub_intent", "")
    raw_prompt = classification.get("raw_prompt", prompt)
    lower_p = raw_prompt.lower()

    if sub_intent == "format_sorting_proposal":
        target_dir = resolve_target_directory(target_folder)
        unorg_files = get_unorganized_download_files(target_dir)
        if not unorg_files:
            return {
                "type": "conversation",
                "intent": "CONVERSATION",
                "message": f"✨ **All files in `{target_folder}/` are already organized!**\n\nNo loose unorganized files remain in the root directory.",
                "analysis": f"All files in `{target_folder}/` are already organized.",
                "version": config.APP_VERSION,
                "model_source": "deterministic-engine",
                "is_llm": False,
                "actions": [],
                "plan": [],
                "badges": ["Chat", "Advice"],
                "quick_chips": [
                    "View organized folders",
                    "🧹 Clean Duplicates",
                    "📋 Sort Student Vouchers"
                ]
            }

        cat_counts = defaultdict(int)
        for f in unorg_files:
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
        msg = f"""Hello {owner_name}. I have analyzed your `{target_folder}/` directory and identified **{len(unorg_files)} unorganized files**.

Here is the breakdown of files by format and their destination subfolders:

| Format | Destination | Files Count |
| :--- | :--- | :--- |
{table_md}

**Total:** **{len(unorg_files)} files** ready to organize.

To ensure system stability and safety, files will be moved in batches (25 files per batch) with 1-Tap Undo available for every batch.

**Would you like me to proceed with moving these files into their respective folders?**"""

        return {
            "type": "conversation",
            "intent": "CONVERSATION",
            "message": msg,
            "analysis": msg,
            "actions": [],
            "plan": [],
            "badges": ["Chat", "Format Breakdown"],
            "format_sorting_active": True,
            "version": config.APP_VERSION,
            "model_source": "deterministic-engine",
            "is_llm": False,
            "quick_chips": [
                "✅ Yes, Move All Files",
                "View organized folders",
                "❌ Cancel"
            ]
        }

    target_dir = resolve_target_directory(target_folder)
    shallow_filenames = []
    if os.path.exists(target_dir) and os.path.isdir(target_dir):
        try:
            shallow_filenames = [
                e.name for e in os.scandir(target_dir)
                if e.is_file() and not e.name.startswith(".")
            ]
        except Exception:
            pass

    if any(w in lower_p for w in ("email", "mail", "sms", "whatsapp", "call", "browse", "web")):
        msg = (
            f"I operate strictly as an **on-device local storage copilot** for your Android filesystem, {owner_name}. "
            "For your privacy and security, I cannot send emails, transmit messages, or browse the external web.\n\n"
            f"However, I can help you find, inspect, or organize documents in `{target_folder}/` ({len(shallow_filenames)} files currently detected)—such as locating professor notes, sorting university lectures, or isolating peer files."
        )
        return {
            "type": "conversation",
            "intent": "CONVERSATION",
            "message": msg,
            "analysis": msg,
            "version": config.APP_VERSION,
            "model_source": "deterministic-engine",
            "is_llm": False,
            "actions": [],
            "plan": [],
            "badges": ["Chat", "Boundary Guard"],
            "quick_chips": [
                "📚 List university lectures",
                "📋 Sort Student Vouchers",
                "👥 Separate Peer Documents",
                "🧹 Clean Duplicates"
            ]
        }

    conv_history_str = _format_conversation_history(conversation_history)
    sys_prompt = f"""You are Storage Copilot, an articulate on-device AI storage assistant running locally on Android for {owner_name} (IDs: {ctx['identifiers']}).
Target Folder: '{target_folder}/' ({len(shallow_filenames)} files present: {', '.join(shallow_filenames[:15]) if shallow_filenames else 'Empty'}).
Known Peers: {', '.join(peer_names)}.
Recent Conversation:
{conv_history_str}

Respond directly and warmly to {owner_name} in concise Markdown.
- NEVER use a static canned greeting template like '👋 Hello Imran Tahir! I am Storage Copilot, running locally on your Android device via Termux'.
- Ground your advice in the actual filenames present above.
- Explain your capabilities clearly (local storage reconnaissance, in-memory PII redaction, academic vs. peer routing, SHA-256 & 3-gram Jaccard deduplication, and 1-tap SQLite WAL rollback).
"""
    llm_text = call_gemini_text(sys_prompt, raw_prompt)
    if llm_text:
        return {
            "type": "conversation",
            "intent": "CONVERSATION",
            "message": llm_text,
            "analysis": llm_text,
            "version": config.APP_VERSION,
            "model_source": "gemini-flash",
            "is_llm": True,
            "actions": [],
            "plan": [],
            "badges": ["Chat", "Advice"],
            "quick_chips": [
                "📋 Sort Student Vouchers",
                "👥 Separate Peer Documents",
                "🏷️ Clean Download Names",
                "🧹 Clean Duplicates"
            ]
        }

    advice_msg = generate_dynamic_storage_advice(
        config.BASE_STORAGE_DIR,
        target_folder=target_folder,
        owner_name=owner_name,
        peer_names=peer_names
    )
    return {
        "type": "conversation",
        "intent": "CONVERSATION",
        "message": advice_msg,
        "analysis": advice_msg,
        "version": config.APP_VERSION,
        "model_source": "offline-heuristic",
        "is_llm": False,
        "actions": [],
        "plan": [],
        "badges": ["Chat", "Advice"],
        "quick_chips": [
            "📋 Sort Student Vouchers",
            "👥 Separate Peer Documents",
            "🏷️ Clean Download Names",
            "🗑️ Cleanup Old Screenshots"
        ]
    }


# =============================================================================
# TIER 2: QUERY_INFO HANDLER (Read-Only Search, Citations & Deep Inspection)
# =============================================================================
def handle_query_info_turn(
    prompt: str,
    classification: Dict[str, Any],
    target_folder: str = "Download",
    conversation_history: Optional[List[Dict[str, Any]]] = None
) -> Dict[str, Any]:
    ctx = _load_profile_context()
    owner_name = ctx["owner_name"]
    sub_intent = classification.get("sub_intent", "")
    raw_prompt = classification.get("raw_prompt", prompt)
    normalized_prompt = classification.get("normalized_prompt", prompt)
    lower_raw = raw_prompt.lower()
    lower_norm = normalized_prompt.lower()

    # 1a. Phase 6 Forgotten Files Audit
    if sub_intent == "forgotten_audit":
        days_match = re.search(r"(\d+)\s*(?:day|days)", lower_norm)
        stale_days = float(days_match.group(1)) if days_match else 90.0
        forg_res = find_forgotten_files(config.BASE_STORAGE_DIR, target_folder=target_folder, stale_days=stale_days)
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
        include_near = (sub_intent == "deduplicate_near_audit")
        dup_res = find_duplicate_clusters(config.BASE_STORAGE_DIR, target_folder, include_near=include_near)
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
            "actions": [],
            "plan": [],
            "badges": ["Search", "Read-Only"],
            "quick_chips": chips
        }

    scan_data = scan_directory_manifest(target_folder=target_folder, query_prompt=raw_prompt)
    manifest = scan_data["manifest"]
    parsed_q = parse_query_intent(normalized_prompt, default_folder=target_folder)

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
    if is_directory_inventory and not parsed_q.get("target_tokens"):
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
            if any(k in f["name"].lower() or k in (f.get("snippet") or "").lower() for k in ("receipt", "voucher", "bill", "invoice", "fee", "challan"))
        ]
    elif any(w in lower_norm for w in ("lecture", "lectures", "notes", "assignment", "academic", "university")):
        matched_items = [
            f for f in manifest
            if classify_document(f["name"], f.get("snippet") or "", user_profile=ctx["profile"])["is_academic_study"]
        ]
    else:
        matched_items = [f for f in manifest if f.get("is_relevant")]
        matched_items.sort(key=lambda x: (x.get("relevance_score", 0.0), x.get("mtime", 0.0)), reverse=True)

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

    if matched_items:
        top_slice = [
            {
                "name": m["name"],
                "path": m["path"],
                "size_kb": m["size_kb"],
                "modified_date": m["modified_date"],
                "relative_age": m["relative_age"],
                "category": classify_document(m["name"], m.get("snippet", ""), user_profile=ctx["profile"])["type_label"],
                "snippet": (m.get("snippet") or "")[:250]
            }
            for m in matched_items[:15]
        ]
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
                "message": llm_msg,
                "analysis": llm_msg,
                "matches": top_slice,
                "badges": ["Search", "Read-Only"],
                "version": config.APP_VERSION,
                "model_source": "gemini-flash",
                "is_llm": True,
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
            lines = [f"Found **{len(matched_items)} matching document(s)** in `{target_folder}/`:\n"]

        for m in matched_items[:15]:
            doc_class = classify_document(m["name"], m.get("snippet") or "", user_profile=ctx["profile"])
            type_label = doc_class.get("type_label", "Document")
            snip_preview = (m.get("snippet") or "").strip().replace("\n", " ")[:140]
            detail_str = f" — *{type_label}*"
            if snip_preview and snip_preview != "None (binary/image)":
                detail_str += f": \"{snip_preview}\""
            lines.append(
                f"- **{m['name']}** (`{m['path']}`, {m['size_kb']} KB, {m.get('modified_date') or m.get('relative_age', 'Unknown')}){detail_str}"
            )
        msg = "\n".join(lines)
    else:
        if "today" in lower_norm:
            msg = f"No files were downloaded **today** in `{target_folder}/` ({len(manifest)} total files indexed)."
        elif "yesterday" in lower_norm:
            msg = f"No files were received **yesterday** in `{target_folder}/` ({len(manifest)} total files indexed)."
        else:
            msg = f"I scanned `{target_folder}/` ({len(manifest)} files indexed) but could not locate documents matching '{raw_prompt}'."

    return {
        "type": "query_info",
        "intent": "QUERY_INFO",
        "message": msg,
        "analysis": msg,
        "matches": [
            {"name": m["name"], "path": m["path"], "size_kb": m["size_kb"]}
            for m in matched_items[:15]
        ],
        "badges": ["Search", "Read-Only"],
        "version": config.APP_VERSION,
        "model_source": "offline-heuristic",
        "is_llm": False,
        "actions": [],
        "plan": [],
        "quick_chips": [
            "📋 Sort Student Vouchers",
            "👥 Separate Peer Documents",
            "🏷️ Clean Download Names",
            "🧹 Clean Duplicates"
        ]
    }


# =============================================================================
# TIER 3: FILE_OPERATION HANDLER (Action Plan Synthesis & Validation)
# =============================================================================
def handle_file_operation_turn(
    prompt: str,
    classification: Dict[str, Any],
    target_folder: str = "Download",
    conversation_history: Optional[List[Dict[str, Any]]] = None
) -> Dict[str, Any]:
    ctx = _load_profile_context()
    owner_name = ctx["owner_name"]
    peer_names = ctx["peer_names"]
    user_profile = ctx["profile"]
    namer = ContentDerivedNamer(user_profile=user_profile)

    sub_intent = classification.get("sub_intent", "")
    raw_prompt = classification.get("raw_prompt", prompt)
    resolved_goal = classification.get("resolved_goal") or classification.get("normalized_prompt") or raw_prompt
    lower_p = resolved_goal.lower()

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
            "quick_chips": [
                "📋 Sort Student Vouchers",
                "👥 Separate Peer Documents",
                "🏷️ Clean Download Names"
            ]
        }

    # 1b. Deduplication Clean Sweep (Exact SHA-256 & Near-Duplicate Jaccard)
    if sub_intent in ("deduplicate_clean", "deduplicate_near_clean"):
        include_near = (sub_intent == "deduplicate_near_clean") or "near" in lower_p
        dup_res = find_duplicate_clusters(config.BASE_STORAGE_DIR, target_folder, include_near=include_near)
        if dup_res["total_duplicates"] == 0 and not include_near:
            near_dup_res = find_duplicate_clusters(config.BASE_STORAGE_DIR, target_folder, include_near=True)
            if near_dup_res["total_duplicates"] > 0:
                dup_res = near_dup_res
                include_near = True

        if dup_res["total_duplicates"] == 0:
            msg = (
                f"✨ **No duplicate files detected in `{target_folder}/`!**\n\n"
                "All documents possess unique cryptographic SHA-256 signatures. No redundant copies were found to clean."
            )
            return {
                "type": "conversation",
                "intent": "FILE_OPERATION",
                "message": msg,
                "analysis": msg,
                "version": config.APP_VERSION,
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
            for dup in cluster["duplicates"]:
                actions.append({
                    "action_id": f"step-{len(actions) + 1}",
                    "type": "trash",
                    "badge": "TRASH",
                    "badge_label": "Soft Delete",
                    "source": dup["path"],
                    "path": dup["path"],
                    "destination": f".agent_trash/{dup['filename']}",
                    "rationale": f"{dup.get('reason', 'Redundant duplicate')} of '{keeper['filename']}'",
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
            "quick_chips": [
                "📋 Sort Student Vouchers",
                "👥 Separate Peer Documents",
                "🏷️ Clean Download Names"
            ]
        }

    # 2. Format Batch Execution
    if sub_intent == "format_batch_execute":
        target_dir = resolve_target_directory(target_folder)
        unorg_files = get_unorganized_download_files(target_dir)
        if not unorg_files:
            msg = f"✨ **All files in `{target_folder}/` are already organized!**\n\nEvery file has been grouped into its dedicated format subfolder."
            return {
                "type": "conversation",
                "intent": "FILE_OPERATION",
                "message": msg,
                "analysis": msg,
                "version": config.APP_VERSION,
                "model_source": "deterministic-engine",
                "is_llm": False,
                "actions": [],
                "plan": [],
                "badges": ["Format Sort", "Complete"],
                "quick_chips": [
                    "View organized folders",
                    "🧹 Clean Duplicates",
                    "📋 Sort Student Vouchers"
                ]
            }

        batch_size = 25
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
            "quick_chips": [
                "⏩ Process Next Batch",
                "View organized folders",
                "🧹 Clean Duplicates"
            ]
        }

    scan_data = scan_directory_manifest(target_folder=target_folder, query_prompt=raw_prompt)
    manifest = scan_data["manifest"]
    manifest_lookup = scan_data["manifest_lookup"]

    use_deterministic_first = any(k in lower_p for k in (
        "university", "academic", "voucher", "fee", "challan", "peer", "separate",
        "clean download names", "older than", "days old", "screenshot", "cleanup"
    ))

    if not use_deterministic_first:
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
                    validated.append(act)
                elif act_type in ("move", "copy", "trash"):
                    src = act.get("source") or act.get("path") or ""
                    matched_src = manifest_paths.get(src.lower()) or manifest_names.get(os.path.basename(src).lower())
                    if matched_src:
                        if act_type in ("move", "copy"):
                            act["source"] = matched_src
                        else:
                            act["path"] = matched_src
                        validated.append(act)

            if validated:
                validated = ensure_unique_plan_destinations(validated)
                formatted_actions = format_plan_badges(validated, peer_names, manifest_lookup)
                plan_id = str(uuid.uuid4())
                resp_msg = gemini_resp.get("message") or f"Synthesized Action Plan for: {resolved_goal}"
                return {
                    "type": "plan",
                    "intent": "FILE_OPERATION",
                    "message": resp_msg,
                    "analysis": resp_msg,
                    "version": config.APP_VERSION,
                    "model_source": gemini_resp.get("_model_used", "gemini-2.5-flash"),
                    "is_llm": True,
                    "actions": formatted_actions,
                    "badges": ["Action Plan", f"{len(formatted_actions)} Actions"],
                    "plan": {
                        "plan_id": plan_id,
                        "version": "1.0",
                        "description": resp_msg,
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

    # 4. Deterministic Grounded Rule Engine powered by Phase 5 ContentDerivedNamer
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
        plan_desc = f"Sort and organize candidate documents in '{target_folder}' into structured taxonomy"
        for item in manifest:
            fn = item["name"]
            snip = item["snippet"] or ""
            if fn.startswith("."):
                continue

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

    if not plan_actions:
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
    msg = f"I've inspected your storage ({len(manifest)} real files on disk) and synthesized an Action Plan for: \"{plan_desc}\""
    return {
        "type": "plan",
        "intent": "FILE_OPERATION",
        "message": msg,
        "analysis": msg,
        "version": config.APP_VERSION,
        "model_source": "offline-heuristic",
        "is_llm": False,
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
