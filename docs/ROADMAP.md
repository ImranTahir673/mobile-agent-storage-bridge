# Project Roadmap & Implementation Phases

## 1. Overview

This roadmap details the engineering phases required to evolve the **Mobile Agent Storage Bridge** from a hybrid developer prototype into an enterprise-grade, standalone consumer Android application.

```
+---------------------------------------------------------------------------------------------------+
| Phase 1: Hardened server.py (Gatekeeper, SQLite Ledger, Batch Plans) [COMPLETED]                  |
+---------------------------------------------------------------------------------------------------+
                                                  |
                                                  v
+---------------------------------------------------------------------------------------------------+
| Phase 2: Client & Decision Engine Evolution (Action Plan Generator & CLI) [COMPLETED]             |
+---------------------------------------------------------------------------------------------------+
                                                  |
                                                  v
+---------------------------------------------------------------------------------------------------+
| Phase 2.5: Custom Goals (--prompt) & Pre-Flight PII Privacy Shield [COMPLETED - ON DEVICE]        |
+---------------------------------------------------------------------------------------------------+
                                                  |
                                                  v
+---------------------------------------------------------------------------------------------------+
| Phase 2.6: Deterministic Ledger Lookup & Semantic Gathering (--find/--gather) [COMPLETED - DEVICE]|
+---------------------------------------------------------------------------------------------------+
                                                  |
                                                  v
+---------------------------------------------------------------------------------------------------+
| Phase 2.7: Local User Profile, Peer Separation & Contextual Routing [COMPLETED - ON DEVICE]       |
+---------------------------------------------------------------------------------------------------+
                                                  |
                                                  v
+---------------------------------------------------------------------------------------------------+
| Phase 3: Android Daemon Resiliency & Named Tunnels [COMPLETED - VALIDATED ON PHYSICAL HARDWARE]   |
+---------------------------------------------------------------------------------------------------+
                                                  |
                                                  v
+---------------------------------------------------------------------------------------------------+
| Phase 3.5: Unified Grounded Architecture & Temporal / Date-Range Engine [COMPLETED]               |
+---------------------------------------------------------------------------------------------------+
                                                  |
                                                  v
+---------------------------------------------------------------------------------------------------+
| Phase 4: Native Standalone Android Application (Jetpack Compose, Room DB, SAF) [NEXT MILESTONE]   |
+---------------------------------------------------------------------------------------------------+
```

---

## 2. Phase 1: Hardening `server.py` & Bridge Engine [COMPLETED]

Eliminated vulnerabilities in the mobile Flask server and replaced ad-hoc tool endpoints with the Policy Gatekeeper and SQLite Rollback Ledger.

### 2.1 Tasks & Deliverables

- [x] **1.1 Strict Path Canonicalization & Containment**
  - Replaced raw string containment in `resolve_safe_path` with `os.path.realpath` to protect against symlink traversal and Unicode normalization attacks.
  - Returns standardized HTTP 403 error codes for boundary violations.

- [x] **1.2 Policy Gatekeeper Integration**
  - Implemented storage blacklist enforcement (`/Android`, `.agent_trash`, system roots, root dot-files).
  - Implemented blast radius validation: Rejects any batch exceeding 20 actions or 500 MB cumulative size.
  - Implemented collision detection modes (`FAIL`, `RENAME_NUMERIC`, `RENAME_TIMESTAMP`, `SKIP`).

- [x] **1.3 SQLite Action Ledger (`ledger.db`) Implementation**
  - Implemented SQLite database initialization with WAL mode (`PRAGMA journal_mode = WAL`).
  - Created `batches`, `action_ledger`, and `trash_index` tables per `SCHEMA_SPEC.md`.
  - Ensured pre-logging of operations with calculated inverted undo vectors prior to disk writes.

