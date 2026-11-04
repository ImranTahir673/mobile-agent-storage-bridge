import os
import time
import requests
from flask import Blueprint, request, jsonify, send_from_directory
from storage_copilot import config
from storage_copilot.intelligence.gemini_client import GEMINI_MODELS

core_bp = Blueprint("core", __name__)

@core_bp.route("/", methods=["GET"])
def health():
    accept = request.headers.get("Accept", "")
    # Serve mobile dashboard if browser requests HTML
    if "text/html" in accept or request.args.get("ui") == "1":
        index_file = os.path.join(config.WEB_DIR, "index.html")
        if os.path.exists(index_file):
            return send_from_directory(config.WEB_DIR, "index.html")
    return jsonify({
        "status": "running",
        "version": config.APP_VERSION,
        "build_id": config.SERVER_BOOT_ID,
        "engine": f"Android Storage Bridge {config.APP_VERSION}",
        "base_dir": config.BASE_DIR,
        "ledger_active": True
    })

@core_bp.route("/health", methods=["GET"])
def health_check():
    return jsonify({
        "status": "running",
        "version": config.APP_VERSION,
        "build_id": config.SERVER_BOOT_ID,
        "engine": f"Android Storage Bridge {config.APP_VERSION}",
        "base_dir": config.BASE_DIR,
        "ledger_active": True
    })

@core_bp.route("/<path:filename>", methods=["GET"])
def serve_static(filename):
    # Strictly serve assets located within WEB_DIR
    target_file = os.path.join(config.WEB_DIR, filename)
    if os.path.isfile(target_file):
        return send_from_directory(config.WEB_DIR, filename)
    return jsonify({"error": f"Static asset not found: {filename}"}), 404

@core_bp.route("/api_status", methods=["GET"])
def api_status():
    """Returns AI intelligence engine status and active Gemini configuration."""
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    is_configured = bool(api_key and len(api_key) > 5)
    masked_key = f"{api_key[:4]}...{api_key[-4:]}" if is_configured and len(api_key) > 8 else ""
    is_quota_limited = bool(time.time() < config.GEMINI_QUOTA_BLOCKED_UNTIL)
    return jsonify({
        "status": "online",
        "gemini_configured": is_configured,
        "quota_limited": is_quota_limited,
        "masked_key": masked_key,
        "active_models": GEMINI_MODELS,
        "app_version": config.APP_VERSION
    })

@core_bp.route("/configure_api_key", methods=["POST"])
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
    config.GEMINI_QUOTA_BLOCKED_UNTIL = 0.0

    env_file = os.path.join(config.REPO_ROOT, ".env")
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

@core_bp.route("/storage_overview", methods=["GET"])
def storage_overview():
    """
    Returns an overview of storage contents across Download/ and Documents/,
    including total file counts and category distributions.
    """
    from collections import defaultdict
    from storage_copilot.intelligence.document_classifier import classify_document

    category_counts = defaultdict(int)
    dl_counts = defaultdict(int)
    doc_counts = defaultdict(int)
    dl_files = 0
    doc_files = 0

    dl_dir = os.path.join(config.BASE_STORAGE_DIR, "Download")
    if os.path.exists(dl_dir) and os.path.isdir(dl_dir):
        for root, dirs, files in os.walk(dl_dir):
            dirs[:] = [d for d in dirs if not d.startswith(".") and d != ".agent_trash"]
            for f in files:
                if not f.startswith("."):
                    dl_files += 1
                    cat = classify_document(f, "")["category"]
                    dl_counts[cat] += 1
                    category_counts[cat] += 1

    doc_dir = os.path.join(config.BASE_STORAGE_DIR, "Documents")
    if os.path.exists(doc_dir) and os.path.isdir(doc_dir):
        for root, dirs, files in os.walk(doc_dir):
            dirs[:] = [d for d in dirs if not d.startswith(".") and d != ".agent_trash"]
            for f in files:
                if not f.startswith("."):
                    doc_files += 1
                    cat = classify_document(f, "")["category"]
                    doc_counts[cat] += 1
                    category_counts[cat] += 1

    total_files = dl_files + doc_files

    return jsonify({
        "status": "success",
        "total_files": total_files,
        "download_files_count": dl_files,
        "documents_files_count": doc_files,
        "category_counts": dict(category_counts),
        "roots": {
            "Download": {"total_files": dl_files, "category_counts": dict(dl_counts)},
            "Documents": {"total_files": doc_files, "category_counts": dict(doc_counts)}
        }
    })
