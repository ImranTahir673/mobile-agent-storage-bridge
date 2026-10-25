import os
import re
import json
import uuid
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from flask import Blueprint, request, jsonify

from storage_copilot import config
from storage_copilot.config import APP_VERSION, BASE_STORAGE_DIR, BASE_DIR
from storage_copilot.db.ledger import get_db
from storage_copilot.security.path_resolver import resolve_safe_path
from storage_copilot.security.pii_sanitizer import sanitize_pii
from storage_copilot.storage.scanner import collect_storage_files
from storage_copilot.storage.content_extractor import extract_file_snippet
from storage_copilot.storage.deduplication import find_duplicate_clusters
from storage_copilot.storage.collision_resolver import ensure_unique_plan_destinations
from storage_copilot.intelligence.gemini_client import call_gemini_json
from storage_copilot.intelligence.document_classifier import classify_document
from storage_copilot.intelligence.search_engine import score_document_match, compute_query_relevance_score
from storage_copilot.execution.plan_builder import format_plan_badges, normalize_user_prompt
from storage_copilot.execution.executor import execute_action_plan
from storage_copilot.advice.advisor import (
    FORMAT_CATEGORIES,
    get_format_category,
    get_unorganized_download_files,
    is_format_sorting_query
)
from storage_copilot.api.profile_routes import get_user_profile_path

plan_bp = Blueprint("plan", __name__)

@plan_bp.route("/execute_plan", methods=["POST"])
def execute_plan():
    plan = request.get_json(force=True) or {}
    res_dict, status_code = execute_action_plan(plan)
    return jsonify(res_dict), status_code

