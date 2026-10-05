import os
import shutil
from flask import Flask, request, jsonify

app = Flask("MobileStorageBridge")

BASE_DIR = os.path.expanduser("~/storage/shared")
TRASH_DIR = os.path.join(BASE_DIR, ".agent_trash")
os.makedirs(TRASH_DIR, exist_ok=True)

def resolve_safe_path(rel_path: str) -> str:
    target = os.path.abspath(os.path.join(BASE_DIR, rel_path.lstrip("/")))
    if not target.startswith(BASE_DIR):
        raise ValueError("Access denied: outside storage boundary")
    return target

@app.route("/")
def health():
    return jsonify({"status": "running", "engine": "Android Storage Bridge"})

@app.route("/list_files", methods=["POST"])
def list_files():
    data = request.get_json(force=True)
    rel_path = data.get("path", "")
    try:
        target = resolve_safe_path(rel_path)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    if not os.path.exists(target):
        return jsonify({"error": "Path not found"}), 404

    items = []
    for entry in os.scandir(target):
        items.append({
            "name": entry.name,
            "is_dir": entry.is_dir(),
            "size_bytes": entry.stat().st_size if not entry.is_dir() else 0
        })
    return jsonify({"path": rel_path, "items": items})

@app.route("/make_directory", methods=["POST"])
def make_directory():
    data = request.get_json(force=True)
    rel_path = data.get("path", "")
    try:
        target = resolve_safe_path(rel_path)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    os.makedirs(target, exist_ok=True)
    return jsonify({"status": "created", "path": rel_path})

@app.route("/move_file", methods=["POST"])
def move_file():
    data = request.get_json(force=True)
    try:
        src = resolve_safe_path(data.get("source", ""))
        dst = resolve_safe_path(data.get("destination", ""))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.move(src, dst)
    return jsonify({"status": "moved", "from": data.get("source"), "to": data.get("destination")})

@app.route("/trash_file", methods=["POST"])
def trash_file():
    data = request.get_json(force=True)
    rel_path = data.get("path", "")
    try:
        src = resolve_safe_path(rel_path)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    dst = os.path.join(TRASH_DIR, os.path.basename(src))
    shutil.move(src, dst)
    return jsonify({"status": "trashed", "path": rel_path})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)