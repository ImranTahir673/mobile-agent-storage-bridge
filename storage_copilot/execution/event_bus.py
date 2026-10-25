# storage_copilot/execution/event_bus.py
import json
import time
import queue
import threading
from collections import deque
from typing import Dict, Any, Generator, Optional


class EventBus:
    """
    Thread-safe, in-memory Server-Sent Events (SSE) pub/sub broker.
    Maintains subscriber queues for live connections and a bounded ring buffer
    of recent events so deterministic unit tests (?once=1) can inspect recent telemetry.
    """
    def __init__(self, max_history: int = 50):
        self._lock = threading.Lock()
        self._subscribers: list[queue.Queue] = []
        self._history: deque = deque(maxlen=max_history)

    def publish(self, event_type: str, data: Dict[str, Any]) -> Dict[str, Any]:
        """Broadcasts a structured SSE event to all active subscribers."""
        payload = {
            "event": event_type,
            "timestamp": round(time.time(), 3),
            "data": data
        }
        with self._lock:
            self._history.append(payload)
            dead_queues = []
            for q in self._subscribers:
                try:
                    q.put_nowait(payload)
                except queue.Full:
                    dead_queues.append(q)
            for dq in dead_queues:
                if dq in self._subscribers:
                    self._subscribers.remove(dq)
        return payload

    def get_recent_events(self, limit: int = 10) -> list[Dict[str, Any]]:
        """Returns up to `limit` most recent events from the ring buffer."""
        with self._lock:
            items = list(self._history)
        return items[-limit:] if limit > 0 else items

    def clear_history(self) -> None:
        with self._lock:
            self._history.clear()

    def subscribe(
        self,
        once: bool = False,
        timeout: float = 15.0,
        include_recent: bool = False
    ) -> Generator[str, None, None]:
        """
        Generator yielding SSE-formatted frames (`event: <name>\ndata: <json>\n\n`).
        - Always emits an immediate `connected` handshake frame.
        - If `once=True`, yields the handshake (plus any buffered recent events if requested)
          or waits up to `timeout` seconds for one live event, then cleanly exits.
        """
        sub_q: queue.Queue = queue.Queue(maxsize=250)
        with self._lock:
            self._subscribers.append(sub_q)
            recent_snapshot = list(self._history) if include_recent else []

        try:
            # 1. Immediate handshake event
            handshake = {
                "status": "connected",
                "server_time": round(time.time(), 3)
            }
            yield format_sse_frame("connected", handshake)

            # 2. Replay buffered recent events if requested
            for item in recent_snapshot:
                yield format_sse_frame(item["event"], item["data"])

            if once:
                # Try to drain any immediately queued event within timeout, then close
                try:
                    evt = sub_q.get(timeout=min(timeout, 0.25))
                    yield format_sse_frame(evt["event"], evt["data"])
                except queue.Empty:
                    pass
                return

            # 3. Continuous streaming loop with periodic heartbeats
            start_time = time.time()
            while True:
                try:
                    wait_window = min(timeout, 10.0) if timeout > 0 else 10.0
                    evt = sub_q.get(timeout=wait_window)
                    yield format_sse_frame(evt["event"], evt["data"])
                except queue.Empty:
                    yield format_sse_frame("heartbeat", {"status": "alive", "server_time": round(time.time(), 3)})
                    if timeout > 0 and (time.time() - start_time) >= timeout:
                        return
        finally:
            with self._lock:
                if sub_q in self._subscribers:
                    self._subscribers.remove(sub_q)


def format_sse_frame(event_name: str, data: Dict[str, Any]) -> str:
    """Formats an event dictionary into standard W3C Server-Sent Events wire format."""
    serialized = json.dumps(data, separators=(",", ":"))
    return f"event: {event_name}\ndata: {serialized}\n\n"


# Global singleton EventBus instance shared across threads
event_bus = EventBus()


def publish_event(event_type: str, data: Dict[str, Any]) -> Dict[str, Any]:
    """Convenience helper to broadcast an event on the global EventBus."""
    return event_bus.publish(event_type, data)
