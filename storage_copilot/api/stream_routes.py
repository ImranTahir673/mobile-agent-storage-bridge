# storage_copilot/api/stream_routes.py
from flask import Blueprint, Response, request, stream_with_context
from storage_copilot.execution.event_bus import event_bus

stream_bp = Blueprint("stream", __name__)


@stream_bp.route("/stream", methods=["GET"])
@stream_bp.route("/events", methods=["GET"])
def stream_events():
    """
    Phase 4 Server-Sent Events (SSE) Endpoint (`GET /stream` & `GET /events`).
    Streams real-time telemetry (`connected`, `scan_progress`, `classifier_intent`,
    `plan_synthesized`, `execution_progress`, `heartbeat`).

    Query Parameters:
    - `once=1`: Emit handshake (and recent events) then close immediately for unit tests.
    - `timeout=<seconds>`: Maximum idle wait time before closing stream (0 = infinite).
    - `replay=1`: Include recent buffered events immediately after handshake.
    - `token=<bearer>`: Supported by `security/auth.py` for browser EventSource clients.
    """
    once_arg = (request.args.get("once") or "").strip().lower()
    is_once = once_arg in ("1", "true", "yes")

    replay_arg = (request.args.get("replay") or "").strip().lower()
    include_recent = replay_arg in ("1", "true", "yes") or is_once

    raw_timeout = request.args.get("timeout")
    if raw_timeout is not None:
        try:
            timeout_sec = max(0.05, float(raw_timeout))
        except ValueError:
            timeout_sec = 1.0
    else:
        timeout_sec = 0.2 if is_once else 0.0

    generator = event_bus.subscribe(
        once=is_once,
        timeout=timeout_sec,
        include_recent=include_recent
    )

    headers = {
        "Content-Type": "text/event-stream; charset=utf-8",
        "Cache-Control": "no-cache, no-transform",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no"
    }
    return Response(
        stream_with_context(generator),
        mimetype="text/event-stream",
        headers=headers
    )
