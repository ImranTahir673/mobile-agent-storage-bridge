import time
import threading
from typing import Dict, Any, Optional, List


class CancellationToken:
    """Thread-safe cancellation token for an in-flight batch execution."""
    def __init__(self, batch_id: str):
        self.batch_id = batch_id
        self.created_at = time.time()
        self.cancelled_at: Optional[float] = None
        self.reason: str = ""
        self._event = threading.Event()

    def cancel(self, reason: str = "User requested cancellation") -> None:
        self.reason = reason
        self.cancelled_at = time.time()
        self._event.set()

    @property
    def is_cancelled(self) -> bool:
        return self._event.is_set()


class CancellationManager:
    """
    Registry of active and pre-cancelled batch tokens.
    Allows `POST /cancel_execution` to gracefully interrupt an in-flight batch
    between actions or chunks without corrupting filesystem or SQLite WAL state.
    """
    def __init__(self):
        self._lock = threading.Lock()
        self._tokens: Dict[str, CancellationToken] = {}
        self._active_batch_ids: List[str] = []

    def register_batch(self, batch_id: str) -> CancellationToken:
        with self._lock:
            token = self._tokens.get(batch_id)
            if token is None:
                token = CancellationToken(batch_id)
                self._tokens[batch_id] = token
            if batch_id not in self._active_batch_ids:
                self._active_batch_ids.append(batch_id)
            return token

    def request_cancel(
        self,
        batch_id: Optional[str] = None,
        reason: str = "User requested cancellation"
    ) -> Tuple[Dict[str, Any], int]:
        """
        Flags a specific batch_id (or active batches if omitted) for graceful cancellation.
        Returns:
            - (data, 200) with status="cancel_requested" when flagged
            - (data, 404) with status="not_found" when batch_id is unknown
            - (data, 409) with status="already_completed" when batch has already finished
        """
        from storage_copilot.db.ledger import get_db

        with self._lock:
            if batch_id:
                token = self._tokens.get(batch_id)
                if token is not None and not token.is_cancelled:
                    token.cancel(reason=reason)
                    return {
                        "status": "cancel_requested",
                        "cancelled_batches": [batch_id],
                        "reason": reason
                    }, 200

                try:
                    with get_db() as conn:
                        cur = conn.cursor()
                        cur.execute("SELECT status FROM batches WHERE batch_id = ?", (batch_id,))
                        row = cur.fetchone()
                        if not row:
                            return {"status": "not_found", "error": f"Batch '{batch_id}' not found"}, 404
                        b_status = row["status"]
                        if b_status in ("COMPLETED", "CANCELLED", "ROLLED_BACK", "FAILED"):
                            return {
                                "status": "already_completed",
                                "error": f"Batch '{batch_id}' has already finished with status '{b_status}'"
                            }, 409
                        elif b_status in ("PENDING", "RUNNING"):
                            token = CancellationToken(batch_id)
                            self._tokens[batch_id] = token
                            token.cancel(reason=reason)
                            return {
                                "status": "cancel_requested",
                                "cancelled_batches": [batch_id],
                                "reason": reason
                            }, 200
                except Exception:
                    return {"status": "not_found", "error": f"Batch '{batch_id}' not found"}, 404

                return {"status": "not_found", "error": f"Batch '{batch_id}' not found"}, 404

            else:
                cancelled_ids: List[str] = []
                for bid in list(self._active_batch_ids):
                    tok = self._tokens.get(bid)
                    if tok and not tok.is_cancelled:
                        tok.cancel(reason=reason)
                        cancelled_ids.append(bid)
                if not cancelled_ids:
                    return {"status": "not_found", "error": "No active batches currently executing"}, 404

                return {
                    "status": "cancel_requested",
                    "cancelled_batches": cancelled_ids,
                    "reason": reason
                }, 200

    def is_cancelled(self, batch_id: str) -> bool:
        with self._lock:
            tok = self._tokens.get(batch_id)
            return bool(tok and tok.is_cancelled)

    def clear_batch(self, batch_id: str) -> None:
        with self._lock:
            if batch_id in self._active_batch_ids:
                self._active_batch_ids.remove(batch_id)
            self._tokens.pop(batch_id, None)

    def reset(self) -> None:
        with self._lock:
            self._tokens.clear()
            self._active_batch_ids.clear()


# Global singleton CancellationManager
cancellation_manager = CancellationManager()


def request_batch_cancellation(
    batch_id: Optional[str] = None,
    reason: str = "User requested cancellation"
) -> Tuple[Dict[str, Any], int]:
    return cancellation_manager.request_cancel(batch_id=batch_id, reason=reason)
