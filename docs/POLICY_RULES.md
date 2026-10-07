# Policy Gatekeeper Safety Specification

## 1. Core Principles & Operating Boundaries

The **Policy Gatekeeper** enforces deterministic, non-negotiable safety guardrails between generative AI reasoning models and mobile storage systems. Generative AI outputs are probabilistic by nature; the filesystem must remain deterministic, privacy-preserving, and resilient against accidental data destruction, hallucinations, or prompt-injection attacks.

### 1.1 Core Operating Role
* **Primary Scope**: The AI agent operates strictly as a **local file management and semantic storage copilot**. It inspects, organizes, sanitizes, gathers, and categorizes files within user-designated storage zones on Android internal storage (`/storage/emulated/0` / `~/storage/shared`).
* **Zero Cloud Storage Architecture**: The application **never stores, uploads, or mirrors** user files, directory trees, or database records to external cloud storage. All operations, metadata ledgers (`.ledger.db`), safety vaults (`.agent_trash`), and user profile configurations (`user_profile.json`) remain strictly on the physical device. The device is the single source of truth.
* **Fail-Closed Storage Scoping**: The agent is permanently blocked from reading or modifying system paths, application private databases, `/Android/data`, `/Android/obb`, `.git`, or hidden runtime system trees.

```
       +---------------------------------------------+
       |           AI Candidate Action Plan          |
       +---------------------------------------------+
                              |
                              v
       +---------------------------------------------+
       |             POLICY GATEKEEPER               |
       |  [Rule 1] Path Containment & Sanitization   |
       |  [Rule 2] Storage Blacklist & System Dirs   |
       |  [Rule 3] Blast Radius Limit (Max 20/batch) |
       |  [Rule 4] Zero Hard-Delete (Trash Routing)  |
       |  [Rule 5] Collision Handling Strategy       |
       |  [Rule 6] Tier 1 vs Tier 2 Privacy Shield   |
       |  [Rule 7] Zero Cloud Storage Invariant      |
       |  [Rule 8] Semantic Search & Gathering Rules |
       |  [Rule 9] Peer Separation & Isolation       |
       |  [Rule 10] Standalone Client Invariants     |
       +---------------------------------------------+
                     /                 \
       (Passed)    v                     v    (Violated)
+-----------------------+           +-----------------------+
|  SQLite Pre-Ledger &  |           |   REJECT TRANSACTION  |
|   Atomic Execution    |           |   & Return Diagnostics|
+-----------------------+           +-----------------------+
```

---

## 2. Rule 1: Path Containment & Boundary Sandbox

### 2.1 Storage Boundary Root
* **Base Boundary**: `BASE_DIR = os.path.expanduser("~/storage/shared")` (resolving to `/sdcard`, `/storage/emulated/0`, or equivalent internal shared storage).
* **Hard Invariant**: No operation shall read, inspect, modify, or traverse outside the boundaries of `BASE_DIR`.

### 2.2 Path Sanitization Algorithm
Before evaluating any path, the Gatekeeper applies canonicalization:
1. Strip leading slashes, null bytes (`\0`), and control characters.
2. Resolve relative navigation elements (`.`, `..`) using canonical path resolution:
   ```python
   target_path = os.path.realpath(os.path.join(BASE_DIR, rel_path.lstrip("/")))
   ```
3. Evaluate boundary containment:
   ```python
   if not (target_path == BASE_DIR or target_path.startswith(BASE_DIR + os.sep)):
       raise PolicyViolation("Security Error: Path traversal outside BASE_DIR boundary.")
   ```
4. **Symlink Traversal Prevention**: If `target_path` is a symlink pointing to an exterior target (e.g., `/data/data/com.termux` or `/etc`), the resolution immediately aborts.

---

## 3. Rule 2: Storage Blacklists & Protected Entities

The filesystem contains system-critical directories, app sandboxes, version control directories, and bridge internal databases that the AI agent must never tamper with or read without explicit operational necessity.

### 3.1 Directory Blacklist Table

| Target Path Pattern | Access Level | Rationale | Violation Action |
| :--- | :--- | :--- | :--- |
| `Android/` (`Android/data`, `Android/obb`) | **DENIED** (Read & Write) | Android OS application sandboxes and game assets. Modifying breaks installed apps. | Immediate Rejection |
| `.agent_trash/` | **RESTRICTED** (Internal Only) | Internal soft-delete vault. Agent cannot delete, move, or modify `.agent_trash/` directly. | Immediate Rejection |
| `.ledger.db*` / `*.db` (root) | **DENIED** (Write) | Rollback databases and write-ahead logs. | Immediate Rejection |
| `.git/` / `.git*` | **DENIED** (Read & Write) | Version control repositories and operational hooks. | Immediate Rejection |
| System Roots (`/system`, `/proc`, `/sys`, `/data`) | **DENIED** (All) | Host Linux/Android filesystem protection. | Immediate Rejection |
| App Private Databases (`*.db`, `*.sqlite`) | **DENIED** (Write) | Third-party application private databases in shared storage. | Immediate Rejection |
| Hidden Dot-Dirs (`.*/`) at Root | **DENIED** (Write) | System/app configuration trees (e.g. `.nomedia`, `.config`). | Immediate Rejection |
| DCIM/Camera/ (Mass Mutation) | **RESTRICTED** | Photos/videos folder requires extra confirmation threshold. | Warning / Prompt |