- [x] **1.4 Batch Execution Endpoint (`/execute_plan`)**
  - Added `POST /execute_plan` accepting the declarative JSON Action Plan.
  - Supported `dry_run=true` returning an impact report and diff summary without modifying storage.
  - Wrapped sequential step executions in a failure-trap that auto-triggers batch rollback if any step throws an unhandled error.

- [x] **1.5 Rollback Endpoint (`/rollback_batch`)**
  - Added `POST /rollback_batch` accepting a `batch_id`.
  - Reverts operations in reverse order (`ORDER BY step_index DESC`).
  - Restores soft-deleted files from `.agent_trash/` back to their original locations.

- [x] **1.6 Production Server Runtime**
  - Supported production WSGI runner (`Waitress` / multi-worker `Gunicorn` via Termux) to prevent single-threaded connection stalls.

---

## 3. Phase 2: Client & Decision Engine Evolution [COMPLETED]

Transitioned client from single-action imperative tool calls to structured batch Action Plan generation.

### 3.1 Tasks & Deliverables

- [x] **2.1 Two-Stage Agent Protocol**
  - **Stage 1 (Reconnaissance)**: Read-only directory listing, text snippet inspection, and metadata gathering.
  - **Stage 2 (Plan Synthesis)**: Compile all proposed mutations into a single `ActionPlan` submitted to `/execute_plan`.

- [x] **2.2 Interactive Human-in-the-Loop CLI**
  - Render terminal table diffs showing:
    - Target files
    - Source $\to$ Destination paths
    - File size
    - Action type (`MOVE`, `TRASH`, `MKDIR`)
  - Require explicit user confirmation `[Y/n]` before firing non-dry-run batches.

- [x] **2.3 Automated End-to-End Test Suite**
  - Created test scripts validating:
    - Directory boundary escape attacks.
    - Blacklist violation rejection.
    - Collision resolution behavior.
    - 100% byte-for-byte fidelity after rollback of multi-file moves and trash actions.

---

## 4. Phase 2.5: Custom Interactive Goals & Pre-Flight PII Sanitization [COMPLETED & VALIDATED ON PHYSICAL HARDWARE]

Enhanced Decision Engine with natural-language user guidance and an uncompromising device-local privacy shield.

### 4.1 Tasks & Deliverables

- [x] **2.5.1 Natural Language Custom Goals (`--prompt` / `-p`)**
  - Implemented CLI argument `--prompt` / `-p` accepting custom user goals (e.g., `python agent_batch_runner.py --prompt "Only organize receipts and fee vouchers"`).
  - Added interactive CLI prompt: `Custom Goal / Instruction (press Enter for general cleanup): `.
  - Injected custom goals into Stage 1 reconnaissance and Stage 2 plan synthesis prompts, prioritizing explicit user constraints.

- [x] **2.5.2 Local Pre-Flight PII Privacy Shield**
  - Implemented deterministic device-local regex scrubbing on `/read_file_snippet` before any content leaves the device:
    - Government IDs / CNIC (`\b\d{5}-\d{7}-\d\b`) ➔ `[REDACTED_CNIC]`
    - Mobile numbers (`(?:\+92[- ]?|0)?3\d{2}[- ]?\d{7}\b`) ➔ `[REDACTED_PHONE]`
    - Payment card numbers (`\b(?:\d{4}[- ]?){3}\d{4}\b`) ➔ `[REDACTED_CARD]`
    - Email addresses ➔ `[REDACTED_EMAIL]`
  - Enforced model role constraints: forbid reconstructing or outputting PII in reasoning steps or proposed filenames.
  - Zero raw image uploads: file categorization is strictly metadata-driven (filenames, sizes, timestamps, EXIF year/month).

---

## 5. Phase 2.6: Deterministic Ledger Lookup & Semantic Gathering [COMPLETED & VALIDATED ON PHYSICAL HARDWARE]

Extended the platform from passive cleanup into active information retrieval and structured file gathering.

### 5.1 Tasks & Deliverables

