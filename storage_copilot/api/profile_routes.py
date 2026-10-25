import os
import json
from flask import Blueprint, request, jsonify
from storage_copilot import config

profile_bp = Blueprint("profile", __name__)

def get_user_profile_path():
    """Resolves active path to user_profile.json on disk."""
    candidates = [
        os.path.join(config.BASE_DIR, "user_profile.json"),
        os.path.join(config.BASE_DIR, ".user_profile.json"),
        os.path.join(config.REPO_ROOT, "user_profile.json"),
        os.path.join(config.REPO_ROOT, "user_profile.example.json"),
    ]
    for p in candidates:
        if os.path.exists(p):
            return p
    return candidates[0]

@profile_bp.route("/user_profile", methods=["GET"])
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
                source_disp = os.path.relpath(p, config.BASE_DIR).replace("\\", "/")
            except Exception:
                pass
            return jsonify({
                "status": "success",
                "version": config.APP_VERSION,
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
        "version": config.APP_VERSION,
        "source": "fallback",
        "profile": default_profile
    }), 200

@profile_bp.route("/user_profile", methods=["POST"])
def update_user_profile():
    """Updates device-local user profile and saves to device storage root."""
    data = request.get_json(force=True) or {}
    profile = data.get("profile") if "profile" in data else data
    if not isinstance(profile, dict) or "user_identity" not in profile:
        return jsonify({"error": "Invalid profile format. Missing 'user_identity'."}), 400

    target_path = os.path.join(config.BASE_DIR, "user_profile.json")
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
