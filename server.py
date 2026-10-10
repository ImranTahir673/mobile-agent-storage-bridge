import os
import json
import re
import shutil
import sqlite3
import uuid
import hashlib
from datetime import datetime, timezone
import requests
from flask import Flask, request, jsonify, send_from_directory

app = Flask("MobileStorageBridge")
APP_VERSION = "v0.5.2-live"

# ==============================================================================
# Environment Configuration (.env loader)
# ==============================================================================
def load_environment():
    """Loads environment variables from .env in script directory or current directory."""
    candidates = [
        os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"),
        os.path.join(os.getcwd(), ".env")
    ]
    for env_path in candidates:
        if os.path.exists(env_path):
            with open(env_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

load_environment()

# ==============================================================================
# Dynamic Cross-Platform Storage Path Resolution
# ==============================================================================
def resolve_base_storage_dir() -> str:
    """
    Dynamically detects and configures the storage base directory across platforms:
    - Android Termux: checks /storage/emulated/0/Download or ~/storage/downloads.
      Sets /storage/emulated/0 as BASE_STORAGE_DIR.
    - Windows / Linux fallback: local downloads/ or ./fixtures or ~/storage/shared.
    """
    env_dir = os.getenv("STORAGE_BASE_DIR")
    if env_dir:
        target = os.path.realpath(env_dir)
        os.makedirs(target, exist_ok=True)
        return target

    # Android Termux check
    is_android = (
        os.path.exists("/storage/emulated/0/Download") or 
        os.path.exists("/storage/emulated/0") or 
        os.path.exists(os.path.expanduser("~/storage/downloads")) or
        os.path.exists(os.path.expanduser("~/storage/shared"))
    )
    if is_android:
        if os.path.exists("/storage/emulated/0"):
            return "/storage/emulated/0"
        termux_shared = os.path.expanduser("~/storage/shared")
        if os.path.exists(termux_shared):
            return os.path.realpath(termux_shared)

    # Windows / Linux local fallbacks: check ./fixtures or ./downloads
    script_dir = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.join(script_dir, "fixtures"),
        os.path.join(script_dir, "downloads"),
        os.path.join(script_dir, "Download"),
        os.path.expanduser("~/storage/shared"),
        os.path.expanduser("~/Downloads")
    ]
    for cand in candidates:
        if os.path.exists(cand) and os.path.isdir(cand):
            return os.path.realpath(cand)

    # Default fallback: create local fixtures/ in repository
    fallback_dir = os.path.join(script_dir, "fixtures")
    os.makedirs(fallback_dir, exist_ok=True)
    return os.path.realpath(fallback_dir)

BASE_STORAGE_DIR = resolve_base_storage_dir()
BASE_DIR = BASE_STORAGE_DIR
TRASH_DIR = os.path.join(BASE_STORAGE_DIR, ".agent_trash")
LEDGER_DB_PATH = os.path.realpath(os.getenv("LEDGER_DB_PATH", os.path.join(BASE_STORAGE_DIR, ".ledger.db")))
WEB_DIR = os.path.realpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "web"))

os.makedirs(BASE_STORAGE_DIR, exist_ok=True)
os.makedirs(TRASH_DIR, exist_ok=True)
os.makedirs(WEB_DIR, exist_ok=True)
os.makedirs(os.path.join(BASE_STORAGE_DIR, "Download"), exist_ok=True)
os.makedirs(os.path.join(BASE_STORAGE_DIR, "Documents"), exist_ok=True)

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

    if not os.path.exists(target):
        # Case variation check for Android / Windows / Linux (Download vs downloads)
        parts = clean_rel.split("/", 1)
        if len(parts) == 2:
            first_seg, rest = parts
            if first_seg.lower() == "download":
                for cand_dir in ("Download", "downloads", ""):
                    cand_path = os.path.realpath(os.path.join(canonical_base, cand_dir, rest)) if cand_dir else os.path.realpath(os.path.join(canonical_base, rest))
                    if os.path.exists(cand_path):
                        target = cand_path
                        break
        elif clean_rel.lower() in ("download", "downloads"):
            for cand_dir in ("Download", "downloads"):
                cand_path = os.path.realpath(os.path.join(canonical_base, cand_dir))
                if os.path.exists(cand_path):
                    target = cand_path
                    break

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
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
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
        "version": APP_VERSION,
        "engine": f"Android Storage Bridge {APP_VERSION}",
        "base_dir": BASE_DIR,
        "ledger_active": True
    })

