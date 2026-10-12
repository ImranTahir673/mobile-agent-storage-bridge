import os

server_path = os.path.join(os.path.dirname(__file__), "..", "server.py")
with open(server_path, "r", encoding="utf-8") as f:
    lines = f.readlines()

start_idx = None
for i, line in enumerate(lines):
    if "# STAGE 1: Three-Tier Intent Classification via Gemini" in line:
        start_idx = i - 1
        break

end_idx = None
for i, line in enumerate(lines):
    if "# 6. Heuristic Fallback (Active if Gemini is offline or API key missing)" in line:
        end_idx = i
        break

if start_idx is None or end_idx is None:
    raise ValueError("Markers not found!")

new_code = '''    # =========================================================================
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
                try:
                    cnt = len([f for f in os.scandir(entry.path) if not f.name.startswith(".")])
                    subfolders.append(f"{entry.name} ({cnt} items)")
                except Exception:
                    subfolders.append(entry.name)
            elif entry.is_file() and not entry.name.startswith("."):
                scanned_entries.append(entry)

    # Snippet inspection & PII scrubbing (up to 300 candidate files)
    manifest = []
    manifest_for_gemini = []
    manifest_lookup = {}
    for entry in scanned_entries[:300]:
        filename = entry.name
        file_size = entry.stat().st_size
        ext = os.path.splitext(filename)[1].lower()

        try:
            mtime = entry.stat().st_mtime
            mtime_str = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M")
        except Exception:
            mtime_str = ""

        raw_text = extract_file_snippet(entry.path, 300)
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
            "text_snippet": sanitized_snippet if sanitized_snippet else "None (binary/image)",
            "has_content": bool(sanitized_snippet)
        }
        manifest_for_gemini.append(manifest_item)

        internal_item = {
            "filename": filename,
            "name": filename,
            "path": rel_path,
            "extension": ext,
            "size_kb": manifest_item["size_kb"],
            "size_bytes": file_size,
            "modified_date": mtime_str,
            "snippet": sanitized_snippet,
            "text_snippet": manifest_item["text_snippet"],
            "has_content": manifest_item["has_content"]
        }
        manifest.append(internal_item)
        manifest_lookup[rel_path.lower()] = internal_item
        manifest_lookup[filename.lower()] = internal_item

    # Fast-path for Deduplication Commands & Queries
    is_deduplicate_req = any(k in normalized_prompt.lower() for k in ("duplicate", "duplicates", "dedup", "deduplicate", "repeated file", "repeated files"))
    if is_deduplicate_req:
        dup_res = find_duplicate_clusters(BASE_STORAGE_DIR, target_folder)
        is_clean_action = any(k in normalized_prompt.lower() for k in ("clean", "delete", "trash", "remove", "wipe")) or is_pure_affirmative
        if dup_res["total_duplicates"] == 0:
            return jsonify({
                "type": "conversation" if is_clean_action else "query_info",
                "message": (
                    f"✨ **No duplicate files detected in `{target_folder}/`!**\\n\\n"
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
                    f"Cleaning them will reclaim **{dup_res['reclaimable_human']}** of storage while safely preserving all original canonical files.\\n\\n"
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
                f"**Total Reclaimable Storage:** `{dup_res['reclaimable_human']}`\\n",
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
                "message": "\\n".join(lines),
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

    # Effective request context
    if is_pure_affirmative:
        last_asst_snippet = last_asst[:400].replace('\\n', ' ') if last_asst else normalized_prompt
        effective_request = f"The user confirmed with '{raw_prompt}' to proceed with the specific action, plan, or recommendation previously discussed by the assistant in the latest turn: \\\"{last_asst_snippet}\\\"."
    elif is_polite_action:
        effective_request = f"The user requested to proceed with the action: '{raw_prompt}'."
    else:
        effective_request = raw_prompt

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

LIVE DIRECTORY STATUS FOR '{target_folder}/':
- Top-level subfolders: {", ".join(subfolders) if subfolders else "None"}
- Direct files on disk ({len(manifest)} files indexed):
{json.dumps(manifest_for_gemini[:60], indent=2)}

YOUR AGENT PERSONA & CAPABILITIES:
- You are an articulate, highly capable AI agent (similar to Google DeepMind / Antigravity). You communicate with clarity, intelligence, and empathy directly to {owner_name}.
- You possess complete self-awareness of your abilities and system boundaries.
- WHAT YOU CAN DO: Local storage reconnaissance, content inspection with in-memory PII sanitization, intelligent file organization (academic vs peer vs personal bills), cryptographic duplicate detection, interactive diff approval, and 1-tap atomic rollbacks via SQLite WAL.
- WHAT YOU CANNOT DO: You cannot send emails, send SMS/WhatsApp messages, browse the live web, access cloud storage directly, launch external apps, or alter Android system configurations. If asked to do something out-of-scope, politely state your local storage boundaries and explain what local storage task you can help with instead.

REASONING RULES & UNIFIED CONTRACT:
Analyze the operational request and dialogue history with full, direct access to the live files manifest above.
Determine your response type natively:

1. "query_info" (SEARCH / LOOKUP / INSPECT / LIST):
   - Triggered when the user asks to find, list, search, view, audit, check, or inspect documents or categories (e.g. "find Java book", "list lectures", "find receipts", "what is inside Download", "what files are here?", "any more sorting for download?").
   - Directly inspect the real files manifest above. If matching documents exist (e.g. JavaTheCompleteReference.pdf for Java language), cite them with bold names, paths in code spans, sizes, and helpful details.
   - For folder listing or inventory requests, provide a clean, grouped markdown overview of the files.
   - For status or audit checks ("any more sorting for download?"), summarize what unorganized files remain or praise the clean state.
   - Set "actions": []

2. "conversation" (ADVICE / GREETINGS / CAPABILITIES / OUT-OF-SCOPE):
   - Triggered for greetings ("Hi", "Hello"), suggestions/advice ("What suggestions do you have for my files?", "How can I organize?"), capabilities ("What can you do?"), follow-ups ("What else?"), or out-of-scope requests (emails, web search).
   - Ground your advice in the actual live directory status above. Cite real filenames and real categories.
   - Set "actions": []

3. "plan" (ACTIONS / CONFIRMATIONS / ORGANIZING):
   - Triggered when the user explicitly commands an action ("Clean Download Names", "Sort student vouchers", "Separate peer documents", "How about we first sort...") OR confirms a previous suggestion ("Yes", "Proceed", "Do it").
   - Synthesize move/rename/trash actions referencing exact filenames from the manifest.
   - Academic Taxonomy:
     * Academic study materials (lectures, assignments, notes) -> Documents/University/<Subject>/
     * Personal tuition vouchers -> Documents/University/Vouchers/
     * Peer documents for classmates ({", ".join(peer_names)}) -> Documents/Peers/<PeerName>/
     * Utility/electricity bills -> Documents/Finances/
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
    if gemini_resp and isinstance(gemini_resp, dict):
        resp_type = (gemini_resp.get("type") or "conversation").lower()
        resp_message = gemini_resp.get("message") or ""
        raw_actions = gemini_resp.get("actions") or []
        quick_chips = gemini_resp.get("quick_chips") or [
            "🧹 Clean Duplicates",
            "📋 Sort Student Vouchers",
            "👥 Separate Peer Documents",
            "🏷️ Clean Download Names"
        ]

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
                    "actions": formatted_actions,
                    "plan": action_plan,
                    "quick_chips": quick_chips
                })

        elif resp_type in ("query_info", "conversation"):
            return jsonify({
                "type": resp_type,
                "message": resp_message or f"I've reviewed your storage in `{target_folder}/`.",
                "version": APP_VERSION,
                "actions": [],
                "quick_chips": quick_chips
            })
'''

replaced_lines = lines[:start_idx] + [new_code + "\n"] + lines[end_idx:]

with open(server_path, "w", encoding="utf-8") as f:
    f.writelines(replaced_lines)

print(f"Replacement successful! New total lines: {len(replaced_lines)}")