- [x] **2.6.1 Deterministic Historical Search (`/lookup_history` / `--find`)**
  - Implemented SQLite query resolver tracking historical moves, renames, and soft-deletes via `action_ledger` and `trash_index`.
  - Resolved former paths and filenames to their current live disk location or soft-deleted vault target.

- [x] **2.6.2 Semantic Content Search & Gathering (`--gather` / `-g`)**
  - Enabled natural-language content querying (e.g., *"Find my operating systems lab"* or *"Gather all machine learning papers into Documents/Research/ML"*).
  - Synthesized candidate matching files into a standard declarative Action Plan using `move` or `copy` operations.
  - Enforced max 20-action blast radius cap and required explicit human-in-the-loop diff confirmation before executing mutations.
  - Guaranteed 1-tap rollback restoration via pre-logged inverted undo vectors in `.ledger.db`.

---

## 6. Phase 2.7: Local User Profile, Peer Separation & Contextual Routing [COMPLETED & VALIDATED ON PHYSICAL HARDWARE]

Enforced strict identity boundaries to prevent misfiling classmate, peer, or third-party documents as personal files.

### 6.1 Tasks & Deliverables

- [x] **2.7.1 Device-Local Configuration Template (`user_profile.json` & `user_profile.example.json`)**
  - Defined user identity schema (`primary_name`, `aliases`, `identifiers`, `organization`).
  - Defined known peer mappings (`name`, `aliases`, `relation`, `designated_folder`).
  - Established base routing rules (`peer_documents_base`, `personal_documents_base`, `academic_base`).

- [x] **2.7.2 Remote & Local Profile Endpoints (`GET/POST /user_profile`)**
  - Exposed bridge endpoint returning device-local user profile and peer mappings to remote agent runners.
  - Supported cross-mount safe resolution and profile persistence on the device.

- [x] **2.7.3 Contextual Routing & Strict Peer Separation in Agent Decision Engine**
  - Injected user identity and peer separation directives into agent system prompt.
  - Enforced zero peer pollution: third-party and peer documents are never moved to `personal_documents_base` even during broad gather directives.
  - Routed recognized peer documents directly into designated peer folders.

---

## 7. Phase 3: Android Daemon Resiliency & Named Tunnels [COMPLETED & VALIDATED ON PHYSICAL HARDWARE]

Ensured the phone server runs reliably in the background without being killed by Android's aggressive memory and battery managers. Fully verified on physical device hardware (`/storage/emulated/0`).

### 7.1 Tasks & Deliverables

- [x] **3.1 Termux Daemonization & Wake-Lock**
  - Integrated `termux-wake-lock` commands in startup scripts to prevent CPU sleep during OTA operations.
  - Configured `termux-notification` to display a persistent foreground service status.
  - Implemented graceful process management, PID tracking, and port cleanup (`scripts/start_daemon.sh`, `scripts/stop_daemon.sh`).

- [x] **3.2 Named Cloudflare Tunnel Configuration**
  - Supported both quick tunnels (`trycloudflare.com`) and dedicated named Cloudflare tunnels with static domains (`cloudflared tunnel run --token <TOKEN>`).
  - Automatically parse live tunnel endpoints and write to `.bridge_url` for consumption by client agents.

- [x] **3.3 Termux Boot Auto-Start**
  - Enabled automatic background launching upon device power-on via Termux:Boot integration.
  - Provided daemon status inspection tool reporting PID, memory footprint, port state, and tunnel connectivity (`scripts/status_daemon.sh`).

---

## 8. Phase 3.5: Unified Grounded Architecture & Temporal / Date-Range Engine [COMPLETED]

Eliminated brittle regex/stopword filters in favor of real on-device storage reconnaissance, smart two-tier snippet inspection, and a unified grounded intelligence pipeline with full temporal date-range querying and lifecycle archiving.

### 8.1 Tasks & Deliverables

