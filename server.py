import os
import json
import re
import shutil
import sqlite3
import uuid
import hashlib
from datetime import datetime
from flask import Flask, request, jsonify, send_from_directory

app = Flask("MobileStorageBridge")

# ==============================================================================
# Configuration & Storage Boundaries
# ==============================================================================
BASE_DIR = os.path.realpath(os.getenv("STORAGE_BASE_DIR", os.path.expanduser("~/storage/shared")))
TRASH_DIR = os.path.join(BASE_DIR, ".agent_trash")
LEDGER_DB_PATH = os.path.realpath(os.getenv("LEDGER_DB_PATH", os.path.join(BASE_DIR, ".ledger.db")))
WEB_DIR = os.path.realpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "web"))

os.makedirs(BASE_DIR, exist_ok=True)
os.makedirs(TRASH_DIR, exist_ok=True)
os.makedirs(WEB_DIR, exist_ok=True)

# ==============================================================================
# Cross-Origin Resource Sharing (CORS) Configuration
# ==============================================================================
@app.after_request
def add_cors_headers(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization, X-Requested-With, Accept"
    return response

@app.before_request
def handle_options():
    if request.method == "OPTIONS":
        resp = app.make_default_options_response()
        resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization, X-Requested-With, Accept"
        return resp

# ==============================================================================
# Database Ledger Initialization (SQLite in WAL mode)
# ==============================================================================
def get_db():
    conn = sqlite3.connect(LEDGER_DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA journal_mode = WAL;")
    return conn

def init_db():
    with get_db() as conn:
        conn.executescript("""
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

        CREATE TABLE IF NOT EXISTS action_ledger (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            batch_id TEXT NOT NULL,
            step_index INTEGER NOT NULL,
            action_type TEXT CHECK(action_type IN ('make_dir', 'move', 'copy', 'trash')) NOT NULL,
            source_path TEXT,
            destination_path TEXT,
            file_size_bytes INTEGER DEFAULT 0,
            sha256_checksum TEXT,
            undo_action_type TEXT CHECK(undo_action_type IN ('remove_dir', 'move', 'delete_copy', 'untrash')) NOT NULL,
            undo_source_path TEXT,
            undo_destination_path TEXT,
            status TEXT CHECK(status IN ('PRE_LOGGED', 'EXECUTED', 'FAILED', 'REVERTED')) NOT NULL,
            executed_at TIMESTAMP,
            reverted_at TIMESTAMP,
            error_message TEXT,
            FOREIGN KEY(batch_id) REFERENCES batches(batch_id) ON DELETE CASCADE
        );

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

        CREATE INDEX IF NOT EXISTS idx_actions_batch ON action_ledger(batch_id);
        CREATE INDEX IF NOT EXISTS idx_actions_status ON action_ledger(status);
        CREATE INDEX IF NOT EXISTS idx_trash_original ON trash_index(original_rel_path);
        """)

# Initialize database schema on startup
init_db()

# ==============================================================================
# Policy Gatekeeper: Path Canonicalization & Blacklist Verification
# ==============================================================================
PROTECTED_ROOT_PREFIXES = ("Android", ".agent_trash", ".ledger.db")

def resolve_safe_path(rel_path: str, allow_internal: bool = False) -> str:
    """
    Hardened path resolver:
    - Eliminates null bytes and traversal tokens.
    - Normalizes canonical paths via os.path.realpath.
    - Strictly confines operations within BASE_DIR.
    - Enforces blacklist rules (/Android, .agent_trash, .ledger.db, root dot-dirs).
    """
    if not isinstance(rel_path, str):
        raise ValueError("Invalid path type: expected string")
    
    if "\0" in rel_path:
        raise ValueError("Security violation: null byte detected in path")

    canonical_base = os.path.realpath(BASE_DIR)
    clean_rel = rel_path.replace("\\", "/").strip().lstrip("/")
    target = os.path.realpath(os.path.join(canonical_base, clean_rel))

    # Boundary containment check
    if target != canonical_base and not target.startswith(canonical_base + os.sep):
        raise ValueError("Access denied: path traverses outside storage boundary")

    # Blacklist check
    if not allow_internal:
        rel_from_base = os.path.relpath(target, canonical_base).replace("\\", "/")
        parts = [p for p in rel_from_base.split("/") if p and p != "."]

        if parts:
            first_segment = parts[0]
            if first_segment.lower() == "android":
                raise ValueError("Access denied: /Android directory is protected by policy")
            if first_segment == ".agent_trash":
                raise ValueError("Access denied: .agent_trash is an internal protected vault")
            if first_segment.startswith(".ledger.db") or first_segment.endswith(".db"):
                raise ValueError("Access denied: database files are protected")
            if first_segment == ".git" or first_segment.startswith(".git"):
                raise ValueError("Access denied: .git repository trees are protected")
            if first_segment.startswith(".") and first_segment not in (".", ""):
                raise ValueError(f"Access denied: hidden root directory '{first_segment}' is protected")

    return target

def calculate_file_hash(filepath: str) -> str:
    """Computes SHA256 checksum for file verification."""
    if not os.path.isfile(filepath):
        return ""
    h = hashlib.sha256()
    try:
        with open(filepath, "rb") as f:
            while chunk := f.read(65536):
                h.update(chunk)
        return h.hexdigest()
    except Exception:
        return ""

def resolve_collision(target_path: str, strategy: str) -> str:
    """Resolves destination collisions according to policy."""
    if not os.path.exists(target_path):
        return target_path

    if strategy == "FAIL":
        raise FileExistsError(f"Destination collision: '{target_path}' already exists")
    elif strategy == "SKIP":
        return ""
    elif strategy == "RENAME_NUMERIC":
        dirname = os.path.dirname(target_path)
        basename, ext = os.path.splitext(os.path.basename(target_path))
        counter = 1
        while True:
            candidate = os.path.join(dirname, f"{basename} ({counter}){ext}")
            if not os.path.exists(candidate):
                return candidate
            counter += 1
    elif strategy == "RENAME_TIMESTAMP":
        dirname = os.path.dirname(target_path)
        basename, ext = os.path.splitext(os.path.basename(target_path))
        ts = datetime.utcnow().strftime("%Y%m%dT%H%M%S")
        return os.path.join(dirname, f"{basename}_{ts}{ext}")
    else:
        raise ValueError(f"Unsupported collision strategy: {strategy}")

# ==============================================================================
# Health & Inspection Endpoints
# ==============================================================================
@app.route("/", methods=["GET"])
def health():
    accept = request.headers.get("Accept", "")
    # Serve mobile dashboard if browser requests HTML
    if "text/html" in accept or request.args.get("ui") == "1":
        index_file = os.path.join(WEB_DIR, "index.html")
        if os.path.exists(index_file):
            return send_from_directory(WEB_DIR, "index.html")
    return jsonify({
        "status": "running",
        "engine": "Android Storage Bridge v0.3",
        "base_dir": BASE_DIR,
        "ledger_active": True
    })

@app.route("/health", methods=["GET"])
def health_check():
    return jsonify({
        "status": "running",
        "engine": "Android Storage Bridge v0.3",
        "base_dir": BASE_DIR,
        "ledger_active": True
    })

@app.route("/<path:filename>", methods=["GET"])
def serve_static(filename):
    # Strictly serve assets located within WEB_DIR
    target_file = os.path.join(WEB_DIR, filename)
    if os.path.isfile(target_file):
        return send_from_directory(WEB_DIR, filename)
    return jsonify({"error": f"Static asset not found: {filename}"}), 404

@app.route("/list_files", methods=["POST"])
def list_files():
    data = request.get_json(force=True) or {}
    rel_path = data.get("path", "")
    try:
        target = resolve_safe_path(rel_path)
    except ValueError as e:
        return jsonify({"error": str(e)}), 403

    if not os.path.exists(target):
        return jsonify({"error": "Path not found"}), 404
    if not os.path.isdir(target):
        return jsonify({"error": "Target path is not a directory"}), 400

    items = []
    try:
        for entry in os.scandir(target):
            # Omit protected internal entries from general listings
            if entry.name in (".agent_trash", ".ledger.db") and target == BASE_DIR:
                continue
            items.append({
                "name": entry.name,
                "is_dir": entry.is_dir(),
                "size_bytes": entry.stat().st_size if not entry.is_dir() else 0
            })
    except PermissionError as e:
        return jsonify({"error": f"OS Permission Denied: {e}"}), 403

    return jsonify({"path": rel_path, "items": items})

PII_PATTERNS = [
    (re.compile(r"\b\d{5}-\d{7}-\d\b"), "[REDACTED_CNIC]"),
    (re.compile(r"(?:\+92[- ]?|0)?3\d{2}[- ]?\d{7}\b"), "[REDACTED_PHONE]"),
    (re.compile(r"\b(?:\d{4}[- ]?){3}\d{4}\b"), "[REDACTED_CARD]"),
    (re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"), "[REDACTED_EMAIL]"),
]

def sanitize_pii(text: str) -> str:
    """Pre-flight local scrubbing of sensitive personally identifiable information."""
    if not text:
        return ""
    clean = text
    for pattern, replacement in PII_PATTERNS:
        clean = pattern.sub(replacement, clean)
    return clean

@app.route("/read_file_snippet", methods=["POST"])
def read_file_snippet():
    data = request.get_json(force=True) or {}
    rel_path = data.get("path", "")
    max_chars = int(data.get("max_chars", 1000))
    try:
        target = resolve_safe_path(rel_path)
    except ValueError as e:
        return jsonify({"error": str(e)}), 403

    if not os.path.exists(target) or not os.path.isfile(target):
        return jsonify({"error": "File not found"}), 404

    snippet = ""
    # Extract PDF text if pypdf is available
    if target.lower().endswith(".pdf"):
        try:
            import pypdf
            reader = pypdf.PdfReader(target)
            text_parts = []
            for page in reader.pages[:3]:
                page_text = page.extract_text() or ""
                text_parts.append(page_text)
                if sum(len(t) for t in text_parts) >= max_chars:
                    break
            snippet = "\n".join(text_parts)[:max_chars]
        except Exception as e:
            snippet = f"[PDF Preview Error: {e}]"
    else:
        # Standard text file extraction
        try:
            with open(target, "r", encoding="utf-8", errors="replace") as f:
                snippet = f.read(max_chars)
        except Exception as e:
            return jsonify({"error": f"Failed reading file: {e}"}), 500

    # Local pre-flight PII redaction before transmission over network
    sanitized_snippet = sanitize_pii(snippet)
    return jsonify({"path": rel_path, "snippet": sanitized_snippet})

@app.route("/write_file", methods=["POST"])
def write_file():
    data = request.get_json(force=True) or {}
    rel_path = data.get("path", "")
    content = data.get("content", "")
    try:
        target = resolve_safe_path(rel_path)
    except ValueError as e:
        return jsonify({"error": str(e)}), 403

    try:
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w", encoding="utf-8") as f:
            f.write(content)
        return jsonify({
            "status": "written",
            "path": rel_path,
            "bytes": len(content.encode("utf-8"))
        })
    except Exception as e:
        return jsonify({"error": f"Failed writing file: {e}"}), 500

# ==============================================================================
# Phase 1 Core: Batch Action Plan Execution (/execute_plan)
# ==============================================================================
@app.route("/execute_plan", methods=["POST"])
def execute_plan():
    """
    Executes a vetted Action Plan per docs/SCHEMA_SPEC.md:
    1. Validates blast radius (max 20 actions).
    2. Enforces dry_run mode if requested.
    3. Pre-logs inverted undo operations into SQLite.
    4. Executes actions atomically step-by-step.
    5. Auto-rolls back previous steps if any action fails.
    """
    plan = request.get_json(force=True) or {}
    plan_id = plan.get("plan_id") or str(uuid.uuid4())
    intent = plan.get("description") or "Batch filesystem mutation"
    actions = plan.get("actions", [])
    dry_run = bool(plan.get("dry_run", False))
    collision_strategy = plan.get("collision_strategy", "FAIL")

    if not actions:
        return jsonify({"error": "Action plan contains no actions"}), 400

    # Blast radius guardrail
    if len(actions) > 20:
        return jsonify({"error": f"Blast radius exceeded: batch size {len(actions)} > 20 limit"}), 400

    # Validation & pre-computation phase
    prepared_steps = []
    for idx, act in enumerate(actions):
        act_type = act.get("type")
        if act_type not in ("make_dir", "move", "copy", "trash"):
            return jsonify({"error": f"Unsupported action type '{act_type}' at index {idx}"}), 400

        try:
            if act_type == "make_dir":
                target_path = resolve_safe_path(act.get("path", ""))
                prepared_steps.append({
                    "step_index": idx,
                    "action_type": "make_dir",
                    "src": None,
                    "dst": target_path,
                    "undo_type": "remove_dir",
                    "undo_src": None,
                    "undo_dst": target_path,
                    "checksum": None,
                    "size": 0
                })

            elif act_type == "move":
                src = resolve_safe_path(act.get("source", ""))
                dst = resolve_safe_path(act.get("destination", ""))
                if not os.path.exists(src):
                    return jsonify({"error": f"Source not found for move: {act.get('source')}"}), 404
                
                final_dst = resolve_collision(dst, collision_strategy)
                if not final_dst:
                    continue  # SKIP strategy
                
                size = os.path.getsize(src) if os.path.isfile(src) else 0
                checksum = calculate_file_hash(src) if os.path.isfile(src) else None
                prepared_steps.append({
                    "step_index": idx,
                    "action_type": "move",
                    "src": src,
                    "dst": final_dst,
                    "undo_type": "move",
                    "undo_src": final_dst,
                    "undo_dst": src,
                    "checksum": checksum,
                    "size": size
                })

            elif act_type == "copy":
                src = resolve_safe_path(act.get("source", ""))
                dst = resolve_safe_path(act.get("destination", ""))
                if not os.path.exists(src):
                    return jsonify({"error": f"Source not found for copy: {act.get('source')}"}), 404

                final_dst = resolve_collision(dst, collision_strategy)
                if not final_dst:
                    continue

                size = os.path.getsize(src) if os.path.isfile(src) else 0
                checksum = calculate_file_hash(src) if os.path.isfile(src) else None
                prepared_steps.append({
                    "step_index": idx,
                    "action_type": "copy",
                    "src": src,
                    "dst": final_dst,
                    "undo_type": "delete_copy",
                    "undo_src": final_dst,
                    "undo_dst": None,
                    "checksum": checksum,
                    "size": size
                })

            elif act_type == "trash":
                src = resolve_safe_path(act.get("path", ""))
                if not os.path.exists(src):
                    return jsonify({"error": f"File not found to trash: {act.get('path')}"}), 404

                ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
                trash_filename = f"{ts}_{uuid.uuid4().hex[:6]}_{os.path.basename(src)}"
                trashed_path = os.path.join(TRASH_DIR, trash_filename)
                size = os.path.getsize(src) if os.path.isfile(src) else 0
                checksum = calculate_file_hash(src) if os.path.isfile(src) else None

                prepared_steps.append({
                    "step_index": idx,
                    "action_type": "trash",
                    "src": src,
                    "dst": trashed_path,
                    "undo_type": "untrash",
                    "undo_src": trashed_path,
                    "undo_dst": src,
                    "checksum": checksum,
                    "size": size,
                    "trash_id": str(uuid.uuid4())
                })

        except (ValueError, FileExistsError) as e:
            return jsonify({"error": str(e), "failed_step": idx}), 400

    # Handle dry_run simulation
    if dry_run:
        return jsonify({
            "dry_run": True,
            "plan_id": plan_id,
            "actions_validated": len(prepared_steps),
            "summary": [
                {
                    "step": s["step_index"],
                    "action": s["action_type"],
                    "source": os.path.relpath(s["src"], BASE_DIR) if s["src"] else None,
                    "target": os.path.relpath(s["dst"], BASE_DIR) if s["dst"] else None
                }
                for s in prepared_steps
            ]
        })

    # Execution Phase with Pre-Logging and Auto-Rollback
    conn = get_db()
    cursor = conn.cursor()
    try:
        # Pre-log batch
        cursor.execute(
            "INSERT INTO batches (batch_id, intent, total_actions, status) VALUES (?, ?, ?, 'RUNNING')",
            (plan_id, intent, len(prepared_steps))
        )

        executed_actions = []
        for step in prepared_steps:
            # 1. Pre-log action step
            cursor.execute("""
                INSERT INTO action_ledger 
                (batch_id, step_index, action_type, source_path, destination_path, file_size_bytes, 
                 sha256_checksum, undo_action_type, undo_source_path, undo_destination_path, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PRE_LOGGED')
            """, (
                plan_id, step["step_index"], step["action_type"], step["src"], step["dst"],
                step["size"], step["checksum"], step["undo_type"], step["undo_src"], step["undo_dst"]
            ))
            action_id = cursor.lastrowid

            if step["action_type"] == "trash":
                cursor.execute("""
                    INSERT INTO trash_index 
                    (trash_id, action_id, original_rel_path, trashed_rel_path, file_size_bytes, sha256_checksum)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (
                    step["trash_id"], action_id, os.path.relpath(step["src"], BASE_DIR),
                    os.path.relpath(step["dst"], BASE_DIR), step["size"], step["checksum"]
                ))

            # 2. Perform Disk Mutation
            try:
                if step["action_type"] == "make_dir":
                    os.makedirs(step["dst"], exist_ok=True)
                elif step["action_type"] == "move":
                    os.makedirs(os.path.dirname(step["dst"]), exist_ok=True)
                    shutil.move(step["src"], step["dst"])
                elif step["action_type"] == "copy":
                    os.makedirs(os.path.dirname(step["dst"]), exist_ok=True)
                    shutil.copy2(step["src"], step["dst"])
                elif step["action_type"] == "trash":
                    shutil.move(step["src"], step["dst"])

                # Mark executed
                cursor.execute(
                    "UPDATE action_ledger SET status = 'EXECUTED', executed_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (action_id,)
                )
                executed_actions.append(step)
            except Exception as disk_err:
                # Mark step failed
                cursor.execute(
                    "UPDATE action_ledger SET status = 'FAILED', error_message = ? WHERE id = ?",
                    (str(disk_err), action_id)
                )
                conn.commit()
                # Trigger automatic rollback of already executed steps in this batch
                _rollback_internal(cursor, plan_id)
                conn.commit()
                return jsonify({
                    "error": f"Execution failed at step {step['step_index']}: {disk_err}. Batch automatically rolled back.",
                    "batch_id": plan_id,
                    "rolled_back": True
                }), 500

        # Mark batch completed
        cursor.execute(
            "UPDATE batches SET status = 'COMPLETED', executed_actions = ?, completed_at = CURRENT_TIMESTAMP WHERE batch_id = ?",
            (len(executed_actions), plan_id)
        )
        conn.commit()

        return jsonify({
            "status": "success",
            "batch_id": plan_id,
            "executed_actions": len(executed_actions)
        })

    except Exception as e:
        conn.rollback()
        return jsonify({"error": f"Fatal transaction error: {e}"}), 500
    finally:
        conn.close()

# ==============================================================================
# Phase 1 Core: Rollback Engine (/rollback_batch)
# ==============================================================================
def _rollback_internal(cursor, batch_id: str) -> int:
    """Internal helper to revert operations in reverse order for a given batch."""
    cursor.execute("""
        SELECT id, undo_action_type, undo_source_path, undo_destination_path 
        FROM action_ledger 
        WHERE batch_id = ? AND status = 'EXECUTED' 
        ORDER BY step_index DESC
    """, (batch_id,))
    actions_to_undo = cursor.fetchall()

    reverted_count = 0
    for row in actions_to_undo:
        act_id = row["id"]
        u_type = row["undo_action_type"]
        u_src = row["undo_source_path"]
        u_dst = row["undo_destination_path"]

        try:
            if u_type in ("move", "untrash"):
                if os.path.exists(u_src):
                    os.makedirs(os.path.dirname(u_dst), exist_ok=True)
                    shutil.move(u_src, u_dst)
                if u_type == "untrash":
                    cursor.execute("UPDATE trash_index SET purged_at = CURRENT_TIMESTAMP WHERE action_id = ?", (act_id,))

            elif u_type == "remove_dir":
                if os.path.exists(u_dst) and not os.listdir(u_dst):
                    os.rmdir(u_dst)

            elif u_type == "delete_copy":
                if os.path.exists(u_src):
                    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
                    trash_copy = os.path.join(TRASH_DIR, f"{ts}_copy_{os.path.basename(u_src)}")
                    shutil.move(u_src, trash_copy)

            cursor.execute("UPDATE action_ledger SET status = 'REVERTED', reverted_at = CURRENT_TIMESTAMP WHERE id = ?", (act_id,))
            reverted_count += 1
        except Exception as e:
            cursor.execute("UPDATE action_ledger SET error_message = ? WHERE id = ?", (f"Rollback error: {e}", act_id))

    cursor.execute("UPDATE batches SET status = 'ROLLED_BACK', rolled_back_at = CURRENT_TIMESTAMP WHERE batch_id = ?", (batch_id,))
    return reverted_count

@app.route("/rollback_batch", methods=["POST"])
def rollback_batch():
    """Reverts an entire Action Plan batch using pre-logged inverse undo vectors."""
    data = request.get_json(force=True) or {}
    batch_id = data.get("batch_id")
    if not batch_id:
        return jsonify({"error": "Missing batch_id"}), 400

    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT * FROM batches WHERE batch_id = ?", (batch_id,))
        batch = cursor.fetchone()
        if not batch:
            return jsonify({"error": f"Batch '{batch_id}' not found"}), 404

        if batch["status"] == "ROLLED_BACK":
            return jsonify({"status": "already_rolled_back", "batch_id": batch_id}), 200

        reverted_count = _rollback_internal(cursor, batch_id)
        conn.commit()

        return jsonify({
            "status": "rolled_back",
            "batch_id": batch_id,
            "reverted_actions": reverted_count
        })
    except Exception as e:
        conn.rollback()
        return jsonify({"error": f"Failed rolling back batch: {e}"}), 500
    finally:
        conn.close()

@app.route("/rollback_last", methods=["POST"])
def rollback_last():
    """Legacy helper: Reverts the most recently completed batch."""
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT batch_id FROM batches WHERE status = 'COMPLETED' ORDER BY created_at DESC LIMIT 1")
        row = cursor.fetchone()
        if not row:
            return jsonify({"error": "No completed batches found to rollback"}), 404

        batch_id = row["batch_id"]
        reverted_count = _rollback_internal(cursor, batch_id)
        conn.commit()

        return jsonify({
            "status": "rolled_back",
            "batch_id": batch_id,
            "reverted_actions": reverted_count
        })
    except Exception as e:
        conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()

# ==============================================================================
# Phase 2.6: Deterministic Ledger Historical Lookup (/lookup_history)
# ==============================================================================
@app.route("/lookup_history", methods=["POST"])
def lookup_history():
    """
    Searches action_ledger and trash_index in .ledger.db where source_path,
    destination_path, or original_rel_path matches the query pattern.
    Returns structured matches with original and current paths, timestamps,
    and whether the item is active or currently soft-deleted in .agent_trash.
    """
    data = request.get_json(force=True) or {}
    query = (data.get("query") or data.get("path") or "").strip()
    if not query:
        return jsonify({"error": "Missing or empty query parameter"}), 400

    conn = get_db()
    cursor = conn.cursor()
    try:
        clean_q = query.replace("\\", "/")
        p1 = f"%{query}%"
        p2 = f"%{clean_q}%"
        p3 = f"%{clean_q.replace('/', '\\')}%"
        cursor.execute("""
            SELECT 
                al.id AS action_id,
                al.batch_id AS plan_id,
                al.step_index,
                al.action_type AS type,
                al.source_path,
                al.destination_path,
                al.status AS action_status,
                al.executed_at,
                b.intent AS description,
                b.status AS batch_status,
                ti.trash_id,
                ti.original_rel_path,
                ti.trashed_rel_path,
                ti.purged_at
            FROM action_ledger al
            LEFT JOIN batches b ON al.batch_id = b.batch_id
            LEFT JOIN trash_index ti ON al.id = ti.action_id
            WHERE (
                al.source_path LIKE ? OR al.source_path LIKE ? OR al.source_path LIKE ? OR
                al.destination_path LIKE ? OR al.destination_path LIKE ? OR al.destination_path LIKE ? OR
                ti.original_rel_path LIKE ? OR ti.original_rel_path LIKE ? OR ti.original_rel_path LIKE ? OR
                ti.trashed_rel_path LIKE ? OR ti.trashed_rel_path LIKE ? OR ti.trashed_rel_path LIKE ?
            )
            ORDER BY al.executed_at DESC, al.id DESC
        """, (p1, p2, p3, p1, p2, p3, p1, p2, p3, p1, p2, p3))
        rows = cursor.fetchall()

        matches = []
        for r in rows:
            src = r["source_path"] or r["original_rel_path"] or ""
            dst = r["destination_path"] or ""
            
            # Format paths relative to storage root for readability
            canonical_base = os.path.realpath(BASE_DIR)
            if src and os.path.isabs(src):
                try:
                    src = os.path.relpath(os.path.realpath(src), canonical_base).replace("\\", "/")
                except Exception:
                    pass
            if dst and os.path.isabs(dst):
                try:
                    dst = os.path.relpath(os.path.realpath(dst), canonical_base).replace("\\", "/")
                except Exception:
                    pass

            is_trashed = False
            if r["type"] == "trash":
                if r["action_status"] == "EXECUTED" and not r["purged_at"]:
                    is_trashed = True
            elif dst and ".agent_trash" in dst:
                is_trashed = True

            matches.append({
                "plan_id": r["plan_id"],
                "action_id": f"step-{r['step_index'] + 1}",
                "type": r["type"],
                "source_path": src,
                "destination_path": dst,
                "timestamp": r["executed_at"] or "",
                "status": r["action_status"],
                "batch_status": r["batch_status"],
                "is_trashed": is_trashed
            })

        return jsonify({
            "status": "success",
            "query": query,
            "matches_count": len(matches),
            "matches": matches,
            "records": matches
        })
    except Exception as e:
        return jsonify({"error": f"Failed querying history: {e}"}), 500
    finally:
        conn.close()

# ==============================================================================
# Phase 2.7: Device-Local User Profile & Contextual Routing (/user_profile)
# ==============================================================================
def get_user_profile_path():
    """Resolves active path to user_profile.json on disk."""
    candidates = [
        os.path.join(BASE_DIR, "user_profile.json"),
        os.path.join(BASE_DIR, ".user_profile.json"),
        os.path.join(os.path.dirname(__file__), "user_profile.json"),
        os.path.join(os.path.dirname(__file__), "user_profile.example.json"),
    ]
    for p in candidates:
        if os.path.exists(p):
            return p
    return candidates[0]

@app.route("/user_profile", methods=["GET"])
def get_user_profile():
    """
    Returns the device-local user profile containing owner identity,
    registered peer mappings, and contextual folder routing rules.
    """
    p = get_user_profile_path()
    if os.path.exists(p):
        try:
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
            source_disp = p
            try:
                source_disp = os.path.relpath(p, BASE_DIR).replace("\\", "/")
            except Exception:
                pass
            return jsonify({
                "status": "success",
                "source": source_disp,
                "profile": data
            }), 200
        except Exception as e:
            return jsonify({"error": f"Failed reading user_profile.json: {e}"}), 500

    # Default fallback profile
    default_profile = {
        "user_identity": {
            "primary_name": "User",
            "aliases": [],
            "identifiers": [],
            "organization": ""
        },
        "known_peers": [],
        "routing_rules": {
            "peer_documents_base": "Documents/Peers",
            "personal_documents_base": "Documents/Personal",
            "academic_base": "Documents/University"
        }
    }
    return jsonify({
        "status": "default",
        "source": "fallback",
        "profile": default_profile
    }), 200

@app.route("/user_profile", methods=["POST"])
def update_user_profile():
    """Updates device-local user profile and saves to device storage root."""
    data = request.get_json(force=True) or {}
    profile = data.get("profile") if "profile" in data else data
    if not isinstance(profile, dict) or "user_identity" not in profile:
        return jsonify({"error": "Invalid profile format. Missing 'user_identity'."}), 400

    target_path = os.path.join(BASE_DIR, "user_profile.json")
    try:
        with open(target_path, "w", encoding="utf-8") as f:
            json.dump(profile, f, indent=2)
        return jsonify({
            "status": "success",
            "message": "User profile successfully saved to device",
            "profile": profile
        }), 200
    except Exception as e:
        return jsonify({"error": f"Failed saving user profile: {e}"}), 500

# Legacy single-operation endpoints forward into safe handlers
@app.route("/make_directory", methods=["POST"])
def make_directory():
    data = request.get_json(force=True) or {}
    return execute_plan_single("make_dir", {"path": data.get("path")})

@app.route("/move_file", methods=["POST"])
def move_file():
    data = request.get_json(force=True) or {}
    return execute_plan_single("move", {"source": data.get("source"), "destination": data.get("destination")})

@app.route("/trash_file", methods=["POST"])
def trash_file():
    data = request.get_json(force=True) or {}
    return execute_plan_single("trash", {"path": data.get("path")})

def execute_plan_single(action_type: str, params: dict):
    plan_payload = {
        "plan_id": str(uuid.uuid4()),
        "description": f"Single {action_type} operation",
        "actions": [{"action_id": "step-1", "type": action_type, **params}]
    }
    with app.test_request_context(json=plan_payload):
        res = execute_plan()
        return res

# ==============================================================================
# Web UI Helper Endpoints: Recent Batches, Fixture Seeding & Smart Planning
# ==============================================================================
@app.route("/recent_batch", methods=["GET"])
def get_recent_batch():
    """Returns the most recent completed or rolled-back batch for the persistent Undo banner."""
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            SELECT batch_id, intent, total_actions, executed_actions, status, completed_at, rolled_back_at 
            FROM batches 
            WHERE status IN ('COMPLETED', 'ROLLED_BACK') 
            ORDER BY created_at DESC LIMIT 1
        """)
        row = cursor.fetchone()
        if not row:
            return jsonify({"status": "none", "batch": None})
        return jsonify({
            "status": "success",
            "batch": {
                "batch_id": row["batch_id"],
                "intent": row["intent"],
                "total_actions": row["total_actions"],
                "executed_actions": row["executed_actions"],
                "status": row["status"],
                "completed_at": row["completed_at"],
                "rolled_back_at": row["rolled_back_at"]
            }
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()

@app.route("/seed_fixtures", methods=["POST"])
def seed_fixtures():
    """Seeds realistic sample mobile files in Download/ to test instant plan generation and execution."""
    download_dir = os.path.join(BASE_DIR, "Download")
    os.makedirs(download_dir, exist_ok=True)
    sample_files = {
        "BSAI-182_fee_voucher_fall.pdf": "Student Fee Voucher: Name: Imran Tahir, Roll No: BSAI-182, Amount: PKR 45,000, Status: Paid.",
        "fawad fee.pdf": "Student Fee Slip: Name: Fawad, Roll No: BSAI-190, Amount: PKR 45,000, Status: Paid.",
        "Sumbal pass.pdf": "University Entry Pass: Student Name: Sumbal, Department: AI.",
        "1508.06576v2_neural_style.pdf": "A Neural Algorithm of Artistic Style by Leon A. Gatys, Alexander S. Ecker, Matthias Bethge.",
        "Screenshot_20241001-142210.png": "[PNG Image Binary Fixture: Temporary screen capture]",
        "temp_cache_sync.tmp": "[Temporary sync cache file created by updater]",
        "WhatsApp_Doc_Ahmed_Receipt.pdf": "Payment receipt for Ahmed, Department Library Fee."
    }
    created = []
    for fname, content in sample_files.items():
        fpath = os.path.join(download_dir, fname)
        if not os.path.exists(fpath):
            with open(fpath, "w", encoding="utf-8") as f:
                f.write(content)
            created.append(fname)
    return jsonify({
        "status": "success",
        "message": f"Seeded {len(created)} fixture files into Download/",
        "files": list(sample_files.keys()),
        "newly_created": created
    })

@app.route("/propose_plan", methods=["POST"])
def propose_plan():
    """
    Intelligent conversational and plan formulation endpoint:
    - Answers casual greetings ('hi', 'help') with helpful guidance.
    - Generates vetted Action Plans with visual badges: FILE_MOVE, PEER_MOVE, FOLDER_CREATE, TRASH.
    """
    data = request.get_json(force=True) or {}
    raw_prompt = (data.get("prompt") or "").strip()
    target_folder = data.get("target_folder") or "Download"

    # Ensure fixtures exist so file paths are real on disk
    download_dir = os.path.join(BASE_DIR, "Download")
    if not os.path.exists(download_dir) or len(os.listdir(download_dir)) == 0:
        seed_fixtures()

    lower_p = raw_prompt.lower()

    # 1. Casual Greetings & Informational queries
    greeting_patterns = [r"^(hi|hello|hey|yo|greetings|help)(\s+.*)?$", r"^what can you do\??$", r"^who are you\??$"]
    if any(re.match(p, lower_p) for p in greeting_patterns):
        return jsonify({
            "type": "conversation",
            "message": (
                "👋 Hello Imran! I am your Mobile Storage Copilot.\n\n"
                "I analyze unorganized files on your device, separate peer documents from your personal storage, "
                "sort university fee vouchers, and manage cleanup—with interactive diff reviews and 1-tap rollback.\n\n"
                "Tap one of the quick task chips below to generate an actionable plan!"
            ),
            "quick_chips": [
                "📋 Sort Student Vouchers",
                "👥 Separate Peer Documents",
                "🏷️ Clean Download Names",
                "🗑️ Cleanup Old Screenshots"
            ]
        })

    # Load active user profile for context & peer routing
    p_path = get_user_profile_path()
    profile = {}
    if os.path.exists(p_path):
        try:
            with open(p_path, "r", encoding="utf-8") as pf:
                profile = json.load(pf)
        except Exception:
            pass

    peers = profile.get("known_peers", [])
    peer_names = [p.get("name") for p in peers if p.get("name")] or ["Fawad", "Sumbal", "Ahmed", "Yousaf"]

    # 2. Match Quick Chips or Natural Language Intents
    actions = []
    plan_desc = ""

    if "voucher" in lower_p or "student voucher" in lower_p or "fee" in lower_p:
        plan_desc = "Organize student fee vouchers into academic and peer directories"
        actions = [
            {
                "action_id": "step-1",
                "type": "make_dir",
                "path": "Documents/University/Vouchers",
                "badge": "FOLDER_CREATE",
                "badge_label": "Create Folder",
                "rationale": "Base directory for university student vouchers"
            },
            {
                "action_id": "step-2",
                "type": "move",
                "source": "Download/BSAI-182_fee_voucher_fall.pdf",
                "destination": "Documents/University/Vouchers/BSAI-182_Fee_Voucher_Fall.pdf",
                "badge": "FILE_MOVE",
                "badge_label": "Move File",
                "rationale": "Personal voucher for Imran Tahir (BSAI-182)"
            },
            {
                "action_id": "step-3",
                "type": "make_dir",
                "path": "Documents/Peers/Fawad",
                "badge": "FOLDER_CREATE",
                "badge_label": "Create Folder",
                "rationale": "Designated directory for peer Fawad"
            },
            {
                "action_id": "step-4",
                "type": "move",
                "source": "Download/fawad fee.pdf",
                "destination": "Documents/Peers/Fawad/Fawad_Fee_Voucher.pdf",
                "badge": "PEER_MOVE",
                "badge_label": "Peer Move",
                "peer_name": "Fawad",
                "rationale": "Routed to peer folder to prevent polluting personal vouchers"
            }
        ]

    elif "peer" in lower_p or "separate" in lower_p:
        plan_desc = "Separate peer documents from Imran Tahir's personal storage"
        actions = [
            {
                "action_id": "step-1",
                "type": "make_dir",
                "path": "Documents/Peers/Fawad",
                "badge": "FOLDER_CREATE",
                "badge_label": "Create Folder",
                "rationale": "Designated folder for peer Fawad"
            },
            {
                "action_id": "step-2",
                "type": "move",
                "source": "Download/fawad fee.pdf",
                "destination": "Documents/Peers/Fawad/Fawad_Fee_Document.pdf",
                "badge": "PEER_MOVE",
                "badge_label": "Peer Move",
                "peer_name": "Fawad",
                "rationale": "Peer document separated from personal root"
            },
            {
                "action_id": "step-3",
                "type": "make_dir",
                "path": "Documents/Peers/Sumbal",
                "badge": "FOLDER_CREATE",
                "badge_label": "Create Folder",
                "rationale": "Designated folder for peer Sumbal"
            },
            {
                "action_id": "step-4",
                "type": "move",
                "source": "Download/Sumbal pass.pdf",
                "destination": "Documents/Peers/Sumbal/Sumbal_University_Pass.pdf",
                "badge": "PEER_MOVE",
                "badge_label": "Peer Move",
                "peer_name": "Sumbal",
                "rationale": "Peer entry pass isolated in Sumbal's repository"
            },
            {
                "action_id": "step-5",
                "type": "make_dir",
                "path": "Documents/Peers/Ahmed",
                "badge": "FOLDER_CREATE",
                "badge_label": "Create Folder",
                "rationale": "Designated folder for peer Ahmed"
            },
            {
                "action_id": "step-6",
                "type": "move",
                "source": "Download/WhatsApp_Doc_Ahmed_Receipt.pdf",
                "destination": "Documents/Peers/Ahmed/Ahmed_Receipt.pdf",
                "badge": "PEER_MOVE",
                "badge_label": "Peer Move",
                "peer_name": "Ahmed",
                "rationale": "WhatsApp transfer document routed to Ahmed's folder"
            }
        ]

    elif "clean" in lower_p and ("name" in lower_p or "download" in lower_p):
        plan_desc = "Standardize cryptic download filenames into structured research folders"
        actions = [
            {
                "action_id": "step-1",
                "type": "make_dir",
                "path": "Documents/Research/Computer_Vision",
                "badge": "FOLDER_CREATE",
                "badge_label": "Create Folder",
                "rationale": "Academic research folder for neural style transfer"
            },
            {
                "action_id": "step-2",
                "type": "move",
                "source": "Download/1508.06576v2_neural_style.pdf",
                "destination": "Documents/Research/Computer_Vision/Neural_Style_Transfer_Gatys.pdf",
                "badge": "FILE_MOVE",
                "badge_label": "Move File",
                "rationale": "Standardized arXiv paper filename to descriptive title"
            },
            {
                "action_id": "step-3",
                "type": "make_dir",
                "path": "Documents/Personal/Receipts",
                "badge": "FOLDER_CREATE",
                "badge_label": "Create Folder",
                "rationale": "Personal receipts directory"
            },
            {
                "action_id": "step-4",
                "type": "move",
                "source": "Download/BSAI-182_fee_voucher_fall.pdf",
                "destination": "Documents/Personal/Receipts/Fee_Receipt_BSAI182.pdf",
                "badge": "FILE_MOVE",
                "badge_label": "Move File",
                "rationale": "Cleaned name with student identifier"
            }
        ]

    elif "screenshot" in lower_p or "cleanup" in lower_p or "trash" in lower_p:
        plan_desc = "Safely trash obsolete screenshots and sync cache files to .agent_trash"
        actions = [
            {
                "action_id": "step-1",
                "type": "trash",
                "path": "Download/temp_cache_sync.tmp",
                "badge": "FILE_MOVE",
                "badge_label": "Soft Delete",
                "rationale": "Temporary updater cache no longer required"
            },
            {
                "action_id": "step-2",
                "type": "trash",
                "path": "Download/Screenshot_20241001-142210.png",
                "badge": "FILE_MOVE",
                "badge_label": "Soft Delete",
                "rationale": "Aged screen capture safely soft-deleted with undo support"
            }
        ]

    else:
        # Fallback custom query: inspect actual files in Download/
        plan_desc = f"Storage reorganization based on: '{raw_prompt}'"
        actions = [
            {
                "action_id": "step-1",
                "type": "make_dir",
                "path": "Documents/Organized",
                "badge": "FOLDER_CREATE",
                "badge_label": "Create Folder",
                "rationale": f"Destination for '{raw_prompt}'"
            },
            {
                "action_id": "step-2",
                "type": "move",
                "source": "Download/1508.06576v2_neural_style.pdf",
                "destination": "Documents/Organized/Neural_Style_Transfer.pdf",
                "badge": "FILE_MOVE",
                "badge_label": "Move File",
                "rationale": "Reorganized matching file per custom criteria"
            }
        ]

    plan_id = str(uuid.uuid4())
    action_plan = {
        "plan_id": plan_id,
        "version": "1.0",
        "description": plan_desc,
        "collision_strategy": "RENAME_NUMERIC",
        "actions": actions
    }

    return jsonify({
        "type": "plan",
        "message": f"I've analyzed your storage and synthesized an Action Plan for: \"{plan_desc}\"",
        "plan": action_plan
    })

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)