### 3.2 Prohibited File Types & Executables
To prevent weaponization or accidental corruption of executable binaries:
- Mutation or creation of executable script binaries (`.sh`, `.apk`, `.dex`, `.so`, `.bin`) in the shared user storage is blocked unless explicitly authorized via elevated admin scope.

---

## 4. Rule 3: Blast Radius Limits

Autonomous agents can become trapped in runaway loops or hallucinate sweeping directory reorganizations. The Gatekeeper enforces strict containment boundaries:

### 4.1 Maximum Action Limits
* **Maximum Actions per Batch**: **20 operations**. Any Action Plan containing more than 20 individual atomic operations is rejected with `BLAST_RADIUS_EXCEEDED`.
* **Rationale**: Keeps transactions human-auditable, prevents prolonged device freezing, and ensures quick rollback cycles.

### 4.2 Cumulative Mutation Volume
* **Batch Size Ceiling**: Maximum cumulative file size modified per batch is capped at **500 MB** for automated moves and trash actions.
* Batches exceeding 500 MB require explicit interactive confirmation (`dry_run=true` followed by explicit user acknowledgment).

### 4.3 Depth & Recursion Bounds
* Maximum directory nesting depth allowed for automatic folder creation is **5 levels** relative to `BASE_DIR`.
* No blanket recursive deletions of non-empty directory trees. Folders must be emptied through individual trashed items or explicitly confirmed.

---

## 5. Rule 4: Zero Hard-Delete Policy (Soft-Delete Routing)

Under no circumstances does the Mobile Bridge invoke permanent physical deletion (`os.remove`, `os.unlink`, or `shutil.rmtree`) for agent requests.

### 5.1 Soft-Delete Invariant
* Every request to "delete" or "remove" a file is automatically routed through the **Trash Driver**.
* The file is moved into:
  ```
  ~/storage/shared/.agent_trash/<timestamp>_<uuid>_<original_filename>
  ```
* Example:
  - Source: `Download/bad_sample.pdf`
  - Trashed to: `.agent_trash/20261006_224500_a8f9c1_bad_sample.pdf`

### 5.2 Trash Metadata & Preservation
1. File permissions and modification timestamps are preserved.
2. A metadata record is persisted in the SQLite ledger detailing:
   - Original relative path.
   - Trashed path.
   - Deletion timestamp.
   - File size and SHA256 checksum (for non-trivial files).
3. Files remain in `.agent_trash/` for a default retention period of **30 days**.
4. Emptying the trash can only be initiated via explicit user-approved maintenance actions, never autonomously by an agent.

---

## 6. Rule 5: Collision Handling Strategy

Destination path collisions occur when an agent attempts to move, copy, or rename a file to a destination path that already exists on disk.

### 6.1 Default Invariant: No Silent Overwrites
Silent overwrites (`overwrite=true` without validation) are **strictly forbidden**.

### 6.2 Collision Resolution Strategies

When submitting an action, the plan may specify one of four deterministic strategies:

```
+-------------------+--------------------------------------------------------------------------+
| Strategy Key      | Operational Behavior                                                     |
+-------------------+--------------------------------------------------------------------------+
| FAIL (Default)    | Aborts the entire batch plan immediately prior to making any mutations.  |
|                   | Returns collision diagnostics to the Decision Engine.                   |
|                   |                                                                          |
| RENAME_NUMERIC    | Automatically appends an incrementing numeric counter before extension.  |
|                   | Example: document.pdf -> document (1).pdf -> document (2).pdf            |
|                   |                                                                          |
| RENAME_TIMESTAMP  | Appends an ISO-8601 millisecond timestamp to guarantee uniqueness.       |
|                   | Example: report_20261006T224500.pdf                                      |
|                   |                                                                          |
| SKIP              | Omits the collided operation, records a warning in execution logs, and   |
|                   | continues executing remainder of the batch.                              |
+-------------------+--------------------------------------------------------------------------+
```

---

## 7. Rule 6: Fail-Safe Privacy Shield & Tier 1 vs. Tier 2 Sanitization

Generative AI reasoning models must never be exposed to raw, unscrubbed personal data from user documents or mobile storage.

