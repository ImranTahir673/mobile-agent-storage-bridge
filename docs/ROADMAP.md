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

## 8. Phase 4: Native Standalone Android Application (Jetpack Compose, Room DB, SAF) [NEXT MILESTONE]

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
