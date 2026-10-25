import os
import shutil
from datetime import datetime, timezone
from storage_copilot import config

def _rollback_internal(cursor, batch_id: str) -> int:
    """Internal helper to revert operations in reverse order for a given batch."""
    cursor.execute("""
        SELECT id, undo_action_type, undo_source_path, undo_destination_path 
        FROM action_ledger 
        WHERE batch_id = ? AND status = 'EXECUTED' 
        ORDER BY step_index DESC
    """, (batch_id,))
    actions_to_undo = cursor.fetchall()

    reverted_count = 0
    for row in actions_to_undo:
        act_id = row["id"]
        u_type = row["undo_action_type"]
        u_src = row["undo_source_path"]
        u_dst = row["undo_destination_path"]

        try:
            if u_type in ("move", "untrash"):
                if os.path.exists(u_src):
                    os.makedirs(os.path.dirname(u_dst), exist_ok=True)
                    shutil.move(u_src, u_dst)
                if u_type == "untrash":
                    cursor.execute("UPDATE trash_index SET purged_at = CURRENT_TIMESTAMP WHERE action_id = ?", (act_id,))

            elif u_type == "remove_dir":
                if os.path.exists(u_dst) and not os.listdir(u_dst):
                    os.rmdir(u_dst)

            elif u_type == "delete_copy":
                if os.path.exists(u_src):
                    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
                    trash_copy = os.path.join(config.TRASH_DIR, f"{ts}_copy_{os.path.basename(u_src)}")
                    shutil.move(u_src, trash_copy)

            cursor.execute("UPDATE action_ledger SET status = 'REVERTED', reverted_at = CURRENT_TIMESTAMP WHERE id = ?", (act_id,))
            reverted_count += 1
        except Exception as e:
            cursor.execute("UPDATE action_ledger SET error_message = ? WHERE id = ?", (f"Rollback error: {e}", act_id))

    cursor.execute("UPDATE batches SET status = 'ROLLED_BACK', rolled_back_at = CURRENT_TIMESTAMP WHERE batch_id = ?", (batch_id,))
    return reverted_count