@plan_bp.route("/recent_batch", methods=["GET"])
def get_recent_batch():
    """Returns the most recent completed or rolled-back batch for the persistent Undo banner."""
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            SELECT batch_id, intent, total_actions, executed_actions, status, completed_at, rolled_back_at 
            FROM batches 
            WHERE status IN ('COMPLETED', 'ROLLED_BACK') 
            ORDER BY created_at DESC LIMIT 1
        """)
        row = cursor.fetchone()
        if not row:
            return jsonify({"status": "none", "batch": None})

        # Load execution summary from ledger for audit viewing
        cursor.execute("""
            SELECT step_index, action_type, source_path, destination_path, executed_at, status
            FROM action_ledger
            WHERE batch_id = ? AND status = 'EXECUTED'
            ORDER BY step_index ASC
        """, (row["batch_id"],))
        action_rows = cursor.fetchall()

        execution_summary = []
        for a in action_rows:
            src_rel = os.path.relpath(a["source_path"], BASE_DIR).replace("\\", "/") if a["source_path"] else None
            dst_rel = os.path.relpath(a["destination_path"], BASE_DIR).replace("\\", "/") if a["destination_path"] else None
            act_t = a["action_type"]
            badge = "FILE_MOVE"
            if act_t == "trash":
                badge = "TRASH"
            elif act_t == "make_dir":
                badge = "FOLDER_CREATE"
            elif dst_rel and "/Peers/" in dst_rel:
                badge = "PEER_MOVE"
            elif src_rel and dst_rel and os.path.basename(src_rel) != os.path.basename(dst_rel):
                badge = "RENAME"

            execution_summary.append({
                "step_index": a["step_index"],
                "action_type": act_t,
                "source": src_rel or "",
                "destination": dst_rel or "",
                "badge": badge,
                "timestamp": a["executed_at"] or row["completed_at"]
            })

        return jsonify({
            "status": "success",
            "batch": {
                "batch_id": row["batch_id"],
                "intent": row["intent"],
                "total_actions": row["total_actions"],
                "executed_actions": row["executed_actions"],
                "status": row["status"],
                "completed_at": row["completed_at"],
                "rolled_back_at": row["rolled_back_at"],
                "execution_summary": execution_summary
            }
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()


@plan_bp.route("/propose_plan", methods=["POST"])
def propose_plan():
    """
    v0.6-live Three-Tier Intent Routing & Dynamic Reconnaissance Endpoint:
    
    TIER 1: CONVERSATION
    - Greetings, general questions, capabilities, follow-ups ("What else?").
    - Dispatched to Gemini with user persona (Imran Tahir, BSAI-182).
    - NEVER returns static canned templates.
    
    TIER 2: QUERY_INFO (READ-ONLY)
    - Queries asking to list, search, find, or inspect files ("List university lectures", "Find receipts").
    - Reads real storage files and extracted PII-sanitized snippets.
    - Synthesizes structured Markdown summary with citations/snippets in chat.
    - Stages ZERO file mutations.
    
    TIER 3: FILE_OPERATION
    - Moves, renames, organization, and trash requests ("Sort student vouchers", "Clean download names").
    - Inspects real files and enforces Academic Taxonomy Priority under Documents/University/<Subject>/.
    - Content-derived naming (never generic Document_2025-04-13.pdf).
    - Strict programmatic collision avoidance ensuring unique destination paths.
    """
    data = request.get_json(force=True) or {}
    raw_prompt = (data.get("prompt") or "").strip()
    target_folder = data.get("target_folder") or "Download"
    conversation_history = data.get("conversation_history") or []
    normalized_prompt = normalize_user_prompt(raw_prompt)
    lower_raw = raw_prompt.lower()
    lower_norm = normalized_prompt.lower()
    affirmative_words = {"yes", "yeah", "yep", "sure", "ok", "okay", "go ahead", "proceed", "do it", "please do", "lets do it", "let's do it"}
    clean_p_start = re.sub(r"^[^\w]+", "", lower_raw).strip()
    is_pure_affirmative = (
        clean_p_start in affirmative_words or 
        lower_norm in affirmative_words or 
        bool(re.search(r"^(?:yes|yeah|yep|sure|ok|okay|go ahead|proceed|do it|please do)\b", clean_p_start))
    )
    is_polite_action = any(lower_norm.startswith(p) for p in ("how about we sort", "how about we organize", "how about we clean", "can we sort", "can we organize", "let's sort", "lets sort", "first sort")) or "first sort" in lower_norm

    last_asst = ""
    for turn in reversed(conversation_history):
        r = (turn.get("role") or "").lower()
        if r in ("assistant", "copilot", "bot"):
            last_asst = (turn.get("content") or turn.get("text") or turn.get("message") or "").strip()
            break

    if is_pure_affirmative:
        lower_asst = last_asst.lower()
        if "duplicate file audit" in lower_asst or "clean up these redundant copies" in lower_asst:
            normalized_prompt = "clean duplicate downloads"
        elif any(p in lower_asst for p in ("peer document", "peer files", "separate peer", "classmate", "peer separation")):
            normalized_prompt = "separate peer documents"
        elif any(p in lower_asst for p in ("tuition voucher", "fee voucher", "fee slip", "student voucher", "student vouchers")):
            normalized_prompt = "sort student vouchers"
        elif any(p in lower_asst for p in ("file format", "format subfolders", "group by format", "by format", "respective folders", "format", "unorganized files", "files ready to organize")):
            normalized_prompt = "process next batch of files in download"
        else:
            normalized_prompt = "organize all my university files"

    # Build dialogue context from recent conversation turns
    context_lines = []
    for turn in conversation_history[-4:]:
        role = (turn.get("role") or "user").upper()
        content = (turn.get("content") or turn.get("text") or turn.get("message") or "").strip()
        if content:
            context_lines.append(f"{role}: {content[:300].replace(chr(10), ' ')}")
    conversation_context_block = "\n".join(context_lines) if context_lines else "None (New conversation)"

    # Load User Profile & Peer Separation Matrix for system context
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

    # =========================================================================
    # RECONNAISSANCE FIRST: Live Storage & Manifest Inspection
    # =========================================================================
    now_dt = datetime.now()
    now_str = now_dt.strftime("%A, %B %d, %Y at %I:%M %p")
    today_date = now_dt.strftime("%Y-%m-%d")

    target_dir = None
    dir_candidates = [
        target_folder,
        target_folder.lower(),
        target_folder.capitalize(),
        os.path.join(BASE_STORAGE_DIR, target_folder),
        os.path.join(BASE_STORAGE_DIR, target_folder.lower()),
    ]
    for c in dir_candidates:
        try:
            p = resolve_safe_path(c) if not os.path.isabs(c) else c
            if os.path.exists(p) and os.path.isdir(p):
                target_dir = p
                break
        except Exception:
            continue

    if not target_dir:
        target_dir = os.path.join(BASE_STORAGE_DIR, target_folder)
        os.makedirs(target_dir, exist_ok=True)

    scanned_entries = []
    subfolders = []
    if os.path.exists(target_dir) and os.path.isdir(target_dir):
        for entry in os.scandir(target_dir):
            if entry.is_dir() and not entry.name.startswith("."):
                cnt = 0
                try:
                    # Scan 1st-level subdirectory files deeply so downloads in subfolders are fully analyzed
                    for sub_entry in os.scandir(entry.path):
                        if sub_entry.is_file() and not sub_entry.name.startswith("."):
                            cnt += 1
                            scanned_entries.append(sub_entry)
                    subfolders.append(f"{entry.name} ({cnt} items)")
                except Exception:
                    subfolders.append(entry.name)
            elif entry.is_file() and not entry.name.startswith("."):
                scanned_entries.append(entry)

    # Snippet inspection & PII scrubbing (up to 1000 candidate files)
    manifest = []
    manifest_for_gemini = []
    manifest_lookup = {}
    for entry in scanned_entries[:1000]:
        filename = entry.name
        file_size = entry.stat().st_size
        ext = os.path.splitext(filename)[1].lower()

        try:
            mtime = entry.stat().st_mtime
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
            mtime = 0.0
            mtime_str = ""
            date_str = ""
            rel_age = "Unknown"
            age_days = 9999.0

        raw_text = extract_file_snippet(entry.path, 1000)
        sanitized_snippet = sanitize_pii(raw_text)

        try:
            rel_path = os.path.relpath(entry.path, BASE_STORAGE_DIR).replace("\\", "/")
        except Exception:
            rel_path = f"{target_folder}/{filename}"

        manifest_item = {
            "filename": filename,
            "path": rel_path,
            "size_kb": round(file_size / 1024, 1),
            "modified_date": mtime_str,
            "relative_age": rel_age,
            "age_days": age_days,
            "mtime": mtime,
            "text_snippet": sanitized_snippet if sanitized_snippet else "None (binary/image)",
            "has_content": bool(sanitized_snippet)
        }
        manifest_for_gemini.append(manifest_item)

        internal_item = {
            "filename": filename,
            "name": filename,
            "path": rel_path,
            "full_path": entry.path,
            "extension": ext,
            "size_kb": manifest_item["size_kb"],
            "size_bytes": file_size,
            "modified_date": mtime_str,
            "relative_age": rel_age,
            "age_days": age_days,
            "mtime": mtime,
            "snippet": sanitized_snippet,
            "text_snippet": manifest_item["text_snippet"],
            "has_content": manifest_item["has_content"]
        }
        manifest.append(internal_item)
        manifest_lookup[rel_path.lower()] = internal_item
        manifest_lookup[filename.lower()] = internal_item

    # Pre-flight query relevance scoring
    for item in manifest:
        item["query_relevance"] = compute_query_relevance_score(raw_prompt, item)
    for item in manifest_for_gemini:
        item["query_relevance"] = compute_query_relevance_score(raw_prompt, item)

    # Chronological Ordering & Temporal Audit Computation (Phase 3.5)
    is_oldest_req = any(w in lower_raw or w in lower_norm for w in ("oldest", "earliest", "first downloaded", "longest ago"))
    
    # Priority sorting: items matching user's keywords/dates first (highest relevance score), followed by chronological
    manifest.sort(key=lambda x: (x.get("query_relevance", 0.0) > 0, x.get("query_relevance", 0.0), x.get("mtime", 0.0) if not is_oldest_req else -x.get("mtime", 0.0)), reverse=True)
    manifest_for_gemini.sort(key=lambda x: (x.get("query_relevance", 0.0) > 0, x.get("query_relevance", 0.0), x.get("mtime", 0.0) if not is_oldest_req else -x.get("mtime", 0.0)), reverse=True)

    today_files = [f["filename"] for f in manifest if f.get("relative_age") == "Today"]
    yesterday_files = [f["filename"] for f in manifest if f.get("relative_age") == "Yesterday"]
    week_files = [f["filename"] for f in manifest if f.get("age_days", 999) <= 7]
    month_old_files = [f["filename"] for f in manifest if f.get("age_days", 0) >= 30]

    sorted_by_newest = sorted(manifest, key=lambda x: x.get("mtime", 0.0), reverse=True)
    newest_file_name = sorted_by_newest[0]["filename"] if sorted_by_newest else "None"
    oldest_file_name = sorted_by_newest[-1]["filename"] if sorted_by_newest else "None"

    # Fast-path for Deduplication Commands & Queries
    is_deduplicate_req = any(k in normalized_prompt.lower() for k in ("duplicate", "duplicates", "dedup", "deduplicate", "repeated file", "repeated files"))
    if is_deduplicate_req:
        dup_res = find_duplicate_clusters(BASE_STORAGE_DIR, target_folder)
        is_clean_action = any(k in normalized_prompt.lower() for k in ("clean", "delete", "trash", "remove", "wipe")) or is_pure_affirmative
        if dup_res["total_duplicates"] == 0:
            return jsonify({
                "type": "conversation" if is_clean_action else "query_info",
                "message": (
                    f"✨ **No duplicate files detected in `{target_folder}/`!**\n\n"
                    "All documents possess unique cryptographic SHA-256 signatures. No redundant copies were found to clean."
                ),
                "version": APP_VERSION,
                "actions": [],
                "duplicate_summary": dup_res,
                "quick_chips": [
                    "📋 Sort Student Vouchers",
                    "👥 Separate Peer Documents",
                    "🏷️ Clean Download Names",
                    "🗑️ Cleanup Old Screenshots"
                ]
            })
        if is_clean_action:
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
                        "rationale": f"Redundant duplicate of '{keeper['filename']}' ({cluster['size_human']} reclaimed)",
                        "ai_inspected": True,
                        "inspected_preview": f"Exact SHA-256 match with {keeper['filename']}"
                    })
            plan_id = str(uuid.uuid4())
            plan_desc = f"Deduplication: Clean {len(actions)} redundant duplicate file(s) ({dup_res['reclaimable_human']})"
            return jsonify({
                "type": "plan",
                "message": (
                    f"Identified **{len(actions)} redundant duplicate file(s)** across {dup_res['total_clusters']} group(s). "
                    f"Cleaning them will reclaim **{dup_res['reclaimable_human']}** of storage while safely preserving all original canonical files.\n\n"
                    "Review the proposed removals below. Tap **Execute Action Plan** to move redundant copies to `.agent_trash` (with 1-tap rollback at any time)."
                ),
                "plan": {
                    "plan_id": plan_id,
                    "description": plan_desc,
                    "actions": actions
                },
                "actions": actions,
                "duplicate_summary": dup_res,
                "version": APP_VERSION
            })
        else:
            lines = [
                f"### 🧹 Duplicate File Audit for `{target_folder}/`",
                f"Found **{dup_res['total_duplicates']} redundant duplicate file(s)** across **{dup_res['total_clusters']} group(s)**.",
                f"**Total Reclaimable Storage:** `{dup_res['reclaimable_human']}`\n",
                "#### Detected Groups:"
            ]
            for idx, cluster in enumerate(dup_res["clusters"], 1):
                k = cluster["keeper"]
                lines.append(f"**Group {idx}** (Size: {cluster['size_human']} each, Reclaimable: {cluster['reclaimable_human']}):")
                lines.append(f"- 🛡️ **Preserving Keeper:** `{k['path']}` *({k['reason']})*")
                for d in cluster["duplicates"]:
                    lines.append(f"- 🗑️ **Redundant Copy:** `{d['path']}` *({d['reason']})*")
                lines.append("")
            lines.append("💡 *To clean up these redundant copies and reclaim space, say **'Clean duplicate downloads'** or tap **Clean Duplicates** below.*")
            return jsonify({
                "type": "query_info",
                "message": "\n".join(lines),
                "version": APP_VERSION,
                "actions": [],
                "query_results": dup_res["clusters"],
                "duplicate_summary": dup_res,
                "quick_chips": [
                    "🧹 Clean duplicate downloads",
                    "📋 Sort Student Vouchers",
                    "👥 Separate Peer Documents",
                    "🏷️ Clean Download Names"
                ]
            })

    # =========================================================================
    # FAST-PATH 1: View Organized Folders Reconnaissance
    # =========================================================================
    is_view_organized_folders = any(k in lower_norm or k in lower_raw for k in (
        "view organized folders", "view organized folder", "see organized folders",
        "show organized folders", "check organized folders", "list organized folders",
        "inspect organized folders", "organized folders"
    ))
    if is_view_organized_folders:
        dl_subfolders = []
        if os.path.exists(target_dir) and os.path.isdir(target_dir):
            for entry in os.scandir(target_dir):
                if entry.is_dir() and not entry.name.startswith("."):
                    cnt = sum(1 for e in os.scandir(entry.path) if e.is_file() and not e.name.startswith("."))
                    dl_subfolders.append((entry.name, cnt))
        
        doc_dir = os.path.join(BASE_STORAGE_DIR, "Documents")
        doc_subfolders = []
        if os.path.exists(doc_dir) and os.path.isdir(doc_dir):
            for root, dirs, files_in_dir in os.walk(doc_dir):
                dirs[:] = [d for d in dirs if not d.startswith(".")]
                rel_root = os.path.relpath(root, BASE_STORAGE_DIR).replace("\\", "/")
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

        chips = ["⏩ Process Next Batch", "🧹 Clean Duplicates", "📋 Sort Student Vouchers"] if unorg_files else ["🧹 Clean Duplicates", "📋 Sort Student Vouchers", "👥 Separate Peer Documents"]
        return jsonify({
            "type": "query_info",
            "message": "\n".join(lines),
            "version": APP_VERSION,
            "actions": [],
            "quick_chips": chips
        })

    # =========================================================================
    # FAST-PATH 2: Format-Based Sorting & Gradual Micro-Batch Execution
    # =========================================================================
    format_sorting_keywords = (
        "file types together", "file types", "by format", "by file type", "by type",
        "group by format", "group files by format", "sort files by format",
        "group by type", "sort by format", "sort format", "group format",
        "format subfolders", "group all files by format"
    )
    is_format_sorting_intent = (
        is_format_sorting_query(raw_prompt) or
        is_format_sorting_query(normalized_prompt) or
        (any(k in lower_norm or k in lower_raw for k in format_sorting_keywords) and any(w in lower_norm or w in lower_raw for w in ("sort", "group", "organize", "consolidate", "together", "separate", "move", "arrange", "put", "place")))
    )
    is_next_batch_req = any(k in lower_norm or k in lower_raw for k in ("next batch", "process next batch", "continue batch", "run next batch"))
    is_format_affirmative = is_pure_affirmative and any(k in lower_asst for k in ("format", "respective", "subfolders", "unorganized files", "files ready to organize"))

    if is_format_sorting_intent or is_next_batch_req or is_format_affirmative:
        unorg_files = get_unorganized_download_files(target_dir)

        # EXECUTION MODE: Triggered on user confirmation ("yes", "proceed") or explicit "next batch" request
        if is_next_batch_req or is_format_affirmative or "proceed" in lower_norm or "yes" in lower_norm or "go ahead" in lower_norm:
            if not unorg_files:
                return jsonify({
                    "type": "conversation",
                    "message": f"✨ **All files in `{target_folder}/` are already organized!**\n\nEvery file has been grouped into its dedicated format subfolder.",
                    "version": APP_VERSION,
                    "model_source": "deterministic-engine",
                    "is_llm": False,
                    "actions": [],
                    "quick_chips": [
                        "View organized folders",
                        "🧹 Clean Duplicates",
                        "📋 Sort Student Vouchers"
                    ]
                })

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
            return jsonify({
                "type": "plan",
                "message": msg,
                "plan": {
                    "plan_id": plan_id,
                    "description": plan_desc,
                    "actions": actions,
                    "batch_info": batch_info
                },
                "actions": actions,
                "batch_info": batch_info,
                "version": APP_VERSION,
                "model_source": "deterministic-engine",
                "is_llm": False,
                "quick_chips": [
                    "⏩ Process Next Batch",
                    "View organized folders",
                    "🧹 Clean Duplicates"
                ]
            })

        # PROPOSAL MODE: Show high-level summary breakdown and ask user permission
        else:
            if not unorg_files:
                return jsonify({
                    "type": "conversation",
                    "message": f"✨ **All files in `{target_folder}/` are already organized!**\n\nNo loose unorganized files remain in the root directory.",
                    "version": APP_VERSION,
                    "model_source": "deterministic-engine",
                    "is_llm": False,
                    "actions": [],
                    "quick_chips": [
                        "View organized folders",
                        "🧹 Clean Duplicates",
                        "📋 Sort Student Vouchers"
                    ]
                })

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

            return jsonify({
                "type": "conversation",
                "message": msg,
                "actions": [],
                "format_sorting_active": True,
                "version": APP_VERSION,
                "model_source": "deterministic-engine",
                "is_llm": False,
                "quick_chips": [
                    "✅ Yes, Move All Files",
                    "View organized folders",
                    "❌ Cancel"
                ]
            })

    # Effective request context
    if is_pure_affirmative:
        last_asst_snippet = last_asst[:400].replace('\n', ' ') if last_asst else normalized_prompt
        effective_request = f"The user confirmed with '{raw_prompt}' to proceed with the specific action, plan, or recommendation previously discussed by the assistant in the latest turn: \"{last_asst_snippet}\"."
    elif is_polite_action:
        effective_request = f"The user requested to proceed with the action: '{raw_prompt}'."
    else:
        effective_request = raw_prompt

    # =========================================================================
    # TIER 2: ON-DEMAND DEEP DOCUMENT INSPECTION (Content Q&A & Summaries)
    # =========================================================================
    deep_inspected_doc = None
    deep_inspected_content = ""
    deep_context_block = ""

    content_inquiry_keywords = (
        "summarize", "summary", "read", "explain", "what is inside", "what does it say",
        "tell me about", "topics", "content of", "details of", "about this", "due date",
        "amount", "bill amount", "questions", "chapters", "findings", "methodology",
        "what's inside", "whats inside", "preview of", "inspect", "show me content", "what does the"
    )
    is_content_inquiry = any(k in lower_raw for k in content_inquiry_keywords) or any(k in lower_norm for k in content_inquiry_keywords)

    target_candidate = None
    best_match_score = 0
    for item in manifest:
        fn_clean = item["filename"].lower()
        stem = os.path.splitext(fn_clean)[0]
        clean_stem = re.sub(r"[-_.]+", " ", stem)
        words = [w for w in re.findall(r"\w+", clean_stem) if len(w) > 3]
        matched_words = [w for w in words if w in lower_raw]

        score = 0
        if stem in lower_raw or fn_clean in lower_raw:
            score = 100
        elif len(matched_words) >= 2:
            score = 50 + (10 * len(matched_words))
        elif len(matched_words) == 1 and len(words) <= 2:
            score = 40

        if score > best_match_score:
            best_match_score = score
            target_candidate = item

    if target_candidate and (is_content_inquiry or best_match_score >= 50):
        try:
            target_full_path = target_candidate.get("full_path") or resolve_safe_path(target_candidate.get("path", ""))
            if os.path.exists(target_full_path):
                raw_deep = extract_file_snippet(target_full_path, 12000)
                deep_inspected_content = sanitize_pii(raw_deep)
                if deep_inspected_content:
                    deep_inspected_doc = target_candidate["filename"]
                    deep_context_block = f"""