### 7.1 Tier 1 vs. Tier 2 Data Classification

Sanitization occurs strictly in **device RAM** before any snippet is emitted to the network:

```
+-----------------------------------------------------------------------------------------------+
|                                PRIVACY SANITIZATION MATRIX                                    |
+-----------------------------------------------------------------------------------------------+
| TIER 1: ZERO-TOLERANCE REDACTION (MANDATORY REGEX SCRUBBING)                                  |
| Pattern Category              | Regex Pattern                             | Replacement Token |
| ----------------------------- | ----------------------------------------- | ----------------- |
| Government ID / CNIC          | \b\d{5}-\d{7}-\d\b                        | [REDACTED_CNIC]   |
| Mobile / Telephone Numbers    | (?:\+92[- ]?|0)?3\d{2}[- ]?\d{7}\b        | [REDACTED_PHONE]  |
| Credit / Debit Payment Cards  | \b(?:\d{4}[- ]?){3}\d{4}\b                | [REDACTED_CARD]   |
| Bank IBANs                    | \b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b          | [REDACTED_IBAN]   |
| Personal Email Addresses      | \b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z]{2,}\b | [REDACTED_EMAIL] |
| Passwords, PINs, Auth Tokens  | (?i)(password|pin|secret|token)\s*[:=]\s*\S+ | [REDACTED_SECRET]|
|                                                                                               |
| TIER 2: SAFE RETAINED CONTEXT (PRESERVED FOR REASONING ACCURACY)                              |
| Data Category                 | Example Context Preserved                 | Reasoning Value   |
| ----------------------------- | ----------------------------------------- | ----------------- |
| Challan / Fee Voucher Numbers | Challan #182-9021-A                       | Matches vouchers   |
| Student Roll / Registration IDs| Roll No: BSAI-182                         | Matches coursework |
| Course Codes / Academic Titles| CS-301, AI-202, Machine Learning          | Course routing     |
| Issuing Banks / Universities  | HBL, Meezan Bank, FAST-NUCES               | Categorization     |
| General Amounts & Currencies  | PKR 145,000, USD 45.00                    | Receipt sorting    |
| Document & Fiscal Dates       | Fall Semester 2026, 2026-10-06             | Temporal indexing  |
+-----------------------------------------------------------------------------------------------+
```

### 7.2 Model Role Constraints (Zero-PII Inference)
1. **No PII in Output**: The model is strictly prohibited from inferring, reconstructing, guessing, or outputting PII (CNICs, phone numbers, card digits, email addresses, personal names) in its chain-of-thought, reasoning steps, action descriptions, or target destination names.
2. **Purely Categorical Interpretation**: The model must treat all input text purely as semantic category indicators (e.g., *"Fee Voucher"*, *"Assignment"*, *"Bank Statement"*, *"Lab Manual"*), without attributing files to specific individuals or account numbers.
3. **Fail-Closed Violation**: Any candidate plan proposing a filename containing unredacted PII patterns is immediately rejected by the Gatekeeper prior to dry-run simulation.

### 7.3 Zero Raw Image / Video Uploads
* Image and video organization is **strictly metadata-driven**:
  - Filenames, file sizes, creation timestamps, and filesystem modification dates.
  - Safe local EXIF metadata extraction (Year/Month, Camera Model) performed on-device.
* **Hard Invariant**: Raw image pixels, thumbnails, or video frames must **never** be transmitted over the network or uploaded to external LLM APIs.

---

## 8. Rule 7: Zero Cloud Storage & Local Device Invariant

The Mobile Agent Storage Bridge is built on a zero-cloud-storage architecture:

1. **No External Storage Mirrors**: User files, folders, and archives are never uploaded, replicated, or backed up to external cloud object stores (e.g., AWS S3, Google Cloud Storage, Firebase).
2. **Local Metadata Ledger**: The transactional SQLite database (`ledger.db`) and its write-ahead logs (`.ledger.db-wal`, `.ledger.db-shm`) reside exclusively within internal device storage.
3. **Local Trash Vault**: Trashed files reside entirely within `.agent_trash/` on the physical device storage root.
4. **Transient Inference Only**: When cloud-hosted LLMs (e.g., Gemini API) are used for reasoning, only sanitized, PII-scrubbed metadata and text snippets are sent over TLS 1.3 for stateless inference. No file data is retained or stored remotely.

---

## 9. Rule 8: Semantic Search & Gathering Safety Specification

When an agent executes semantic search or file gathering directives (e.g., *"Find my operating systems lab"* or *"Gather all machine learning papers into Documents/Research/ML"*):

