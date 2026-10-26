from flask import Blueprint, request, jsonify
from storage_copilot import config
from storage_copilot.storage.deduplication import (
    find_duplicate_clusters,
    find_forgotten_files
)

dedup_bp = Blueprint("dedup", __name__)


@dedup_bp.route("/scan_duplicates", methods=["GET", "POST"])
def scan_duplicates():
    """
    Phase 1 & Phase 6 Endpoint:
    Scans target_folder (defaults to Download/) for:
    - Exact duplicate clusters using streaming SHA-256.
    - Optional near-duplicate clusters using 3-gram Jaccard similarity (threshold >= 0.85)
      when `include_near=1` or `include_near=true` is passed.
    """
    data = {}
    if request.method == "POST":
        data = request.get_json(silent=True) or {}
    target_folder = data.get("target_folder") or request.args.get("target_folder") or "Download"

    raw_near = data.get("include_near") if "include_near" in data else request.args.get("include_near", "0")
    include_near = str(raw_near).strip().lower() in ("1", "true", "yes")

    raw_thresh = data.get("threshold") or request.args.get("threshold") or 0.85
    try:
        near_threshold = float(raw_thresh)
    except ValueError:
        near_threshold = 0.85

    try:
        res = find_duplicate_clusters(
            config.BASE_STORAGE_DIR,
            target_folder,
            include_near=include_near,
            near_threshold=near_threshold
        )
        return jsonify(res)
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500


@dedup_bp.route("/scan_forgotten", methods=["GET", "POST"])
def scan_forgotten():
    """
    Phase 6 Forgotten Files Finder Endpoint:
    Identifies stale downloads (>90 days old by default), temporary cache files,
    and orphaned screenshots in target_folder.
    """
    data = {}
    if request.method == "POST":
        data = request.get_json(silent=True) or {}
    target_folder = data.get("target_folder") or request.args.get("target_folder") or "Download"
    raw_days = data.get("stale_days") or request.args.get("stale_days") or 90.0
    try:
        stale_days = float(raw_days)
    except ValueError:
        stale_days = 90.0

    try:
        res = find_forgotten_files(
            config.BASE_STORAGE_DIR,
            target_folder=target_folder,
            stale_days=stale_days
        )
        return jsonify(res)
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500
