import os
from flask import Blueprint, request, jsonify
from storage_copilot import config

utility_bp = Blueprint("utility", __name__)

@utility_bp.route("/seed_fixtures", methods=["POST"])
def seed_fixtures():
    """Seeds realistic sample mobile files in Download/ to test instant plan generation, query inspection, and execution."""
    data = request.get_json(silent=True) or {}
    include_duplicates = bool(data.get("duplicates") or request.args.get("duplicates"))
    download_dir = os.path.join(config.BASE_DIR, "Download")
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
