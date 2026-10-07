# Schema Specification: Action Plans, Rollback Ledger & Profiles

## 1. Overview

This document specifies the strict schema contracts governing the **Mobile Agent Storage Bridge**:
1. **The JSON Action Plan Schema**: Declarative contract emitted by the Decision Engine (LLM) and vetted by the Policy Gatekeeper.
2. **The SQLite Rollback Ledger Schema**: Persistent, write-ahead transactional database (`.ledger.db`) storing every executed operation, metadata, and inverted undo vectors for complete rollback capability.
3. **The User Profile & Peer Schema (`user_profile.json`)**: Device-local identity grounding schema and `/user_profile` bridge contract.
4. **The Phase 4 Interactive Diff Card & IPC Approval Schema**: Client-to-engine contract for granular per-card action approval in the native Android application.

---

## 2. JSON Action Plan Schema

All multi-step agent mutations must be encapsulated within a single declarative **Action Plan**.

### 2.1 JSON Schema Definition (Draft-07)

```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "title": "MobileBridgeActionPlan",
  "type": "object",
  "required": ["plan_id", "version", "description", "actions"],
  "properties": {
    "plan_id": {
      "type": "string",
      "format": "uuid",
      "description": "Unique UUIDv4 identifier for this execution plan."
    },
    "version": {
      "type": "string",
      "enum": ["1.0"],
      "description": "Schema version."
    },
    "timestamp": {
      "type": "string",
      "format": "date-time",
      "description": "ISO-8601 generation timestamp."
    },
    "description": {
      "type": "string",
      "minLength": 5,
      "maxLength": 256,
      "description": "Human-readable intent describing what this batch accomplishes."
    },
    "dry_run": {
      "type": "boolean",
      "default": false,
      "description": "If true, simulates operations and returns an impact report without touching disk."
    },
    "collision_strategy": {
      "type": "string",
      "enum": ["FAIL", "RENAME_NUMERIC", "RENAME_TIMESTAMP", "SKIP"],
      "default": "FAIL",
      "description": "How the execution driver handles destination collisions."
    },
    "actions": {
      "type": "array",
      "minItems": 1,
      "maxItems": 20,
      "description": "Ordered sequence of atomic operations (Max 20 per blast radius limits).",
      "items": {
        "type": "object",
        "required": ["action_id", "type"],
        "properties": {
          "action_id": {
            "type": "string",
            "description": "Deterministic step ID (e.g. step-1, step-2)."
          },
          "type": {
            "type": "string",
            "enum": ["make_dir", "move", "copy", "trash"]
          },
          "path": {
            "type": "string",
            "description": "Target path relative to storage root for make_dir or trash."
          },
          "source": {
            "type": "string",
            "description": "Source path relative to storage root for move or copy."
          },
          "destination": {
            "type": "string",
            "description": "Destination path relative to storage root for move or copy."
          },
          "expected_checksum": {
            "type": "string",
            "description": "Optional SHA256 pre-condition check to avoid moving modified files."
          }
        },
        "allOf": [
          {
            "if": { "properties": { "type": { "enum": ["make_dir", "trash"] } } },
            "then": { "required": ["path"] }
          },
          {
            "if": { "properties": { "type": { "enum": ["move", "copy"] } } },
            "then": { "required": ["source", "destination"] }
          }
        ]
      }
    }
  }
}
```

### 2.2 Realistic Example Action Plan Payload

```json
{
  "plan_id": "f47ac10b-58cc-4372-a567-0e02b2c3d479",
  "version": "1.0",
  "timestamp": "2026-10-06T20:45:00Z",
  "description": "Organize arXiv research papers and trash duplicate download receipts",
  "dry_run": false,
  "collision_strategy": "RENAME_NUMERIC",
  "actions": [
    {
      "action_id": "step-1",
      "type": "make_dir",
      "path": "Documents/Research/Computer_Vision"
    },
    {
      "action_id": "step-2",
      "type": "move",
      "source": "Download/1508.06576v2.pdf",
      "destination": "Documents/Research/Computer_Vision/Neural_Algorithm_of_Artistic_Style_Gatys.pdf"
    },
    {
      "action_id": "step-3",
      "type": "trash",
      "path": "Download/DOC-20220503-WA0103.pdf"
    }
  ]
}
```

