# Schema Specification: Action Plans & Rollback Ledger

## 1. Overview

This document specifies the strict schema contracts governing the **Mobile Agent Storage Bridge**:
1. **The JSON Action Plan Schema**: Declarative contract emitted by the Decision Engine (LLM) and vetted by the Policy Gatekeeper.
2. **The SQLite Rollback Ledger Schema**: Persistent, write-ahead transactional database (`ledger.db`) storing every executed operation, metadata, and inverted undo vectors for complete rollback capability.

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

### Rollback Process Flow:
1. Begin SQLite Transaction.
2. Iterate through each row in reverse step index:
   - For `move`: Atomically move `undo_source_path` back to `undo_destination_path`.
   - For `remove_dir`: Check if directory is empty; if empty, call `os.rmdir`.
   - For `untrash`: Move file from `.agent_trash/` back to original relative path and mark `trash_index.purged_at = CURRENT_TIMESTAMP`.
3. Update `action_ledger.status = 'REVERTED'`.
4. Update `batches.status = 'ROLLED_BACK'` and `batches.rolled_back_at = CURRENT_TIMESTAMP`.
5. Commit SQLite Transaction.

---

## 6. Semantic Search & Gathering Contract

This section defines the contracts for deterministic historical lookups and semantic content-driven file gathering.

### 6.1 Deterministic Historical Search Contract (`/lookup_history`)

Allows instant, exact lookups for renamed, relocated, or soft-deleted files using the local SQLite ledger without touching file content.

#### Request Schema:
```json
{
  "query_type": "HISTORICAL_LOOKUP",
  "path": "Download/1508.06576v2.pdf"
}
```

#### SQL Resolution Logic:
```sql
-- Step 1: Trace forward moves from the original path
SELECT batch_id, action_type, source_path, destination_path, executed_at, status
FROM action_ledger
WHERE source_path = :query_path AND status = 'EXECUTED'
ORDER BY executed_at DESC LIMIT 1;

-- Step 2: Check if file was soft-deleted
SELECT trash_id, original_rel_path, trashed_rel_path, trashed_at, purged_at
FROM trash_index
WHERE original_rel_path = :query_path;
```

#### Response Schema:
```json
{
  "status": "FOUND",
  "original_path": "Download/1508.06576v2.pdf",
  "current_location": "Documents/Research/Computer_Vision/Neural_Algorithm_of_Artistic_Style_Gatys.pdf",
  "is_trashed": false,
  "last_batch_id": "f47ac10b-58cc-4372-a567-0e02b2c3d479",
  "modified_at": "2026-10-06T20:45:12Z"
}
```

---

### 6.2 Semantic Content Search & Gathering Contract

Enables natural-language content aggregation (e.g., *"Find my operating systems lab"* or *"Gather all machine learning papers into Documents/Research/ML"*). The agent synthesizes search findings into a standard declarative Action Plan before any mutation occurs.

#### Gathering Action Plan Example:
```json
{
  "plan_id": "7b89e1a2-3c4d-5e6f-7a8b-9c0d1e2f3a4b",
  "version": "1.0",
  "timestamp": "2026-10-07T14:30:00Z",
  "description": "Gather Machine Learning research papers into Documents/Research/ML",
  "dry_run": false,
  "collision_strategy": "RENAME_NUMERIC",
  "actions": [
    {
      "action_id": "step-1",
      "type": "make_dir",
      "path": "Documents/Research/ML"
    },
    {
      "action_id": "step-2",
      "type": "move",
      "source": "Download/2301.00001.pdf",
      "destination": "Documents/Research/ML/Deep_Learning_Foundations.pdf"
    },
    {
      "action_id": "step-3",
      "type": "copy",
      "source": "Documents/Drafts/attention_paper.pdf",
      "destination": "Documents/Research/ML/attention_paper.pdf"
    }
  ]
}
```

#### Invariants:
1. **Zero Raw Cloud Upload**: Content matching is achieved via PII-scrubbed inspection snippets; full files are never streamed to remote APIs.
2. **Blast Radius Guarantee**: Max 20 gathered actions per execution batch.
3. **Diff Confirmation**: Terminal/UI diff preview must be explicitly confirmed by the user (`[y/N]`) before non-dry-run execution.
4. **Reversible Mutations**: All `move` and `copy` gathering actions produce inverse vectors in `ledger.db` for 1-tap rollback.
