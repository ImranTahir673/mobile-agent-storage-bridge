# Policy Gatekeeper Safety Specification

## 1. Core Principles

The **Policy Gatekeeper** enforces deterministic, non-negotiable safety guardrails between generative AI reasoning models and mobile storage systems. Generative AI outputs are probabilistic by nature; the filesystem must remain deterministic and resilient against accidental data destruction, hallucinations, or prompt-injection attacks.

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

The filesystem contains system-critical directories, app sandboxes, and bridge internal databases that the AI agent must never tamper with or read without explicit operational necessity.

### 3.1 Directory Blacklist Table

| Target Path Pattern | Access Level | Rationale | Violation Action |
| :--- | :--- | :--- | :--- |
| `Android/` (`Android/data`, `Android/obb`) | **DENIED** (Read & Write) | Android OS application sandboxes and game assets. Modifying breaks installed apps. | Immediate Rejection |
| `.agent_trash/` | **RESTRICTED** (Internal Only) | Internal soft-delete vault. Agent cannot delete, move, or modify `.agent_trash/` directly. | Immediate Rejection |
| `.ledger.db*` / `*.db` (root) | **DENIED** (Write) | Rollback databases and write-ahead logs. | Immediate Rejection |
| System Roots (`/system`, `/proc`, `/sys`, `/data`) | **DENIED** (All) | Host Linux/Android filesystem protection. | Immediate Rejection |
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

## 7. Gatekeeper Validation Workflow

```python
def validate_action_plan(plan: dict) -> ValidationResult:
    actions = plan.get("actions", [])
    
    # Check 1: Blast Radius
    if len(actions) > 20:
        return ValidationResult(valid=False, error="Batch exceeds max limit of 20 actions")
        
    for idx, action in enumerate(actions):
        # Check 2: Path Containment & Blacklists
        for path_field in ["source", "destination", "path"]:
            if path_field in action:
                resolved = resolve_safe_path(action[path_field])
                if is_blacklisted(resolved):
                    return ValidationResult(valid=False, error=f"Path {action[path_field]} is blacklisted")
                    
        # Check 3: Zero Hard-Delete Enforcement
        if action.get("type") == "delete":
            action["type"] = "trash"  # Coerce to soft-delete
            
        # Check 4: Collision Verification
        if action.get("type") in ["move", "copy"]:
            dest = resolve_safe_path(action.get("destination"))
            if os.path.exists(dest):
                handle_collision(action, plan.get("collision_strategy", "FAIL"))
                
    return ValidationResult(valid=True, vetted_plan=plan)
```