---

## 3. SQLite Rollback Ledger Schema

The rollback ledger persists inside `~/storage/shared/.ledger.db` (or internal Termux storage `~/.agent_bridge/ledger.db`). It uses SQLite in **Write-Ahead Logging (WAL)** mode for transaction safety.

### 3.1 DDL Specification

```sql
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

-- 1. Batches table: Tracks overall Action Plan submissions
CREATE TABLE IF NOT EXISTS batches (
    batch_id TEXT PRIMARY KEY,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    intent TEXT NOT NULL,
    total_actions INTEGER NOT NULL,
    executed_actions INTEGER DEFAULT 0,
    status TEXT CHECK(status IN ('PENDING', 'RUNNING', 'COMPLETED', 'FAILED', 'ROLLED_BACK')) NOT NULL,
    completed_at TIMESTAMP,
    rolled_back_at TIMESTAMP
);

-- 2. Action Ledger: Records every single atomic disk mutation with undo instructions
CREATE TABLE IF NOT EXISTS action_ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id TEXT NOT NULL,
    step_index INTEGER NOT NULL,
    action_type TEXT CHECK(action_type IN ('make_dir', 'move', 'copy', 'trash')) NOT NULL,
    source_path TEXT,
    destination_path TEXT,
    file_size_bytes INTEGER DEFAULT 0,
    sha256_checksum TEXT,
    
    -- Inverted Undo Vector definition
    undo_action_type TEXT CHECK(undo_action_type IN ('remove_dir', 'move', 'delete_copy', 'untrash')) NOT NULL,
    undo_source_path TEXT,
    undo_destination_path TEXT,
    
    status TEXT CHECK(status IN ('PRE_LOGGED', 'EXECUTED', 'FAILED', 'REVERTED')) NOT NULL,
    executed_at TIMESTAMP,
    reverted_at TIMESTAMP,
    error_message TEXT,
    FOREIGN KEY(batch_id) REFERENCES batches(batch_id) ON DELETE CASCADE
);

-- 3. Trash Index: Maps all soft-deleted items to their original home
CREATE TABLE IF NOT EXISTS trash_index (
    trash_id TEXT PRIMARY KEY,
    action_id INTEGER NOT NULL,
    original_rel_path TEXT NOT NULL,
    trashed_rel_path TEXT NOT NULL,
    file_size_bytes INTEGER NOT NULL,
    sha256_checksum TEXT,
    trashed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    expires_at TIMESTAMP,
    purged_at TIMESTAMP,
    FOREIGN KEY(action_id) REFERENCES action_ledger(id) ON DELETE CASCADE
);

-- Indexes for performance
CREATE INDEX IF NOT EXISTS idx_actions_batch ON action_ledger(batch_id);
CREATE INDEX IF NOT EXISTS idx_actions_status ON action_ledger(status);
CREATE INDEX IF NOT EXISTS idx_trash_original ON trash_index(original_rel_path);
```

---

## 4. Inverted Undo Vector Specification

Every forward filesystem mutation generates a mathematically inverted operation stored in the ledger prior to execution:

| Forward Action | Parameters | Inverted Undo Vector | Undo Operational Behavior |
| :--- | :--- | :--- | :--- |
| **`make_dir`** | `path: D` | `remove_dir(path: D)` | Deletes directory `D` **only if empty**. |
| **`move`** | `source: S`, `destination: D` | `move(source: D, destination: S)` | Moves file from `D` back to original location `S`. |
| **`copy`** | `source: S`, `destination: D` | `trash(path: D)` | Soft-deletes the created duplicate at `D`. |
| **`trash`** | `path: S` $\to$ `.agent_trash/T` | `move(source: .agent_trash/T, destination: S)` | Restores the file from `.agent_trash/T` back to `S`. |

---

## 5. Rollback Execution Pattern

When rolling back a batch (`batch_id`), the engine applies undo vectors in **strict reverse order** of execution:

```sql
SELECT id, undo_action_type, undo_source_path, undo_destination_path
FROM action_ledger
WHERE batch_id = :target_batch_id 
  AND status = 'EXECUTED'
ORDER BY step_index DESC;
```

---

## 6. Semantic Search & Gathering Contract

### 6.1 Deterministic Historical Search Contract (`/lookup_history`)

#### Request Schema:
```json
{
  "query": "Download/1508.06576v2.pdf"
}
```

#### SQL Resolution Logic:
```sql
-- Step 1: Trace mutations in action_ledger matching query
SELECT batch_id, step_index, action_type, source_path, destination_path, status, executed_at
FROM action_ledger
WHERE source_path LIKE '%' || :query || '%' OR destination_path LIKE '%' || :query || '%'
ORDER BY id DESC;

-- Step 2: Check active or past trash records
SELECT trash_id, original_rel_path, trashed_rel_path, trashed_at, purged_at
FROM trash_index
WHERE original_rel_path LIKE '%' || :query || '%' OR trashed_rel_path LIKE '%' || :query || '%';
```

#### Response Schema:
```json
{
  "query": "sample_paper_1.pdf",
  "count": 1,
  "matches": [
    {
      "plan_id": "e4099765-710b-49c3-95d9-4ddeaa0e7b74",
      "step_index": 2,
      "type": "move",
      "source_path": "Download/sample_paper_1.pdf",
      "destination_path": "Documents/Organized_Batch/sample_paper_1.pdf",
      "status": "EXECUTED",
      "timestamp": "2026-10-07 14:09:41",
      "is_trashed": false
    }
  ]
}
```

---

## 7. Device-Local User Profile & Peer Separation Schema (`user_profile.json`)

To enable context-aware routing and isolate peer/third-party files, the bridge consumes a device-local `user_profile.json`.

### 7.1 JSON Schema Specification

```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "title": "UserProfileConfig",
  "type": "object",
  "required": ["user_identity", "known_peers", "routing_rules"],
  "properties": {
    "user_identity": {
      "type": "object",
      "required": ["primary_name"],
      "properties": {
        "primary_name": { "type": "string" },
        "aliases": { "type": "array", "items": { "type": "string" } },
        "identifiers": { "type": "array", "items": { "type": "string" } },
        "organization": { "type": "string" }
      }
    },
    "known_peers": {
      "type": "array",
      "items": {
        "type": "object",
        "required": ["name", "designated_folder"],
        "properties": {
          "name": { "type": "string" },
          "aliases": { "type": "array", "items": { "type": "string" } },
          "relation": { "type": "string" },
          "designated_folder": { "type": "string" }
        }
      }
    },
    "routing_rules": {
      "type": "object",
      "required": ["peer_documents_base", "personal_documents_base", "academic_base"],
      "properties": {
        "peer_documents_base": { "type": "string" },
        "personal_documents_base": { "type": "string" },
        "academic_base": { "type": "string" }
      }
    }
  }
}
```

### 7.2 Example `user_profile.json` Payload

```json
{
  "user_identity": {
    "primary_name": "Imran Tahir",
    "aliases": ["imran", "imran_tahir", "tahir_imran"],
    "identifiers": ["BSAI-182", "bsai182"],
    "organization": "University / Sidehustle"
  },
  "known_peers": [
    {
      "name": "Fawad",
      "aliases": ["fawad", "fawad_khan", "fawadkhan"],
      "relation": "Classmate / Colleague",
      "designated_folder": "Documents/Peers/Fawad"
    },
    {
      "name": "Sumbal",
      "aliases": ["sumbal", "sumbal_ai", "sumbal_cs"],
      "relation": "Classmate / Project Partner",
      "designated_folder": "Documents/Peers/Sumbal"
    }
  ],
  "routing_rules": {
    "peer_documents_base": "Documents/Peers",
    "personal_documents_base": "Documents/Personal",
    "academic_base": "Documents/University"
  }
}
```

### 7.3 `GET /user_profile` Endpoint Contract

