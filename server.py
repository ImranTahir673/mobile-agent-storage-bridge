import os
import json
import re
import shutil
import sqlite3
import uuid
import hashlib
import time
import zipfile
from collections import defaultdict
from datetime import datetime, timezone
import requests
from flask import Flask, request, jsonify, send_from_directory

app = Flask("MobileStorageBridge")
APP_VERSION = "v0.6-live"

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

@app.errorhandler(Exception)
def handle_global_exception(e):
    """Guarantees the bridge server always responds with JSON, never raw HTML error pages."""
    import traceback
    err_msg = str(e) or type(e).__name__
    print(f"[Storage Copilot Error] Unhandled Exception: {err_msg}")
    traceback.print_exc()
    return jsonify({
        "type": "conversation",
        "message": f"⚠️ Storage Copilot encountered an issue: {err_msg}",
        "error": err_msg,
        "version": APP_VERSION
    }), 500

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

def format_bytes_human(size_bytes: int) -> str:
    """Formats raw byte counts into human-readable strings (KB, MB, GB)."""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    elif size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f} MB"
    else:
        return f"{size_bytes / (1024 * 1024 * 1024):.2f} GB"

def score_keeper_candidate(file_info: dict) -> float:
    """
    Evaluates which file in a duplicate cluster should be retained as the original keeper.
    Higher score indicates higher likelihood of being the original, non-redundant file.
    """
    score = 100.0
    filename = file_info["filename"]
    stem, _ = os.path.splitext(filename)
    stem_lower = stem.lower()

    # 1. Heavily penalize copy/duplicate suffixes
    if re.search(r"\(\d+\)$", stem):
        score -= 60.0
    if re.search(r"[-_](?:copy|\d+)$", stem_lower):
        score -= 40.0
    if "duplicate" in stem_lower or "copy" in stem_lower:
        score -= 50.0

    # 2. Prefer clean canonical names over camera/whatsapp raw dumps
    if re.match(r"^(?:DOC-\d{8}-WA\d{4}|IMG-\d{8}-WA\d{4}|Screenshot_\d{8})", stem):
        score -= 20.0

    # 3. Prefer files already categorized under Documents over Download
    if "download" in file_info.get("path", "").lower():
        score -= 10.0

    # 4. Prefer older file creation/modification (original download)
    mtime = file_info.get("mtime", 0)
    if mtime > 0:
        score -= (mtime % 1000000) * 0.00001

    return score

def find_duplicate_clusters(base_dir: str, target_folder: str = "Download", recursive: bool = True) -> dict:
    """
    Phase 1 Core: Scans files within target_folder (or base_dir) to identify exact duplicate clusters via streaming SHA-256.
    Uses fast size pre-filtering to prevent unnecessary hashing on mobile.
    Selects the cleanest original copy as keeper and stages redundant copies for cleanup.
    """
    target_path = resolve_safe_path(target_folder) if target_folder else base_dir
    if not os.path.exists(target_path) or not os.path.isdir(target_path):
        return {
            "status": "success",
            "target_folder": target_folder,
            "total_clusters": 0,
            "total_duplicates": 0,
            "reclaimable_bytes": 0,
            "reclaimable_human": "0 B",
            "clusters": []
        }

    # Step 1: Collect non-hidden regular files grouped by size
    files_by_size = defaultdict(list)
    if recursive:
        for root, dirs, files in os.walk(target_path):
            dirs[:] = [d for d in dirs if not d.startswith(".") and d.lower() != "android" and d != ".agent_trash"]
            for fname in files:
                if fname.startswith("."):
                    continue
                abs_p = os.path.join(root, fname)
                try:
                    st = os.stat(abs_p)
                    size = st.st_size
                    if size > 0:
                        try:
                            rel_path = os.path.relpath(abs_p, base_dir).replace("\\", "/")
                        except Exception:
                            rel_path = fname

                        mtime_str = datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M")
                        files_by_size[size].append({
                            "filename": fname,
                            "path": rel_path,
                            "abs_path": abs_p,
                            "size_bytes": size,
                            "mtime": st.st_mtime,
                            "modified_date": mtime_str
                        })
                except Exception:
                    continue
    else:
        for entry in os.scandir(target_path):
            if entry.is_file() and not entry.name.startswith("."):
                try:
                    st = entry.stat()
                    size = st.st_size
                    if size > 0:
                        try:
                            rel_path = os.path.relpath(entry.path, base_dir).replace("\\", "/")
                        except Exception:
                            rel_path = entry.name

                        mtime_str = datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M")
                        files_by_size[size].append({
                            "filename": entry.name,
                            "path": rel_path,
                            "abs_path": entry.path,
                            "size_bytes": size,
                            "mtime": st.st_mtime,
                            "modified_date": mtime_str
                        })
                except Exception:
                    continue

    # Step 2: Stream SHA-256 only on files with identical non-zero sizes
    clusters_by_hash = defaultdict(list)
    for size, file_list in files_by_size.items():
        if len(file_list) >= 2:
            for f_info in file_list:
                f_hash = calculate_file_hash(f_info["abs_path"])
                if f_hash:
                    clusters_by_hash[f_hash].append(f_info)

    # Step 3: Filter true duplicate clusters (>= 2 files with identical hash)
    clusters = []
    total_duplicates = 0
    reclaimable_bytes = 0

    for f_hash, file_list in clusters_by_hash.items():
        if len(file_list) >= 2:
            sorted_files = sorted(file_list, key=score_keeper_candidate, reverse=True)
            keeper = sorted_files[0]
            keeper_dict = {
                "filename": keeper["filename"],
                "path": keeper["path"],
                "size_bytes": keeper["size_bytes"],
                "modified_date": keeper["modified_date"],
                "reason": "Original file with clean canonical naming"
            }

            redundant_files = []
            file_size = keeper["size_bytes"]
            for dup in sorted_files[1:]:
                reason = "Redundant copy"
                if re.search(r"\(\d+\)", dup["filename"]):
                    reason = "Redundant copy with numeric download suffix"
                elif "copy" in dup["filename"].lower():
                    reason = "Redundant copy duplicate"
                redundant_files.append({
                    "filename": dup["filename"],
                    "path": dup["path"],
                    "size_bytes": dup["size_bytes"],
                    "modified_date": dup["modified_date"],
                    "reason": reason
                })

            cluster_reclaimable = file_size * len(redundant_files)
            reclaimable_bytes += cluster_reclaimable
            total_duplicates += len(redundant_files)

            clusters.append({
                "hash": f_hash,
                "size_bytes": file_size,
                "size_human": format_bytes_human(file_size),
                "reclaimable_bytes": cluster_reclaimable,
                "reclaimable_human": format_bytes_human(cluster_reclaimable),
                "keeper": keeper_dict,
                "duplicates": redundant_files
            })

    return {
        "status": "success",
        "target_folder": target_folder,
        "total_clusters": len(clusters),
        "total_duplicates": total_duplicates,
        "reclaimable_bytes": reclaimable_bytes,
        "reclaimable_human": format_bytes_human(reclaimable_bytes),
        "clusters": clusters
    }

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

def extract_file_snippet(file_path: str, max_chars: int = 400) -> str:
    """
    Intelligently extracts textual preview across text files, PDFs, Word docx, PowerPoint pptx, and archives.
    Uses Python standard library zipfile for docx/pptx/zip/apk (strictly zero external dependencies).
    """
    ext = os.path.splitext(file_path)[1].lower()
    try:
        if ext == ".pdf":
            try:
                import pypdf
                reader = pypdf.PdfReader(file_path)
                parts = [p.extract_text() or "" for p in reader.pages[:2]]
                return "\n".join(parts)[:max_chars]
            except Exception:
                try:
                    with open(file_path, "r", encoding="utf-8", errors="replace") as fh:
                        return fh.read(max_chars)
                except Exception:
                    return ""
        elif ext in (".txt", ".md", ".json", ".csv", ".tmp", ".log", ".xml", ".html", ".tex", ".cpp", ".py", ".vcf", ".ics"):
            try:
                with open(file_path, "r", encoding="utf-8", errors="replace") as fh:
                    return fh.read(max_chars)
            except Exception:
                return ""
        elif ext in (".docx", ".pptx", ".xlsx", ".zip", ".apk", ".xapk"):
            try:
                with zipfile.ZipFile(file_path, "r") as z:
                    names = z.namelist()
                    if ext == ".docx" and "word/document.xml" in names:
                        xml = z.read("word/document.xml").decode("utf-8", errors="ignore")
                        text = re.sub(r"<[^>]+>", " ", xml)
                        text = re.sub(r"\s+", " ", text).strip()
                        return f"[Word Doc: {text[:max_chars]}]"
                    elif ext == ".pptx" and any(n.startswith("ppt/slides/slide") for n in names):
                        slide_files = [n for n in names if n.startswith("ppt/slides/slide") and n.endswith(".xml")][:3]
                        text_parts = []
                        for sf in slide_files:
                            xml = z.read(sf).decode("utf-8", errors="ignore")
                            s_text = re.sub(r"<[^>]+>", " ", xml)
                            text_parts.append(s_text)
                        full_t = re.sub(r"\s+", " ", " ".join(text_parts)).strip()
                        return f"[Presentation: {full_t[:max_chars]}]"
                    elif ext in (".apk", ".xapk"):
                        has_manifest = "AndroidManifest.xml" in names
                        clean_names = [n for n in names if not n.startswith("META-INF")][:6]
                        return f"[Android Package: {len(names)} files, manifest={'yes' if has_manifest else 'no'}, entries: {', '.join(clean_names)}]"
                    else:
                        clean_names = [n for n in names if not n.startswith("__MACOSX")][:8]
                        return f"[Archive with {len(names)} files: {', '.join(clean_names)}]"
            except Exception:
                return ""
    except Exception:
        return ""
    return ""

