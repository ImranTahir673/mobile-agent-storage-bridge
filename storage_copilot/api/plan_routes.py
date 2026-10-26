import os
from flask import Blueprint, request, jsonify

from storage_copilot import config
from storage_copilot.db.ledger import get_db
from storage_copilot.execution.executor import execute_action_plan
from storage_copilot.execution.cancellation import request_batch_cancellation
from storage_copilot.execution.event_bus import publish_event
from storage_copilot.intelligence.intent_classifier import UserIntent, classify_intent
from storage_copilot.execution.tier_handlers import (
    handle_conversation_turn,
    handle_query_info_turn,
    handle_file_operation_turn
)

plan_bp = Blueprint("plan", __name__)


@plan_bp.route("/execute_plan", methods=["POST"])
def execute_plan():
    plan = request.get_json(force=True) or {}
    res_dict, status_code = execute_action_plan(plan)
    return jsonify(res_dict), status_code


@plan_bp.route("/cancel_execution", methods=["POST"])
def cancel_execution():
    """
    Phase 7 Endpoint: Gracefully signals an in-flight batch execution to stop
    after the current step commits to SQLite WAL, leaving filesystem state consistent.
    Accepts optional `{"batch_id": "...", "reason": "..."}`.
    """
    data = request.get_json(silent=True) or {}
    batch_id = (data.get("batch_id") or "").strip() or None
    reason = (data.get("reason") or "User requested cancellation").strip()
    res = request_batch_cancellation(batch_id=batch_id, reason=reason)
    publish_event("execution_progress", {
        "phase": "cancel_requested",
        "batch_id": batch_id or "active",
        "reason": reason
    })
    return jsonify(res), 200


@plan_bp.route("/recent_batch", methods=["GET"])
def get_recent_batch():
    """Returns the most recent completed or rolled-back batch for the persistent Undo banner."""
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            SELECT batch_id, intent, total_actions, executed_actions, status, completed_at, rolled_back_at 
            FROM batches 
            WHERE status IN ('COMPLETED', 'ROLLED_BACK', 'FAILED') 
            ORDER BY created_at DESC LIMIT 1
        """)
        row = cursor.fetchone()
        if not row:
            return jsonify({"status": "none", "batch": None})

        cursor.execute("""
            SELECT step_index, action_type, source_path, destination_path, executed_at, status
            FROM action_ledger
            WHERE batch_id = ? AND status = 'EXECUTED'
            ORDER BY step_index ASC
        """, (row["batch_id"],))
        action_rows = cursor.fetchall()

        execution_summary = []
        for a in action_rows:
            src_rel = os.path.relpath(a["source_path"], config.BASE_DIR).replace("\\", "/") if a["source_path"] else None
            dst_rel = os.path.relpath(a["destination_path"], config.BASE_DIR).replace("\\", "/") if a["destination_path"] else None
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
    Phase 2 & Phase 4 Three-Tier Intent Architecture Endpoint with SSE Telemetry:
    1. Classifies user intent via Deterministic Fast-Path Gate + Micro-LLM Router.
    2. Emits `classifier_intent` SSE frame.
    3. Dispatches strictly to dedicated tier handler.
    4. Emits `plan_synthesized` SSE frame.
    """
    data = request.get_json(force=True) or {}
    raw_prompt = (data.get("prompt") or "").strip()
    target_folder = data.get("target_folder") or "Download"
    conversation_history = data.get("conversation_history") or []

    classification = classify_intent(
        prompt=raw_prompt,
        conversation_history=conversation_history
    )
    intent = classification["intent"]

    publish_event("classifier_intent", {
        "intent": intent.value if hasattr(intent, "value") else str(intent),
        "fast_path_matched": classification.get("fast_path_matched", False),
        "sub_intent": classification.get("sub_intent", "")
    })

    if intent == UserIntent.CONVERSATION:
        result = handle_conversation_turn(
            prompt=raw_prompt,
            classification=classification,
            target_folder=target_folder,
            conversation_history=conversation_history
        )
    elif intent == UserIntent.QUERY_INFO:
        result = handle_query_info_turn(
            prompt=raw_prompt,
            classification=classification,
            target_folder=target_folder,
            conversation_history=conversation_history
        )
    else:
        result = handle_file_operation_turn(
            prompt=raw_prompt,
            classification=classification,
            target_folder=target_folder,
            conversation_history=conversation_history
        )

    publish_event("plan_synthesized", {
        "type": result.get("type"),
        "intent": result.get("intent"),
        "actions_count": len(result.get("actions", []))
    })

    return jsonify(result)