- [x] **3.5.1 Unified Grounded Intelligence Pipeline (Single-Turn Architecture) [COMPLETED]**
  - Reconnaissance first: Python gathers live manifest (filenames, sizes, dates, PII-sanitized text) before calling LLM.
  - Single unified Gemini call eliminating two-stage latency and stopword scoring bottlenecks.
  - Smart Two-Tier Context: 1,000-char rich overview for directory scans + on-demand deep inspection (up to 12,000 chars) for specific document Q&A.
  - Zero external pip dependencies; strict on-device PII scrubbing in RAM.

- [x] **3.5.2 Temporal Recency & Relative Ordering Engine [COMPLETED]**
  - Natural language recency queries: *"Show latest files"*, *"What's the newest download?"*, *"List oldest files"*, *"What was modified most recently?"*.
  - Python mtime extraction & timestamp sorting (ascending / descending) directly reflected in Gemini's candidate ranking.

- [x] **3.5.3 Exact Calendar Date & Time-Window Filtering [COMPLETED]**
  - Exact calendar matching: *"Find files from today"*, *"What did I download yesterday?"*, *"Show files from October 10"*, *"Files downloaded on 2026-10-12"*.
  - Device clock grounding (`now_str` & `today_date`) to accurately resolve relative terms (*"today"*, *"yesterday"*, *"last Friday"*).

- [x] **3.5.4 Duration & Interval Range Queries [COMPLETED]**
  - Natural interval ranges: *"Files from the past 7 days"*, *"Files downloaded in the past 2 weeks"*, *"Files between March 2025 and May 2025"*, *"Files older than 30 days"*.
  - Python pre-flight date calculation and manifest filtering so Gemini receives the exact matching time bracket.

- [x] **3.5.5 Temporal Action Directives & Lifecycle Archiving [COMPLETED]**
  - Time-driven action plans: *"Move files older than 30 days to Documents/Archive/"*, *"Sort all files downloaded today into Documents/University/"*, *"Clean downloads older than 6 months"*.
  - Full atomic review via interactive diff cards and 1-tap rollback in `.ledger.db`.

---

## 9. Production Architecture & Hardening Track (Storage Copilot v0.7+)

In response to the comprehensive Technical Audit, the core Python backend is undergoing an 8-Phase Architectural Modernization to prepare for commercial multi-user deployment on Android.

```
+---------------------------------------------------------------------------------------------------+
| Phase 1: Structural Decomposition & Package Modularization (storage_copilot/) [COMPLETED]         |
+---------------------------------------------------------------------------------------------------+
                                                  |
                                                  v
+---------------------------------------------------------------------------------------------------+
| Phase 2: Three-Tier Intent Classifier & Grounded Reasoning Engine [NEXT MILESTONE]                |
+---------------------------------------------------------------------------------------------------+
                                                  |
                                                  v
+---------------------------------------------------------------------------------------------------+
| Phase 3: Security Hardening (Bearer Auth for Mutations, Restrict CORS, PII Word-Boundary Regex)   |
+---------------------------------------------------------------------------------------------------+
                                                  |
                                                  v
+---------------------------------------------------------------------------------------------------+
| Phase 4: Performance, Manifest Indexing & Server-Sent Events (SSE Streaming)                      |
+---------------------------------------------------------------------------------------------------+
                                                  |
                                                  v
+---------------------------------------------------------------------------------------------------+
| Phase 5: Content-Derived Collision-Free Naming Engine                                             |
+---------------------------------------------------------------------------------------------------+
                                                  |
                                                  v
+---------------------------------------------------------------------------------------------------+
| Phase 6: Deep Deduplication & "Forgotten Files" Finder                                            |
+---------------------------------------------------------------------------------------------------+
                                                  |
                                                  v
+---------------------------------------------------------------------------------------------------+
| Phase 7: Micro-Batch Architecture & Android OS Watchdog Yields                                    |
+---------------------------------------------------------------------------------------------------+
                                                  |
                                                  v
+---------------------------------------------------------------------------------------------------+
| Phase 8: Mobile Web UI Streaming & Native Standalone Android Application (Jetpack Compose / Room) |
+---------------------------------------------------------------------------------------------------+
```

