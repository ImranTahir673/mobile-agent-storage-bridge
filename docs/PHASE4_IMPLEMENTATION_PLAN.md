# Storage Copilot — Phase 4: Performance, Manifest Indexing & Server-Sent Events (SSE)
## Architectural Specification & Implementation Plan

> **Target Phase**: Phase 4 of 8  
> **Status**: Completed & Verified  
> **Pre-requisite**: Phases 1–3 completed. All 180+ existing tests pass.

---

## 1. Executive Summary & Objectives

In **Phases 1–3**, the monolithic server was decomposed into `storage_copilot/`, evolved into a Three-Tier Intent Architecture, and fortified with Dual-Mode Bearer Authentication, Origin Allowlists, and FUSE-safe SQLite WAL storage.

**Phase 4 Goal**:
Eliminate synchronous filesystem bottlenecks on large mobile directories (`/storage/emulated/0/Download` with 500+ files) and provide real-time Server-Sent Events (SSE) progress telemetry across scanning, intent classification, plan synthesis, and chunked execution.

### Core Deliverables:
1. **Two-Stage Lazy Manifest Indexer & Background Directory Cache (`storage_copilot/storage/scanner.py`)**:
   - Fast POSIX `os.scandir()` metadata pass with directory `(target_dir, dir_mtime, file_count)` cache invalidation.
   - Smart lazy snippet hydration (`hydrate_candidate_snippets`) that immediately hydrates cryptic filenames (`DOC-*`, `WA*`, `1508*`, `document*`, `scan*`, `IMG_*`) and small documents ($\le 5\text{ MB}$) up to `MAX_HYDRATE_CANDIDATES = 50`, while skipping expensive upfront extraction on binary/media assets (`.png`, `.jpg`, `.mp4`, `.mp3`, `.apk`, `.zip` $> 10\text{ MB}$) or when an exact filename match already satisfies the query.
2. **Thread-Safe Pub/Sub Event Bus (`storage_copilot/execution/event_bus.py`)**:
   - Zero-dependency standard library `queue.Queue` broadcaster supporting concurrent SSE subscribers and a bounded replay ring buffer for unit testing (`?once=1`).
   - Emits structured events: `connected`, `scan_progress`, `classifier_intent`, `plan_synthesized`, `execution_progress`, and `heartbeat`.
3. **Real-Time SSE Streaming Blueprint (`storage_copilot/api/stream_routes.py`)**:
   - Exposes `GET /stream` and alias `GET /events` with `Content-Type: text/event-stream`, `Cache-Control: no-cache`, and `X-Accel-Buffering: no`.
   - Supports deterministic unit-test query parameters `?once=1` and `?timeout=1`.
4. **EventSource Query-Token Authentication (`storage_copilot/security/auth.py`)**:
   - Standard browser `EventSource` cannot send custom `Authorization: Bearer` headers.
   - Extends `auth.py` so `GET /stream` and `GET /events` validate `?token=<token>` query parameters in addition to headers, while preserving loopback bypass when `COPILOT_ENFORCE_AUTH=0`.
5. **Single-Worker Multi-Threaded Production WSGI Runner (`server.py` & `scripts/start_daemon.sh`)**:
   - Prefers `waitress` (`threads=8`) or `gunicorn` (`--workers 1 --threads 8`) with fallback to threaded Flask (`threaded=True`), ensuring `_SNIPPET_CACHE`, `ManifestIndexManager`, and `EventBus` share a single process memory space without blocking on open SSE connections.