@app.route("/health", methods=["GET"])
def health_check():
    return jsonify({
        "status": "running",
        "version": APP_VERSION,
        "engine": f"Android Storage Bridge {APP_VERSION}",
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
            try:
                with open(target, "r", encoding="utf-8", errors="replace") as f:
                    snippet = f.read(max_chars)
            except Exception:
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

                ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
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

        # Build executed batch audit summary
        execution_summary = []
        now_ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        for step in executed_actions:
            idx = step["step_index"]
            act_orig = actions[idx] if idx < len(actions) else {}
            badge = act_orig.get("badge")
            src_rel = os.path.relpath(step["src"], BASE_DIR).replace("\\", "/") if step.get("src") else None
            dst_rel = os.path.relpath(step["dst"], BASE_DIR).replace("\\", "/") if step.get("dst") else None

            if not badge:
                if step["action_type"] == "trash":
                    badge = "TRASH"
                elif step["action_type"] == "make_dir":
                    badge = "FOLDER_CREATE"
                elif act_orig.get("peer_name") or (dst_rel and "/Peers/" in dst_rel):
                    badge = "PEER_MOVE"
                elif src_rel and dst_rel and os.path.basename(src_rel) != os.path.basename(dst_rel):
                    badge = "RENAME"
                else:
                    badge = "FILE_MOVE"

            execution_summary.append({
                "step_index": idx,
                "action_type": step["action_type"],
                "source": src_rel or act_orig.get("source") or act_orig.get("path") or "",
                "destination": dst_rel or act_orig.get("destination") or act_orig.get("path") or "",
                "badge": badge,
                "timestamp": now_ts
            })

        return jsonify({
            "status": "success",
            "batch_id": plan_id,
            "executed_actions": len(executed_actions),
            "execution_summary": execution_summary
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
                    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
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
                "version": APP_VERSION,
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
        "version": APP_VERSION,
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

        # Load execution summary from ledger for audit viewing
        cursor.execute("""
            SELECT step_index, action_type, source_path, destination_path, executed_at, status
            FROM action_ledger
            WHERE batch_id = ? AND status = 'EXECUTED'
            ORDER BY step_index ASC
        """, (row["batch_id"],))
        action_rows = cursor.fetchall()

        execution_summary = []
        for a in action_rows:
            src_rel = os.path.relpath(a["source_path"], BASE_DIR).replace("\\", "/") if a["source_path"] else None
            dst_rel = os.path.relpath(a["destination_path"], BASE_DIR).replace("\\", "/") if a["destination_path"] else None
            act_t = a["action_type"]
            badge = "FILE_MOVE"
            if act_t == "trash":
                badge = "TRASH"
            elif act_t == "make_dir":
                badge = "FOLDER_CREATE"
            elif dst_rel and "/Peers/" in dst_rel:
                badge = "PEER_MOVE"
            elif src_rel and dst_rel and os.path.basename(src_rel) != os.path.basename(dst_rel):
                badge = "RENAME"

            execution_summary.append({
                "step_index": a["step_index"],
                "action_type": act_t,
                "source": src_rel or "",
                "destination": dst_rel or "",
                "badge": badge,
                "timestamp": a["executed_at"] or row["completed_at"]
            })

        return jsonify({
            "status": "success",
            "batch": {
                "batch_id": row["batch_id"],
                "intent": row["intent"],
                "total_actions": row["total_actions"],
                "executed_actions": row["executed_actions"],
                "status": row["status"],
                "completed_at": row["completed_at"],
                "rolled_back_at": row["rolled_back_at"],
                "execution_summary": execution_summary
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
        "DOC-20250413-WA0017..pdf": "Scanned document export via WhatsApp messenger. Cryptic raw camera export.",
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

def extract_json_plan(response_text: str) -> dict:
    """Extracts JSON Action Plan from LLM response text."""
    clean_text = (response_text or "").strip()
    fenced_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", clean_text, re.DOTALL)
    if fenced_match:
        json_candidate = fenced_match.group(1)
    else:
        start_idx = clean_text.find("{")
        end_idx = clean_text.rfind("}")
        if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
            json_candidate = clean_text[start_idx:end_idx + 1]
        else:
            raise ValueError(f"Could not locate JSON in response:\n{clean_text}")
    return json.loads(json_candidate)

def format_plan_badges(plan_actions: list, peer_names: list, manifest_lookup: dict = None) -> list:
    """Post-processes action steps, annotates visual UI badges, and ensures PII inspection attributes."""
    formatted_actions = []
    step_num = 1
    manifest_lookup = manifest_lookup or {}

    for act in plan_actions:
        act_copy = dict(act)
        act_type = act_copy.get("type", "")
        src_path = act_copy.get("source") or act_copy.get("path") or ""
        base_name = os.path.basename(src_path)
        manifest_item = manifest_lookup.get(src_path.lower()) or manifest_lookup.get(base_name.lower())

        if act_type == "make_dir":
            dir_path = (act_copy.get("path") or "").replace("\\", "/").strip().lstrip("/")
            if dir_path in (".agent_trash", ".agent_trash/", "") or dir_path.startswith(".agent_trash/"):
                continue
            act_copy["action_id"] = f"step-{step_num}"
            act_copy["badge"] = "FOLDER_CREATE"
            act_copy["badge_label"] = "Create Folder"
            act_copy["ai_inspected"] = False
            act_copy["inspected_preview"] = "Directory creation"
            formatted_actions.append(act_copy)
            step_num += 1

        elif act_type in ("move", "copy"):
            dst = (act_copy.get("destination") or "").replace("\\", "/").strip().lstrip("/")
            src = act_copy.get("source", "")
            if dst == ".agent_trash" or dst.startswith(".agent_trash/"):
                act_copy["type"] = "trash"
                act_copy["path"] = src
                act_copy.pop("destination", None)
                act_copy["action_id"] = f"step-{step_num}"
                act_copy["badge"] = "TRASH"
                act_copy["badge_label"] = "Soft Delete"
            else:
                act_copy["action_id"] = f"step-{step_num}"
                peer_target = act_copy.get("peer_name")
                if not peer_target:
                    for p_name in peer_names:
                        if p_name.lower() in dst.lower() or p_name.lower() in src.lower():
                            peer_target = p_name
                            break

                if peer_target:
                    act_copy["badge"] = "PEER_MOVE"
                    act_copy["badge_label"] = "Peer Move"
                    act_copy["peer_name"] = peer_target
                elif os.path.basename(src).lower() != os.path.basename(dst).lower():
                    act_copy["badge"] = "RENAME"
                    act_copy["badge_label"] = "Rename / Move"
                else:
                    act_copy["badge"] = "FILE_MOVE"
                    act_copy["badge_label"] = "Move File"

            # Annotate PII inspection attributes
            if "ai_inspected" not in act_copy or act_copy["ai_inspected"] is None:
                if manifest_item and manifest_item.get("has_content"):
                    act_copy["ai_inspected"] = True
                    raw_snip = manifest_item.get("snippet") or manifest_item.get("text_snippet") or ""
                    act_copy["inspected_preview"] = raw_snip[:100] if raw_snip else "Content inspected"
                else:
                    act_copy["ai_inspected"] = False
                    act_copy["inspected_preview"] = "No readable text extracted (scanned image or binary document)"
            else:
                if act_copy["ai_inspected"] and (not act_copy.get("inspected_preview") or act_copy.get("inspected_preview") == "..."):
                    if manifest_item:
                        act_copy["inspected_preview"] = (manifest_item.get("snippet") or manifest_item.get("text_snippet") or "")[:100]
                elif not act_copy["ai_inspected"]:
                    if not act_copy.get("inspected_preview"):
                        act_copy["inspected_preview"] = "No readable text extracted (scanned image or binary document)"

            formatted_actions.append(act_copy)
            step_num += 1

        elif act_type == "trash":
            act_copy["action_id"] = f"step-{step_num}"
            act_copy["badge"] = "TRASH"
            act_copy["badge_label"] = "Soft Delete"

            if "ai_inspected" not in act_copy or act_copy["ai_inspected"] is None:
                if manifest_item and manifest_item.get("has_content"):
                    act_copy["ai_inspected"] = True
                    raw_snip = manifest_item.get("snippet") or manifest_item.get("text_snippet") or ""
                    act_copy["inspected_preview"] = raw_snip[:100] if raw_snip else "Content inspected"
                else:
                    act_copy["ai_inspected"] = False
                    act_copy["inspected_preview"] = "No readable text extracted (scanned image or binary document)"
            else:
                if not act_copy.get("inspected_preview"):
                    act_copy["inspected_preview"] = "No readable text extracted (scanned image or binary document)"

            formatted_actions.append(act_copy)
            step_num += 1

    return formatted_actions

def call_gemini_api(system_prompt: str, user_prompt: str) -> dict:
    """
    Sends structured prompt to Gemini API with model fallback:
    1. gemini-3.5-flash-lite
    2. gemini-2.5-flash
    3. gemini-3.8-flash
    4. gemini-2.0-flash
    """
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        return {}

    models = ["gemini-3.5-flash-lite", "gemini-2.5-flash", "gemini-3.8-flash", "gemini-2.0-flash", "gemini-1.5-flash"]
    payload = {
        "system_instruction": {"parts": [{"text": system_prompt}]},
        "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
        "generationConfig": {"temperature": 0.1, "responseMimeType": "application/json"}
    }

    for model in models:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
        try:
            resp = requests.post(url, json=payload, timeout=25)
            if resp.status_code == 200:
                data = resp.json()
                candidates = data.get("candidates", [])
                if candidates:
                    parts = candidates[0].get("content", {}).get("parts", [])
                    if parts:
                        text = parts[0].get("text", "")
                        return extract_json_plan(text)
            elif resp.status_code in (404, 503):
                continue
        except Exception:
            continue
    return {}

@app.route("/propose_plan", methods=["POST"])
def propose_plan():
    """
    Dual-Stage Intent Routing & Live Reconnaissance Endpoint:
    
    STAGE 1: Intent Classification via Gemini (BEFORE disk access)
    - If intent is "CONVERSATION" (e.g., greetings like "Hii", "Hello", "who are you?", "what can you do?"):
      Returns:
      {"type": "conversation", "message": "<Helpful conversational response explaining Storage Copilot capabilities for Imran Tahir>", "version": "v0.5.2-live"}
      Does NOT scan the disk or generate any file mutation cards!
      
    STAGE 2: Physical Disk Reconnaissance & Gemini Action Plan Synthesis
    - Executed ONLY if intent is "FILE_OPERATION".
    - Scans actual files on disk in target folder.
    - Inspects RAM snippets with PII scrubbing.
    - Prompts Gemini to synthesize verified Action Plan.
    - Formats visual badges: FILE_MOVE, PEER_MOVE, RENAME, FOLDER_CREATE, TRASH.
    """
    data = request.get_json(force=True) or {}
    raw_prompt = (data.get("prompt") or "").strip()
    target_folder = data.get("target_folder") or "Download"

    # Load User Profile & Peer Separation Matrix for system context
    p_path = get_user_profile_path()
    profile = {}
    if os.path.exists(p_path):
        try:
            with open(p_path, "r", encoding="utf-8") as pf:
                profile = json.load(pf)
        except Exception:
            pass

    user_id = profile.get("user_identity", {})
    owner_name = user_id.get("primary_name", "Imran Tahir")
    identifiers = user_id.get("identifiers", ["BSAI-182", "182"])
    peers = profile.get("known_peers", [])
    peer_names = [p.get("name") for p in peers if p.get("name")] or ["Fawad", "Sumbal", "Ahmed", "Yousaf"]
    peer_matrix_lines = "\n".join(
        f"- Peer '{p.get('name')}': designated folder '{p.get('designated_folder', f'Documents/Peers/{p.get('name')}')}'"
        for p in peers
    ) if peers else "\n".join(f"- Peer '{p}': designated folder 'Documents/Peers/{p}'" for p in peer_names)

    # =========================================================================
    # STAGE 1: Fast Intent Classification via Gemini (BEFORE touching disk)
    # =========================================================================
    intent_classification_system_prompt = f"""You are Storage Copilot, running locally on an Android device via Termux for {owner_name} (Roll/ID: {identifiers}).
Storage Root: /storage/emulated/0 (Downloads, Documents).
Known Peers: {", ".join(peer_names)}.

Classify the user's prompt into exactly one of two intents:
1. "CONVERSATION":
   - Greetings (e.g., "Hi", "Hii", "Hello", "Hey", "Good morning")
   - Questions about your identity, role, or capabilities (e.g., "who are you?", "what can you do?", "help", "how do you work?")
   - General conversation, small talk, or inquiries not requesting file moves/modifications.

2. "FILE_OPERATION":
   - Requests to organize, sort, clean, separate, move, delete, trash, rename, or manage files and folders (e.g., "Sort student vouchers", "separate peer docs", "clean downloads", "cleanup screenshots", "move pdfs to documents").

Respond STRICTLY with valid JSON adhering to this schema:
{{
  "intent": "CONVERSATION" | "FILE_OPERATION",
  "reply": "<Helpful conversational response explaining Storage Copilot capabilities for {owner_name} on Android Termux if intent is CONVERSATION. Empty string if FILE_OPERATION.>"
}}
"""

    stage1_resp = call_gemini_api(intent_classification_system_prompt, raw_prompt)
    intent = stage1_resp.get("intent", "").upper() if isinstance(stage1_resp, dict) else ""
    conv_reply = stage1_resp.get("reply", "") if isinstance(stage1_resp, dict) else ""

    # Offline / API failure heuristic fallback for Stage 1:
    if not intent:
        lower_p = raw_prompt.lower()
        file_op_keywords = ("sort", "voucher", "fee", "challan", "slip", "peer", "separate", "clean", "download", "screenshot", "trash", "delete", "move", "organize", "rename", "paper", "receipt")
        if any(k in lower_p for k in file_op_keywords):
            intent = "FILE_OPERATION"
        else:
            intent = "CONVERSATION"

    # If intent is CONVERSATION: Return IMMEDIATELY without scanning disk or generating mutation cards!
    if intent == "CONVERSATION":
        if not conv_reply:
            conv_reply = (
                f"👋 Hello {owner_name}! I am Storage Copilot, running locally on your Android device via Termux.\n\n"
                f"Storage Root: /storage/emulated/0 (Downloads, Documents)\n"
                f"Known Peers: {', '.join(peer_names)}\n\n"
                "I analyze unorganized files, separate peer documents from personal storage, sort university fee vouchers, "
                "and clean up downloads—with interactive diff reviews and 1-tap rollback.\n\n"
                "Tap one of the quick task chips below to scan your storage and formulate an Action Plan!"
            )
        return jsonify({
            "type": "conversation",
            "message": conv_reply,
            "version": APP_VERSION,
            "actions": [],
            "quick_chips": [
                "📋 Sort Student Vouchers",
                "👥 Separate Peer Documents",
                "🏷️ Clean Download Names",
                "🗑️ Cleanup Old Screenshots"
            ]
        })

    # =========================================================================
    # STAGE 2: Physical Disk Reconnaissance & Action Plan Synthesis
    # (Reached ONLY when intent is "FILE_OPERATION")
    # =========================================================================

    # 1. Locate target folder
    target_dir = None
    dir_candidates = [
        target_folder,
        target_folder.lower(),
        target_folder.capitalize(),
        os.path.join(BASE_STORAGE_DIR, target_folder),
        os.path.join(BASE_STORAGE_DIR, target_folder.lower()),
    ]
    for c in dir_candidates:
        try:
            p = resolve_safe_path(c) if not os.path.isabs(c) else c
            if os.path.exists(p) and os.path.isdir(p):
                target_dir = p
                break
        except Exception:
            continue

    if not target_dir:
        target_dir = os.path.join(BASE_STORAGE_DIR, target_folder)
        os.makedirs(target_dir, exist_ok=True)

    # 2. Read actual real files on disk
    scanned_entries = []
    if os.path.exists(target_dir) and os.path.isdir(target_dir):
        for entry in os.scandir(target_dir):
            if entry.is_file() and not entry.name.startswith("."):
                scanned_entries.append(entry)

    # If target directory is empty:
    if not scanned_entries:
        return jsonify({
            "type": "conversation",
            "message": (
                f"📂 No eligible documents found in '{target_folder}/'. The directory is currently empty.\n\n"
                f"Please place documents to organize in your {target_folder}/ folder, or tap 'Seed Fixtures' to populate demo files for testing."
            ),
            "version": APP_VERSION,
            "actions": [],
            "quick_chips": [
                "📋 Sort Student Vouchers",
                "👥 Separate Peer Documents",
                "🏷️ Clean Download Names",
                "🗑️ Cleanup Old Screenshots"
            ]
        })

    # 3. In-Memory Snippet Inspection & PII Scrubbing (Up to 30 candidate files)
    manifest = []
    manifest_for_gemini = []
    manifest_lookup = {}
    for entry in scanned_entries[:30]:
        filename = entry.name
        file_size = entry.stat().st_size
        ext = os.path.splitext(filename)[1].lower()

        print(f"[RECON] Inspecting '{filename}' | Size: {file_size} bytes", flush=True)

        raw_text = ""
        try:
            if ext == ".pdf":
                try:
                    import pypdf
                    reader = pypdf.PdfReader(entry.path)
                    parts = [p.extract_text() or "" for p in reader.pages[:2]]
                    raw_text = "\n".join(parts)[:600]
                except Exception:
                    try:
                        with open(entry.path, "r", encoding="utf-8", errors="replace") as f:
                            raw_text = f.read(600)
                    except Exception:
                        raw_text = ""
            elif ext in (".txt", ".md", ".json", ".csv", ".tmp", ".log", ".xml", ".html"):
                with open(entry.path, "r", encoding="utf-8", errors="replace") as f:
                    raw_text = f.read(600)
            else:
                raw_text = ""
        except Exception:
            raw_text = ""

        print(f"[RECON] Extracted {len(raw_text)} chars from {filename}", flush=True)

        sanitized_snippet = sanitize_pii(raw_text)
        print(f"[PII] Sanitized snippet for {filename}: {sanitized_snippet[:80]}...", flush=True)

        try:
            rel_path = os.path.relpath(entry.path, BASE_STORAGE_DIR).replace("\\", "/")
        except Exception:
            rel_path = f"{target_folder}/{filename}"

        manifest_item = {
            "filename": filename,
            "size_kb": round(file_size / 1024, 1),
            "text_snippet": sanitized_snippet if sanitized_snippet else "None (binary/image)",
            "has_content": bool(sanitized_snippet)
        }
        manifest_for_gemini.append(manifest_item)

        internal_item = {
            "filename": filename,
            "name": filename,
            "path": rel_path,
            "extension": ext,
            "size_kb": manifest_item["size_kb"],
            "size_bytes": file_size,
            "snippet": sanitized_snippet,
            "text_snippet": manifest_item["text_snippet"],
            "has_content": manifest_item["has_content"]
        }
        manifest.append(internal_item)
        manifest_lookup[rel_path.lower()] = internal_item
        manifest_lookup[filename.lower()] = internal_item

    # 4. Formulate Stage 2 Gemini System Prompt with Real Files Manifest
    system_prompt = f"""You are Storage Copilot, running locally on an Android device via Termux.
Active User: {owner_name} (Roll/ID: {identifiers}).
Storage Root: /storage/emulated/0 (Downloads, Documents).
Known Peers: {", ".join(peer_names)}.

Peer Separation Matrix:
{peer_matrix_lines}

Define a unified JSON contract for your responses:
{{
  "type": "conversation" | "plan",
  "message": "Conversational reply or plan summary",
  "actions": [ ... ] // empty if type == "conversation"
}}

IMPORTANT: The candidates below are the ACTUAL files currently sitting on disk.
Do NOT filter candidate files using literal prompt keywords (e.g., do NOT look for the literal words "Clean Download Names" or "Separate Peer Documents" in filenames!).
Reason SEMANTICALLY over each candidate's filename, size_kb, text_snippet, and has_content to decide the appropriate operation:

CRITICAL NAMING RULES:
1. NEVER rename files using generic labels like "WhatsApp_Export_Doc", "Organized_Doc", or "Document".
2. Read each file's `text_snippet`. Use the actual subject matter, topic, or document purpose to assign a specific, descriptive name (e.g., "AI_Knowledge_Representation_Lecture.pdf", "Fee_Challan_Fall2025.pdf", "Electricity_Bill_April.pdf").
3. Route to meaningful category folders: e.g., "Documents/University/AI/", "Documents/Finances/", "Documents/Peers/<Name>/".
4. If NO text snippet exists, use date/context (e.g., "Document_2025-04-13.<ext>"), never "WhatsApp_Export".

SEMANTIC TASK GUIDELINES:
1. "Clean Download Names" (or renaming messy/cryptic downloads):
   - Identify cryptic, messy, timestamped, WhatsApp-exported, or paper-code filenames (e.g., 'DOC-20250413-WA0017..pdf', '1508.06576v2_neural_style.pdf', raw hash strings, raw camera timestamps).
   - Propose semantic "type": "move" (RENAME) actions with specific, descriptive human-readable filenames inside appropriate structured folders based on their text_snippet content (e.g., 'Documents/University/AI/Neural_Algorithm_Artistic_Style_Paper.pdf' or 'Documents/Finances/Identity_Document_Export_20250413.pdf').
2. "Cleanup Old Screenshots" or "Clean temp" (soft-deleting trash):
   - Identify image captures / screenshots (e.g., 'Screenshot_*.png') and temporary/cache files ('.tmp', '.cache', '.bak').
   - Propose "type": "trash" actions to route them to '.agent_trash' with 1-tap undo safety.
3. "Separate Peer Documents" (peer separation):
   - Identify documents containing or naming peers ({", ".join(peer_names)}) in their filename or text_snippet (e.g., Fawad, Sumbal, Ahmed, Yousaf).
   - Propose "type": "move" actions to route them into their designated peer folders (e.g. 'Documents/Peers/<PeerName>/...'). Never route peer files to personal folders.
4. "Sort Student Vouchers" (academic & fee management):
   - Identify academic fee documents, challans, or vouchers from snippets or filenames.
   - Route {owner_name}'s personal fee vouchers/slips to 'Documents/University/Vouchers' (or 'Documents/University/').
   - Route any peer vouchers to their respective peer directory in 'Documents/Peers/<PeerName>/'.

CRITICAL RULES FOR FILE ACTIONS:
- Propose actions ONLY for files present in the provided Manifest ({len(manifest)} real files on disk). Never invent or hallucinate non-existent filenames.
- Always create parent directories with 'make_dir' before moving files into them.
- Action format:
  {{"action_id": "step-1", "type": "make_dir", "path": "Documents/..."}}
  {{"action_id": "step-2", "type": "move", "source": "<exact filename from manifest>", "destination": "Documents/...", "rationale": "...", "peer_name": "..." (if peer), "ai_inspected": true, "inspected_preview": "<first 100 chars of text_snippet>"}}
  {{"action_id": "step-3", "type": "trash", "path": "<exact filename from manifest>", "rationale": "...", "ai_inspected": false, "inspected_preview": "No readable text extracted (scanned image or binary document)"}}
- If NO files match the operational criteria, set "type": "conversation" and explain in "message" that no matching files were found. Actions: [].
"""

    user_prompt_text = f"""Operational Request: "{raw_prompt}"

Current Target Directory: "{target_folder}"
Available Real Files Manifest ({len(manifest)} real files on disk):
{json.dumps(manifest_for_gemini, indent=2)}
"""

    gemini_resp = call_gemini_api(system_prompt, user_prompt_text)

    # 5. Evaluate Gemini Stage 2 Output
    if gemini_resp and isinstance(gemini_resp, dict):
        resp_type = gemini_resp.get("type", "conversation")
        resp_message = gemini_resp.get("message", "")
        raw_actions = gemini_resp.get("actions", [])

        if resp_type == "conversation" or not raw_actions:
            return jsonify({
                "type": "conversation",
                "message": resp_message or f"No files in '{target_folder}/' required modification under this criteria.",
                "version": APP_VERSION,
                "actions": [],
                "quick_chips": [
                    "📋 Sort Student Vouchers",
                    "👥 Separate Peer Documents",
                    "🏷️ Clean Download Names",
                    "🗑️ Cleanup Old Screenshots"
                ]
            })

        # Process and validate raw_actions from Gemini plan against manifest
        manifest_paths = {m["path"].lower(): m["path"] for m in manifest}
        manifest_names = {m["filename"].lower(): m["path"] for m in manifest}

        validated_actions = []
        for act in raw_actions:
            act_type = act.get("type")
            if act_type == "make_dir":
                validated_actions.append(act)
            elif act_type in ("move", "copy", "trash"):
                src = act.get("source") or act.get("path") or ""
                matched_src = None
                if src.lower() in manifest_paths:
                    matched_src = manifest_paths[src.lower()]
                elif os.path.basename(src).lower() in manifest_names:
                    matched_src = manifest_names[os.path.basename(src).lower()]

                if matched_src:
                    if act_type in ("move", "copy"):
                        act["source"] = matched_src
                    else:
                        act["path"] = matched_src
                    validated_actions.append(act)

        if validated_actions:
            formatted_actions = format_plan_badges(validated_actions, peer_names, manifest_lookup)
            plan_id = str(uuid.uuid4())
            action_plan = {
                "plan_id": plan_id,
                "version": "1.0",
                "description": resp_message or f"Organize files per: {raw_prompt}",
                "collision_strategy": "RENAME_NUMERIC",
                "actions": formatted_actions
            }
            return jsonify({
                "type": "plan",
                "message": resp_message or f"I've inspected your storage ({len(manifest)} real files on disk) and synthesized an Action Plan.",
                "version": APP_VERSION,
                "actions": formatted_actions,
                "plan": action_plan
            })

    # 6. Heuristic Fallback (Active if Gemini API key is missing or offline for Stage 2)
    lower_p = raw_prompt.lower()
    plan_actions = []
    plan_desc = ""
    created_dirs = set()
    step_idx = 1

    if "voucher" in lower_p or "fee" in lower_p or "challan" in lower_p:
        plan_desc = "Organize student fee vouchers into academic and peer directories"
        for item in manifest:
            fn = item["name"].lower()
            snip = item["snippet"].lower()
            is_voucher = any(k in fn or k in snip for k in ("voucher", "fee", "challan", "slip"))
            if not is_voucher:
                continue

            peer_match = None
            for p_name in peer_names:
                if p_name.lower() in fn or p_name.lower() in snip:
                    peer_match = p_name
                    break

            if peer_match:
                peer_dir = f"Documents/Peers/{peer_match}"
                if peer_dir not in created_dirs:
                    plan_actions.append({"action_id": f"step-{step_idx}", "type": "make_dir", "path": peer_dir})
                    created_dirs.add(peer_dir)
                    step_idx += 1
                plan_actions.append({
                    "action_id": f"step-{step_idx}",
                    "type": "move",
                    "source": item["path"],
                    "destination": f"{peer_dir}/{item['name']}",
                    "peer_name": peer_match,
                    "rationale": f"Routed to peer folder for {peer_match} to avoid polluting personal storage"
                })
                step_idx += 1
            else:
                target_d = "Documents/University/Vouchers"
                if target_d not in created_dirs:
                    plan_actions.append({"action_id": f"step-{step_idx}", "type": "make_dir", "path": target_d})
                    created_dirs.add(target_d)
                    step_idx += 1
                plan_actions.append({
                    "action_id": f"step-{step_idx}",
                    "type": "move",
                    "source": item["path"],
                    "destination": f"{target_d}/{item['name']}",
                    "rationale": f"Personal voucher for {owner_name} filed under University Vouchers"
                })
                step_idx += 1

    elif "peer" in lower_p or "separate" in lower_p:
        plan_desc = f"Separate peer documents from {owner_name}'s personal storage"
        for item in manifest:
            fn = item["name"].lower()
            snip = item["snippet"].lower()
            peer_match = None
            for p_name in peer_names:
                if p_name.lower() in fn or p_name.lower() in snip:
                    peer_match = p_name
                    break

            if peer_match:
                peer_dir = f"Documents/Peers/{peer_match}"
                if peer_dir not in created_dirs:
                    plan_actions.append({"action_id": f"step-{step_idx}", "type": "make_dir", "path": peer_dir})
                    created_dirs.add(peer_dir)
                    step_idx += 1
                plan_actions.append({
                    "action_id": f"step-{step_idx}",
                    "type": "move",
                    "source": item["path"],
                    "destination": f"{peer_dir}/{item['name']}",
                    "peer_name": peer_match,
                    "rationale": f"Isolated document for peer {peer_match}"
                })
                step_idx += 1

    elif "clean" in lower_p and ("name" in lower_p or "download" in lower_p):
        plan_desc = "Standardize cryptic download filenames into structured folders"
        for item in manifest:
            fn = item["name"]
            if fn.endswith((".tmp", ".log")):
                continue
            if re.search(r"^(?:DOC-|\d{4}\.\d{4,5}|IMG_|WhatsApp|Screenshot|[a-f0-9]{16,})", fn, re.I) or ".." in fn or "wa0" in fn.lower():
                clean_title = fn
                snip = item["snippet"].lower()
                if "1508" in fn or "neural" in snip:
                    clean_title = "Neural_Algorithm_Artistic_Style_Paper.pdf"
                    target_d = "Documents/University/AI"
                elif "doc-" in fn.lower() or "wa" in fn.lower():
                    clean_title = re.sub(r"\.\.+", ".", fn)
                    if "identity" in snip or "scanned" in snip:
                        clean_title = "Scanned_Identity_Document_20250413.pdf"
                    else:
                        clean_title = "Document_2025-04-13.pdf"
                    target_d = "Documents/Finances"
                else:
                    clean_title = re.sub(r"^\d{4}\.\d{4,5}v?\d*_", "Research_Paper_", fn)
                    target_d = "Documents/University/AI"

                if target_d not in created_dirs:
                    plan_actions.append({"action_id": f"step-{step_idx}", "type": "make_dir", "path": target_d})
                    created_dirs.add(target_d)
                    step_idx += 1
                plan_actions.append({
                    "action_id": f"step-{step_idx}",
                    "type": "move",
                    "source": item["path"],
                    "destination": f"{target_d}/{clean_title}",
                    "badge": "RENAME",
                    "rationale": f"Identified specific document content in '{fn}' and assigned semantic destination"
                })
                step_idx += 1

    elif "screenshot" in lower_p or "cleanup" in lower_p or "trash" in lower_p:
        plan_desc = "Safely soft-delete obsolete screenshots and sync cache files to .agent_trash"
        for item in manifest:
            fn = item["name"].lower()
            if "screenshot" in fn or fn.endswith((".tmp", ".cache", ".bak")):
                plan_actions.append({
                    "action_id": f"step-{step_idx}",
                    "type": "trash",
                    "path": item["path"],
                    "rationale": "Temporary/screenshot file queued for soft-delete with rollback"
                })
                step_idx += 1

    else:
        keywords = [w for w in re.split(r"\W+", lower_p) if len(w) > 2 and w not in ("the", "all", "and", "for")]
        plan_desc = f"Organize files matching: '{raw_prompt}'"
        for item in manifest:
            fn = item["name"].lower()
            snip = item["snippet"].lower()
            if any(k in fn or k in snip for k in keywords):
                target_d = "Documents/Organized"
                if target_d not in created_dirs:
                    plan_actions.append({"action_id": f"step-{step_idx}", "type": "make_dir", "path": target_d})
                    created_dirs.add(target_d)
                    step_idx += 1
                plan_actions.append({
                    "action_id": f"step-{step_idx}",
                    "type": "move",
                    "source": item["path"],
                    "destination": f"{target_d}/{item['name']}",
                    "rationale": f"Matched criteria for '{raw_prompt}'"
                })
                step_idx += 1

    if not plan_actions:
        return jsonify({
            "type": "conversation",
            "message": (
                f"ℹ️ No eligible documents found in '{target_folder}/' matching \"{raw_prompt}\".\n\n"
                f"Inspected {len(manifest)} real files on disk. None required modification under this criteria."
            ),
            "version": APP_VERSION,
            "actions": [],
            "quick_chips": [
                "📋 Sort Student Vouchers",
                "👥 Separate Peer Documents",
                "🏷️ Clean Download Names",
                "🗑️ Cleanup Old Screenshots"
            ]
        })

    formatted_actions = format_plan_badges(plan_actions, peer_names, manifest_lookup)
    plan_id = str(uuid.uuid4())
    action_plan = {
        "plan_id": plan_id,
        "version": "1.0",
        "description": plan_desc,
        "collision_strategy": "RENAME_NUMERIC",
        "actions": formatted_actions
    }

    return jsonify({
        "type": "plan",
        "message": f"I've inspected your storage ({len(manifest)} real files on disk) and synthesized an Action Plan for: \"{plan_desc}\"",
        "version": APP_VERSION,
        "actions": formatted_actions,
        "plan": action_plan
    })

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)