### 9.1 Phase 1: Structural Decomposition & Package Modularization [COMPLETED]
- [x] **Modular Package Scaffolding (`storage_copilot/`)**:
  - `storage_copilot/config.py`: Centralized `.env` loading, cross-platform storage detection, path constants, and mutable quota tracking.
  - `storage_copilot/db/ledger.py`: Isolated SQLite WAL operations and schema initialization.
  - `storage_copilot/security/`: Dedicated PII sanitizer (`pii_sanitizer.py`) and hardened path resolver (`path_resolver.py`).
  - `storage_copilot/storage/`: Clean separation for content extraction (`content_extractor.py`), streaming deduplication (`deduplication.py`), collision resolution (`collision_resolver.py`), and storage scanning (`scanner.py`).
  - `storage_copilot/intelligence/`: Gemini client with multi-model failover (`gemini_client.py`), document classification (`document_classifier.py`), and search scoring (`search_engine.py`).
  - `storage_copilot/execution/`: Batch plan builder (`plan_builder.py`), micro-chunk executor (`executor.py`), and atomic rollback engine (`rollback.py`).
  - `storage_copilot/advice/`: Dynamic storage advisor and format category matching (`advisor.py`).
  - `storage_copilot/api/`: Seven clean Blueprint route modules with unified error handling.
- [x] **Backward-Compatible Entry Point (`server.py`)**:
  - Slashed `server.py` from 3,759 lines to 110 lines.
  - Implemented dynamic attribute mutation proxy (`_ServerCompatModule`) ensuring test suites and external runners modifying `server.BASE_DIR` stay 100% in sync with `storage_copilot.config`.
  - Verified 100% test pass rate across all 162 automated test cases.

### 9.2 Upcoming Implementation Phases

#### Phase 2: Three-Tier Intent Classifier & Grounded Reasoning Engine [COMPLETED]
- [x] Implement standalone `IntentClassifier` module with two-stage resolution:
  - Fast-path deterministic detection for unambiguous keywords and multi-turn affirmative context ("Yes", "Proceed", "Do it").
  - Structured, low-latency classification via `call_gemini_micro_classifier()` (`gemini-3.5-flash-lite`, ~50 tokens, temperature 0.0).
- [x] Implement dedicated tier handlers (`storage_copilot/execution/tier_handlers.py`):
  - `handle_conversation_turn()`: Pure chat/advice & format breakdown, 0 deep scans, 0 file mutations.
  - `handle_query_info_turn()`: Read-only search, temporal filters, snippet citations, 0 file mutations.
  - `handle_file_operation_turn()`: Action plan synthesis with interactive diff previews and collision validation.
- [x] Unify dual relevance scoring functions (`score_document_match` and `compute_query_relevance_score`) into `score_file_relevance()` (0-100 scale) with backward-compatible wrappers.
- [x] Slashed `storage_copilot/api/plan_routes.py` from ~1,399 lines to 110 lines by delegating to dedicated tier handlers.
- [x] Added dedicated Phase 2 test suite (`test_three_tier_intent.py`) with 100% pass rate.