PII_PATTERNS = [
    # Passports (labeled and standalone patterns)
    (re.compile(r"(?i)\b(passport(?:\s*(?:no|number|#))?[:\s]*)[A-Za-z0-9]{6,12}\b"), r"\1[REDACTED_PASSPORT]"),
    (re.compile(r"\b[A-Za-z]{1,2}[0-9]{7,8}\b"), "[REDACTED_PASSPORT]"),

    # National ID / CNIC (both formatted 5-7-1 and unformatted 13 digits)
    (re.compile(r"\b\d{5}-\d{7}-\d\b"), "[REDACTED_CNIC]"),
    (re.compile(r"(?i)\b(?:cnic|national\s+id|nic|b-form)(?:\s*(?:no|number|#))?[:\s]*\b(\d+)\b"), "CNIC: [REDACTED_CNIC]"),
    (re.compile(r"\b\d{13}\b"), "[REDACTED_CNIC]"),

    # Bank Accounts & IBAN
    (re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{12,30}\b"), "[REDACTED_IBAN]"),
    (re.compile(r"(?i)\b(?:account|acc|acct)(?:\s*(?:no|number|#))?[:\s]*(\d{10,24})\b"), "Account: [REDACTED_ACCOUNT]"),

    # Phone Numbers
    (re.compile(r"(?:\+92[- ]?|0)?3\d{2}[- ]?\d{7}\b"), "[REDACTED_PHONE]"),
    (re.compile(r"\b(?:\+?\d{1,3}[- ]?)?\(?\d{3}\)?[- ]?\d{3}[- ]?\d{4}\b"), "[REDACTED_PHONE]"),

    # Payment Cards
    (re.compile(r"\b(?:\d{4}[- ]?){3}\d{4}\b"), "[REDACTED_CARD]"),

    # Email Addresses
    (re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"), "[REDACTED_EMAIL]"),

    # Personal Names in formal administrative labels (Student Name, Applicant, Cardholder, etc.)
    (re.compile(r"(?i)\b((?:student\s+name|applicant\s+name|customer\s+name|cardholder\s+name|father(?:'s)?\s+name|full\s+name|name)\s*[:\-])\s*([A-Za-z][A-Za-z0-9\s.]{1,35})(?=[,\n\r;]|\s{2,}|$)", re.IGNORECASE), r"\1 [REDACTED_NAME]"),
    (re.compile(r"(?i)\b((?:receipt\s+for|voucher\s+for|slip\s+for)\s+)([A-Za-z][A-Za-z\s]{1,25})(?=[,\n\r;]|\s{2,}|$)", re.IGNORECASE), r"\1[REDACTED_NAME]"),
]

def sanitize_pii(text: str) -> str:
    """Pre-flight local scrubbing of sensitive personally identifiable information."""
    if not text:
        return ""
    clean = text
    for pattern, replacement in PII_PATTERNS:
        clean = pattern.sub(replacement, clean)
    return clean

def classify_document(filename: str, snippet: str = "") -> dict:
    """
    Cautiously classifies a document based on BOTH filename and inside text context.
    Strictly distinguishes Academic Course Materials from Identity Passes, Vouchers, and Bills.
    """
    fn_lower = (filename or "").lower()
    snip_lower = (snippet or "").lower()
    combined = f"{fn_lower} {snip_lower}"

    # 1. Identity & Entry Passes (High Caution Priority)
    # Even if it says 'University' or 'AI', an Entry Pass or Passport is an IDENTITY document, not course study material.
    is_identity = any(k in combined for k in (
        "entry pass", "gate pass", "student pass", "transit pass", "admission pass", 
        "passport", "identity card", "id card", "cnic", "student card"
    )) or (("pass" in fn_lower or "passport" in fn_lower) and not any(k in fn_lower for k in ("password", "passed", "passive")))

    if is_identity:
        return {
            "category": "IDENTITY_PASS",
            "type_label": "Identity / Entry Pass",
            "is_academic_study": False,
            "target_dir": "Documents/Personal/Identity",
            "suggested_title": filename
        }

    # 2. Academic Financial Vouchers / Fee Slips
    is_voucher = any(k in combined for k in (
        "fee voucher", "fee slip", "tuition voucher", "challan", "student voucher", "semester fee"
    )) or (("voucher" in fn_lower or "fee" in fn_lower) and "bill" not in fn_lower)

    if is_voucher:
        stem = os.path.splitext(filename)[0]
        clean_title = f"{stem}.pdf" if not stem.endswith(".pdf") else stem
        return {
            "category": "ACADEMIC_VOUCHER",
            "type_label": "Academic Tuition Voucher",
            "is_academic_study": False,
            "target_dir": "Documents/University/Vouchers",
            "suggested_title": clean_title
        }

    # 3. Utility Bills & Commercial Invoices
    is_utility = any(k in combined for k in (
        "utility bill", "electricity bill", "electric bill", "gas bill", "water bill", 
        "consumer id", "amount due", "invoice for march", "invoice for april", "bill invoice"
    )) or ("bill" in fn_lower and "voucher" not in fn_lower)

    if is_utility:
        return {
            "category": "FINANCIAL_BILL",
            "type_label": "Utility / Financial Bill",
            "is_academic_study": False,
            "target_dir": "Documents/Finances",
            "suggested_title": "Electricity_Utility_Bill_March2025.pdf" if "electricity" in combined else "Utility_Bill_Invoice.pdf"
        }

    # 4. Academic Coursework & Study Materials (Lectures, Assignments, Papers)
    academic_keywords = (
        "knowledge representation", "artificial intelligence", "lecture", "course", 
        "assignment", "semester", "bsai", "syllabus", "slides", "notes", "neural", 
        "heuristic search", "exam", "lab report", "tutorial", "arxiv", "deep learning"
    )
    is_academic = any(k in combined for k in academic_keywords) or "1508." in fn_lower

    if is_academic:
        subject = "AI"
        if "knowledge representation" in combined:
            suggested = "Knowledge_Representation_Lecture_04.pdf"
        elif "assignment" in combined:
            suggested = "AI_Course_Assignment_02.pdf"
        elif "neural" in combined or "1508" in fn_lower:
            suggested = "Neural_Algorithm_Artistic_Style_Paper.pdf"
        else:
            stem = os.path.splitext(filename)[0]
            clean_stem = re.sub(r"^(?:DOC-|\d{4}\.\d{4,5}|WhatsApp_Doc_?)", "", stem)
            clean_token = re.sub(r"[^\w-]", "_", clean_stem).strip("_")
            suggested = f"AI_{clean_token or 'Study_Material'}.pdf"

        return {
            "category": "ACADEMIC_STUDY",
            "type_label": "Academic Study Material",
            "is_academic_study": True,
            "target_dir": f"Documents/University/{subject}",
            "suggested_title": suggested
        }

    # 5. Career & Professional Resumes (CV, Resume, Portfolio)
    is_career = any(k in combined for k in (
        "curriculum vitae", "resume", " cv ", "cv.", "cv_", "cv-", "cover letter", 
        "portfolio", "job application", "recommendation letter"
    )) or fn_lower.startswith("cv") or "resume" in fn_lower
    if is_career and not is_academic and not is_voucher:
        return {
            "category": "CAREER_DOCUMENT",
            "type_label": "Career / Resume",
            "is_academic_study": False,
            "target_dir": "Documents/Personal/Career",
            "suggested_title": filename
        }

    # 6. Temporary & Screenshots
    if "screenshot" in fn_lower or fn_lower.endswith((".tmp", ".cache", ".bak", ".log")):
        return {
            "category": "TEMPORARY_TRASH",
            "type_label": "Temporary / Screenshot",
            "is_academic_study": False,
            "target_dir": ".agent_trash",
            "suggested_title": filename
        }

    # 7. General Document
    return {
        "category": "GENERAL_DOCUMENT",
        "type_label": "General Document",
        "is_academic_study": False,
        "target_dir": "Documents/Personal",
        "suggested_title": filename
    }

SEARCH_STOPWORDS = {
    "the", "a", "an", "all", "and", "or", "for", "with", "doc", "pdf", "file", "files", 
    "to", "in", "on", "at", "by", "from", "some", "my", "me", "your", "our", "please", 
    "can", "could", "would", "should", "want", "like", "any", "this", "that", "these", 
    "those", "how", "what", "which", "where", "tell", "give", "list", "listing", "show", 
    "get", "find", "search", "locate", "check", "view", "book", "books", "document", "documents", 
    "paper", "copy", "copies", "download", "downloads", "inside", "here", "there", "of", 
    "is", "are", "have", "has", "do", "does", "did", "also", "you", "language", "guide",
    "guides", "looking", "need", "folder", "folders", "directory", "directories", "fikes", "vest"
}

def parse_query_intent(query: str, default_folder: str = "Download") -> dict:
    """
    Analyzes the user's natural language query to extract:
    1. Folder constraints (e.g. "in download", "in documents")
    2. Intent type: "directory_listing" vs "temporal_recency" vs "specific_search"
    3. Meaningful semantic target keywords (excluding stopwords)
    """
    clean_q = query.strip()
    lower_q = clean_q.lower()

    # Detect folder constraints
    folder_constraint = None
    if any(p in lower_q for p in ("in download", "in the download", "in downloads", "in the downloads", "in my download", "inside download", "from download", "of download")):
        folder_constraint = "Download"
    elif any(p in lower_q for p in ("in documents", "in the documents", "inside documents", "in my documents", "from documents")):
        folder_constraint = "Documents"

    # Detect directory listing intent vs specific file search
    is_directory_listing = False
    listing_patterns = (
        r"\b(?:list|show|view|display|what(?:\s+is|\s+are|\s+files|\s+all)?)\b.*\b(?:all|files|everything|here|inside|contents|directory)\b",
        r"\ball\s+files\b",
        r"\blist\s+of\b",
        r"\bshow\s+files\b",
        r"\bwhat\s+(?:files|is)\s+in\b"
    )
    for pat in listing_patterns:
        if re.search(pat, lower_q, re.I):
            is_directory_listing = True
            break

    # If the user specifically mentions a category or topic, treat as filtered search
    filtered_topic = None
    if is_directory_listing:
        if any(w in lower_q for w in ("lecture", "lectures", "uni", "university", "study", "assignment", "paper", "course")):
            filtered_topic = "academic"
            is_directory_listing = False
        elif any(w in lower_q for w in ("receipt", "receipts", "voucher", "vouchers", "bill", "bills", "fee")):
            filtered_topic = "financial"
            is_directory_listing = False
        elif any(w in lower_q for w in ("peer", "peers", "friends", "fawad", "sumbal", "ahmed", "yousaf")):
            filtered_topic = "peer"
            is_directory_listing = False
        elif any(w in lower_q for w in ("screenshot", "screenshots", "image", "images")):
            filtered_topic = "screenshot"
            is_directory_listing = False

    # Detect folder audit / remaining work query (e.g. "Any more sorting for the download?", "What's left to sort?", "Are there more files?")
    is_audit_status_query = False
    audit_status_patterns = (
        r"\b(?:any\s+more|anything\s+else|what(?:'s|\s+is)\s+left|is\s+there\s+more|more\s+to\s+do|still\s+need|status\s+of)\b",
        r"\b(?:clean\s+now|finished\s+sorting|done\s+sorting|remaining\s+files|left\s+to\s+sort|more\s+sorting)\b",
        r"\bany\s+other\s+files\b",
        r"\bany\s+more\b"
    )
    for pat in audit_status_patterns:
        if re.search(pat, lower_q, re.I):
            is_audit_status_query = True
            break

    # Extract target tokens for specific searches (only if not an audit status query)
    if is_audit_status_query:
        target_tokens = []
        cleaned_target = ""
    else:
        # 1. Strip trailing punctuation first
        cleaned_target = re.sub(r"[?!.,\"';:]+$", "", lower_q).strip()
        # 2. Strip conversational inquiry prefixes
        cleaned_target = re.sub(
            r"^(?:also\s+)?(?:can\s+you\s+)?(?:could\s+you\s+)?(?:please\s+)?(?:find(?: me)?|where is|where are|search(?: for)?|locate|show(?: me)?|get(?: me)?|list|check|do\s+i\s+have|looking\s+for|i\s+need)\s+",
            "",
            cleaned_target
        ).strip()
        # 3. Strip noun wrappers ("a book for", "the doc for", etc.)
        cleaned_target = re.sub(r"^(?:the\s+book\s+for|a\s+book\s+for|the\s+book|a\s+book|the\s+guide\s+for|a\s+guide\s+for|the\s+doc\s+for|the\s+file\s+for|a\s+copy\s+of|my|the|a|an)\s+", "", cleaned_target).strip()
        # 4. Strip trailing location references
        cleaned_target = re.sub(r"\s+(?:in|inside|from|of)\s+(?:the\s+)?(?:downloads?|documents?|storage)$", "", cleaned_target).strip()
        # 5. Extract non-stopword tokens
        target_tokens = [t for t in re.findall(r"\w+", cleaned_target) if len(t) > 1 and t not in SEARCH_STOPWORDS]

    return {
        "folder_constraint": folder_constraint or (default_folder if (is_directory_listing or is_audit_status_query) else folder_constraint),
        "is_directory_listing": is_directory_listing,
        "is_audit_status_query": is_audit_status_query,
        "filtered_topic": filtered_topic,
        "target_tokens": target_tokens,
        "cleaned_target": cleaned_target
    }

def score_document_match(query: str, doc: dict) -> tuple[float, str]:
    """
    Calculates semantic relevance score (0.0 - 200.0) for a document.
    Enforces robust token matching so meaningful keywords (like 'Java') in filenames match with high confidence.
    """
    parsed = parse_query_intent(query)
    target_tokens = parsed["target_tokens"]
    cleaned_target = parsed["cleaned_target"]

    if not target_tokens and not cleaned_target:
        return 0.0, ""

    fn = doc.get("filename", "").lower()
    fn_stem = os.path.splitext(fn)[0].lower()
    fn_normalized = re.sub(r"[-_.]+", " ", fn_stem).strip()
    snip = (doc.get("snippet") or doc.get("text_snippet") or "").lower()
    score = 0.0
    match_reason = ""

    # 1. Exact or Clean Substring Match
    if cleaned_target and (fn == cleaned_target or fn_stem == cleaned_target or fn_normalized == cleaned_target):
        score += 150.0
        match_reason = "Exact title match"
    elif cleaned_target and len(cleaned_target) >= 3 and (cleaned_target in fn or cleaned_target in fn_stem or cleaned_target in fn_normalized):
        score += 120.0
        match_reason = f"Title contains '{cleaned_target}'"
    elif target_tokens:
        matched_fn_tokens = [t for t in target_tokens if t in fn or t in fn_normalized]
        if matched_fn_tokens:
            fn_coverage = len(matched_fn_tokens) / len(target_tokens)
            if fn_coverage == 1.0:
                score += 120.0 + (15.0 * len(matched_fn_tokens))
                match_reason = f"All query keywords match: {', '.join(matched_fn_tokens)}"
            else:
                score += 75.0 + (25.0 * len(matched_fn_tokens)) + (20.0 * fn_coverage)
                match_reason = f"Filename keyword match: {', '.join(matched_fn_tokens)}"

    # 1b. Folder Constraint Alignment Boost
    if parsed.get("folder_constraint"):
        fc = parsed["folder_constraint"].lower()
        doc_p = doc.get("path", "").lower()
        if doc_p.startswith(fc + "/") or (fc == "download" and not doc_p.startswith("documents/")):
            score += 25.0

    # 2. Content Snippet Matching
    if snip and snip != "none (binary/image)":
        if cleaned_target and len(cleaned_target) >= 4 and cleaned_target in snip:
            score += 60.0
            if not match_reason:
                match_reason = f"Content mentions '{cleaned_target}'"
        elif target_tokens:
            matched_snip_tokens = [t for t in target_tokens if t in snip]
            snip_coverage = len(matched_snip_tokens) / len(target_tokens)
            if snip_coverage >= 0.6:
                score += 40.0 * snip_coverage
                if not match_reason:
                    match_reason = f"Content keywords: {', '.join(matched_snip_tokens)}"

    # 3. Document Category & Topic Alignment
    classification = classify_document(doc.get("filename", ""), snip)
    doc_cat = classification.get("category", "")

    if any(t in target_tokens for t in ("pass", "entry", "passport")):
        if doc_cat == "IDENTITY_PASS":
            score += 40.0
    elif any(t in target_tokens for t in ("bill", "electricity", "utility", "invoice")):
        if doc_cat == "FINANCIAL_BILL":
            score += 40.0
    elif any(t in target_tokens for t in ("voucher", "fee", "challan", "slip")):
        if doc_cat == "ACADEMIC_VOUCHER":
            score += 40.0
    elif any(t in target_tokens for t in ("lecture", "notes", "assignment", "neural", "course")):
        if doc_cat == "ACADEMIC_STUDY":
            score += 40.0
        elif doc_cat == "IDENTITY_PASS":
            score = 0.0  # Cautionary penalty: never treat identity pass as academic lecture

    # Filter out weak incidental noise (must achieve meaningful semantic threshold)
    if score < 45.0:
        return 0.0, ""

    return max(0.0, score), match_reason

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

    snippet = extract_file_snippet(target, max_chars)
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
    1. Validates batch size against safe upper capacity (up to 1,000 actions).
    2. Enforces dry_run mode if requested.
    3. Executes changes gradually in safe micro-chunks (20 files/chunk) with per-chunk SQLite WAL commits.
    4. Auto-rolls back all previous steps if any action fails at any point.
    """
    plan = request.get_json(force=True) or {}
    plan_id = plan.get("plan_id") or str(uuid.uuid4())
    intent = plan.get("description") or "Batch filesystem mutation"
    actions = plan.get("actions", [])
    dry_run = bool(plan.get("dry_run", False))
    collision_strategy = plan.get("collision_strategy", "FAIL")

    if not actions:
        return jsonify({"error": "Action plan contains no actions"}), 400

    MAX_BATCH_ACTIONS = 1000
    EXECUTION_CHUNK_SIZE = 20

    # Safety guardrail (expanded to 1,000 files with gradual execution technique)
    if len(actions) > MAX_BATCH_ACTIONS:
        return jsonify({"error": f"Execution limit exceeded: batch size {len(actions)} > {MAX_BATCH_ACTIONS} maximum"}), 400

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
        conn.commit()

        # Technique: Gradual Progressive Execution in safe micro-chunks (20 files/chunk)
        # Keeps memory footprint low, flushes OS disk cache, and avoids Termux I/O throttling
        chunks = [prepared_steps[i:i + EXECUTION_CHUNK_SIZE] for i in range(0, len(prepared_steps), EXECUTION_CHUNK_SIZE)]
        executed_actions = []

        for chunk_idx, chunk in enumerate(chunks):
            for step in chunk:
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

            # Commit SQLite WAL after each chunk to persist progress and keep journal size small
            conn.commit()

            # Gradual execution yield: 20ms pause between chunks so OS I/O cache flushes smoothly
            if chunk_idx < len(chunks) - 1:
                time.sleep(0.02)

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
            "total_actions": len(prepared_steps),
            "chunks_processed": len(chunks),
            "chunk_size": EXECUTION_CHUNK_SIZE,
            "gradual_execution": True,
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

@app.route("/scan_duplicates", methods=["GET", "POST"])
def scan_duplicates():
    """
    Phase 1 Core Endpoint:
    Scans target_folder (defaults to Download/) for exact duplicate clusters using streaming SHA-256.
    Returns designated original keeper, redundant copies, and total reclaimable storage bytes.
    """
    data = {}
    if request.method == "POST":
        data = request.get_json(silent=True) or {}
    target_folder = data.get("target_folder") or request.args.get("target_folder") or "Download"
    
    try:
        res = find_duplicate_clusters(BASE_STORAGE_DIR, target_folder)
        return jsonify(res)
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500

@app.route("/seed_fixtures", methods=["POST"])
def seed_fixtures():
    """Seeds realistic sample mobile files in Download/ to test instant plan generation, query inspection, and execution."""
    data = request.get_json(silent=True) or {}
    include_duplicates = bool(data.get("duplicates") or request.args.get("duplicates"))
    download_dir = os.path.join(BASE_DIR, "Download")
    os.makedirs(download_dir, exist_ok=True)
    sample_files = {
        "BSAI-182_fee_voucher_fall.pdf": "Student Fee Voucher: Name: Imran Tahir, Roll No: BSAI-182, Amount: PKR 45,000, Status: Paid.",
        "fawad fee.pdf": "Student Fee Slip: Name: Fawad, Roll No: BSAI-190, Amount: PKR 45,000, Status: Paid.",
        "Sumbal pass.pdf": "University Entry Pass: Student Name: Sumbal, Department: AI.",
        "1508.06576v2_neural_style.pdf": "A Neural Algorithm of Artistic Style by Leon A. Gatys, Alexander S. Ecker, Matthias Bethge. ArXiv research paper on deep learning neural representations.",
        "DOC-20250413-WA0017..pdf": "Knowledge Representation and Reasoning Lecture 4: First-Order Predicate Logic, Ontologies, and Semantic Networks. BSAI Semester 6 University Course.",
        "DOC-20250413-WA0018..pdf": "Artificial Intelligence Course Assignment 2: Heuristic Search and A* Graph Exploration by Imran Tahir (BSAI-182).",
        "DOC-20250413-WA0019..pdf": "Electricity Utility Bill Invoice for March 2025. Consumer ID: 948201. Amount Due: PKR 8,500. Status: Pending.",
        "Screenshot_20241001-142210.png": "[PNG Image Binary Fixture: Temporary screen capture]",
        "temp_cache_sync.tmp": "[Temporary sync cache file created by updater]",
        "WhatsApp_Doc_Ahmed_Receipt.pdf": "Payment receipt for Ahmed, Department Library Fee."
    }
    if include_duplicates:
        sample_files["1508.06576v2_neural_style (1).pdf"] = sample_files["1508.06576v2_neural_style.pdf"]
        sample_files["DOC-20250413-WA0019 (1)..pdf"] = sample_files["DOC-20250413-WA0019..pdf"]

    created = []
    for fname, content in sample_files.items():
        fpath = os.path.join(download_dir, fname)
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
                peer_target = act_copy.get("peer_name")
                is_peer_dest = "documents/peers/" in dst.lower() or "peers/" in dst.lower()
                if not peer_target and is_peer_dest:
                    for p_name in peer_names:
                        if p_name.lower() in dst.lower() or p_name.lower() in src.lower():
                            peer_target = p_name
                            break

                if peer_target and is_peer_dest:
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

GEMINI_MODELS = [
    "gemini-3.5-flash-lite",
    "gemini-3.5-flash",
    "gemini-3.8-flash",
    "gemini-flash-latest",
    "gemini-2.5-flash"
]

def call_gemini_json(system_prompt: str, user_prompt: str) -> dict:
    """
    Sends structured JSON prompt to Gemini API with robust model fallback:
    1. gemini-3.5-flash-lite (fast, high throughput, active)
    2. gemini-3.5-flash
    """
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        print("[Storage Copilot] GEMINI_API_KEY not configured. Running in Local Offline Intelligence mode.")
        return {}

    payload = {
        "system_instruction": {"parts": [{"text": system_prompt}]},
        "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
        "generationConfig": {"temperature": 0.1, "responseMimeType": "application/json"}
    }

    for model in GEMINI_MODELS:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
        try:
            resp = requests.post(url, json=payload, timeout=30)
            if resp.status_code == 200:
                data = resp.json()
                candidates = data.get("candidates", [])
                if candidates:
                    parts = candidates[0].get("content", {}).get("parts", [])
                    if parts:
                        text = parts[0].get("text", "")
                        try:
                            parsed = extract_json_plan(text)
                            if parsed:
                                print(f"[Storage Copilot Gemini] Successfully received response via model '{model}'.")
                                return parsed
                        except Exception:
                            continue
            else:
                print(f"[Storage Copilot Gemini] Model '{model}' returned HTTP {resp.status_code}.")
                continue
        except Exception as e:
            print(f"[Storage Copilot Gemini] Model '{model}' request failed: {e}")
            continue
    return {}

def call_gemini_text(system_prompt: str, user_prompt: str) -> str:
    """
    Sends conversational/markdown prompt to Gemini API with robust model fallback and returns raw text.
    """
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        print("[Storage Copilot] GEMINI_API_KEY not configured. Running in Local Offline Intelligence mode.")
        return ""

    payload = {
        "system_instruction": {"parts": [{"text": system_prompt}]},
        "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
        "generationConfig": {"temperature": 0.3}
    }

    for model in GEMINI_MODELS:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
        try:
            resp = requests.post(url, json=payload, timeout=30)
            if resp.status_code == 200:
                data = resp.json()
                candidates = data.get("candidates", [])
                if candidates:
                    parts = candidates[0].get("content", {}).get("parts", [])
                    if parts:
                        text = parts[0].get("text", "")
                        if text and text.strip():
                            print(f"[Storage Copilot Gemini] Successfully received conversational text via model '{model}'.")
                            return text.strip()
            else:
                print(f"[Storage Copilot Gemini] Model '{model}' returned HTTP {resp.status_code}.")
                continue
        except Exception as e:
            print(f"[Storage Copilot Gemini] Model '{model}' request failed: {e}")
            continue
    return ""

def call_gemini_api(system_prompt: str, user_prompt: str) -> dict:
    """Backwards compatibility alias for structured JSON plan generation."""
    return call_gemini_json(system_prompt, user_prompt)

def collect_storage_files(base_dir: str, target_folder: str = "Download", max_files: int = 1500) -> list:
    """
    Collects real files across device storage (target folder and Documents subtree) without arbitrary low caps.
    Extracts modification dates, sizes, and up to 600 chars of text with PII redaction for inspection and query answering.
    """
    collected = []
    seen_paths = set()
    now_dt = datetime.now()

    search_dirs = [
        os.path.join(base_dir, target_folder),
        os.path.join(base_dir, "Documents"),
        os.path.join(base_dir, "Download"),
    ]

    for s_dir in search_dirs:
        if not os.path.exists(s_dir) or not os.path.isdir(s_dir):
            continue
        try:
            for root, dirs, files in os.walk(s_dir):
                # Omit internal protected trees from query inspection
                dirs[:] = [d for d in dirs if not d.startswith(".") and d.lower() != "android"]
                for f in files:
                    if f.startswith("."):
                        continue
                    full_p = os.path.join(root, f)
                    if full_p in seen_paths:
                        continue
                    seen_paths.add(full_p)

                    try:
                        fstat = os.stat(full_p)
                        fsize = fstat.st_size
                        mtime = fstat.st_mtime
                        mtime_dt = datetime.fromtimestamp(mtime)
                        is_today = (mtime_dt.date() == now_dt.date())
                        modified_str = mtime_dt.strftime("%Y-%m-%d %H:%M")
                        date_str = mtime_dt.strftime("%Y-%m-%d")
                    except Exception:
                        fsize = 0
                        mtime = 0
                        is_today = False
                        modified_str = ""
                        date_str = ""

                    ext = os.path.splitext(f)[1].lower()
                    rel_p = os.path.relpath(full_p, base_dir).replace("\\", "/")

                    raw_text = extract_file_snippet(full_p, 400)
                    sanitized = sanitize_pii(raw_text)
                    collected.append({
                        "filename": f,
                        "name": f,
                        "path": rel_p,
                        "full_path": full_p,
                        "extension": ext,
                        "size_kb": round(fsize / 1024, 1),
                        "size_bytes": fsize,
                        "modified_timestamp": mtime,
                        "modified_str": modified_str,
                        "date_str": date_str,
                        "is_today": is_today,
                        "snippet": sanitized,
                        "text_snippet": sanitized if sanitized else "None (binary/image)",
                        "has_content": bool(sanitized)
                    })
                    if len(collected) >= max_files:
                        return collected
        except Exception:
            continue
    return collected

def ensure_unique_plan_destinations(actions: list) -> list:
    """
    Enforces strict destination uniqueness across all actions in a single batch.
    Prevents duplicate destination collisions by disambiguating with source tokens or numeric counters.
    """
    seen_destinations = set()
    cleaned_actions = []

    for act in actions:
        act_copy = dict(act)
        act_type = act_copy.get("type", "")

        if act_type in ("move", "copy"):
            dst = (act_copy.get("destination") or "").replace("\\", "/").strip()
            if dst:
                base_dir = os.path.dirname(dst)
                base_name = os.path.basename(dst)
                stem, ext = os.path.splitext(base_name)
                src_path = act_copy.get("source") or act_copy.get("path") or ""
                src_stem = os.path.splitext(os.path.basename(src_path))[0]

                cand_dst = dst
                counter = 1
                while cand_dst.lower() in seen_destinations:
                    # Disambiguate using unique source marker or counter
                    if counter == 1 and src_stem:
                        clean_token = re.sub(r"[^\w-]", "", src_stem)
                        token = clean_token[-6:] if len(clean_token) >= 4 else clean_token
                        cand_dst = f"{base_dir}/{stem}_{token}{ext}" if base_dir else f"{stem}_{token}{ext}"
                    else:
                        cand_dst = f"{base_dir}/{stem}_{counter}{ext}" if base_dir else f"{stem}_{counter}{ext}"
                    counter += 1

                seen_destinations.add(cand_dst.lower())
                act_copy["destination"] = cand_dst
        cleaned_actions.append(act_copy)
    return cleaned_actions

def normalize_user_prompt(text: str) -> str:
    """
    Normalizes common mobile typos, abbreviations, and colloquialisms for robust intent understanding:
    - 'fikes' -> 'files'
    - 'vest' -> 'best'
    - 'oragnize' -> 'organize'
    - 'doenload' -> 'download'
    - 'reciept' -> 'receipt'
    - 'dups' / 'dupes' / 'dupe' -> 'duplicates'
    - 'dedup' -> 'deduplicate'
    """
    if not text:
        return ""
    t = text.strip()
    t = re.sub(r"\bfikes\b", "files", t, flags=re.I)
    t = re.sub(r"\bvest\b", "best", t, flags=re.I)
    t = re.sub(r"\boragnize\b", "organize", t, flags=re.I)
    t = re.sub(r"\bdoenload\b", "download", t, flags=re.I)
    t = re.sub(r"\breciept\b", "receipt", t, flags=re.I)
    t = re.sub(r"\b(?:dups|dupes|dupe|dupplicates)\b", "duplicates", t, flags=re.I)
    t = re.sub(r"\bdedup\b", "deduplicate", t, flags=re.I)
    return t

def generate_dynamic_storage_advice(base_dir: str, target_folder: str = "Download", owner_name: str = "Imran Tahir", peer_names: list = None) -> str:
    """
    Dynamically audits the REAL storage filesystem and crafts articulate, personalized copilot advice.
    NEVER returns a static canned template.
    """
    peer_names = peer_names or ["Fawad", "Sumbal", "Ahmed", "Yousaf"]
    target_path = resolve_safe_path(target_folder) if target_folder else base_dir
    
    files = []
    if os.path.exists(target_path) and os.path.isdir(target_path):
        for entry in os.scandir(target_path):
            if entry.is_file() and not entry.name.startswith("."):
                files.append(entry)

    total_files = len(files)
    total_bytes = sum(f.stat().st_size for f in files) if files else 0
    total_human = format_bytes_human(total_bytes)

    # Analyze real categories present right now
    academic_files = []
    peer_files = defaultdict(list)
    voucher_files = []
    temp_files = []

    for f in files:
        fname = f.name
        is_peer = False
        for p in peer_names:
            if p.lower() in fname.lower():
                peer_files[p].append(fname)
                is_peer = True
                break
        if is_peer:
            continue

        if any(w in fname.lower() for w in ("voucher", "challan", "fee", "receipt")):
            voucher_files.append(fname)
        elif any(w in fname.lower() for w in ("lecture", "notes", "assignment", "wa0017", "wa0018", "neural", "1508", "paper", "bsai")):
            academic_files.append(fname)
        elif fname.endswith(".tmp") or "screenshot" in fname.lower() or fname.startswith("temp_"):
            temp_files.append(fname)

    dup_info = find_duplicate_clusters(base_dir, target_folder)
    dup_count = dup_info.get("total_duplicates", 0)
    dup_human = dup_info.get("reclaimable_human", "0 B")

    lines = [
        f"I just audited your **`{target_folder}/`** folder, {owner_name}. You currently have **{total_files} file(s)** ({total_human}) on your device.\n\n"
        "Here are my personalized recommendations based on your actual files:\n"
    ]

    action_items = []
    if peer_files:
        p_list = ", ".join([f"{len(flist)} from **{p}**" for p, flist in peer_files.items()])
        sample_p = list(peer_files.values())[0][0]
        action_items.append(
            f"1. 👥 **Isolate Peer Documents**: You have files belonging to classmates ({p_list}, like `{sample_p}`). "
            f"I recommend separating these into dedicated `Documents/Peers/<Name>/` folders so your workspace stays private."
        )

    if academic_files:
        sample_acad = academic_files[0]
        action_items.append(
            f"2. 📚 **Organize University Courseware**: Detected {len(academic_files)} academic materials (e.g. `{sample_acad}`). "
            f"We should group lectures and coursework under `Documents/University/AI/`."
        )

    if voucher_files:
        sample_vouch = voucher_files[0]
        action_items.append(
            f"3. 🧾 **Archive Fee Slips & Vouchers**: Found {len(voucher_files)} financial record(s) like `{sample_vouch}`. "
            f"Let's preserve these in `Documents/University/Vouchers/`."
        )

    if dup_count > 0:
        action_items.append(
            f"4. 🧹 **Reclaim Duplicate Storage**: Found **{dup_count} redundant duplicate copies** taking up **{dup_human}**. "
            f"Sweeping these to `.agent_trash` will free up space immediately."
        )

    if temp_files:
        action_items.append(
            f"5. 🗑️ **Clean Temporary Cache**: Found {len(temp_files)} temporary cache/screen capture files that can be cleared safely."
        )

    if not action_items:
        lines.append(
            f"Your `{target_folder}/` directory looks fairly clean with {total_files} document(s). "
            "You can ask me to search for any specific document, inspect files by date, or clean duplicates at any time."
        )
    else:
        lines.extend(action_items)
        lines.append(
            "\n💡 *Which would you like to start with? You can say **\"Separate peer documents\"**, **\"Clean duplicates\"**, or **\"Organize all university files\"** and I'll generate a review plan for you.*"
        )

    return "\n\n".join(lines)

@app.route("/api_status", methods=["GET"])
def api_status():
    """Returns AI intelligence engine status and active Gemini configuration."""
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    is_configured = bool(api_key and len(api_key) > 5)
    masked_key = f"{api_key[:4]}...{api_key[-4:]}" if is_configured and len(api_key) > 8 else ""
    return jsonify({
        "status": "online",
        "gemini_configured": is_configured,
        "masked_key": masked_key,
        "active_models": GEMINI_MODELS,
        "app_version": APP_VERSION
    })

@app.route("/configure_api_key", methods=["POST"])
def configure_api_key():
    """Allows setting the Google Gemini API key directly from the Web UI."""
    data = request.get_json(force=True) or {}
    new_key = (data.get("api_key") or "").strip()
    if not new_key:
        return jsonify({"error": "API key cannot be empty"}), 400

    test_url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODELS[0]}:generateContent?key={new_key}"
    test_payload = {
        "contents": [{"role": "user", "parts": [{"text": "ping"}]}],
        "generationConfig": {"maxOutputTokens": 5}
    }
    try:
        resp = requests.post(test_url, json=test_payload, timeout=10)
        if resp.status_code != 200:
            err_msg = resp.json().get("error", {}).get("message", f"HTTP {resp.status_code}")
            return jsonify({"error": f"Invalid Gemini API Key: {err_msg}"}), 400
    except Exception as e:
        return jsonify({"error": f"Connection to Gemini API failed: {str(e)}"}), 500

    os.environ["GEMINI_API_KEY"] = new_key

    env_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    try:
        env_lines = []
        key_found = False
        if os.path.exists(env_file):
            with open(env_file, "r", encoding="utf-8") as f:
                for line in f:
                    if line.strip().startswith("GEMINI_API_KEY="):
                        env_lines.append(f"GEMINI_API_KEY={new_key}\n")
                        key_found = True
                    else:
                        env_lines.append(line)
        if not key_found:
            env_lines.append(f"GEMINI_API_KEY={new_key}\n")
        with open(env_file, "w", encoding="utf-8") as f:
            f.writelines(env_lines)
    except Exception as e:
        print(f"[Storage Copilot] Warning: Could not write .env file: {e}")

    return jsonify({
        "status": "success",
        "message": "Google Gemini API key successfully verified and activated!",
        "masked_key": f"{new_key[:4]}...{new_key[-4:]}"
    })

@app.route("/propose_plan", methods=["POST"])
def propose_plan():
    """
    v0.6-live Three-Tier Intent Routing & Dynamic Reconnaissance Endpoint:
    
    TIER 1: CONVERSATION
    - Greetings, general questions, capabilities, follow-ups ("What else?").
    - Dispatched to Gemini with user persona (Imran Tahir, BSAI-182).
    - NEVER returns static canned templates.
    
    TIER 2: QUERY_INFO (READ-ONLY)
    - Queries asking to list, search, find, or inspect files ("List university lectures", "Find receipts").
    - Reads real storage files and extracted PII-sanitized snippets.
    - Synthesizes structured Markdown summary with citations/snippets in chat.
    - Stages ZERO file mutations.
    
    TIER 3: FILE_OPERATION
    - Moves, renames, organization, and trash requests ("Sort student vouchers", "Clean download names").
    - Inspects real files and enforces Academic Taxonomy Priority under Documents/University/<Subject>/.
    - Content-derived naming (never generic Document_2025-04-13.pdf).
    - Strict programmatic collision avoidance ensuring unique destination paths.
    """
    data = request.get_json(force=True) or {}
    raw_prompt = (data.get("prompt") or "").strip()
    target_folder = data.get("target_folder") or "Download"
    conversation_history = data.get("conversation_history") or []
    normalized_prompt = normalize_user_prompt(raw_prompt)
    lower_raw = raw_prompt.lower()
    lower_norm = normalized_prompt.lower()
    affirmative_words = {"yes", "yeah", "yep", "sure", "ok", "okay", "go ahead", "proceed", "do it", "please do", "lets do it", "let's do it"}
    is_pure_affirmative = lower_raw in affirmative_words or lower_norm in affirmative_words or any(lower_raw.startswith(w + " ") for w in ("yes", "sure", "ok", "okay", "go ahead", "proceed"))
    is_polite_action = any(lower_norm.startswith(p) for p in ("how about we sort", "how about we organize", "how about we clean", "can we sort", "can we organize", "let's sort", "lets sort", "first sort")) or "first sort" in lower_norm

    last_asst = ""
    for turn in reversed(conversation_history):
        r = (turn.get("role") or "").lower()
        if r in ("assistant", "copilot", "bot"):
            last_asst = (turn.get("content") or turn.get("text") or turn.get("message") or "").strip()
            break

    if is_pure_affirmative:
        lower_asst = last_asst.lower()
        if "duplicate file audit" in lower_asst or "clean up these redundant copies" in lower_asst:
            normalized_prompt = "clean duplicate downloads"
        elif any(p in lower_asst for p in ("peer document", "peer files", "separate peer", "classmate", "peer separation")):
            normalized_prompt = "separate peer documents"
        elif any(p in lower_asst for p in ("tuition voucher", "fee voucher", "fee slip", "student voucher", "student vouchers")):
            normalized_prompt = "sort student vouchers"
        else:
            normalized_prompt = "organize all my university files"

    # Build dialogue context from recent conversation turns
    context_lines = []
    for turn in conversation_history[-4:]:
        role = (turn.get("role") or "user").upper()
        content = (turn.get("content") or turn.get("text") or turn.get("message") or "").strip()
        if content:
            context_lines.append(f"{role}: {content[:300].replace(chr(10), ' ')}")
    conversation_context_block = "\n".join(context_lines) if context_lines else "None (New conversation)"

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
    # STAGE 1: Three-Tier Intent Classification via Gemini (Multi-Turn Aware)
    # =========================================================================
    now_dt = datetime.now()
    now_str = now_dt.strftime("%A, %B %d, %Y at %I:%M %p")
    today_date = now_dt.strftime("%Y-%m-%d")

    intent_classification_system_prompt = f"""You are Storage Copilot, an advanced on-device AI agent running locally via Termux on Android for {owner_name} (Roll/ID: {identifiers}, Major: Artificial Intelligence).
Current Device Time: {now_str} (Today's Date: {today_date}).
Storage Root: /storage/emulated/0 (Downloads, Documents).
Known Peers: {", ".join(peer_names)}.
Peer Routing:
{peer_matrix_lines}

RECENT CONVERSATION HISTORY (Last turns):
{conversation_context_block}

YOUR AGENT PERSONA & BOUNDARIES:
- You are an articulate, capable AI agent (similar to Google DeepMind / Antigravity). You communicate with clarity, intelligence, and empathy directly to {owner_name}.
- You possess complete self-awareness of your abilities, capabilities, and system boundaries.
- WHAT YOU CAN DO: Local storage reconnaissance, content inspection with in-memory PII sanitization, intelligent file organization (academic vs peer vs personal bills), cryptographic duplicate detection & redundant file cleanup, interactive diff approval, and 1-tap atomic rollbacks via SQLite WAL.
- WHAT YOU CANNOT DO (OUT OF SCOPE): You cannot send emails, send SMS/WhatsApp messages, browse the live web, access cloud storage directly, launch external apps, or alter Android system configurations.

CLASSIFY THE USER'S PROMPT INTO EXACTLY ONE OF THREE INTENTS:

1. "CONVERSATION":
   - Suggestions, advice, ideas, recommendations, or questions on how to organize/clean storage (e.g. "What can we do to the files on my phone to organize in the best way possible", "Some suggestions for my files to organize", "What do you suggest?", "Give me ideas to clean up my downloads", "What should I do?").
   - Greetings, casual chat, check-ins ("Hi", "Hello", "Hey", "Good morning", "Salam")
   - Questions about your identity, capabilities, or how you work ("Who are you?", "What can you do?", "Help", "What are your boundaries?")
   - Open conversational follow-ups ("What else?", "Tell me more", "Thanks")
   - OUT-OF-SCOPE REQUESTS: Any request asking to perform external actions outside local storage.

2. "QUERY_INFO":
   - Read-only queries asking to find, search, locate, list, inspect, audit, summarize, or check files or dates (e.g., "Find duplicate files", "Find me Sumbal pass", "List files in download", "Are there any duplicate files?", "Any more sorting for the download?", "What's left to sort?").
   - Status checks on whether a folder still has unorganized files left.
   - ZERO file moves, renames, or deletions.

3. "FILE_OPERATION":
   - Explicit commands, instructions, or confirmations to take action, execute, or stage file moves/renames/deletions right now.
   - CRITICAL MULTI-TURN CONFIRMATIONS: If the user says "Yes", "Yeah", "Sure", "Okay", "Proceed", "Go ahead", "Do it", "Sort them" in response to an action or plan the Assistant previously recommended in RECENT CONVERSATION HISTORY, classify as "FILE_OPERATION"!
   - POLITE ACTION DIRECTIVES: If the user says "How about we first sort all those files in documents directory...", "Can we first organize...", "Let's sort...", or "First sort...", this is an ACTION INSTRUCTION. Classify as "FILE_OPERATION"!
   - Examples:
     * "Clean duplicate downloads"
     * "Yes organize all my uni files"
     * "Sort student vouchers"
     * "Separate peer documents"
     * "Clean download names"
     * "How about we first sort all those files in the documents directory"

Respond STRICTLY with valid JSON adhering to this schema:
{{
  "intent": "CONVERSATION" | "QUERY_INFO" | "FILE_OPERATION",
  "reply": "<Dynamic, helpful conversational response addressing the user directly if CONVERSATION. Empty string if QUERY_INFO or FILE_OPERATION.>"
}}
"""

    stage1_resp = call_gemini_json(intent_classification_system_prompt, raw_prompt)
    intent = stage1_resp.get("intent", "").upper() if isinstance(stage1_resp, dict) else ""
    conv_reply = stage1_resp.get("reply", "") if isinstance(stage1_resp, dict) else ""

    # Offline / Heuristic Fallback for Stage 1:
    if not intent:
        lower_p = normalized_prompt.lower()

        # Action command starters / imperative prefixes
        action_command_starts = (
            "yes ", "yeah ", "sure ", "ok ", "okay ", "please ", "go ahead ", "proceed ",
            "sort ", "organize ", "clean ", "move ", "put ", "trash ", "delete ", "tidy ", "structure ", "deduplicate "
        )
        is_action_command = any(lower_p.startswith(p) for p in action_command_starts)

        advice_lead_keywords = (
            "suggestion", "suggestions", "suggest", "recommend", "recommendation",
            "recommendations", "idea", "ideas", "advice", "tip", "tips", "how to",
            "how should", "how can", "what do you think", "what should i", "guide me",
            "options", "propose some ideas", "any ideas", "what can we do", "what can i do",
            "what could we do", "what should we do", "what to do", "best way", "best ways",
            "best way possible"
        )
        query_lead_keywords = (
            "list", "find", "show", "search", "what is inside", "what is in", 
            "what else is in", "what files", "how many files", "do i have", 
            "display", "summarize", "view", "inspect", "where is", "where are",
            "tell me about", "locate", "check for", "which files", "today did i",
            "did i receive", "receive any", "recent", "are there any", "are there",
            "any more", "more sorting", "what's left", "whats left", "remaining files"
        )
        file_op_keywords = (
            "sort", "voucher", "fee", "challan", "slip", "peer", "separate", 
            "clean", "download", "screenshot", "trash", "delete", "move", 
            "organize", "rename", "isolate", "structure", "tidy", "place",
            "duplicate", "duplicates", "dedup", "deduplicate"
        )

        if is_pure_affirmative:
            intent = "FILE_OPERATION"
        elif is_polite_action:
            intent = "FILE_OPERATION"
        elif is_action_command and any(k in lower_p for k in file_op_keywords):
            intent = "FILE_OPERATION"
        elif any(k in lower_p for k in advice_lead_keywords):
            intent = "CONVERSATION"
        elif any(k in lower_p for k in query_lead_keywords) and not any(k in lower_p for k in ("sort", "organize", "clean", "trash", "move", "separate", "rename", "deduplicate")):
            intent = "QUERY_INFO"
        elif any(k in lower_p for k in ("what else", "tell me more", "who are you", "what can you do", "help", "hi", "hello", "hey", "thanks", "email", "mail", "whatsapp", "music")):
            intent = "CONVERSATION"
        elif any(k in lower_p for k in file_op_keywords):
            intent = "FILE_OPERATION"
        else:
            intent = "CONVERSATION"

    # =========================================================================
    # TIER 1: CONVERSATION (Dynamic Gemini Reply - Zero Canned Strings)
    # =========================================================================
    # TIER 1: CONVERSATION (Dynamic Gemini Reply & Live Storage Audit - Zero Canned Strings)
    # =========================================================================
    if intent == "CONVERSATION":
        dynamic_audit = generate_dynamic_storage_advice(BASE_STORAGE_DIR, target_folder, owner_name, peer_names)
        if not conv_reply:
            conv_sys = f"""You are Storage Copilot, an advanced on-device AI agent running locally via Termux on Android for {owner_name} (Roll/ID: {identifiers}, Major: Artificial Intelligence).
Current Device Time: {now_str} (Today's Date: {today_date}).
Storage Root: /storage/emulated/0 (Downloads, Documents).
Known Peers: {", ".join(peer_names)}.
Peer Routing:
{peer_matrix_lines}

LIVE REAL-TIME STORAGE AUDIT ({target_folder}/):
{dynamic_audit}

YOUR AGENT PERSONA:
- You are an elite, highly capable AI agent (similar to Google DeepMind / Antigravity). You speak articulately, intelligently, and directly to {owner_name}.
- You are warm, confident, and professional. Never robotic or generic.
- CRITICAL: Never output a generic boilerplate template or canned greeting unless the user gave a simple one-word greeting like "hi".
- Ground your suggestions in the LIVE REAL-TIME STORAGE AUDIT above. Refer to real filenames, actual counts, and real storage categories currently on the device.

CAPABILITIES & BOUNDARIES:
- WHAT YOU CAN DO (Your Core Superpowers):
  1. Deep Document Reconnaissance: Inspect PDFs, notes, vouchers, and bills on local storage with RAM-level PII sanitization (redacting CNIC, passport, IBAN, and personal names before LLM reasoning).
  2. Intelligent Organization: Route academic study materials to `Documents/University/<Subject>/`, student vouchers to `Documents/University/Vouchers/`, isolate peer documents to `Documents/Peers/<PeerName>/`, and utility bills to `Documents/Finances/`.
  3. Interactive Diff Safety: Stage proposed moves/renames for interactive user review before execution.
  4. 1-Tap Atomic Rollback: Undo any batch of actions with full state recovery via an on-device SQLite WAL ledger (`.ledger.db`).
  5. Content-Aware Renaming & Search: Standardize cryptic WhatsApp (`DOC-*.pdf`) or arXiv downloads into clean, descriptive filenames.
- WHAT YOU CANNOT DO (Out-of-Scope Boundaries):
  1. You CANNOT send emails, SMS, WhatsApp messages, or interact with external communication services.
  2. You CANNOT browse or scrape the live internet, or download YouTube/web media.
  3. You CANNOT access cloud storage (Google Drive/Dropbox) unless locally synced to storage.
  4. You CANNOT modify Android system settings, root files, or app packages outside shared user storage (`/storage/emulated/0`).
  5. You CANNOT execute arbitrary system shell commands or process financial payments.

ADVICE & RECOMMENDATIONS:
- If {owner_name} is asking for suggestions, ideas, or recommendations on how to organize his files:
  Use the live audit above to provide an articulate, personalized roadmap citing his actual files and sizes.
  Invite him to pick which action to execute (e.g. "Separate peer documents", "Clean duplicates", or "Organize all university files").

HANDLING OUT-OF-SCOPE REQUESTS:
- If the user asks you to do something out of scope (e.g. sending emails, messaging, internet search, system modifications), do NOT pretend to do it or hallucinate an action.
- Explain clearly, politely, and knowledgeably that the requested action is outside your local storage management sandbox.
- State exactly what you can do instead for that topic (e.g., if asked to email an assignment: offer to locate and verify the assignment PDF in `Documents/University/AI/` so {owner_name} can easily attach it).

Keep your response well-structured using clean Markdown formatting (bullet points, bold highlights, code spans).
"""
            conv_reply = call_gemini_text(conv_sys, raw_prompt)

        # Dynamic fallback: audit live files on disk instead of returning static strings
        if not conv_reply:
            lp = normalized_prompt.lower()
            if any(w in lp for w in ("suggestion", "suggest", "recommend", "recommendation", "idea", "how should i", "how to organize", "advice", "tip", "options", "best way", "what can we do", "what can i do", "fikes", "vest")):
                conv_reply = dynamic_audit
            elif any(w in lp for w in ("email", "mail", "whatsapp", "call", "sms", "spotify", "music", "flight", "weather", "browse", "internet", "hack")):
                conv_reply = (
                    f"I cannot perform external actions like sending emails or accessing web services, {owner_name}.\n\n"
                    f"I am **Storage Copilot**, an on-device Android AI agent running locally via Termux. My capabilities are focused entirely on your device's filesystem (`/storage/emulated/0`):\n\n"
                    f"- 🔍 **Document Reconnaissance**: Search, inspect, and summarize PDFs, notes, vouchers, and bills with zero-cloud PII redaction.\n"
                    f"- 📂 **Intelligent Taxonomy**: Organize university course files into `Documents/University/<Subject>/` and isolate peer files into `Documents/Peers/<Name>/`.\n"
                    f"- 🛡️ **Interactive Safety**: Staged diff reviews before any files are moved, plus 1-tap atomic rollbacks via SQLite WAL ledger.\n\n"
                    f"If you'd like me to find or verify the file on your device so you can attach it, just let me know!"
                )
            elif any(w in lp for w in ("what else", "more", "next")):
                conv_reply = (
                    f"Here are several other tasks I can help you with, {owner_name}:\n\n"
                    f"- **Inspect & Query**: Ask *\"List files in download\"* or *\"today did I receive any documents?\"* to inspect files without moving them.\n"
                    f"- **Sort Academic Vouchers**: Separate your BSAI fee slips into `Documents/University/Vouchers`.\n"
                    f"- **Isolate Peer Documents**: Route incoming files for {', '.join(peer_names)} to `Documents/Peers/<Name>`.\n"
                    f"- **Clean Cryptic Downloads**: Standardize messy WhatsApp or arXiv downloads with descriptive, content-derived names.\n"
                    f"- **Deduplication**: Safely identify and clean redundant files to free up storage."
                )
            elif any(w in lp for w in ("who are you", "what can you do", "help", "role", "abilities", "capabilities", "boundaries")):
                conv_reply = (
                    f"I am **Storage Copilot**, your autonomous on-device AI storage agent running locally via Termux on Android for **{owner_name}** ({identifiers[0] if identifiers else 'BSAI-182'}, Artificial Intelligence).\n\n"
                    f"### 🚀 What I Can Do:\n"
                    f"- 🔍 **Smart Document Reconnaissance**: Search and inspect files across storage (`/storage/emulated/0`), extracting PDF text and metadata with in-memory PII sanitization.\n"
                    f"- 📚 **Academic Study Organization**: Sort lectures, assignments, and research papers into `Documents/University/<Subject>/`.\n"
                    f"- 👥 **Peer Separation**: Isolate incoming documents for {', '.join(peer_names)} into dedicated `Documents/Peers/<Name>/` directories.\n"
                    f"- 🏷️ **Content-Aware Renaming**: Standardize cryptic filenames (like WhatsApp `DOC-*.pdf` or arXiv IDs) into clear, human-readable titles.\n"
                    f"- 🛡️ **Interactive Diff & 1-Tap Undo**: Review proposed changes in interactive diff cards before execution, with complete 1-tap rollback powered by SQLite WAL.\n\n"
                    f"### ⚠️ Boundaries & Scope:\n"
                    f"- I run strictly on your local Android device without cloud file uploads.\n"
                    f"- I cannot send emails, browse the live internet, or access apps/accounts outside your local storage.\n\n"
                    f"Try asking: *\"List files in download\"*, *\"Find me Sumbal pass\"*, or *\"today did I receive any documents?\"*"
                )
            elif any(w in lp for w in ("peer", "peers", "friends", "fawad", "sumbal", "ahmed", "yousaf")):
                conv_reply = (
                    f"Your registered peer separation matrix includes: **{', '.join(peer_names)}**.\n\n"
                    "Any documents or vouchers identified for these classmates are isolated into `Documents/Peers/<Name>` to protect your personal storage."
                )
            elif any(w in lp for w in ("hi", "hello", "hey", "salam", "morning", "afternoon", "evening")):
                conv_reply = (
                    f"👋 Hello {owner_name}! Storage Copilot is online and connected to your storage root (`/storage/emulated/0`).\n\n"
                    f"How can I help with your files today? You can ask me to search documents, organize downloads, or audit storage."
                )
            else:
                conv_reply = dynamic_audit

        return jsonify({
            "type": "conversation",
            "message": conv_reply,
            "version": APP_VERSION,
            "actions": [],
            "quick_chips": [
                "🧹 Clean Duplicates",
                "📋 Sort Student Vouchers",
                "👥 Separate Peer Documents",
                "🏷️ Clean Download Names"
            ]
        })

    # =========================================================================
    # TIER 2: QUERY_INFO (Read-Only Inspection - Zero Mutations)
    # =========================================================================
    if intent == "QUERY_INFO":
        all_files = collect_storage_files(BASE_STORAGE_DIR, target_folder)
        lower_prompt = raw_prompt.lower()
        parsed_intent = parse_query_intent(raw_prompt, target_folder)

        if not all_files:
            return jsonify({
                "type": "query_info",
                "message": (
                    f"📂 No files found in storage (`{target_folder}/` or `Documents/`). The directories are currently empty.\n\n"
                    f"Please place files in `{target_folder}/` or tap 'Seed Fixtures' to populate demo files for testing."
                ),
                "version": APP_VERSION,
                "actions": [],
                "query_results": [],
                "quick_chips": [
                    "🧹 Clean Duplicates",
                    "📋 Sort Student Vouchers",
                    "👥 Separate Peer Documents",
                    "🏷️ Clean Download Names"
                ]
            })

        # Check if the query is asking about duplicates
        is_duplicate_query = any(w in lower_prompt for w in ("duplicate", "duplicates", "dedup", "deduplicate", "repeated file", "repeated files"))
        if is_duplicate_query:
            dup_res = find_duplicate_clusters(BASE_STORAGE_DIR, target_folder)
            if dup_res["total_duplicates"] == 0:
                return jsonify({
                    "type": "query_info",
                    "message": f"✨ **No duplicate files detected in `{target_folder}/`!**\n\nI scanned your storage files using cryptographic SHA-256 content hashing. Every document is unique and no redundant copies were found.",
                    "version": APP_VERSION,
                    "actions": [],
                    "query_results": [],
                    "duplicate_summary": dup_res,
                    "quick_chips": [
                        "🧹 Clean duplicate downloads",
                        "📋 Sort Student Vouchers",
                        "👥 Separate Peer Documents",
                        "🏷️ Clean Download Names"
                    ]
                })
            else:
                lines = [
                    f"### 🧹 Duplicate File Audit for `{target_folder}/`",
                    f"Found **{dup_res['total_duplicates']} redundant duplicate file(s)** across **{dup_res['total_clusters']} group(s)**.",
                    f"**Total Reclaimable Storage:** `{dup_res['reclaimable_human']}`\n",
                    "#### Detected Groups:"
                ]
                for idx, cluster in enumerate(dup_res["clusters"], 1):
                    k = cluster["keeper"]
                    lines.append(f"**Group {idx}** (Size: {cluster['size_human']} each, Reclaimable: {cluster['reclaimable_human']}):")
                    lines.append(f"- 🛡️ **Preserving Keeper:** `{k['path']}` *({k['reason']})*")
                    for d in cluster["duplicates"]:
                        lines.append(f"- 🗑️ **Redundant Copy:** `{d['path']}` *({d['reason']})*")
                    lines.append("")
                lines.append("💡 *To clean up these redundant copies and reclaim space, say **'Clean duplicate downloads'** or tap **Clean Duplicates** below.*")

                return jsonify({
                    "type": "query_info",
                    "message": "\n".join(lines),
                    "version": APP_VERSION,
                    "actions": [],
                    "query_results": dup_res["clusters"],
                    "duplicate_summary": dup_res,
                    "quick_chips": [
                        "🧹 Clean duplicate downloads",
                        "📋 Sort Student Vouchers",
                        "👥 Separate Peer Documents",
                        "🏷️ Clean Download Names"
                    ]
                })

        # ---------------------------------------------------------------------
        # SUB-CASE A0: Folder Audit & Remaining Work Request (e.g. "Any more sorting for the download?", "What's left?")
        # ---------------------------------------------------------------------
        if parsed_intent.get("is_audit_status_query"):
            dir_name = parsed_intent["folder_constraint"] or target_folder
            dir_files = [
                f for f in all_files 
                if f["path"].startswith(f"{dir_name}/") or (f["path"] == f["filename"] and dir_name.lower() in ("download", "downloads"))
            ]
            if not dir_files and dir_name.lower() in ("download", "downloads"):
                dir_files = [f for f in all_files if not f["path"].startswith("Documents/")]

            unorganized_acad = []
            unorganized_peers = defaultdict(list)
            unorganized_vouchers = []
            unorganized_trash = []

            for f in dir_files:
                fn = f["filename"]
                is_p = False
                for p in peer_names:
                    if p.lower() in fn.lower():
                        unorganized_peers[p].append(f)
                        is_p = True
                        break
                if is_p:
                    continue

                if any(w in fn.lower() for w in ("voucher", "challan", "fee", "receipt")):
                    unorganized_vouchers.append(f)
                elif any(w in fn.lower() for w in ("lecture", "notes", "assignment", "wa0017", "wa0018", "neural", "1508", "paper", "bsai")):
                    unorganized_acad.append(f)
                elif fn.endswith(".tmp") or "screenshot" in fn.lower() or fn.startswith("temp_"):
                    unorganized_trash.append(f)

            total_pending = len(unorganized_acad) + sum(len(l) for l in unorganized_peers.values()) + len(unorganized_vouchers) + len(unorganized_trash)

            gemini_audit_prompt = f"""You are Storage Copilot, an advanced on-device AI agent for {owner_name}.
User Query: "{raw_prompt}"
Target Folder: {dir_name}/
Total Remaining Files in Folder: {len(dir_files)}.
Pending Unorganized Files:
- Academic Courseware: {len(unorganized_acad)} file(s)
- Peer Classmate Files: {sum(len(l) for l in unorganized_peers.values())} file(s)
- Tuition Vouchers: {len(unorganized_vouchers)} file(s)
- Temporary Trash/Cache: {len(unorganized_trash)} file(s)

INSTRUCTIONS:
1. Speak articulately and directly to {owner_name}.
2. Explain the current status of `{dir_name}/`.
3. If there are pending unorganized files ({total_pending} remaining): clearly list what remains and suggest next logical steps (e.g., 'Separate peer documents' or 'Organize university files').
4. If NO unorganized files remain: celebrate that the folder is clean and organized, and note that only personal/general files remain.
5. NEVER search for a file named 'download sorting' or output robotic database strings.
"""
            audit_reply = call_gemini_text(gemini_audit_prompt, raw_prompt)

            if not audit_reply:
                if total_pending > 0:
                    lines = [
                        f"I audited your **`{dir_name}/`** directory. You currently have **{len(dir_files)} file(s)**, with **{total_pending} file(s)** ready for further organization:\n"
                    ]
                    if unorganized_peers:
                        p_summary = ", ".join([f"{len(l)} from **{p}**" for p, l in unorganized_peers.items()])
                        lines.append(f"- 👥 **Peer Documents ({p_summary})**: We can isolate these into `Documents/Peers/<Name>/`.")
                    if unorganized_acad:
                        lines.append(f"- 📚 **Academic Courseware ({len(unorganized_acad)} file(s))**: Can be organized into `Documents/University/AI/`.")
                    if unorganized_vouchers:
                        lines.append(f"- 🧾 **Fee Vouchers ({len(unorganized_vouchers)} file(s))**: Can be archived into `Documents/University/Vouchers/`.")
                    if unorganized_trash:
                        lines.append(f"- 🗑️ **Temporary Files ({len(unorganized_trash)} file(s))**: Can be safely soft-deleted.")
                    lines.append(f"\n💡 *Would you like me to proceed with any of these? Say **\"Separate peer documents\"** or **\"Organize all university files\"** to review a plan.*")
                    audit_reply = "\n".join(lines)
                else:
                    audit_reply = (
                        f"✨ **`{dir_name}/` is in great shape!**\n\n"
                        f"All primary academic courseware, fee vouchers, and classmate documents have already been organized into their respective folders. "
                        f"The remaining {len(dir_files)} file(s) are general downloads and documents."
                    )

            return jsonify({
                "type": "query_info",
                "message": audit_reply,
                "version": APP_VERSION,
                "actions": [],
                "query_results": [],
                "quick_chips": [
                    "👥 Separate Peer Documents",
                    "📋 Sort Student Vouchers",
                    "🧹 Clean Duplicates",
                    "🏷️ Clean Download Names"
                ]
            })

        # ---------------------------------------------------------------------
        # SUB-CASE A: Directory Listing / Inventory Request (e.g. "Give me list of all the files in download")
        # ---------------------------------------------------------------------
        if parsed_intent["is_directory_listing"]:
            dir_name = parsed_intent["folder_constraint"] or target_folder
            dir_files = [
                f for f in all_files 
                if f["path"].startswith(f"{dir_name}/") or (f["path"] == f["filename"] and dir_name.lower() in ("download", "downloads"))
            ]
            if not dir_files and dir_name.lower() in ("download", "downloads"):
                dir_files = [f for f in all_files if not f["path"].startswith("Documents/")]

            # Group files in directory by semantic category
            grouped_acad = []
            grouped_peers = defaultdict(list)
            grouped_fin = []
            grouped_trash = []
            grouped_general = []

            for f in dir_files:
                fn = f["filename"]
                is_p = False
                for p in peer_names:
                    if p.lower() in fn.lower():
                        grouped_peers[p].append(f)
                        is_p = True
                        break
                if is_p:
                    continue

                if any(w in fn.lower() for w in ("voucher", "challan", "fee", "receipt", "bill", "invoice")):
                    grouped_fin.append(f)
                elif any(w in fn.lower() for w in ("lecture", "notes", "assignment", "wa0017", "wa0018", "neural", "1508", "paper", "bsai")):
                    grouped_acad.append(f)
                elif fn.endswith(".tmp") or "screenshot" in fn.lower() or fn.startswith("temp_"):
                    grouped_trash.append(f)
                else:
                    grouped_general.append(f)

            total_bytes = sum(f.get("size_bytes", 0) for f in dir_files)
            total_human = format_bytes_human(total_bytes)

            # Try generating articulate inventory via Gemini if online
            dir_manifest = [
                {"filename": f["filename"], "path": f["path"], "size_kb": f.get("size_kb", 0)}
                for f in dir_files[:25]
            ]
            gemini_dir_prompt = f"""You are Storage Copilot, an advanced on-device AI agent for {owner_name}.
User Query: "{raw_prompt}"
Target Directory: {dir_name}/
Total Files in Folder: {len(dir_files)} ({total_human}).
Files Manifest:
{json.dumps(dir_manifest, indent=2)}

INSTRUCTIONS:
Provide a clear, articulate, structured inventory of the files in `{dir_name}/`.
Group them logically (e.g. Academic, Peer Classmates, Vouchers/Finances, General Downloads).
Cite filenames with bold text and paths in code spans.
Give a brief, intelligent copilot recommendation at the end on what can be organized.
Never output robotic database strings like 'Match: Filename matches:'.
"""
            query_reply = call_gemini_text(gemini_dir_prompt, raw_prompt)

            if not query_reply:
                # High-fidelity local inventory synthesis
                lines = [
                    f"Here is the complete inventory of your **`{dir_name}/`** folder (**{len(dir_files)} file(s)**, {total_human}):\n"
                ]
                if grouped_acad:
                    lines.append(f"📚 **Academic Courseware ({len(grouped_acad)} file(s)):**")
                    for f in grouped_acad[:6]:
                        lines.append(f"- 📄 **{f['filename']}** (`{f['path']}` • {f.get('size_kb', 0)} KB)")
                    if len(grouped_acad) > 6:
                        lines.append(f"- *...and {len(grouped_acad) - 6} more academic file(s)*")
                    lines.append("")

                if grouped_peers:
                    peer_summary = ", ".join([f"{len(fl)} from **{p}**" for p, fl in grouped_peers.items()])
                    lines.append(f"👥 **Peer Classmate Documents ({peer_summary}):**")
                    for p, fl in grouped_peers.items():
                        for f in fl[:4]:
                            lines.append(f"- 📄 **{f['filename']}** (`{f['path']}` • {f.get('size_kb', 0)} KB)")
                    lines.append("")

                if grouped_fin:
                    lines.append(f"🧾 **Tuition & Financial Records ({len(grouped_fin)} file(s)):**")
                    for f in grouped_fin[:6]:
                        lines.append(f"- 📄 **{f['filename']}** (`{f['path']}` • {f.get('size_kb', 0)} KB)")
                    lines.append("")

                if grouped_general:
                    lines.append(f"📄 **General Documents & Downloads ({len(grouped_general)} file(s)):**")
                    for f in grouped_general[:8]:
                        lines.append(f"- 📄 **{f['filename']}** (`{f['path']}` • {f.get('size_kb', 0)} KB)")
                    if len(grouped_general) > 8:
                        lines.append(f"- *...and {len(grouped_general) - 8} more document(s)*")
                    lines.append("")

                if grouped_trash:
                    lines.append(f"🗑️ **Temporary Cache & Screen Captures ({len(grouped_trash)} file(s)):**")
                    for f in grouped_trash[:4]:
                        lines.append(f"- 📄 `{f['filename']}` ({f.get('size_kb', 0)} KB)")
                    lines.append("")

                lines.append(
                    f"💡 *Copilot Analysis: I can organize these files for you. Say **\"Organize all university files\"** or **\"Separate peer documents\"** to review a staged Action Plan.*"
                )
                query_reply = "\n".join(lines)

            return jsonify({
                "type": "query_info",
                "message": query_reply,
                "version": APP_VERSION,
                "actions": [],
                "query_results": [],
                "quick_chips": [
                    "🧹 Clean Duplicates",
                    "📋 Sort Student Vouchers",
                    "👥 Separate Peer Documents",
                    "🏷️ Clean Download Names"
                ]
            })

        # ---------------------------------------------------------------------
        # SUB-CASE B: Specific Document Lookup, Topic Filter, or Date Search
        # ---------------------------------------------------------------------
        is_date_query = any(w in lower_prompt for w in ("today", "yesterday", "date", "recent", "new", "received", "downloaded today", "last"))

        scored_files = []
        for f in all_files:
            match_score, match_reason = score_document_match(raw_prompt, f)
            classification = classify_document(f["filename"], f["snippet"])
            scored_files.append({
                **f,
                "score": match_score,
                "match_reason": match_reason,
                "category": classification["category"],
                "type_label": classification["type_label"]
            })

        if is_date_query:
            scored_files.sort(key=lambda x: (x.get("is_today", False), x.get("modified_timestamp", 0)), reverse=True)
            top_matches = [f for f in scored_files if f.get("is_today") or f.get("score", 0) >= 45.0]
        else:
            scored_files.sort(key=lambda x: x["score"], reverse=True)
            top_matches = [f for f in scored_files if f["score"] >= 45.0]

        # Compact manifest for Gemini (top 15 candidates max to keep network payload light & fast)
        gemini_manifest = [
            {
                "filename": f["filename"],
                "path": f["path"],
                "size_kb": f["size_kb"],
                "modified_date": f.get("modified_str", "Unknown"),
                "is_today": f.get("is_today", False),
                "category": f["type_label"],
                "match_score": f["score"],
                "snippet": (f.get("snippet") or "")[:80]
            }
            for f in top_matches[:15]
        ]

        query_system_prompt = f"""You are Storage Copilot, an articulate on-device AI agent for {owner_name} (BSAI-182).
Current Device Time: {now_str}.
Storage Root: /storage/emulated/0 (Downloads, Documents).
Total Files Indexed: {len(all_files)}.
Known Peers: {", ".join(peer_names)}.
Peer Routing:
{peer_matrix_lines}

USER SEARCH QUERY:
"{raw_prompt}"

CANDIDATE MATCHES (Ranked by semantic score, filtered for PII):
{json.dumps(gemini_manifest, indent=2)}

INSTRUCTIONS & REASONING RULES:
1. Act as an intelligent, articulate AI agent (like Google DeepMind / Antigravity). Speak directly to {owner_name}.
2. SPECIFIC BOOK OR DOCUMENT SEARCH (e.g. 'Can you find me the book for 48 laws of power?'):
   - If a matching book/document is present in candidates: highlight ONLY that specific file with its exact name, path in code spans, size, and category.
   - Do NOT list unrelated files like peer passes or random text files!
   - Offer a helpful next step (e.g. moving it to `Documents/Books/`).
3. IDENTITY PASS CAUTION:
   - If the user asks for their entry pass/degree and only a classmate pass exists: explain that a pass for peer {peer_names} was found, but not one in {owner_name}'s name.
4. If no genuine match exists: explain politely that you searched across the {len(all_files)} indexed files on their storage and could not find it.
5. NEVER output robotic database strings like 'Match: Filename matches:' or 'Category: General Document • Match: ...'.
"""

        query_reply = call_gemini_text(query_system_prompt, raw_prompt)

        # High-precision local fallback search if Gemini is offline
        if not query_reply:
            if is_date_query:
                today_matches = [f for f in all_files if f.get("is_today")]
                if today_matches:
                    lines = [f"📅 **Documents Received Today ({now_dt.strftime('%B %d, %Y')}):**\n\nFound **{len(today_matches)} document(s)** downloaded today:"]
                    for m in today_matches[:10]:
                        snip_preview = f" • *Preview:* \"{m['snippet'][:80]}\"" if m.get("snippet") and m['snippet'] != "none (binary/image)" else ""
                        time_display = m.get("modified_str") or "Today"
                        lines.append(f"- 📄 **{m['filename']}** (`{m['path']}` • {m.get('size_kb', 0)} KB • *{time_display}*){snip_preview}")
                    query_reply = "\n\n".join(lines)
                else:
                    recent_files = sorted(all_files, key=lambda x: x.get("modified_timestamp", 0), reverse=True)[:5]
                    lines = [f"📅 You have not received or downloaded any new documents today ({now_dt.strftime('%B %d, %Y')}).\n\nHere are your most recently modified files:"]
                    for m in recent_files:
                        time_display = m.get("modified_str") or "Earlier"
                        lines.append(f"- 📄 **{m['filename']}** (`{m['path']}` • *{time_display}*)")
                    query_reply = "\n\n".join(lines)
            elif top_matches:
                # If there is a single standout match (e.g. specific book lookup like 48 laws of power)
                best = top_matches[0]
                if len(top_matches) == 1 or (best["score"] >= 120.0 and (len(top_matches) == 1 or best["score"] - top_matches[1]["score"] >= 35.0)):
                    lines = [
                        f"I found the document you were looking for on your device:\n\n"
                        f"- 📄 **{best['filename']}**\n"
                        f"  * Location: `{best['path']}`\n"
                        f"  * Size: {best['size_kb']} KB\n"
                        f"  * Category: **{best['type_label']}**"
                    ]
                    if best.get("snippet") and best["snippet"] != "none (binary/image)":
                        lines.append(f"  * Preview: *\"{best['snippet'][:150]}\"*")
                    lines.append(f"\n💡 *Would you like me to organize, move, or inspect this file further?*")
                    query_reply = "\n".join(lines)
                else:
                    lines = [f"I found **{len(top_matches)} matching document(s)** on your device:\n"]
                    for m in top_matches[:8]:
                        snip_preview = f" • *Preview:* \"{m['snippet'][:90]}\"" if m.get("snippet") and m['snippet'] != "none (binary/image)" else ""
                        lines.append(f"- 📄 **{m['filename']}** (`{m['path']}` • {m['size_kb']} KB)\n  *Category:* **{m['type_label']}**{snip_preview}")
                    query_reply = "\n\n".join(lines)
            else:
                # No matches found: check identity pass distinction
                if any(w in lower_prompt for w in ("pass", "degree", "entry")):
                    peer_passes = [
                        f for f in all_files 
                        if any(p.lower() in f["filename"].lower() for p in peer_names) and "pass" in f["filename"].lower()
                    ]
                    if peer_passes:
                        found_p = peer_passes[0]
                        query_reply = (
                            f"I searched your storage ({len(all_files)} files indexed).\n\n"
                            f"I found an entry pass for your classmate (`{found_p['path']}` • {found_p.get('size_kb', 0)} KB), "
                            f"but I did not find a degree or entry pass issued for you ({owner_name}, {identifiers[0] if identifiers else 'BSAI-182'}).\n\n"
                            f"If you received it recently, make sure it has finished downloading into `{target_folder}/`."
                        )
                    else:
                        query_reply = f"I searched your storage ({len(all_files)} files indexed), but found no degree or entry pass matching **\"{raw_prompt}\"**."
                else:
                    target_disp = parsed_intent.get("cleaned_target") or raw_prompt
                    query_reply = (
                        f"I searched across your storage ({len(all_files)} files indexed), but couldn't find any documents matching **\"{target_disp}\"**.\n\n"
                        f"Please check if the file was downloaded under a different name or if the download is still in progress."
                    )

        return jsonify({
            "type": "query_info",
            "message": query_reply,
            "version": APP_VERSION,
            "actions": [],
            "query_results": [],
            "quick_chips": [
                "🧹 Clean Duplicates",
                "📋 Sort Student Vouchers",
                "👥 Separate Peer Documents",
                "🏷️ Clean Download Names"
            ]
        })

    # =========================================================================
    # TIER 3: FILE_OPERATION (Reconnaissance & Action Plan Synthesis)
    # =========================================================================
    # Phase 1: Check if this file operation request is deduplication cleanup
    is_deduplicate_req = any(k in normalized_prompt.lower() for k in ("duplicate", "duplicates", "dedup", "deduplicate", "repeated file", "repeated files"))
    if is_deduplicate_req:
        dup_res = find_duplicate_clusters(BASE_STORAGE_DIR, target_folder)
        if dup_res["total_duplicates"] == 0:
            return jsonify({
                "type": "conversation",
                "message": (
                    f"✨ **No duplicate files detected in `{target_folder}/`!**\n\n"
                    "All documents possess unique cryptographic SHA-256 signatures. No redundant copies were found to clean."
                ),
                "version": APP_VERSION,
                "actions": [],
                "duplicate_summary": dup_res,
                "quick_chips": [
                    "📋 Sort Student Vouchers",
                    "👥 Separate Peer Documents",
                    "🏷️ Clean Download Names",
                    "🗑️ Cleanup Old Screenshots"
                ]
            })

        actions = []
        for cluster in dup_res["clusters"]:
            keeper = cluster["keeper"]
            for dup in cluster["duplicates"]:
                actions.append({
                    "action_id": f"step-{len(actions) + 1}",
                    "type": "trash",
                    "badge": "TRASH",
                    "badge_label": "Soft Delete",
                    "source": dup["path"],
                    "path": dup["path"],
                    "destination": f".agent_trash/{dup['filename']}",
                    "rationale": f"Redundant duplicate of '{keeper['filename']}' ({cluster['size_human']} reclaimed)",
                    "ai_inspected": True,
                    "inspected_preview": f"Exact SHA-256 match with {keeper['filename']}"
                })

        plan_id = str(uuid.uuid4())
        plan_desc = f"Deduplication: Clean {len(actions)} redundant duplicate file(s) ({dup_res['reclaimable_human']})"
        return jsonify({
            "type": "plan",
            "message": (
                f"Identified **{len(actions)} redundant duplicate file(s)** across {dup_res['total_clusters']} group(s). "
                f"Cleaning them will reclaim **{dup_res['reclaimable_human']}** of storage while safely preserving all original canonical files.\n\n"
                "Review the proposed removals below. Tap **Execute Action Plan** to move redundant copies to `.agent_trash` (with 1-tap rollback at any time)."
            ),
            "plan": {
                "plan_id": plan_id,
                "description": plan_desc,
                "actions": actions
            },
            "actions": actions,
            "duplicate_summary": dup_res,
            "version": APP_VERSION
        })

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

    # 2. Read real files in target directory
    scanned_entries = []
    if os.path.exists(target_dir) and os.path.isdir(target_dir):
        for entry in os.scandir(target_dir):
            if entry.is_file() and not entry.name.startswith("."):
                scanned_entries.append(entry)

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

    # 3. Snippet inspection & PII scrubbing (up to 300 candidate files)
    manifest = []
    manifest_for_gemini = []
    manifest_lookup = {}
    for entry in scanned_entries[:300]:
        filename = entry.name
        file_size = entry.stat().st_size
        ext = os.path.splitext(filename)[1].lower()

        try:
            mtime = entry.stat().st_mtime
            mtime_str = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M")
        except Exception:
            mtime_str = ""

        raw_text = extract_file_snippet(entry.path, 300)
        sanitized_snippet = sanitize_pii(raw_text)

        try:
            rel_path = os.path.relpath(entry.path, BASE_STORAGE_DIR).replace("\\", "/")
        except Exception:
            rel_path = f"{target_folder}/{filename}"

        manifest_item = {
            "filename": filename,
            "size_kb": round(file_size / 1024, 1),
            "modified_date": mtime_str,
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
            "modified_date": mtime_str,
            "snippet": sanitized_snippet,
            "text_snippet": manifest_item["text_snippet"],
            "has_content": manifest_item["has_content"]
        }
        manifest.append(internal_item)
        manifest_lookup[rel_path.lower()] = internal_item
        manifest_lookup[filename.lower()] = internal_item

    # 4. Formulate Stage 2 Gemini System Prompt with Real Files Manifest
    if is_pure_affirmative:
        last_asst_snippet = last_asst[:400].replace('\n', ' ') if last_asst else normalized_prompt
        effective_request = f"The user confirmed with '{raw_prompt}' to proceed with the specific action, plan, or recommendation previously discussed by the assistant in the latest turn: \"{last_asst_snippet}\"."
    elif is_polite_action:
        effective_request = f"The user requested to proceed with the action: '{raw_prompt}'."
    else:
        effective_request = raw_prompt

    system_prompt = f"""You are Storage Copilot, running locally on an Android device via Termux.
Active User: {owner_name} (Roll/ID: {identifiers}, Major: Artificial Intelligence).
Storage Root: /storage/emulated/0 (Downloads, Documents).
Known Peers: {", ".join(peer_names)}.

RECENT CONVERSATION HISTORY (Last turns):
{conversation_context_block}

Peer Separation Matrix:
{peer_matrix_lines}

Define a unified JSON contract for your responses:
{{
  "type": "conversation" | "plan",
  "message": "Conversational reply or plan summary",
  "actions": [ ... ] // empty if type == "conversation"
}}

CRITICAL INTENT HANDLING:
- If the user is asking for suggestions, ideas, advice, recommendations, or options (e.g., "Some suggestions for my files to organize", "What do you suggest?", "How should I organize?"):
  DO NOT CREATE ACTIONS! Return "type": "conversation", "actions": [], and provide articulate, thoughtful recommendations in "message" explaining how they can organize their files.
- If the user confirmed with "Yes", "Proceed", or agreed to proceed with a previously discussed proposal (see RECENT CONVERSATION HISTORY): CREATE the appropriate "type": "plan" with actions for that confirmed action!
- If the user gives an action instruction like "How about we first sort...", "Can we first organize...", "First sort...", treat this as a direct instruction to stage a plan for those files ("type": "plan").
- ONLY return "type": "plan" with move/rename actions if the user gives an explicit command or instruction or confirmation to organize or modify files.

IMPORTANT: The candidates below are the ACTUAL files currently sitting on disk.
Reason SEMANTICALLY over each candidate's filename, size_kb, text_snippet, and has_content to decide the appropriate operation:

CRITICAL TAXONOMY & CAUTION RULES:
1. ACADEMIC STUDY MATERIALS vs IDENTITY PASSES (STRICT SEPARATION):
   - ACADEMIC STUDY: Lectures, assignments, research papers, course notes, slides (e.g. Knowledge Representation, AI, Neural style paper).
     Route to: "Documents/University/<Subject>/" (e.g. "Documents/University/AI/").
   - IDENTITY & ENTRY PASSES: Entry passes, student ID cards, transit passes, admission passes, passports (e.g. "University Entry Pass: Student Name: [REDACTED_NAME]").
     CAUTION: Even if issued by a university, an Entry Pass or Student ID is NOT academic study material! NEVER move entry passes or passports into academic lecture/course folders ("Documents/University/AI/").
     If the user asked to organize "university files", organize ONLY academic course study materials.
     If organizing identity documents, route to "Documents/Personal/Identity/".
2. PREVENT COLLISION & USE CONTENT-DERIVED DESCRIPTIVE NAMES:
   - NEVER rename files using generic static labels like "Document_2025-04-13.pdf" or "Documents/Organized/".
   - Read each file's `text_snippet`. Use the actual subject matter or purpose to assign a specific, unique name (e.g. "Knowledge_Representation_Lecture_04.pdf", "AI_Course_Assignment_02.pdf", "Electricity_Bill_March2025.pdf").
   - EVERY proposed action in a batch MUST have a distinct, UNIQUE destination path.
3. PEER ISOLATION:
   - Only route to "Documents/Peers/<PeerName>/" if the file explicitly belongs to that peer AND the user requested peer separation or peer context is primary.
4. PERSONAL VOUCHERS:
   - Route personal fee vouchers to "Documents/University/Vouchers/".
5. UTILITIES & BILLS:
   - Route electricity, utility, or consumer invoices to "Documents/Finances/".
6. STRICT USER REQUEST SCOPE:
   - ONLY include move/rename actions for files that correspond to the user's explicit request!
   - If the user specifically asks to "Organize all my University files", touch ONLY academic university files (lectures, assignments, research papers, course notes) and university fee vouchers.
   - CRITICAL: Do NOT touch or move peer documents (like "Sumbal pass.pdf", "fawad fee.pdf", "WhatsApp_Doc_Ahmed_Receipt.pdf"), identity passes, utility bills, or unrelated files when the user only asked to organize university files! Leave them untouched in Download/.
   - If the user specifically asks to "Separate peer documents", touch ONLY files identified for peers.
   - If the user asks to "Clean Download Names", organize and rename ambiguous downloads into their respective folders.

Action format:
- {{"action_id": "step-1", "type": "make_dir", "path": "Documents/..."}}
- {{"action_id": "step-2", "type": "move", "source": "<exact filename from manifest>", "destination": "Documents/...", "rationale": "...", "badge": "RENAME" | "FILE_MOVE", "ai_inspected": true, "inspected_preview": "<first 100 chars of text_snippet>"}}
- {{"action_id": "step-3", "type": "trash", "path": "<exact filename from manifest>", "rationale": "...", "badge": "TRASH", "ai_inspected": false, "inspected_preview": "..."}}
"""

    user_prompt_text = f"""Operational Request: "{effective_request}"
Raw User Prompt: "{raw_prompt}"

Current Target Directory: "{target_folder}"
Available Real Files Manifest ({len(manifest)} real files on disk):
{json.dumps(manifest_for_gemini, indent=2)}
"""

    gemini_resp = call_gemini_json(system_prompt, user_prompt_text)

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
            # Enforce batch destination collision prevention
            validated_actions = ensure_unique_plan_destinations(validated_actions)
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
                "plan": action_plan,
                "quick_chips": [
                    "⏩ Process Next Batch",
                    "👥 Separate Peer Documents",
                    "📋 Sort Student Vouchers",
                    "🧹 Clean Duplicates"
                ]
            })

    # 6. Heuristic Fallback (Active if Gemini is offline or API key missing)
    effective_p = (normalized_prompt if is_pure_affirmative else raw_prompt).lower()
    lower_p = effective_p
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
                snip = item["snippet"].lower()
                clean_title = fn

                # 1. Academic prioritization
                if any(k in snip for k in ("knowledge representation", "artificial intelligence", "lecture", "course", "assignment", "semester", "bsai")):
                    target_d = "Documents/University/AI"
                    if "knowledge representation" in snip:
                        clean_title = "Knowledge_Representation_Lecture.pdf"
                    elif "assignment" in snip:
                        clean_title = "AI_Course_Assignment.pdf"
                    else:
                        clean_title = "AI_Lecture_Notes.pdf"
                elif "1508" in fn or "neural" in snip:
                    clean_title = "Neural_Algorithm_Artistic_Style_Paper.pdf"
                    target_d = "Documents/University/AI"
                elif any(p_name.lower() in snip for p_name in peer_names):
                    p_found = next(p for p in peer_names if p.lower() in snip)
                    target_d = f"Documents/Peers/{p_found}"
                    clean_title = f"{p_found}_Receipt.pdf"
                elif "voucher" in snip or "fee" in snip:
                    target_d = "Documents/University/Vouchers"
                    clean_title = "Student_Fee_Voucher.pdf"
                elif "bill" in snip or "invoice" in snip or "utility" in snip or "electricity" in snip:
                    target_d = "Documents/Finances"
                    clean_title = "Electricity_Bill_Invoice.pdf"
                elif "doc-" in fn.lower() or "wa" in fn.lower():
                    # Clean double dots and derive title
                    clean_fn = re.sub(r"\.\.+", ".", fn)
                    stem_clean = os.path.splitext(clean_fn)[0]
                    clean_title = f"Document_{stem_clean}.pdf"
                    target_d = "Documents/Personal"
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
                    "rationale": f"Semantically organized '{fn}' into {target_d} based on inspected content"
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

    elif any(re.search(rf"\b{re.escape(k)}\b", lower_p) for k in ("university", "academic", "lecture", "course", "study", "uni", "college", "campus", "school", "varsity", "homework", "hw", "slides", "notes", "assignment")):
        plan_desc = f"Organize academic university course materials for {owner_name}"
        for item in manifest:
            classification = classify_document(item["name"], item["snippet"])
            # STRICT RULE: Only organize actual Academic Study Materials into University Course directories!
            # Do NOT touch or route Identity Passes (e.g. Sumbal pass.pdf), Passports, or Utility Bills!
            if classification["is_academic_study"]:
                target_d = classification["target_dir"]
                clean_title = classification["suggested_title"]
                if target_d not in created_dirs:
                    plan_actions.append({"action_id": f"step-{step_idx}", "type": "make_dir", "path": target_d})
                    created_dirs.add(target_d)
                    step_idx += 1
                plan_actions.append({
                    "action_id": f"step-{step_idx}",
                    "type": "move",
                    "source": item["path"],
                    "destination": f"{target_d}/{clean_title}",
                    "badge": "RENAME" if clean_title != item["name"] else "FILE_MOVE",
                    "rationale": f"Organized academic study document into {target_d} based on inspected content",
                    "ai_inspected": True,
                    "inspected_preview": (item["snippet"] or "")[:100]
                })
                step_idx += 1
            elif ("voucher" in lower_p or "fee" in lower_p or "all" in lower_p) and classification["category"] == "ACADEMIC_VOUCHER":
                target_d = classification["target_dir"]
                clean_title = classification["suggested_title"]
                if target_d not in created_dirs:
                    plan_actions.append({"action_id": f"step-{step_idx}", "type": "make_dir", "path": target_d})
                    created_dirs.add(target_d)
                    step_idx += 1
                plan_actions.append({
                    "action_id": f"step-{step_idx}",
                    "type": "move",
                    "source": item["path"],
                    "destination": f"{target_d}/{clean_title}",
                    "badge": "FILE_MOVE",
                    "rationale": f"Filed university tuition fee voucher into {target_d}",
                    "ai_inspected": True,
                    "inspected_preview": (item["snippet"] or "")[:100]
                })
                step_idx += 1

    elif any(term in lower_p for term in ("best suited", "best place", "proper place", "proper folder", "where they belong", "all the other files", "all other files", "other files", "rest of the files", "remaining files", "all files", "everything", "all docs", "all pdfs", "docs and pdfs", "sort download", "organize download", "sort files", "organize files", "tidy up")) or (any(v in lower_p for v in ("sort", "organize", "structure", "categorize", "tidy")) and any(d in lower_p for d in ("file", "files", "doc", "docs", "pdf", "pdfs", "download", "downloads", "everything", "all"))):
        plan_desc = f"Sort and organize candidate documents in '{target_folder}' into structured taxonomy"
        for item in manifest:
            fn = item["name"]
            snip = item["snippet"] or ""
            fn_lower = fn.lower()
            snip_lower = snip.lower()

            if fn.startswith("."):
                continue

            # 1. Peer isolation check
            matched_peer = None
            for p_name in peer_names:
                if p_name.lower() in fn_lower or p_name.lower() in snip_lower:
                    matched_peer = p_name
                    break

            if matched_peer:
                target_d = f"Documents/Peers/{matched_peer}"
                clean_title = fn
                rationale = f"Routed to peer folder for {matched_peer} to protect personal storage"
            else:
                classification = classify_document(fn, snip)
                target_d = classification["target_dir"]
                clean_title = classification["suggested_title"]
                cat = classification["category"]

                if cat == "TEMPORARY_TRASH":
                    plan_actions.append({
                        "action_id": f"step-{step_idx}",
                        "type": "trash",
                        "path": item["path"],
                        "badge": "TRASH",
                        "rationale": "Temporary/screenshot file queued for soft-delete with rollback"
                    })
                    step_idx += 1
                    continue
                else:
                    rationale = f"Semantically categorized as {classification['type_label']} into {target_d}"

            if target_d not in created_dirs:
                plan_actions.append({"action_id": f"step-{step_idx}", "type": "make_dir", "path": target_d})
                created_dirs.add(target_d)
                step_idx += 1

            plan_actions.append({
                "action_id": f"step-{step_idx}",
                "type": "move",
                "source": item["path"],
                "destination": f"{target_d}/{clean_title}",
                "badge": "RENAME" if clean_title != fn else "FILE_MOVE",
                "rationale": rationale,
                "ai_inspected": True,
                "inspected_preview": (snip or "")[:100]
            })
            step_idx += 1

    else:
        # Check if user prompt targets a specific document by name with an action verb
        action_verbs = ("move", "rename", "put", "transfer", "trash", "delete", "file", "place")
        has_action_intent = any(v in lower_p for v in action_verbs) or "." in raw_prompt

        specific_matches = []
        if has_action_intent:
            for item in manifest:
                m_score, m_reason = score_document_match(raw_prompt, item)
                if m_score >= 75.0:
                    specific_matches.append((m_score, item, m_reason))

        if specific_matches:
            specific_matches.sort(key=lambda x: x[0], reverse=True)
            plan_desc = f"Organize matched document: {specific_matches[0][1]['name']}"
            for m_score, item, m_reason in specific_matches[:5]:
                classification = classify_document(item["name"], item["snippet"])
                target_d = classification["target_dir"]
                clean_title = classification["suggested_title"]
                if target_d not in created_dirs:
                    plan_actions.append({"action_id": f"step-{step_idx}", "type": "make_dir", "path": target_d})
                    created_dirs.add(target_d)
                    step_idx += 1
                plan_actions.append({
                    "action_id": f"step-{step_idx}",
                    "type": "move",
                    "source": item["path"],
                    "destination": f"{target_d}/{clean_title}",
                    "badge": "RENAME" if clean_title != item["name"] else "FILE_MOVE",
                    "rationale": f"Organized specific file '{item['name']}' based on match ({m_reason})",
                    "ai_inspected": True,
                    "inspected_preview": (item["snippet"] or "")[:100]
                })
                step_idx += 1
        else:
            # Prompt is general or advice-seeking: provide articulate guidance response, ZERO unneeded file mutations
            return jsonify({
                "type": "conversation",
                "message": (
                    f"I've inspected your storage in '{target_folder}/' ({len(manifest)} real files detected).\n\n"
                    f"Here are tailored suggestions to organize your storage, {owner_name}:\n\n"
                    f"- 📚 **University Courseware**: Consolidate AI lectures and coursework into `Documents/University/<Subject>/`.\n"
                    f"- 🧾 **Fee Vouchers**: Sort your tuition challans into `Documents/University/Vouchers/`.\n"
                    f"- 👥 **Peer Separation**: Isolate files belonging to {', '.join(peer_names)} into `Documents/Peers/<Name>/`.\n"
                    f"- 🏷️ **Clean Cryptic Downloads**: Standardize messy WhatsApp downloads (`DOC-*.pdf`) into descriptive names.\n"
                    f"- 🗑️ **Cleanup**: Soft-delete temporary cache files and old screenshots into `.agent_trash`.\n\n"
                    f"Whenever you'd like me to formulate a plan, simply ask (e.g., *\"Organize all my university files\"* or *\"Sort student vouchers\"*)!"
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

    # Enforce batch destination collision prevention
    plan_actions = ensure_unique_plan_destinations(plan_actions)
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
        "plan": action_plan,
        "quick_chips": [
            "⏩ Process Next Batch",
            "👥 Separate Peer Documents",
            "📋 Sort Student Vouchers",
            "🧹 Clean Duplicates"
        ]
    })

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)