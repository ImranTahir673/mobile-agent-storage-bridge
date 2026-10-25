import os
from flask import Blueprint, request, jsonify
from storage_copilot import config
from storage_copilot.db.ledger import get_db
from storage_copilot.execution.rollback import _rollback_internal

history_bp = Blueprint("history", __name__)

@history_bp.route("/lookup_history", methods=["POST"])
def lookup_history():
    """
    Searches action_ledger and trash_index in .ledger.db where source_path,
    destination_path, or original_rel_path matches the query pattern.
    Returns structured matches with original and current paths, timestamps,
    and whether the item is active or currently soft-deleted in .agent_trash.
    """
    data = request.get_json(force=True) or {}
    query = (data.get("query") or data.get("path") or "").strip()
    if not query:
        return jsonify({"error": "Missing or empty query parameter"}), 400

    conn = get_db()
    cursor = conn.cursor()
    try:
        clean_q = query.replace("\\", "/")
        p1 = f"%{query}%"
        p2 = f"%{clean_q}%"
        p3 = f"%{clean_q.replace('/', '\\')}%"
        cursor.execute("""
            SELECT 
                al.id AS action_id,
                al.batch_id AS plan_id,
                al.step_index,
                al.action_type AS type,
                al.source_path,
                al.destination_path,
                al.status AS action_status,
                al.executed_at,
                b.intent AS description,
                b.status AS batch_status,
                ti.trash_id,
                ti.original_rel_path,
                ti.trashed_rel_path,
                ti.purged_at
            FROM action_ledger al
            LEFT JOIN batches b ON al.batch_id = b.batch_id
            LEFT JOIN trash_index ti ON al.id = ti.action_id
            WHERE (
                al.source_path LIKE ? OR al.source_path LIKE ? OR al.source_path LIKE ? OR
                al.destination_path LIKE ? OR al.destination_path LIKE ? OR al.destination_path LIKE ? OR
                ti.original_rel_path LIKE ? OR ti.original_rel_path LIKE ? OR ti.original_rel_path LIKE ? OR
                ti.trashed_rel_path LIKE ? OR ti.trashed_rel_path LIKE ? OR ti.trashed_rel_path LIKE ?
            )
            ORDER BY al.executed_at DESC, al.id DESC
        """, (p1, p2, p3, p1, p2, p3, p1, p2, p3, p1, p2, p3))
        rows = cursor.fetchall()

        matches = []
        for r in rows:
            src = r["source_path"] or r["original_rel_path"] or ""
            dst = r["destination_path"] or ""
            
            # Format paths relative to storage root for readability
            canonical_base = os.path.realpath(config.BASE_DIR)
            if src and os.path.isabs(src):
                try:
                    src = os.path.relpath(os.path.realpath(src), canonical_base).replace("\\", "/")
                except Exception:
                    pass
            if dst and os.path.isabs(dst):
                try:
                    dst = os.path.relpath(os.path.realpath(dst), canonical_base).replace("\\", "/")
                except Exception:
                    pass

            is_trashed = False
            if r["type"] == "trash":
                if r["action_status"] == "EXECUTED" and not r["purged_at"]:
                    is_trashed = True
            elif dst and ".agent_trash" in dst:
                is_trashed = True

            matches.append({
                "plan_id": r["plan_id"],
                "action_id": f"step-{r['step_index'] + 1}",
                "type": r["type"],
                "source_path": src,
                "destination_path": dst,
                "timestamp": r["executed_at"] or "",
                "status": r["action_status"],
                "batch_status": r["batch_status"],
                "is_trashed": is_trashed
            })

        return jsonify({
            "status": "success",
            "query": query,
            "matches_count": len(matches),
            "matches": matches,
            "records": matches
        })
    except Exception as e:
        return jsonify({"error": f"Failed querying history: {e}"}), 500
    finally:
        conn.close()

@history_bp.route("/rollback_batch", methods=["POST"])
def rollback_batch():
    """Reverts an entire Action Plan batch using pre-logged inverse undo vectors."""
    data = request.get_json(force=True) or {}
    batch_id = data.get("batch_id")
    if not batch_id:
        return jsonify({"error": "Missing batch_id"}), 400

    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT * FROM batches WHERE batch_id = ?", (batch_id,))
        batch = cursor.fetchone()
        if not batch:
            return jsonify({"error": f"Batch '{batch_id}' not found"}), 404

        if batch["status"] == "ROLLED_BACK":
            return jsonify({"status": "already_rolled_back", "batch_id": batch_id}), 200

        reverted_count = _rollback_internal(cursor, batch_id)
        conn.commit()

        return jsonify({
            "status": "rolled_back",
            "batch_id": batch_id,
            "reverted_actions": reverted_count
        })
    except Exception as e:
        conn.rollback()
        return jsonify({"error": f"Failed rolling back batch: {e}"}), 500
    finally:
        conn.close()

@history_bp.route("/rollback_last", methods=["POST"])
def rollback_last():
    """Legacy helper: Reverts the most recently completed batch."""
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT batch_id FROM batches WHERE status = 'COMPLETED' ORDER BY created_at DESC LIMIT 1")
        row = cursor.fetchone()
        if not row:
            return jsonify({"error": "No completed batches found to rollback"}), 404

        batch_id = row["batch_id"]
        reverted_count = _rollback_internal(cursor, batch_id)
        conn.commit()

        return jsonify({
            "status": "rolled_back",
            "batch_id": batch_id,
            "reverted_actions": reverted_count
        })
    except Exception as e:
        conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()