#### Phase 3: Security Hardening & Zero-Trust Boundary [COMPLETED]
- [x] Add pre-shared Bearer Token authentication (`~/.storage_copilot_token`) required for all mutation endpoints (`/execute_plan`, `/write_file`, `/configure_api_key`, `/trash_file`, `/move_file`, `/make_directory`, `/rollback_batch`, `/rollback_last`, `POST /user_profile`).
- [x] Restrict CORS from wildcard `*` to verified localhost, loopback, and Termux WebView origins (`storage_copilot/security/auth.py`).
- [x] Fix PII regex overmatching: context guards preserving academic IDs (`BSAI-182`, `CS-301`), ArXiv IDs (`1508.06576v2`), and Unix millisecond timestamps (`1713000000000`) while scrubbing secrets, CNICs, and passports.
- [x] Move default `LEDGER_DB_PATH` from Android emulated storage (`/storage/emulated/0/.ledger.db`) to Termux app-private storage (`$HOME/.storage_copilot_ledger.db`) to eliminate SQLite WAL corruption on FUSE.
- [x] Added dedicated Phase 3 security test suite (`test_phase3_security.py`) with 100% pass rate.

#### Phase 4: Performance, Manifest Indexing & Server-Sent Events (SSE) [COMPLETED]
- [x] Background manifest indexer thread with file mtime cache invalidation.
- [x] Server-Sent Events (`GET /stream`) for real-time progress events during scanning and execution.
- [x] Lazy snippet extraction triggered only for relevant search candidates.
- [x] Production WSGI server configuration (`waitress` / `gunicorn`).

#### Phase 5: Content-Derived Collision-Free Naming Engine [COMPLETED]
- [x] Deep sequence & title extraction (`ContentDerivedNamer`) for university lectures, assignments, and papers.
- [x] Elimination of hardcoded sequence counters (`_04`, `_02`).
- [x] User-configurable naming templates via `user_profile.json`.

#### Phase 6: Deduplication & Stale File Reconnaissance [COMPLETED]
- [x] Near-duplicate text detection using 3-gram Jaccard similarity (threshold $\ge 0.85$).
- [x] "Forgotten Files Finder" identifying stale downloads (>90 days), temporary files, and orphaned screenshots.

#### Phase 7: Micro-Batch Architecture & Android OS Watchdog Yields [COMPLETED]
- [x] Chunked execution (20 files/batch) with explicit OS sleep yields (50ms) to prevent Android thermal/process killer termination.
- [x] Safe partial failure recovery and cancellation handlers.
- [x] Real-time SSE progress events (`execution_progress`) with per-chunk SQLite WAL commits.
- [x] Graceful in-flight batch cancellation via `POST /cancel_execution` and `cancellation_manager`.

#### Phase 8: Mobile Web UI Streaming & Native Standalone Android Application [NEXT]
- [ ] SSE streaming integration in `web/app.js`.
- [ ] Interactive diff preview cards with individual action checkboxes.
- [ ] Standalone Kotlin/Compose Android client synchronization.

---

## 10. Native Standalone Android Application (Jetpack Compose, Room DB, SAF)

Transition from the 3-tier hybrid developer prototype (Termux + Python + Cloudflare Tunnel + Desktop CLI) into an **All-in-One Standalone Android App**.

### 8.1 Detailed Milestones & Implementation Tasks

#### Milestone 4.1: Project Scaffolding & Architecture Setup
- [ ] Initialize Android Studio project with **Kotlin** and **Jetpack Compose (Material 3)**.
- [ ] Target Android 14+ (API Level 34), Min SDK 26 (Android 8.0).
- [ ] Configure Gradle dependencies:
  - Compose BOM, Navigation Compose, ViewModel lifecycle.
  - Kotlin Coroutines & Flow for asynchronous reactive operations.
  - Room Database (with SQLite WAL support) for persistent action ledgers.
  - Ktor Client / Retrofit for secure stateless Gemini API communication.
  - EncryptedSharedPreferences for safe on-device API key storage.
- [ ] Define clean MVVM / MVI architecture separating UI State, Domain Use Cases, and Storage Repository.

