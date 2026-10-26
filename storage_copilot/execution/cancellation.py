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
    ) -> Dict[str, Any]:
        """
        Flags a specific `batch_id` (or all currently active batches if `batch_id` is omitted)
        for graceful cancellation. Can also pre-register cancellation for a known `batch_id`
        before its execution loop begins.
        """
        cancelled_ids: List[str] = []
        with self._lock:
            if batch_id:
                token = self._tokens.get(batch_id)
                if token is None:
                    token = CancellationToken(batch_id)
                    self._tokens[batch_id] = token
                token.cancel(reason=reason)
                cancelled_ids.append(batch_id)
            else:
                for bid in list(self._active_batch_ids):
                    tok = self._tokens.get(bid)
                    if tok:
                        tok.cancel(reason=reason)
                        cancelled_ids.append(bid)

        return {
            "status": "cancel_requested",
            "cancelled_batches": cancelled_ids,
            "reason": reason
        }

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
) -> Dict[str, Any]:
    return cancellation_manager.request_cancel(batch_id=batch_id, reason=reason)