1. **Declarative Plan Required**: The agent cannot directly copy or move search results. It must compile a standard declarative Action Plan adhering to `docs/SCHEMA_SPEC.md` using `move` or `copy` actions.
2. **Blast Radius Cap**: File gathering batches are strictly limited to **20 files** per batch to prevent runaway disk copying or mass moves.
3. **Non-Destructive Defaults**: Semantic search queries default to non-destructive inspections. When gathering files, target folder creation must precede file operations (`make_dir` followed by `move` or `copy`).
4. **Human-in-the-Loop Diff**: Every gathering operation must generate a dry-run terminal/UI diff report requiring explicit user confirmation before any disk mutation occurs.
5. **Full Rollback Guarantee**: All gathered files are pre-logged with inverted undo vectors in `.ledger.db`, ensuring 1-tap rollback restoration.

---

## 10. Rule 9: Peer Separation & Profile Isolation

To prevent misfiling classmate, peer, colleague, or third-party documents into personal user storage zones:

1. **Strict Non-Pollution Invariant**: The agent must **never** move or classify documents belonging to identified peers or third parties into personal user directories (`personal_documents_base`, e.g. `Documents/Personal/Receipts`).
2. **Identity Grounding via `user_profile.json`**: The agent grounds user identity in the device-local profile:
   - Primary user identity: `user_identity.primary_name` and `user_identity.identifiers`.
   - Known peers list: `known_peers` (names, aliases, relation, and designated folder).
3. **Automated Peer Folder Routing**:
   - Files containing peer names or aliases (e.g., `fawad fee.pdf`, `sumbal assignment.docx`) must be routed directly into their corresponding peer folder (e.g., `Documents/Peers/Fawad/`).
   - If an unrecognized peer document is encountered, it must be kept in the peer root (`Documents/Peers/Unassigned/`) or left untouched—never co-mingled with personal files.
4. **Precedence Over Generic Directives**: Rule 9 overrides broad gathering directives. Even if the user specifies *"Gather all fee vouchers into Documents/Personal/Receipts"*, any voucher matching a peer name must be routed to `Documents/Peers/<Peer_Name>` or excluded from the batch.

---

## 11. Rule 10: Standalone Mobile Client Gatekeeper Invariants

In the Phase 4 native Android application (APK), the local Android Service and embedded engine must enforce identical safeguards to the server-side gatekeeper:

1. **Equal Rigor on Mobile**: Running locally inside an Android Service does not relax any safety rules. The 20-action blast radius cap, 500 MB volume limit, path sandboxing, and directory blacklists remain active.
2. **Mandatory Room / SQLite Pre-Logging**: Every forward action must have an inverted undo vector pre-logged in the local Room/SQLite database before invoking disk I/O.
3. **Soft-Delete Only**: The Android client must route all file deletions to `.agent_trash/`. Direct calls to `File.delete()` on non-trash targets are strictly prohibited.
4. **Foreground Execution & Wake-Lock**: Large batches must run within a foreground Android Service with a persistent notification and wake-lock to prevent OS process killing mid-transaction.
5. **Granular Card-Level Approval**: The UI must allow users to toggle off specific actions from an Action Plan before execution, re-validating the resulting plan against the Gatekeeper.

---

## 12. Gatekeeper Validation Workflow

```python
def validate_action_plan(plan: dict, user_profile: dict = None) -> ValidationResult:
    actions = plan.get("actions", [])
    
    # Check 1: Blast Radius (Max 20 actions)
    if len(actions) > 20:
        return ValidationResult(valid=False, error="Batch exceeds max limit of 20 actions")
        
    for idx, action in enumerate(actions):
        # Check 2: Path Containment & Blacklists (/Android, .git, .agent_trash, .ledger.db)
        for path_field in ["source", "destination", "path"]:
            if path_field in action:
                resolved = resolve_safe_path(action[path_field])
                if is_blacklisted(resolved):
                    return ValidationResult(valid=False, error=f"Path {action[path_field]} is blacklisted")
                    
        # Check 3: Zero Hard-Delete Enforcement (Coerce delete -> trash)
        if action.get("type") == "delete":
            action["type"] = "trash"
            
        # Check 4: PII Shield in Destination Filenames (Tier 1 Check)
        if "destination" in action:
            if contains_tier1_pii(action["destination"]):
                return ValidationResult(valid=False, error="Proposed destination contains unredacted PII")
                
        # Check 5: Peer Separation Invariant (Rule 9)
        if user_profile and "destination" in action:
            dest = action["destination"]
            if violates_peer_isolation(dest, action.get("source", ""), user_profile):
                return ValidationResult(valid=False, error="Peer document routed into personal folder")
            
        # Check 6: Collision Verification
        if action.get("type") in ["move", "copy"]:
            dest = resolve_safe_path(action.get("destination"))
            if os.path.exists(dest):
                handle_collision(action, plan.get("collision_strategy", "FAIL"))
                
    return ValidationResult(valid=True, vetted_plan=plan)
```
