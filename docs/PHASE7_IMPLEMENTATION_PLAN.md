# Storage Copilot — Phase 7: Micro-Batch Architecture & Android OS Watchdog Yields
## Architectural Specification & Implementation Plan

> **Target Phase**: Phase 7 of 8  
> **Status**: Ready for Implementation  
> **Pre-requisite**: Phases 1–6 completed. All 191 existing tests pass.

---

## 1. Executive Summary & Objectives

In **Phases 1–6**, Storage Copilot was modularized, equipped with a Three-Tier Intent Classifier, Zero-Trust Bearer & Origin Security, Two-Stage Lazy Manifest Indexing with SSE Streaming, `ContentDerivedNamer`, and 3-Gram Jaccard Near-Duplicate + Forgotten Files detection.

However, when executing large batches (e.g., 60 to 500+ file moves/trash operations) on physical Android hardware via Termux (`/storage/emulated/0`), three critical OS-level hazards arise:
1. **Android Watchdog & Thermal/I/O Reaping**: Continuous back-to-back FUSE filesystem mutations saturate the flash I/O controller and trigger Android's `ActivityManager` / phantom process / thermal watchdog killers unless the process explicitly yields CPU and I/O time between micro-batches (`yield_ms = 50` ms default).
2. **Rigid Failure Handling**: Previously, `executor.py` only supported unconditional auto-rollback on error. When a user runs a 100-file batch and step #35 fails due to a transient file lock or permission error, callers need configurable **Safe Partial Failure Recovery** (`on_error="partial"` vs `"rollback"`) where steps #1..34 remain safely committed in the SQLite WAL ledger, the exact error at step #35 is recorded, and `/rollback_batch` can still atomically revert the 34 executed steps on demand.
3. **In-Flight Batch Cancellation (`POST /cancel_execution`)**: Users had no way to gracefully stop a multi-chunk batch mid-execution without killing the server process and leaving an uncommitted SQLite transaction.

**Phase 7 Goal**:
Upgrade the execution engine with configurable **Micro-Batch Chunking (`chunk_size = 20`)**, **Explicit Android OS Watchdog Yields (`yield_ms = 50` ms)**, rich **`execution_progress` SSE telemetry** (`current_chunk`, `total_chunks`, `actions_completed`, `percent`), **Safe Partial Failure Recovery**, and a thread-safe **Cancellation Manager (`POST /cancel_execution`)**.

---

## 2. Core Components & Architecture

### 2.1 Thread-Safe Cancellation Manager (`storage_copilot/execution/cancellation.py`)

Manages in-flight batch execution tokens across concurrent HTTP worker threads:
- `register_batch(batch_id: str) -> CancellationToken`
- `request_cancel(batch_id: Optional[str] = None) -> Dict[str, Any]` (cancels a specific `batch_id` or the currently active batch if `batch_id` is omitted)
- `is_cancelled(batch_id: str) -> bool`
- `clear_batch(batch_id: str) -> None`

When cancellation is triggered during `execute_action_plan()`:
- The current step finishes cleanly and commits to SQLite WAL.
- The loop halts before the next step/chunk, leaving all already-executed steps intact and logged in `action_ledger` (`status = 'EXECUTED'`), so the user can either keep the completed subset or 1-tap rollback via `/rollback_batch`.

### 2.2 Upgraded Micro-Batch Executor (`storage_copilot/execution/executor.py`)

1. **Configurable Micro-Batch Parameters**:
   - `DEFAULT_CHUNK_SIZE = 20` (overridable per plan via `plan["chunk_size"]`).
   - `DEFAULT_YIELD_MS = 50` (50ms sleep yield between chunks, overridable via `plan["yield_ms"]`).
   - `on_error` policy:
     - `"rollback"` (default when `partial_recovery` is not set): automatically rolls back previous steps in the batch on disk error.
     - `"partial"` (or `partial_recovery=True`): preserves steps #1..N-1 committed in SQLite WAL, records the error on step #N, marks the batch with `partial_failure=True`, and allows `/rollback_batch` to atomically undo strictly the executed steps (#1..N-1).
2. **Rich Real-Time SSE Progress Events (`event="execution_progress"`)**:
   - Emits standardized fields on every chunk and lifecycle transition:
     ```json
     {
       "phase": "started" | "chunk_committed" | "completed" | "cancelled" | "partial_failure" | "rolled_back",
       "batch_id": "...",
       "current_chunk": 2,
       "chunk_index": 2,
       "total_chunks": 5,
       "actions_completed": 40,
       "executed": 40,
       "total": 100,
       "percent": 40.0
     }
     ```
3. **Bug Fix in `_invalidate_manifest_cache()`**:
   - Fixes the recursive typo in `_invalidate_manifest_cache()` so `manifest_index_manager.invalidate()` is properly invoked after every batch execution or rollback.

### 2.3 Cancellation & Execution Routes (`storage_copilot/api/plan_routes.py` & `auth.py`)

- Adds `POST /cancel_execution` to `plan_routes.py` (and registers `/cancel_execution` in `PROTECTED_MUTATION_PATHS` in `auth.py`).
- Supports cancelling by `{"batch_id": "..."}` or cancelling all active in-flight batches when called with `{}`.
