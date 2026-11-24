import os
import shutil
import uuid
import hashlib
from typing import Dict, Any, List
from flask import Blueprint, request, jsonify
from storage_copilot import config

utility_bp = Blueprint("utility", __name__)

SEED_FIXTURE_CONTENTS: Dict[str, str] = {
    "BSAI-182_fee_voucher_fall.pdf": "Student Fee Voucher: Name: Imran Tahir, Roll No: BSAI-182, Amount: PKR 45,000, Status: Paid.",
    "fawad fee.pdf": "Student Fee Slip: Name: Fawad, Roll No: BSAI-190, Amount: PKR 45,000, Status: Paid.",
    "Sumbal pass.pdf": "University Entry Pass: Student Name: Sumbal, Department: AI.",
    "1508.06576v2_neural_style.pdf": "A Neural Algorithm of Artistic Style by Leon A. Gatys, Alexander S. Ecker, Matthias Bethge. ArXiv research paper on deep learning neural representations.",
    "DOC-20250413-WA0017..pdf": "Knowledge Representation and Reasoning Lecture 4: First-Order Predicate Logic, Ontologies, and Semantic Networks. BSAI Semester 6 University Course.",
    "DOC-20250413-WA0018..pdf": "Artificial Intelligence Course Assignment 2: Heuristic Search and A* Graph Exploration by Imran Tahir (BSAI-182).",
    "DOC-20250413-WA0019..pdf": "Electricity Utility Bill Invoice for March 2025. Consumer ID: 948201. Amount Due: PKR 8,500. Status: Pending.",
    "Screenshot_20241001-142210.png": "[PNG Image Binary Fixture: Temporary screen capture]",
    "temp_cache_sync.tmp": "[Temporary sync cache file created by updater]",
    "WhatsApp_Doc_Ahmed_Receipt.pdf": "Payment receipt for Ahmed, Department Library Fee.",
    "1508.06576v2_neural_style (1).pdf": "A Neural Algorithm of Artistic Style by Leon A. Gatys, Alexander S. Ecker, Matthias Bethge. ArXiv research paper on deep learning neural representations.",
    "DOC-20250413-WA0019 (1)..pdf": "Electricity Utility Bill Invoice for March 2025. Consumer ID: 948201. Amount Due: PKR 8,500. Status: Pending."
}

FIXTURE_SIGNATURES: Dict[str, str] = {
    fname: hashlib.sha256(content.encode("utf-8")).hexdigest()
    for fname, content in SEED_FIXTURE_CONTENTS.items()
}
FIXTURE_HASHES: set = set(FIXTURE_SIGNATURES.values())


def is_fixture_signature(file_path: str) -> bool:
    """
    Returns True if the file content matches one of the known seed stub SHA-256 signatures,
    regardless of filename (handles renamed stubs e.g. in Documents/Finances).
    """
    try:
        if os.path.exists(file_path) and os.path.isfile(file_path):
            # Seed stubs are small text fixtures (< 10 KB)
            if os.path.getsize(file_path) > 10240:
                return False
            with open(file_path, "rb") as fh:
                file_hash = hashlib.sha256(fh.read()).hexdigest()
            return file_hash in FIXTURE_HASHES
    except Exception:
        pass
    return False


@utility_bp.route("/seed_fixtures", methods=["POST"])
def seed_fixtures():
    """
    Seeds sample mobile files into isolated <BASE_DIR>/.demo_sandbox/Download/
    (NEVER writes fixture stubs to real Download/ or /storage/emulated/0).
    """
    data = request.get_json(silent=True) or {}
    include_duplicates = bool(data.get("duplicates") or request.args.get("duplicates"))
    sandbox_download_dir = os.path.join(config.BASE_DIR, ".demo_sandbox", "Download")
    os.makedirs(sandbox_download_dir, exist_ok=True)

    files_to_seed = {
        k: v for k, v in SEED_FIXTURE_CONTENTS.items()
        if not (k.endswith(" (1).pdf") or k.endswith(" (1)..pdf")) or include_duplicates
    }

    created = []
    for fname, content in files_to_seed.items():
        fpath = os.path.join(sandbox_download_dir, fname)
        with open(fpath, "w", encoding="utf-8") as f:
            f.write(content)
        created.append(fname)

    return jsonify({
        "status": "success",
        "message": f"Seeded {len(created)} fixture files into isolated .demo_sandbox/Download/",
        "sandbox_dir": os.path.join(config.BASE_DIR, ".demo_sandbox"),
        "files": list(files_to_seed.keys()),
        "newly_created": created
    })


@utility_bp.route("/scan_seed_fixtures", methods=["GET"])
def scan_seed_fixtures():
    """
    Scans real active storage (Download/ and Documents/) for any seed stubs accidentally
    placed or leftover in production storage, returning a standard 'trash' ActionPlan for cleanup.
    """
    base_dir = config.BASE_STORAGE_DIR
    active_roots = ["Download", "Documents"]
    found_stubs: List[str] = []

    for root_name in active_roots:
        root_path = os.path.join(base_dir, root_name)
        if not os.path.exists(root_path):
            continue
        for root, dirs, files in os.walk(root_path):
            dirs[:] = [d for d in dirs if not d.startswith(".") and d.lower() != "android" and d != ".agent_trash" and d != ".demo_sandbox"]
            for fname in files:
                abs_p = os.path.join(root, fname)
                if is_fixture_signature(abs_p):
                    found_stubs.append(abs_p)

    actions = []
    for abs_p in found_stubs:
        try:
            rel_p = os.path.relpath(abs_p, config.BASE_DIR).replace("\\", "/")
        except Exception:
            rel_p = abs_p
        actions.append({
            "type": "trash",
            "path": rel_p,
            "source": rel_p,
            "filename": os.path.basename(abs_p),
            "reason": "Seed fixture stub detected in active storage (safety isolation cleanup)"
        })

    action_plan = None
    if actions:
        action_plan = {
            "plan_id": str(uuid.uuid4()),
            "action": "trash",
            "description": "Clean up contaminated seed fixture stubs from active storage",
            "collision_strategy": "FAIL",
            "actions": actions
        }

    return jsonify({
        "status": "success",
        "action": "trash" if actions else "none",
        "found_stubs_count": len(actions),
        "contaminated_files": [a["path"] for a in actions],
        "plan": action_plan,
        "actions": actions
    })


@utility_bp.route("/clear_demo_sandbox", methods=["POST"])
def clear_demo_sandbox():
    """Cleans up the entire .demo_sandbox directory tree."""
    sandbox_dir = os.path.join(config.BASE_DIR, ".demo_sandbox")
    if os.path.exists(sandbox_dir):
        shutil.rmtree(sandbox_dir, ignore_errors=True)
    return jsonify({
        "status": "success",
        "message": "Demo sandbox cleanly removed"
    })
