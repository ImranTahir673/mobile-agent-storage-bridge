import os
import time
import uuid
import shutil
from datetime import datetime, timezone
from storage_copilot import config
from storage_copilot.security.path_resolver import resolve_safe_path
from storage_copilot.storage.collision_resolver import resolve_collision
from storage_copilot.storage.deduplication import calculate_file_hash
from storage_copilot.db.ledger import get_db
from storage_copilot.execution.rollback import _rollback_internal

def execute_action_plan(plan: dict) -> tuple[dict, int]:
    """
    Executes a vetted Action Plan per docs/SCHEMA_SPEC.md:
    1. Validates batch size against safe upper capacity (up to 1,000 actions).
    2. Enforces dry_run mode if requested.
    3. Executes changes gradually in safe micro-chunks (20 files/chunk) with per-chunk SQLite WAL commits.
    4. Auto-rolls back all previous steps if any action fails at any point.
    Returns (response_dict, status_code).
    """
    plan = plan or {}
    plan_id = plan.get("plan_id") or str(uuid.uuid4())
    intent = plan.get("description") or "Batch filesystem mutation"
    actions = plan.get("actions", [])
    dry_run = bool(plan.get("dry_run", False))
    collision_strategy = plan.get("collision_strategy", "FAIL")

    if not actions:
        return {"error": "Action plan contains no actions"}, 400

    MAX_BATCH_ACTIONS = 1000
    EXECUTION_CHUNK_SIZE = 20

    # Safety guardrail (expanded to 1,000 files with gradual execution technique)
    if len(actions) > MAX_BATCH_ACTIONS:
        return {"error": f"Execution limit exceeded: batch size {len(actions)} > {MAX_BATCH_ACTIONS} maximum"}, 400

    # Validation & pre-computation phase
    prepared_steps = []
    for idx, act in enumerate(actions):
        act_type = act.get("type")
        if act_type not in ("make_dir", "move", "copy", "trash"):
            return {"error": f"Unsupported action type '{act_type}' at index {idx}"}, 400

        try:
            if act_type == "make_dir":
                target_path = resolve_safe_path(act.get("path", ""))
                prepared_steps.append({
                    "step_index": idx,
                    "action_type": "make_dir",
                    "src": None,
                    "dst": target_path,
                    "undo_type": "remove_dir",
                    "undo_src": None,
                    "undo_dst": target_path,
                    "checksum": None,
                    "size": 0
                })

            elif act_type == "move":
                src = resolve_safe_path(act.get("source", ""))
                dst = resolve_safe_path(act.get("destination", ""))
                if not os.path.exists(src):
                    return {"error": f"Source not found for move: {act.get('source')}"}, 404
                
                final_dst = resolve_collision(dst, collision_strategy)
                if not final_dst:
                    continue  # SKIP strategy
                
                size = os.path.getsize(src) if os.path.isfile(src) else 0
                checksum = calculate_file_hash(src) if os.path.isfile(src) else None
                prepared_steps.append({
                    "step_index": idx,
                    "action_type": "move",
                    "src": src,
                    "dst": final_dst,
                    "undo_type": "move",
                    "undo_src": final_dst,
                    "undo_dst": src,
                    "checksum": checksum,
                    "size": size
                })

            elif act_type == "copy":
                src = resolve_safe_path(act.get("source", ""))
                dst = resolve_safe_path(act.get("destination", ""))
                if not os.path.exists(src):
                    return {"error": f"Source not found for copy: {act.get('source')}"}, 404

                final_dst = resolve_collision(dst, collision_strategy)
                if not final_dst:
                    continue

                size = os.path.getsize(src) if os.path.isfile(src) else 0
                checksum = calculate_file_hash(src) if os.path.isfile(src) else None
                prepared_steps.append({
                    "step_index": idx,
                    "action_type": "copy",
                    "src": src,
                    "dst": final_dst,
                    "undo_type": "delete_copy",
                    "undo_src": final_dst,
                    "undo_dst": None,
                    "checksum": checksum,
                    "size": size
                })

            elif act_type == "trash":
                src = resolve_safe_path(act.get("path", ""))
                if not os.path.exists(src):
                    return {"error": f"File not found to trash: {act.get('path')}"}, 404

                ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
                trash_filename = f"{ts}_{uuid.uuid4().hex[:6]}_{os.path.basename(src)}"
                trashed_path = os.path.join(config.TRASH_DIR, trash_filename)
                size = os.path.getsize(src) if os.path.isfile(src) else 0
                checksum = calculate_file_hash(src) if os.path.isfile(src) else None

                prepared_steps.append({
                    "step_index": idx,
                    "action_type": "trash",
                    "src": src,
                    "dst": trashed_path,
                    "undo_type": "untrash",
                    "undo_src": trashed_path,
                    "undo_dst": src,
                    "checksum": checksum,
                    "size": size,
                    "trash_id": str(uuid.uuid4())
                })

        except (ValueError, FileExistsError) as e:
            return {"error": str(e), "failed_step": idx}, 400

    # Handle dry_run simulation
    if dry_run:
        return {
            "dry_run": True,
            "plan_id": plan_id,
            "actions_validated": len(prepared_steps),
            "summary": [
                {
                    "step": s["step_index"],
                    "action": s["action_type"],
                    "source": os.path.relpath(s["src"], config.BASE_DIR) if s["src"] else None,
                    "target": os.path.relpath(s["dst"], config.BASE_DIR) if s["dst"] else None
                }
                for s in prepared_steps
            ]
        }, 200

    # Execution Phase with Pre-Logging and Auto-Rollback
    conn = get_db()
    cursor = conn.cursor()
    try:
        # Pre-log batch
        cursor.execute(
            "INSERT INTO batches (batch_id, intent, total_actions, status) VALUES (?, ?, ?, 'RUNNING')",
            (plan_id, intent, len(prepared_steps))
        )
        conn.commit()

        # Technique: Gradual Progressive Execution in safe micro-chunks (20 files/chunk)
        # Keeps memory footprint low, flushes OS disk cache, and avoids Termux I/O throttling
        chunks = [prepared_steps[i:i + EXECUTION_CHUNK_SIZE] for i in range(0, len(prepared_steps), EXECUTION_CHUNK_SIZE)]
        executed_actions = []

        for chunk_idx, chunk in enumerate(chunks):
            for step in chunk:
                # 1. Pre-log action step
                cursor.execute("""
                    INSERT INTO action_ledger 
                    (batch_id, step_index, action_type, source_path, destination_path, file_size_bytes, 
                     sha256_checksum, undo_action_type, undo_source_path, undo_destination_path, status)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PRE_LOGGED')
                """, (
                    plan_id, step["step_index"], step["action_type"], step["src"], step["dst"],
                    step["size"], step["checksum"], step["undo_type"], step["undo_src"], step["undo_dst"]
                ))
                action_id = cursor.lastrowid

                if step["action_type"] == "trash":
                    cursor.execute("""
                        INSERT INTO trash_index 
                        (trash_id, action_id, original_rel_path, trashed_rel_path, file_size_bytes, sha256_checksum)
                        VALUES (?, ?, ?, ?, ?, ?)
                    """, (
                        step["trash_id"], action_id, os.path.relpath(step["src"], config.BASE_DIR),
                        os.path.relpath(step["dst"], config.BASE_DIR), step["size"], step["checksum"]
                    ))

                # 2. Perform Disk Mutation
                try:
                    if step["action_type"] == "make_dir":
                        os.makedirs(step["dst"], exist_ok=True)
                    elif step["action_type"] == "move":
                        os.makedirs(os.path.dirname(step["dst"]), exist_ok=True)
                        shutil.move(step["src"], step["dst"])
                    elif step["action_type"] == "copy":
                        os.makedirs(os.path.dirname(step["dst"]), exist_ok=True)
                        shutil.copy2(step["src"], step["dst"])
                    elif step["action_type"] == "trash":
                        shutil.move(step["src"], step["dst"])

                    # Mark executed
                    cursor.execute(
                        "UPDATE action_ledger SET status = 'EXECUTED', executed_at = CURRENT_TIMESTAMP WHERE id = ?",
                        (action_id,)
                    )
                    executed_actions.append(step)
                except Exception as disk_err:
                    # Mark step failed
                    cursor.execute(
                        "UPDATE action_ledger SET status = 'FAILED', error_message = ? WHERE id = ?",
                        (str(disk_err), action_id)
                    )
                    conn.commit()
                    # Trigger automatic rollback of already executed steps in this batch
                    _rollback_internal(cursor, plan_id)
                    conn.commit()
                    return {
                        "error": f"Execution failed at step {step['step_index']}: {disk_err}. Batch automatically rolled back.",
                        "batch_id": plan_id,
                        "rolled_back": True
                    }, 500

            # Commit SQLite WAL after each chunk to persist progress and keep journal size small
            conn.commit()

            # Gradual execution yield: 20ms pause between chunks so OS I/O cache flushes smoothly
            if chunk_idx < len(chunks) - 1:
                time.sleep(0.02)

        # Mark batch completed
        cursor.execute(
            "UPDATE batches SET status = 'COMPLETED', executed_actions = ?, completed_at = CURRENT_TIMESTAMP WHERE batch_id = ?",
            (len(executed_actions), plan_id)
        )
        conn.commit()

        # Build executed batch audit summary
        execution_summary = []
        now_ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        for step in executed_actions:
            idx = step["step_index"]
            act_orig = actions[idx] if idx < len(actions) else {}
            badge = act_orig.get("badge")
            src_rel = os.path.relpath(step["src"], config.BASE_DIR).replace("\\", "/") if step.get("src") else None
            dst_rel = os.path.relpath(step["dst"], config.BASE_DIR).replace("\\", "/") if step.get("dst") else None

            if not badge:
                if step["action_type"] == "trash":
                    badge = "TRASH"
                elif step["action_type"] == "make_dir":
                    badge = "FOLDER_CREATE"
                elif act_orig.get("peer_name") or (dst_rel and "/Peers/" in dst_rel):
                    badge = "PEER_MOVE"
                elif src_rel and dst_rel and os.path.basename(src_rel) != os.path.basename(dst_rel):
                    badge = "RENAME"
                else:
                    badge = "FILE_MOVE"

            execution_summary.append({
                "step_index": idx,
                "action_type": step["action_type"],
                "source": src_rel or act_orig.get("source") or act_orig.get("path") or "",
                "destination": dst_rel or act_orig.get("destination") or act_orig.get("path") or "",
                "badge": badge,
                "timestamp": now_ts
            })

        batch_info = plan.get("batch_info")
        if batch_info and isinstance(batch_info, dict):
            target_f = batch_info.get("target_folder", "Download")
            try:
                t_dir = resolve_safe_path(target_f)
                unorg_cnt = sum(1 for e in os.scandir(t_dir) if e.is_file() and not e.name.startswith("."))
                batch_info["remaining_files"] = unorg_cnt
            except Exception:
                pass

        return {
            "status": "success",
            "batch_id": plan_id,
            "executed_actions": len(executed_actions),
            "total_actions": len(prepared_steps),
            "chunks_processed": len(chunks),
            "chunk_size": EXECUTION_CHUNK_SIZE,
            "gradual_execution": True,
            "execution_summary": execution_summary,
            "batch_info": batch_info
        }, 200

    except Exception as e:
        conn.rollback()
        return {"error": f"Fatal transaction error: {e}"}, 500
    finally:
        conn.close()

def execute_plan_single(action_type: str, params: dict):
    plan_payload = {
        "plan_id": str(uuid.uuid4()),
        "description": f"Single {action_type} operation",
        "actions": [{"action_id": "step-1", "type": action_type, **params}]
    }
    res_dict, status_code = execute_action_plan(plan_payload)
    from flask import jsonify
    return jsonify(res_dict), status_code