TARGET DOCUMENT DEEP INSPECTION FOR '{deep_inspected_doc}' (Up to 12,000 characters with PII sanitized in RAM):
{deep_inspected_content}
"""
        except Exception:
            pass

    # Compact file manifest optimized for low token consumption and high Gemini throughput
    compact_manifest = []
    snippet_count = 0
    for f in manifest_for_gemini[:120]:
        item = {
            "name": f["filename"],
            "path": f["path"],
            "size_kb": f["size_kb"],
            "date": f["modified_date"],
            "age": f["relative_age"]
        }
        if snippet_count < 6 and f.get("query_relevance", 0) > 0 and f.get("text_snippet") and f["text_snippet"] != "None (binary/image)":
            item["snippet"] = f["text_snippet"][:250]
            snippet_count += 1
        compact_manifest.append(item)
    compact_manifest_json = json.dumps(compact_manifest, separators=(',', ':'))

    # =========================================================================
    # UNIFIED GROUNDED GEMINI REASONING (Single Unified Pipeline Turn)
    # =========================================================================
    unified_system_prompt = f"""You are Storage Copilot, an elite, articulate on-device AI agent running locally via Termux on Android for {owner_name} (Roll/ID: {identifiers}, Major: Artificial Intelligence).
Current Device Time: {now_str} (Today's Date: {today_date}).
Storage Root: /storage/emulated/0 (Downloads, Documents).
Known Peers: {", ".join(peer_names)}.
Peer Routing:
{peer_matrix_lines}

RECENT CONVERSATION HISTORY (Last turns):
{conversation_context_block}

TEMPORAL DIRECTORY AUDIT STATS (for target folder '{target_folder}/'):
- Files downloaded Today ({today_date}): {len(today_files)} files ({', '.join(today_files[:10]) if today_files else 'None'})
- Files downloaded Yesterday: {len(yesterday_files)} files ({', '.join(yesterday_files[:10]) if yesterday_files else 'None'})
- Files from past 7 days: {len(week_files)} files
- Files older than 30 days: {len(month_old_files)} files ({', '.join(month_old_files[:10]) if month_old_files else 'None'})
- Single Most Recent File: {newest_file_name}
- Single Oldest File: {oldest_file_name}

LIVE DIRECTORY STATUS FOR '{target_folder}/' (Sorted {'oldest first' if is_oldest_req else 'newest first'}):
- Top-level subfolders: {", ".join(subfolders) if subfolders else "None"}
- Direct files on disk ({len(manifest)} files indexed):
{compact_manifest_json}
{deep_context_block}

YOUR AGENT PERSONA & CAPABILITIES:
- You are an articulate, highly capable AI agent (similar to Google DeepMind / Antigravity). You communicate with clarity, intelligence, and empathy directly to {owner_name}.
- You possess complete self-awareness of your abilities and system boundaries.
- WHAT YOU CAN DO: Local storage reconnaissance, content inspection with in-memory PII sanitization, intelligent file organization (academic vs peer vs personal bills), cryptographic duplicate detection, temporal recency & date-range queries, interactive diff approval, and 1-tap atomic rollbacks via SQLite WAL.
- WHAT YOU CANNOT DO: You cannot send emails, send SMS/WhatsApp messages, browse the live web, access cloud storage directly, launch external apps, or alter Android system configurations. If asked to do something out-of-scope, politely state your local storage boundaries and explain what local storage task you can help with instead.

REASONING RULES & UNIFIED CONTRACT:
Analyze the operational request and dialogue history with full, direct access to the live files manifest above.
Determine your response type natively:

1. "query_info" (SEARCH / LOOKUP / INSPECT / LIST / DEEP DOCUMENT Q&A / TEMPORAL RECENCY & DATES):
   - Triggered when the user asks to find, list, search, view, audit, check, summarize, explain, inspect, or query documents or recency (e.g. "find Java book", "48 laws of power", "list lectures", "summarize neural style paper", "what is my latest download?", "did I download anything today?", "what did I receive yesterday?", "show files from past 7 days", "what are my oldest files?", "can you give me the list of files on 12 sep 2023").
   - If a 'TARGET DOCUMENT DEEP INSPECTION' block is provided above for a file, use its deep text to answer the user's detailed questions (summarize main findings/methodology, extract due dates, explain topics, cite questions).
   - TEMPORAL RECENCY & DATES:
     * If user asks about recency (e.g. "latest files", "newest download", "oldest files"), cite the exact matching files with bold names, paths in code spans, sizes, and exact modified timestamps / relative age.
     * If user asks about exact dates or durations (e.g. "files from today", "yesterday", "past 7 days", "older than 30 days", "files on 12 sep 2023"), use the temporal audit stats and `relative_age`/`modified_date` from the manifest to accurately answer.
   - Directly inspect the real files manifest above. If matching documents exist (e.g. JavaTheCompleteReference.pdf for Java, the-48-laws-of-power... for 48 laws), cite them with bold names, paths in code spans, sizes, and helpful details.
   - For folder listing or inventory requests, provide a clean, grouped markdown overview of the files.
   - For status or audit checks ("any more sorting for download?"), summarize what unorganized files remain or praise the clean state.
   - CRITICAL RULE: For "query_info", "actions" MUST ALWAYS be an empty list []. NEVER propose file mutations or actions for read-only queries!

2. "conversation" (ADVICE / GREETINGS / CAPABILITIES / FOLLOW-UPS / OUT-OF-SCOPE):
   - Triggered for greetings ("Hi", "Hello"), capabilities ("What can you do?", "Who are you?"), conversational follow-ups ("What else?", "Tell me more", "What other things can you do?"), suggestions/advice ("What suggestions do you have for my files?", "How can I organize?"), or out-of-scope requests (emails, web search).
   - CRITICAL: If the user asks "What else?", "What else can you do?", or "What can you do?", do NOT treat it as a file search query. Explain your broader capabilities and what actions {owner_name} can take next.
   - Ground your advice in the actual live directory status above. Cite real filenames and real categories.
   - Set "actions": []

3. "plan" (ACTIONS / CONFIRMATIONS / ORGANIZING / TEMPORAL LIFECYCLE ARCHIVING):
   - Triggered when the user explicitly commands an action ("Clean Download Names", "Sort student vouchers", "Separate peer documents", "Move files older than 30 days to Documents/Archive/") OR confirms a previous suggestion ("Yes", "Proceed", "Do it").
   - TEMPORAL ACTIONS & LIFECYCLE ARCHIVING:
     * If user commands an action based on age or dates (e.g. "archive files older than 30 days", "sort today's downloads into University/"), target EXACTLY the files matching that age criterion from the manifest.
   - Synthesize move/rename/trash actions referencing exact filenames from the manifest.
   - Academic Taxonomy:
     * Academic study materials (lectures, assignments, notes) -> Documents/University/<Subject>/
     * Personal tuition vouchers -> Documents/University/Vouchers/
     * Peer documents for classmates ({", ".join(peer_names)}) -> Documents/Peers/<PeerName>/
     * Utility/electricity bills -> Documents/Finances/
     * MANDATORY TAXONOMY ROUTING: ALL move/rename destinations (including for 'Clean Download Names') MUST route the file into its designated taxonomy folder under 'Documents/' (e.g. 'Documents/University/AI/Knowledge_Representation_Lecture_04.pdf' or 'Documents/Finances/Electricity_Bill.pdf'). NEVER leave destination paths inside 'Download/'.
   - Content-Derived Naming: Standardize ambiguous WhatsApp (DOC-*.pdf) or cryptic downloads into clean, descriptive titles (e.g. 'Knowledge_Representation_Lecture_04.pdf'). NEVER use generic 'Document_2025-04-13.pdf'.
   - Ensure every proposed action has a unique, collision-free destination.

OUTPUT SCHEMA (Strict JSON):
{{
  "type": "conversation" | "query_info" | "plan",
  "message": "<Articulate, empathetic, direct markdown response addressing {owner_name}>",
  "actions": [
    {{
      "action_id": "step-1",
      "type": "move" | "trash" | "make_dir",
      "source": "<exact filename from manifest>",
      "destination": "Documents/...",
      "rationale": "...",
      "badge": "RENAME" | "FILE_MOVE" | "TRASH"
    }}
  ],
  "quick_chips": [ ... 3-4 dynamic, contextual suggestions ... ]
}}
"""

    user_query_text = f"""Operational Request: "{effective_request}"
Raw User Prompt: "{raw_prompt}"
Target Folder: "{target_folder}"
"""

    gemini_resp = call_gemini_json(unified_system_prompt, user_query_text)

    # Process Unified Gemini Output
    if gemini_resp and isinstance(gemini_resp, dict) and gemini_resp.get("type"):
        resp_type = (gemini_resp.get("type") or "conversation").lower()
        resp_message = gemini_resp.get("message") or ""
        raw_actions = gemini_resp.get("actions") or []
        quick_chips = gemini_resp.get("quick_chips") or [
            "🧹 Clean Duplicates",
            "📋 Sort Student Vouchers",
            "👥 Separate Peer Documents",
            "🏷️ Clean Download Names"
        ]

        model_source = gemini_resp.get("_model_used") or "gemini-2.5-flash"
        is_llm = True
        model_footer = f"\n\n---\n*⚡ Model: {model_source}*"
        if resp_message and model_footer not in resp_message:
            resp_message += model_footer

        print(f"[Storage Copilot Response] Prompt: '{raw_prompt}' -> Model: '{model_source}' (is_llm={is_llm}) | Type: '{resp_type}'")

        if resp_type == "plan" and raw_actions:
            manifest_paths = {m["path"].lower(): m["path"] for m in manifest}
            manifest_names = {m["filename"].lower(): m["path"] for m in manifest}

            validated_actions = []
            for act in raw_actions:
                act_type = act.get("type")
                if act_type == "make_dir":
                    validated_actions.append(act)
                elif act_type in ("move", "copy", "trash"):
                    src = act.get("source") or act.get("path") or ""
                    matched_src = None
                    if src.lower() in manifest_paths:
                        matched_src = manifest_paths[src.lower()]
                    elif os.path.basename(src).lower() in manifest_names:
                        matched_src = manifest_names[os.path.basename(src).lower()]

                    if matched_src:
                        if act_type in ("move", "copy"):
                            act["source"] = matched_src
                        else:
                            act["path"] = matched_src
                        validated_actions.append(act)

            if validated_actions:
                validated_actions = ensure_unique_plan_destinations(validated_actions)
                formatted_actions = format_plan_badges(validated_actions, peer_names, manifest_lookup)
                plan_id = str(uuid.uuid4())
                action_plan = {
                    "plan_id": plan_id,
                    "version": "1.0",
                    "description": resp_message or f"Organize files per: {raw_prompt}",
                    "collision_strategy": "RENAME_NUMERIC",
                    "actions": formatted_actions
                }
                return jsonify({
                    "type": "plan",
                    "message": resp_message or f"I've inspected your storage ({len(manifest)} real files on disk) and synthesized an Action Plan.",
                    "version": APP_VERSION,
                    "model_source": model_source,
                    "is_llm": is_llm,
                    "actions": formatted_actions,
                    "plan": action_plan,
                    "quick_chips": quick_chips
                })

        elif resp_type in ("query_info", "conversation") or (resp_type == "plan" and not raw_actions):
            final_type = resp_type if resp_type in ("query_info", "conversation") else "conversation"
            return jsonify({
                "type": final_type,
                "message": resp_message or f"I've reviewed your storage in `{target_folder}/`.",
                "version": APP_VERSION,
                "model_source": model_source,
                "is_llm": is_llm,
                "actions": [],
                "quick_chips": quick_chips
            })

    # 6. Heuristic Fallback (Active if Gemini is offline or API key missing)
    effective_p = (normalized_prompt if is_pure_affirmative else raw_prompt).lower()
    lower_p = effective_p
    plan_actions = []
    plan_desc = ""
    created_dirs = set()
    step_idx = 1

    # Check if user prompt is an explicit informational query or search intent FIRST
    is_explicit_query = (any(lower_p.startswith(q) for q in (
        "can you", "could you", "what", "which", "where", "how many", "list", "show", "find", "search", "tell me", "is there", "do i have"
    )) or any(k in lower_p for k in (
        "what's the oldest", "whats the oldest", "what is the oldest", "oldest file", "newest file", "latest file", "list of files", "list files", "law of power", "laws of power", "java", "complete reference"
    )) or is_oldest_req or ("today" in lower_p) or ("yesterday" in lower_p)) and not any(k in lower_p for k in ("what else", "what can you do", "who are you", "what other", "suggestions"))

    true_action_verbs = ("move", "rename", "relocate", "transfer", "trash", "delete", "organize", "sort", "tidy", "clean")
    has_action_verb = any(re.search(rf"\b{v}\b", lower_p) for v in true_action_verbs)

    if is_explicit_query and not has_action_verb:
        matched_items = []
        if is_oldest_req or any(w in lower_p for w in ("oldest", "earliest")):
            matched_items = sorted_by_newest[-10:] if sorted_by_newest else []
            matched_items.reverse()
        elif any(w in lower_p for w in ("latest", "newest", "most recent")):
            matched_items = sorted_by_newest[:10]
        elif "today" in lower_p:
            matched_items = [f for f in manifest if f.get("relative_age") == "Today"]
        elif "yesterday" in lower_p:
            matched_items = [f for f in manifest if f.get("relative_age") == "Yesterday"]
        elif any(w in lower_p for w in ("receipt", "voucher", "fee", "bill", "invoice")):
            matched_items = [f for f in manifest if any(k in f["name"].lower() or k in (f.get("snippet") or "").lower() for k in ("receipt", "voucher", "bill", "invoice", "fee"))]
        elif any(w in lower_p for w in ("lecture", "notes", "assignment", "academic", "university")):
            matched_items = [f for f in manifest if classify_document(f["name"], f.get("snippet") or "")["is_academic_study"]]
        else:
            matched_items = [f for f in manifest if f.get("query_relevance", 0.0) > 0]
            if not matched_items:
                words = [w for w in re.findall(r"\w+", lower_p) if len(w) > 2 and w not in ("find", "search", "list", "show", "files", "download", "documents", "give", "can", "you", "the")]
                matched_items = [f for f in manifest if any(w in f["name"].lower() or w in (f.get("snippet") or "").lower() for w in words)]

        offline_source = "offline-heuristic"
        offline_is_llm = False
        offline_footer = "\n\n---\n*⚠️ Mode: Local Offline Heuristic (Gemini bypassed or unavailable)*"

        if matched_items:
            lines = [f"Found **{len(matched_items)} matching document(s)** in `{target_folder}/`:\n"]
            for m in matched_items[:15]:
                lines.append(f"- **{m['name']}** (`{m['path']}`, {m['size_kb']} KB, {m.get('modified_date') or m.get('relative_age', 'Unknown')})")
            msg = "\n".join(lines) + offline_footer
        else:
            msg = f"I scanned `{target_folder}/` ({len(manifest)} files indexed) but could not locate documents matching '{raw_prompt}'." + offline_footer

        print(f"[Storage Copilot Response] Prompt: '{raw_prompt}' -> Model: '{offline_source}' (is_llm={offline_is_llm}) | Type: 'query_info'")
        return jsonify({
            "type": "query_info",
            "message": msg,
            "version": APP_VERSION,
            "model_source": offline_source,
            "is_llm": offline_is_llm,
            "actions": [],
            "quick_chips": [
                "📋 Sort Student Vouchers",
                "👥 Separate Peer Documents",
                "🏷️ Clean Download Names",
                "🧹 Clean Duplicates"
            ]
        })

    if "voucher" in lower_p or "fee" in lower_p or "challan" in lower_p:
        plan_desc = "Organize student fee vouchers into academic and peer directories"
        for item in manifest:
            fn = item["name"].lower()
            snip = item["snippet"].lower()
            is_voucher = any(k in fn or k in snip for k in ("voucher", "fee", "challan", "slip"))
            if not is_voucher:
                continue

            peer_match = None
            for p_name in peer_names:
                if p_name.lower() in fn or p_name.lower() in snip:
                    peer_match = p_name
                    break

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
            peer_match = None
            for p_name in peer_names:
                if p_name.lower() in fn or p_name.lower() in snip:
                    peer_match = p_name
                    break

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
                snip = item["snippet"].lower()
                clean_title = fn

                # 1. Academic prioritization
                if any(k in snip for k in ("knowledge representation", "artificial intelligence", "lecture", "course", "assignment", "semester", "bsai")):
                    target_d = "Documents/University/AI"
                    if "knowledge representation" in snip:
                        clean_title = "Knowledge_Representation_Lecture.pdf"
                    elif "assignment" in snip:
                        clean_title = "AI_Course_Assignment.pdf"
                    else:
                        clean_title = "AI_Lecture_Notes.pdf"
                elif "1508" in fn or "neural" in snip:
                    clean_title = "Neural_Algorithm_Artistic_Style_Paper.pdf"
                    target_d = "Documents/University/AI"
                elif any(p_name.lower() in snip for p_name in peer_names):
                    p_found = next(p for p in peer_names if p.lower() in snip)
                    target_d = f"Documents/Peers/{p_found}"
                    clean_title = f"{p_found}_Receipt.pdf"
                elif "voucher" in snip or "fee" in snip:
                    target_d = "Documents/University/Vouchers"
                    clean_title = "Student_Fee_Voucher.pdf"
                elif "bill" in snip or "invoice" in snip or "utility" in snip or "electricity" in snip:
                    target_d = "Documents/Finances"
                    clean_title = "Electricity_Bill_Invoice.pdf"
                elif "doc-" in fn.lower() or "wa" in fn.lower():
                    # Clean double dots and derive title
                    clean_fn = re.sub(r"\.\.+", ".", fn)
                    stem_clean = os.path.splitext(clean_fn)[0]
                    clean_title = f"Document_{stem_clean}.pdf"
                    target_d = "Documents/Personal"
                else:
                    clean_title = re.sub(r"^\d{4}\.\d{4,5}v?\d*_", "Research_Paper_", fn)
                    target_d = "Documents/University/AI"

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

    elif any(re.search(rf"\b{re.escape(k)}\b", lower_p) for k in ("university", "academic", "lecture", "course", "study", "uni", "college", "campus", "school", "varsity", "homework", "hw", "slides", "notes", "assignment")):
        plan_desc = f"Organize academic university course materials for {owner_name}"
        for item in manifest:
            classification = classify_document(item["name"], item["snippet"])
            # STRICT RULE: Only organize actual Academic Study Materials into University Course directories!
            # Do NOT touch or route Identity Passes (e.g. Sumbal pass.pdf), Passports, or Utility Bills!
            if classification["is_academic_study"]:
                target_d = classification["target_dir"]
                clean_title = classification["suggested_title"]
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
            elif ("voucher" in lower_p or "fee" in lower_p or "all" in lower_p) and classification["category"] == "ACADEMIC_VOUCHER":
                target_d = classification["target_dir"]
                clean_title = classification["suggested_title"]
                if target_d not in created_dirs:
                    plan_actions.append({"action_id": f"step-{step_idx}", "type": "make_dir", "path": target_d})
                    created_dirs.add(target_d)
                    step_idx += 1
                plan_actions.append({
                    "action_id": f"step-{step_idx}",
                    "type": "move",
                    "source": item["path"],
                    "destination": f"{target_d}/{clean_title}",
                    "badge": "FILE_MOVE",
                    "rationale": f"Filed university tuition fee voucher into {target_d}",
                    "ai_inspected": True,
                    "inspected_preview": (item["snippet"] or "")[:100]
                })
                step_idx += 1

    elif any(term in lower_p for term in ("best suited", "best place", "proper place", "proper folder", "where they belong", "all the other files", "all other files", "other files", "rest of the files", "remaining files", "all files", "everything", "all docs", "all pdfs", "docs and pdfs", "sort download", "organize download", "sort files", "organize files", "tidy up")) or (any(v in lower_p for v in ("sort", "organize", "structure", "categorize", "tidy")) and any(d in lower_p for d in ("file", "files", "doc", "docs", "pdf", "pdfs", "download", "downloads", "everything", "all"))):
        plan_desc = f"Sort and organize candidate documents in '{target_folder}' into structured taxonomy"
        for item in manifest:
            fn = item["name"]
            snip = item["snippet"] or ""
            fn_lower = fn.lower()
            snip_lower = snip.lower()

            if fn.startswith("."):
                continue

            # 1. Peer isolation check
            matched_peer = None
            for p_name in peer_names:
                if p_name.lower() in fn_lower or p_name.lower() in snip_lower:
                    matched_peer = p_name
                    break

            if matched_peer:
                target_d = f"Documents/Peers/{matched_peer}"
                clean_title = fn
                rationale = f"Routed to peer folder for {matched_peer} to protect personal storage"
            else:
                classification = classify_document(fn, snip)
                target_d = classification["target_dir"]
                clean_title = classification["suggested_title"]
                cat = classification["category"]

                if cat == "TEMPORARY_TRASH":
                    plan_actions.append({
                        "action_id": f"step-{step_idx}",
                        "type": "trash",
                        "path": item["path"],
                        "badge": "TRASH",
                        "rationale": "Temporary/screenshot file queued for soft-delete with rollback"
                    })
                    step_idx += 1
                    continue
                else:
                    rationale = f"Semantically categorized as {classification['type_label']} into {target_d}"

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
                "inspected_preview": (snip or "")[:100]
            })
            step_idx += 1

    else:
        # Check if user prompt is an explicit informational query or content inquiry
        is_explicit_query = (any(lower_p.startswith(q) for q in (
            "can you", "could you", "what", "which", "where", "how many", "list", "show", "find", "search", "tell me", "is there", "do i have"
        )) or any(k in lower_p for k in (
            "what's the oldest", "whats the oldest", "what is the oldest", "oldest file", "newest file", "latest file", "list of files", "list files", "law of power", "laws of power", "java", "complete reference"
        )) or is_oldest_req or ("today" in lower_p) or ("yesterday" in lower_p)) and not any(k in lower_p for k in ("what else", "what can you do", "who are you", "what other", "suggestions"))

        true_action_verbs = ("move", "rename", "relocate", "transfer", "trash", "delete", "organize", "sort", "tidy", "clean")
        has_action_verb = any(re.search(rf"\b{v}\b", lower_p) for v in true_action_verbs)
        has_action_intent = has_action_verb and not is_explicit_query

        specific_matches = []
        if has_action_intent:
            for item in manifest:
                m_score, m_reason = score_document_match(raw_prompt, item)
                if m_score >= 75.0:
                    specific_matches.append((m_score, item, m_reason))

        if specific_matches:
            specific_matches.sort(key=lambda x: x[0], reverse=True)
            plan_desc = f"Organize matched document: {specific_matches[0][1]['name']}"
            for m_score, item, m_reason in specific_matches[:5]:
                classification = classify_document(item["name"], item["snippet"])
                target_d = classification["target_dir"]
                clean_title = classification["suggested_title"]
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
                    "rationale": f"Organized specific file '{item['name']}' based on match ({m_reason})",
                    "ai_inspected": True,
                    "inspected_preview": (item["snippet"] or "")[:100]
                })
                step_idx += 1
        else:
            # Informational query / temporal search intent in offline mode
            is_query_intent = (is_explicit_query or any(k in lower_p for k in (
                "find", "search", "list", "show", "what", "where", "how many", "which",
                "latest", "newest", "oldest", "recent", "today", "yesterday", "receipt", "lecture", "book", "voucher"
            ))) and not any(k in lower_p for k in ("what else", "what can you do", "who are you", "what other", "suggestions"))

            if is_query_intent:
                matched_items = []
                if is_oldest_req or any(w in lower_p for w in ("oldest", "earliest")):
                    matched_items = sorted_by_newest[-10:] if sorted_by_newest else []
                    matched_items.reverse()
                elif any(w in lower_p for w in ("latest", "newest", "most recent")):
                    matched_items = sorted_by_newest[:10]
                elif "today" in lower_p:
                    matched_items = [f for f in manifest if f.get("relative_age") == "Today"]
                elif "yesterday" in lower_p:
                    matched_items = [f for f in manifest if f.get("relative_age") == "Yesterday"]
                elif any(w in lower_p for w in ("receipt", "voucher", "fee", "bill", "invoice")):
                    matched_items = [f for f in manifest if any(k in f["name"].lower() or k in (f.get("snippet") or "").lower() for k in ("receipt", "voucher", "bill", "invoice", "fee"))]
                elif any(w in lower_p for w in ("lecture", "notes", "assignment", "academic", "university")):
                    matched_items = [f for f in manifest if classify_document(f["name"], f.get("snippet") or "")["is_academic_study"]]
                else:
                    # Query relevance and keyword matching
                    matched_items = [f for f in manifest if f.get("query_relevance", 0.0) > 0]
                    if not matched_items:
                        words = [w for w in re.findall(r"\w+", lower_p) if len(w) > 2 and w not in ("find", "search", "list", "show", "files", "download", "documents", "give", "can", "you", "the")]
                        matched_items = [f for f in manifest if any(w in f["name"].lower() or w in (f.get("snippet") or "").lower() for w in words)]

                offline_source = "offline-heuristic"
                offline_is_llm = False
                offline_footer = "\n\n---\n*⚠️ Mode: Local Offline Heuristic (Gemini bypassed or unavailable)*"

                if matched_items:
                    lines = [f"Found **{len(matched_items)} matching document(s)** in `{target_folder}/`:\n"]
                    for m in matched_items[:15]:
                        lines.append(f"- **{m['name']}** (`{m['path']}`, {m['size_kb']} KB, {m.get('modified_date') or m.get('relative_age', 'Unknown')})")
                    msg = "\n".join(lines) + offline_footer
                else:
                    msg = f"I scanned `{target_folder}/` ({len(manifest)} files indexed) but could not locate documents matching '{raw_prompt}'." + offline_footer

                print(f"[Storage Copilot Response] Prompt: '{raw_prompt}' -> Model: '{offline_source}' (is_llm={offline_is_llm}) | Type: 'query_info'")
                return jsonify({
                    "type": "query_info",
                    "message": msg,
                    "version": APP_VERSION,
                    "model_source": offline_source,
                    "is_llm": offline_is_llm,
                    "actions": [],
                    "quick_chips": [
                        "📋 Sort Student Vouchers",
                        "👥 Separate Peer Documents",
                        "🏷️ Clean Download Names",
                        "🧹 Clean Duplicates"
                    ]
                })

            # Prompt is general or advice-seeking: provide articulate guidance response, ZERO unneeded file mutations
            offline_source = "offline-heuristic"
            offline_is_llm = False
            offline_footer = "\n\n---\n*⚠️ Mode: Local Offline Heuristic (Gemini bypassed or unavailable)*"
            print(f"[Storage Copilot Response] Prompt: '{raw_prompt}' -> Model: '{offline_source}' (is_llm={offline_is_llm}) | Type: 'conversation'")
            return jsonify({
                "type": "conversation",
                "message": (
                    f"I've inspected your storage in '{target_folder}/' ({len(manifest)} real files detected).\n\n"
                    f"Here are tailored suggestions to organize your storage, {owner_name}:\n\n"
                    f"- 📚 **University Courseware**: Consolidate AI lectures and coursework into `Documents/University/<Subject>/`.\n"
                    f"- 🧾 **Fee Vouchers**: Sort your tuition challans into `Documents/University/Vouchers/`.\n"
                    f"- 👥 **Peer Separation**: Isolate files belonging to {', '.join(peer_names)} into `Documents/Peers/<Name>/`.\n"
                    f"- 🏷️ **Clean Cryptic Downloads**: Standardize messy WhatsApp downloads (`DOC-*.pdf`) into descriptive names.\n"
                    f"- 🗑️ **Cleanup**: Soft-delete temporary cache files and old screenshots into `.agent_trash`.\n\n"
                    f"Whenever you'd like me to formulate a plan, simply ask (e.g., *\"Organize all my university files\"* or *\"Sort student vouchers\"*)!"
                    + offline_footer
                ),
                "version": APP_VERSION,
                "model_source": offline_source,
                "is_llm": offline_is_llm,
                "actions": [],
                "quick_chips": [
                    "📋 Sort Student Vouchers",
                    "👥 Separate Peer Documents",
                    "🏷️ Clean Download Names",
                    "🗑️ Cleanup Old Screenshots"
                ]
            })

    offline_source = "offline-heuristic"
    offline_is_llm = False
    offline_footer = "\n\n---\n*⚠️ Mode: Local Offline Heuristic (Gemini bypassed or unavailable)*"

    if not plan_actions:
        print(f"[Storage Copilot Response] Prompt: '{raw_prompt}' -> Model: '{offline_source}' (is_llm={offline_is_llm}) | Type: 'conversation'")
        return jsonify({
            "type": "conversation",
            "message": (
                f"ℹ️ No eligible documents found in '{target_folder}/' matching \"{raw_prompt}\".\n\n"
                f"Inspected {len(manifest)} real files on disk. None required modification under this criteria."
                + offline_footer
            ),
            "version": APP_VERSION,
            "model_source": offline_source,
            "is_llm": offline_is_llm,
            "actions": [],
            "quick_chips": [
                "📋 Sort Student Vouchers",
                "👥 Separate Peer Documents",
                "🏷️ Clean Download Names",
                "🗑️ Cleanup Old Screenshots"
            ]
        })

    # Enforce batch destination collision prevention
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

    print(f"[Storage Copilot Response] Prompt: '{raw_prompt}' -> Model: '{offline_source}' (is_llm={offline_is_llm}) | Type: 'plan'")
    return jsonify({
        "type": "plan",
        "message": f"I've inspected your storage ({len(manifest)} real files on disk) and synthesized an Action Plan for: \"{plan_desc}\"" + offline_footer,
        "version": APP_VERSION,
        "model_source": offline_source,
        "is_llm": offline_is_llm,
        "actions": formatted_actions,
        "plan": action_plan,
        "quick_chips": [
            "⏩ Process Next Batch",
            "👥 Separate Peer Documents",
            "📋 Sort Student Vouchers",
            "🧹 Clean Duplicates"
        ]
    })

