import os
import time
import uuid
import shutil
from datetime import datetime, timezone
from typing import Callable, Optional, Dict, Any, Tuple

from storage_copilot import config
from storage_copilot.security.path_resolver import resolve_safe_path
from storage_copilot.storage.collision_resolver import resolve_collision
from storage_copilot.storage.deduplication import calculate_file_hash
from storage_copilot.db.ledger import get_db
from storage_copilot.execution.rollback import _rollback_internal
from storage_copilot.execution.event_bus import publish_event
from storage_copilot.execution.cancellation import cancellation_manager

DEFAULT_CHUNK_SIZE = 20
DEFAULT_YIELD_MS = 50
MAX_BATCH_ACTIONS = 1000


def _invalidate_manifest_cache() -> None:
    try:
        from storage_copilot.storage.scanner import manifest_index_manager
        manifest_index_manager.invalidate()
    except Exception:
        pass


def execute_action_plan(
    plan: dict,
    step_hook: Optional[Callable[[int, Dict[str, Any]], None]] = None
) -> Tuple[Dict[str, Any], int]:
    """
    Phase 7 Micro-Batch Action Plan Executor:
    1. Validates batch size against safe upper capacity (up to 1,000 actions).
    2. Enforces dry_run mode if requested.
    3. Executes changes in configurable micro-batches (`chunk_size = 20` default)
       with explicit OS watchdog sleep yields (`yield_ms = 50` ms default) between chunks
       and per-chunk SQLite WAL commits.
    4. Emits rich `execution_progress` SSE events (`current_chunk`, `total_chunks`,
       `actions_completed`, `percent`).
    5. Supports graceful in-flight cancellation via `cancellation_manager` (`POST /cancel_execution`).
    6. Supports both Safe Partial Failure Recovery (`on_error="partial"` / `partial_recovery=True`,
       where pre-executed steps remain committed in SQLite WAL and can be rolled back via `/rollback_batch`)
       and full auto-rollback (`on_error="rollback"` default).
    """
    plan = plan or {}
    plan_id = plan.get("plan_id") or str(uuid.uuid4())
    intent = plan.get("description") or "Batch filesystem mutation"
    actions = plan.get("actions", [])
    dry_run = bool(plan.get("dry_run", False))
    collision_strategy = plan.get("collision_strategy", "FAIL")

    # Configurable micro-batch chunk size & OS watchdog sleep yield (ms)
    try:
        chunk_size = max(1, int(plan.get("chunk_size", DEFAULT_CHUNK_SIZE)))
    except (ValueError, TypeError):
        chunk_size = DEFAULT_CHUNK_SIZE

    try:
        yield_ms = max(0, int(plan.get("yield_ms", DEFAULT_YIELD_MS)))
    except (ValueError, TypeError):
        yield_ms = DEFAULT_YIELD_MS

    # Failure recovery mode: "partial" keeps prior executed steps committed; "rollback" auto-reverts
    on_error_mode = str(plan.get("on_error", "")).strip().lower()
    partial_recovery = bool(plan.get("partial_recovery", False)) or (on_error_mode in ("partial", "keep_committed", "preserve"))

    if not actions:
        return {"error": "Action plan contains no actions"}, 400

    if len(actions) > MAX_BATCH_ACTIONS:
        return {"error": f"Execution limit exceeded: batch size {len(actions)} > {MAX_BATCH_ACTIONS} maximum"}, 400

    # Pre-computation & path validation phase
    # Note: If `partial_recovery=True`, we defer missing-source checks to the execution loop
    # so that if action #35 of 100 has a missing/locked file, actions #1..34 still execute and commit!
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
                if not os.path.exists(src) and not partial_recovery:
                    return {"error": f"Source not found for move: {act.get('source')}"}, 404

                final_dst = resolve_collision(dst, collision_strategy)
                if not final_dst:
                    continue

                size = os.path.getsize(src) if os.path.exists(src) and os.path.isfile(src) else 0
                checksum = calculate_file_hash(src) if os.path.exists(src) and os.path.isfile(src) else None
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
                if not os.path.exists(src) and not partial_recovery:
                    return {"error": f"Source not found for copy: {act.get('source')}"}, 404

                final_dst = resolve_collision(dst, collision_strategy)
                if not final_dst:
                    continue

                size = os.path.getsize(src) if os.path.exists(src) and os.path.isfile(src) else 0
                checksum = calculate_file_hash(src) if os.path.exists(src) and os.path.isfile(src) else None
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
                src = resolve_safe_path(act.get("path") or act.get("source") or "")
                if not os.path.exists(src) and not partial_recovery:
                    return {"error": f"File not found to trash: {act.get('path') or act.get('source')}"}, 404

                ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
                trash_filename = f"{ts}_{uuid.uuid4().hex[:6]}_{os.path.basename(src)}"
                trashed_path = os.path.join(config.TRASH_DIR, trash_filename)
                size = os.path.getsize(src) if os.path.exists(src) and os.path.isfile(src) else 0
                checksum = calculate_file_hash(src) if os.path.exists(src) and os.path.isfile(src) else None

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

    total_steps = len(prepared_steps)
    chunks = [prepared_steps[i:i + chunk_size] for i in range(0, total_steps, chunk_size)]
    total_chunks = len(chunks)

    cancel_token = cancellation_manager.register_batch(plan_id)

    publish_event("execution_progress", {
        "phase": "started",
        "batch_id": plan_id,
        "current_chunk": 0,
        "chunk_index": 0,
        "total_chunks": total_chunks,
        "actions_completed": 0,
        "executed": 0,
        "total": total_steps,
        "percent": 0.0
    })

    conn = get_db()
    cursor = conn.cursor()
    executed_actions = []
    chunks_completed = 0
    total_yield_sleep_ms = 0

    try:
        cursor.execute(
            "INSERT INTO batches (batch_id, intent, total_actions, status) VALUES (?, ?, ?, 'RUNNING')",
            (plan_id, intent, total_steps)
        )
        conn.commit()

        for chunk_idx, chunk in enumerate(chunks):
            current_chunk_num = chunk_idx + 1

            for step in chunk:
                # Check cancellation token before starting next step
                if cancel_token.is_cancelled:
                    conn.commit()
                    cursor.execute(
                        "UPDATE batches SET status = 'COMPLETED', executed_actions = ?, completed_at = CURRENT_TIMESTAMP WHERE batch_id = ?",
                        (len(executed_actions), plan_id)
                    )
                    conn.commit()
                    _invalidate_manifest_cache()
                    pct = round((len(executed_actions) / total_steps) * 100.0, 1) if total_steps > 0 else 100.0
                    publish_event("execution_progress", {
                        "phase": "cancelled",
                        "batch_id": plan_id,
                        "current_chunk": current_chunk_num,
                        "chunk_index": current_chunk_num,
                        "total_chunks": total_chunks,
                        "actions_completed": len(executed_actions),
                        "executed": len(executed_actions),
                        "total": total_steps,
                        "percent": pct,
                        "reason": cancel_token.reason
                    })
                    return {
                        "status": "cancelled",
                        "cancelled": True,
                        "batch_id": plan_id,
                        "executed_actions": len(executed_actions),
                        "total_actions": total_steps,
                        "chunks_processed": chunks_completed,
                        "chunk_size": chunk_size,
                        "yield_ms": yield_ms,
                        "reason": cancel_token.reason
                    }, 200

                # 1. Pre-log action step in SQLite WAL
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
                    if step_hook is not None:
                        step_hook(step["step_index"], step)

                    if step["action_type"] == "make_dir":
                        os.makedirs(step["dst"], exist_ok=True)
                    elif step["action_type"] == "move":
                        if not os.path.exists(step["src"]):
                            raise FileNotFoundError(f"Source file missing at step {step['step_index']}: {step['src']}")
                        os.makedirs(os.path.dirname(step["dst"]), exist_ok=True)
                        shutil.move(step["src"], step["dst"])
                    elif step["action_type"] == "copy":
                        if not os.path.exists(step["src"]):
                            raise FileNotFoundError(f"Source file missing at step {step['step_index']}: {step['src']}")
                        os.makedirs(os.path.dirname(step["dst"]), exist_ok=True)
                        shutil.copy2(step["src"], step["dst"])
                    elif step["action_type"] == "trash":
                        if not os.path.exists(step["src"]):
                            raise FileNotFoundError(f"Source file missing at step {step['step_index']}: {step['src']}")
                        os.makedirs(config.TRASH_DIR, exist_ok=True)
                        shutil.move(step["src"], step["dst"])

                    cursor.execute(
                        "UPDATE action_ledger SET status = 'EXECUTED', executed_at = CURRENT_TIMESTAMP WHERE id = ?",
                        (action_id,)
                    )
                    executed_actions.append(step)

                except Exception as disk_err:
                    cursor.execute(
                        "UPDATE action_ledger SET status = 'FAILED', error_message = ? WHERE id = ?",
                        (str(disk_err), action_id)
                    )
                    conn.commit()
                    _invalidate_manifest_cache()

                    pct = round((len(executed_actions) / total_steps) * 100.0, 1) if total_steps > 0 else 0.0

                    # Safe Partial Failure Recovery Mode (Phase 7):
                    # Keep actions #1..N-1 safely committed in SQLite WAL and allow on-demand rollback of executed subset
                    if partial_recovery:
                        cursor.execute(
                            "UPDATE batches SET status = 'FAILED', executed_actions = ?, completed_at = CURRENT_TIMESTAMP WHERE batch_id = ?",
                            (len(executed_actions), plan_id)
                        )
                        conn.commit()
                        publish_event("execution_progress", {
                            "phase": "partial_failure",
                            "batch_id": plan_id,
                            "current_chunk": current_chunk_num,
                            "chunk_index": current_chunk_num,
                            "total_chunks": total_chunks,
                            "actions_completed": len(executed_actions),
                            "executed": len(executed_actions),
                            "total": total_steps,
                            "percent": pct,
                            "failed_step": step["step_index"],
                            "error": str(disk_err)
                        })
                        return {
                            "status": "partial_failure",
                            "partial_failure": True,
                            "rolled_back": False,
                            "batch_id": plan_id,
                            "executed_actions": len(executed_actions),
                            "total_actions": total_steps,
                            "failed_step": step["step_index"],
                            "error": f"Execution failed at step {step['step_index']}: {disk_err}. Pre-executed {len(executed_actions)} action(s) remain safely committed in SQLite WAL.",
                            "can_rollback_executed": len(executed_actions) > 0
                        }, 207

                    # Default Full Auto-Rollback Mode
                    _rollback_internal(cursor, plan_id)
                    conn.commit()
                    publish_event("execution_progress", {
                        "phase": "rolled_back",
                        "batch_id": plan_id,
                        "current_chunk": current_chunk_num,
                        "chunk_index": current_chunk_num,
                        "total_chunks": total_chunks,
                        "actions_completed": len(executed_actions),
                        "executed": len(executed_actions),
                        "total": total_steps,
                        "percent": pct,
                        "failed_step": step["step_index"],
                        "error": str(disk_err)
                    })
                    return {
                        "error": f"Execution failed at step {step['step_index']}: {disk_err}. Batch automatically rolled back.",
                        "batch_id": plan_id,
                        "rolled_back": True,
                        "failed_step": step["step_index"]
                    }, 500

            # Commit SQLite WAL after each micro-batch chunk
            conn.commit()
            chunks_completed += 1
            pct = round((len(executed_actions) / total_steps) * 100.0, 1) if total_steps > 0 else 100.0

            publish_event("execution_progress", {
                "phase": "chunk_committed",
                "batch_id": plan_id,
                "current_chunk": current_chunk_num,
                "chunk_index": current_chunk_num,
                "total_chunks": total_chunks,
                "actions_completed": len(executed_actions),
                "executed": len(executed_actions),
                "total": total_steps,
                "percent": pct
            })

            # Explicit Android OS Watchdog Sleep Yield between chunks (default 50ms)
            if chunk_idx < total_chunks - 1 and yield_ms > 0:
                time.sleep(yield_ms / 1000.0)
                total_yield_sleep_ms += yield_ms

        cursor.execute(
            "UPDATE batches SET status = 'COMPLETED', executed_actions = ?, completed_at = CURRENT_TIMESTAMP WHERE batch_id = ?",
            (len(executed_actions), plan_id)
        )
        conn.commit()
        _invalidate_manifest_cache()

        publish_event("execution_progress", {
            "phase": "completed",
            "batch_id": plan_id,
            "current_chunk": total_chunks,
            "chunk_index": total_chunks,
            "total_chunks": total_chunks,
            "actions_completed": len(executed_actions),
            "executed": len(executed_actions),
            "total": total_steps,
            "percent": 100.0
        })

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
            "total_actions": total_steps,
            "chunks_processed": chunks_completed,
            "total_chunks": total_chunks,
            "chunk_size": chunk_size,
            "yield_ms": yield_ms,
            "total_yield_sleep_ms": total_yield_sleep_ms,
            "gradual_execution": True,
            "execution_summary": execution_summary,
            "batch_info": batch_info
        }, 200

    except Exception as e:
        conn.rollback()
        return {"error": f"Fatal transaction error: {e}"}, 500
    finally:
        cancellation_manager.clear_batch(plan_id)
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
