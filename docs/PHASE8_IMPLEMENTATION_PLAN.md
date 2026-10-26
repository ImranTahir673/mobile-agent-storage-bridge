# Storage Copilot — Phase 8: Mobile Web UI Streaming, Selective Diff Execution & Manual Testing Runbook
## Architectural Specification & Implementation Plan

> **Target Phase**: Phase 8 of 8 (Final Phase of Web-First Architecture Track)  
> **Status**: Ready for Implementation  
> **Pre-requisite**: Phases 1–7 completed. All 194 existing tests pass.

---

## 1. Executive Summary & Scope Decision

Across **Phases 1–7**, the Python backend was modularized into `storage_copilot/`, upgraded with a Three-Tier Intent Classifier, Zero-Trust Bearer & Origin Security, Two-Stage Lazy Manifest Indexing with SSE Streaming, `ContentDerivedNamer`, 3-Gram Jaccard Near-Duplicate + Forgotten Files detection, and Micro-Batch Execution with Android OS Watchdog Yields.

**Phase 8 Scoping Decision**:
Before packaging a native Kotlin/Compose APK, we focus strictly on upgrading the **Mobile Web UI (`web/index.html`, `web/app.js`, `web/styles.css`)** so every feature built in Phases 1–7 can be tested interactively in the browser (locally on Termux or via Cloudflare Tunnel), accompanied by a comprehensive **Manual Testing Runbook (`docs/MANUAL_TESTING_GUIDE.md`)**.

---

## 2. Core Web UI Upgrades (`web/`)

### 2.1 Real-Time SSE Progress Bar & Telemetry (`GET /stream`)
- Connects browser `EventSource` to `/stream?token=<bearer_token>` (automatically reconnecting if dropped).
- Listens for:
  - `connected`: Updates header SSE connection pill (`Live Stream Online`).
  - `classifier_intent`: Shows real-time intent classification badge (`CONVERSATION`, `QUERY_INFO`, `FILE_OPERATION`).
  - `scan_progress`: Displays live directory scanning and lazy snippet hydration telemetry.
  - `execution_progress`: Renders an animated progress bar (`0%` to `100%`) showing `current_chunk / total_chunks` and `actions_completed / total`.

### 2.2 In-Flight Batch Cancellation (`POST /cancel_execution`)
- Displays a high-contrast **"⏹ Stop Execution"** button inside the live progress bar whenever a micro-batch execution is running.
- Clicking **Stop Execution** dispatches `POST /cancel_execution`, cleanly halting the batch after the current action commits to SQLite WAL and refreshing the 1-Tap Undo banner.

### 2.3 Interactive Diff Review Cards with Selective Action Checkboxes
- Each proposed action in a `plan` response is rendered as an interactive diff card with:
  - A checkbox (`checked` by default) allowing the user to include or exclude individual operations.
  - **Select All** / **Clear All** quick toggles and a live counter (`Execute Selected (N of M)`).
  - Operation badge (`MOVE`, `RENAME`, `PEER_MOVE`, `TRASH`, `FOLDER_CREATE`), source $	o$ destination path preview, rationale, and PII-sanitized snippet inspection badge.
- Submitting the plan sends **only the checked actions** to `POST /execute_plan`.

### 2.4 Phase 6 Deep Deduplication & Forgotten Files Toolbar
- Adds a dedicated quick-action bar in the Web UI with:
  - **🔍 Scan Duplicates** button + **Near-Duplicate (3-Gram Jaccard $\ge 85\%$)** toggle checkbox.
  - **🕰️ Forgotten Files Finder** button (audits stale downloads $\ge 90$ days, `.tmp`/`.cache` files, and orphaned screenshots).
  - **🌱 Seed Demo Fixtures** button (1-click seeding of all realistic university, peer, voucher, and bill fixtures, with optional duplicate seeding).
  - **⏪ 1-Tap Undo Banner**: Persistent top banner displaying the most recent completed/cancelled/partial batch with a 1-tap **Undo / Rollback Batch** button wired to `POST /rollback_batch`.