#### Milestone 4.2: Storage Permissions & Local File Provider
- [ ] Request and handle `android.permission.MANAGE_EXTERNAL_STORAGE` for Android 11+ (API 30+) via `Settings.ACTION_MANAGE_ALL_FILES_ACCESS_PERMISSION`.
- [ ] Implement fallback Storage Access Framework (SAF) folder picker for scoped storage environments.
- [ ] Create `AndroidFileRepository` providing high-performance, non-blocking file inspection, directory tree traversal, and metadata extraction across `/storage/emulated/0`.

#### Milestone 4.3: Porting Engine & Local PII Sanitizer to Native Android
- [ ] Port the **Two-Tier Privacy Sanitizer** to native Kotlin:
  - Implement Tier 1 Zero-Tolerance regex scrubbing (`CNIC`, `PHONE`, `CARD`, `IBAN`, `SECRETS`, `EMAIL`) in RAM before network dispatch.
  - Preserve Tier 2 context (`VOUCHER_NUMBERS`, `STUDENT_IDS`, `COURSE_CODES`, `DATES`, `AMOUNTS`) for reasoning fidelity.
- [ ] Port the **Policy Gatekeeper** to Kotlin:
  - Strict path canonicalization via `File.canonicalPath`.
  - Blacklists (`/Android`, `.agent_trash`, `.ledger.db`, `.git`, system trees).
  - Blast radius containment (max 20 actions, max 500 MB volume).
  - Deterministic collision handling (`FAIL`, `RENAME_NUMERIC`, `RENAME_TIMESTAMP`, `SKIP`).
- [ ] Implement Room Entities & DAOs matching `docs/SCHEMA_SPEC.md`:
  - `Batches`, `ActionLedger`, and `TrashIndex`.
  - Inverted undo vector generator (`make_dir` $\to$ `remove_dir`, `move` $\to$ `move_back`, `trash` $\to$ `untrash`).
- [ ] Create a Foreground Android Service (`StorageExecutionService`) with `PARTIAL_WAKE_LOCK` and persistent notification to guarantee uninterrupted atomic batch execution.

#### Milestone 4.4: UI/UX Build (Chat/Prompt Input, Diff Cards, 1-Tap Undo)
- [ ] **Home / Assistant Screen**:
  - Natural-language goal bar (text input + voice-to-text integration).
  - Quick action chips (*"Organize Downloads"*, *"Gather Fee Receipts"*, *"Clean Empty Folders"*, *"Undo Last Batch"*).
  - Device storage overview indicator (Used vs. Free storage, Cleaned space count).
- [ ] **Interactive Diff Review Cards**:
  - Render candidate actions as Material 3 cards with operation badges (`MOVE`, `COPY`, `TRASH`).
  - Display file details: source path, destination path, formatted size, and peer badges.
  - Granular selection: Checkbox / swipe toggle on each card enabling the user to approve or reject individual operations before firing the batch.
- [ ] **Execution Progress & 1-Tap Rollback**:
  - Live progress bar showing step-by-step execution.
  - Immediate bottom Snackbar upon batch completion with a high-contrast **"UNDO (1-Tap)"** action.
  - Ongoing Android Notification with an instant **"Rollback"** action button.
- [ ] **History & Audit Screen**:
  - Visual timeline of past batches and operations.
  - Soft-delete vault browser (`.agent_trash`) with individual or batch restore actions.

#### Milestone 4.5: APK Release, On-Device Testing & Monetization
- [ ] Implement inference modes:
  - **BYOK (Bring-Your-Own-Key)**: Input personal Gemini API key stored in `EncryptedSharedPreferences`.
  - **Freemium Tier**: Daily free quota supported by rewarded ads via Google AdMob SDK.
  - **Pro Subscription**: In-App Billing (Google Play Billing 6+) for automated daily background audits.
- [ ] Conduct end-to-end on-device test suite:
  - Permission acquisition and denial recovery.
  - 20-file batch execution on real `/storage/emulated/0/Download` directory.
  - 1-tap instant rollback verification (100% byte fidelity).
- [ ] Generate signed release APK / AAB bundle and publish documentation.
