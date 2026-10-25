from flask import Blueprint, request, jsonify
from storage_copilot import config
from storage_copilot.storage.deduplication import find_duplicate_clusters

dedup_bp = Blueprint("dedup", __name__)

@dedup_bp.route("/scan_duplicates", methods=["GET", "POST"])
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
        res = find_duplicate_clusters(config.BASE_STORAGE_DIR, target_folder)
        return jsonify(res)
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500