* **Endpoint**: `GET /user_profile`
* **Response (HTTP 200)**:
```json
{
  "status": "ok",
  "profile": {
    "user_identity": {
      "primary_name": "Imran Tahir",
      "aliases": ["imran", "imran_tahir"],
      "identifiers": ["BSAI-182"],
      "organization": "University"
    },
    "known_peers": [
      {
        "name": "Fawad",
        "aliases": ["fawad", "fawad_khan"],
        "relation": "Classmate",
        "designated_folder": "Documents/Peers/Fawad"
      }
    ],
    "routing_rules": {
      "peer_documents_base": "Documents/Peers",
      "personal_documents_base": "Documents/Personal",
      "academic_base": "Documents/University"
    }
  }
}
```

---

## 8. Phase 4 Client-to-Engine IPC & Interactive Card Approval Schema

In the Phase 4 native Android application, the UI presents each proposed operation as an interactive Material 3 card. Users can toggle individual items on or off before confirming execution.

### 8.1 Plan Card Presentation Model (Engine -> UI)

```json
{
  "plan_id": "8d3e91a2-4c5b-6f7a-8b9c-0d1e2f3a4b5c",
  "description": "Organize Fall 2026 University Fee Receipts and Sort Peer Submissions",
  "total_actions": 3,
  "estimated_bytes": 1420500,
  "cards": [
    {
      "action_id": "step-1",
      "type": "make_dir",
      "path": "Documents/University/Fee_Vouchers",
      "display_title": "Create Folder: Fee Vouchers",
      "display_subtitle": "Documents/University/Fee_Vouchers",
      "icon_type": "FOLDER_CREATE",
      "is_destructive": false,
      "default_approved": true
    },
    {
      "action_id": "step-2",
      "type": "move",
      "source": "Download/voucher_182.pdf",
      "destination": "Documents/University/Fee_Vouchers/Imran_Fee_Voucher_Fall2026.pdf",
      "display_title": "Move: voucher_182.pdf",
      "display_subtitle": "-> Documents/University/Fee_Vouchers/Imran_Fee_Voucher_Fall2026.pdf",
      "file_size_formatted": "1.2 MB",
      "icon_type": "FILE_MOVE",
      "is_destructive": false,
      "default_approved": true
    },
    {
      "action_id": "step-3",
      "type": "move",
      "source": "Download/fawad_fee.pdf",
      "destination": "Documents/Peers/Fawad/fawad_fee.pdf",
      "display_title": "Route Peer File: Fawad Fee Receipt",
      "display_subtitle": "-> Documents/Peers/Fawad/fawad_fee.pdf",
      "peer_badge": "PEER: Fawad",
      "file_size_formatted": "220 KB",
      "icon_type": "PEER_MOVE",
      "is_destructive": false,
      "default_approved": true
    }
  ]
}
```

### 8.2 User Interactive Approval Submission (UI -> Engine)

When the user confirms the plan, the UI emits the user's granular decisions back to the local Android engine:

```json
{
  "plan_id": "8d3e91a2-4c5b-6f7a-8b9c-0d1e2f3a4b5c",
  "approved_action_ids": [
    "step-1",
    "step-2"
  ],
  "rejected_action_ids": [
    "step-3"
  ],
  "collision_strategy": "RENAME_NUMERIC",
  "dry_run": false
}
```

### 8.3 Execution Progress & Rollback IPC Events (Engine -> UI)

Emitted via Kotlin Flow / StateFlow (or SSE in bridge mode):

```json
{
  "event": "STEP_COMPLETED",
  "plan_id": "8d3e91a2-4c5b-6f7a-8b9c-0d1e2f3a4b5c",
  "completed_step": 1,
  "total_steps": 2,
  "action_id": "step-1",
  "message": "Created directory: Documents/University/Fee_Vouchers"
}
```

```json
{
  "event": "BATCH_COMPLETE",
  "batch_id": "8d3e91a2-4c5b-6f7a-8b9c-0d1e2f3a4b5c",
  "status": "COMPLETED",
  "executed_count": 2,
  "reverted_count": 0,
  "undo_available": true,
  "undo_token": "8d3e91a2-4c5b-6f7a-8b9c-0d1e2f3a4b5c"
}
```
