import os
import re
from collections import defaultdict
from storage_copilot import config
from storage_copilot.security.path_resolver import resolve_safe_path
from storage_copilot.storage.deduplication import find_duplicate_clusters

FORMAT_CATEGORIES = {
    "PDFs": {
        "folder": "PDFs",
        "label": "PDF Documents",
        "emoji": "📄",
        "extensions": {".pdf"}
    },
    "APKs": {
        "folder": "APKs",
        "label": "Android Packages (APK)",
        "emoji": "📦",
        "extensions": {".apk", ".xapk", ".apks"}
    },
    "Images": {
        "folder": "Images",
        "label": "Photos & Images",
        "emoji": "🖼️",
        "extensions": {".jpg", ".jpeg", ".png", ".webp", ".gif", ".svg", ".bmp", ".heic"}
    },
    "Audio": {
        "folder": "Audio",
        "label": "Audio & Voice Notes",
        "emoji": "🎵",
        "extensions": {".mp3", ".m4a", ".wav", ".aac", ".ogg", ".opus", ".amr", ".flac"}
    },
    "Videos": {
        "folder": "Videos",
        "label": "Videos & Clips",
        "emoji": "🎬",
        "extensions": {".mp4", ".mkv", ".mov", ".avi", ".webm", ".3gp"}
    },
    "Documents": {
        "folder": "Documents",
        "label": "Word & Office Docs",
        "emoji": "📝",
        "extensions": {".docx", ".doc", ".pptx", ".ppt", ".xlsx", ".xls", ".txt", ".csv"}
    },
    "Archives": {
        "folder": "Archives",
        "label": "Compressed Archives",
        "emoji": "🗜️",
        "extensions": {".zip", ".rar", ".7z", ".tar", ".gz"}
    }
}

def get_format_category(filename: str) -> dict:
    """Matches a filename to its format category dictionary."""
    ext = os.path.splitext(filename)[1].lower()
    for cat_name, info in FORMAT_CATEGORIES.items():
        if ext in info["extensions"]:
            return info
    return {
        "folder": "Other",
        "label": "Other Files",
        "emoji": "📁",
        "extensions": set()
    }

def get_unorganized_download_files(target_dir: str) -> list:
    """
    Returns list of unorganized loose files directly in the root of target_dir.
    Files already placed in subfolders (e.g. Download/PDFs/) are considered organized.
    """
    if not os.path.exists(target_dir) or not os.path.isdir(target_dir):
        return []
    unorganized = []
    try:
        for entry in os.scandir(target_dir):
            if entry.is_file() and not entry.name.startswith("."):
                unorganized.append(entry)
    except Exception:
        pass
    unorganized.sort(key=lambda e: e.name.lower())
    return unorganized

def is_format_sorting_query(text: str) -> bool:
    """
    Evaluates whether a user prompt represents an intent to sort/group files by format/extension.
    Handles typos ('Dowlands'), synonyms ('arrange', 'put', 'consolidate'), and compound phrases
    ('arrange all the same format files in 1 place . From the downloads. And put them inside the folders').
    """
    if not text:
        return False
    t = text.lower().replace("_", " ").replace("-", " ")
    
    direct_patterns = [
        r"same\s+format",
        r"same\s+type",
        r"same\s+extension",
        r"file\s*types?\s+together",
        r"by\s+(?:file\s*)?(?:format|type|extension)",
        r"(?:sort|group|arrange|organize|separate|put|consolidate)\s+(?:all\s+)?(?:the\s+)?(?:by\s+)?(?:file\s*)?(?:format|type|extension)s?",
        r"(?:format|type)\s+subfolders?",
        r"(?:format|type)\s+folders?",
        r"into\s+(?:the\s+)?folders?\s+inside\s+down?loads?",
        r"(?:arrange|sort|group|put)\s+.*(?:same|format|type).*(?:folder|place|download)",
        r"(?:same\s+format\s+files|files\s+of\s+same\s+format)",
        r"(?:all\s+format|each\s+format)\s+in\s+(?:1|one)\s+place"
    ]
    for pattern in direct_patterns:
        if re.search(pattern, t):
            return True
            
    has_format_concept = any(w in t for w in (
        "format", "formats", "filetype", "file type", "file types", "extension", "extensions", "same type"
    ))
    has_grouping_concept = any(w in t for w in (
        "sort", "group", "arrange", "organize", "consolidate", "separate", "together", "1 place", "one place", "into folders", "in folders", "put"
    ))
    return bool(has_format_concept and has_grouping_concept)

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
    total_human = config.format_bytes_human(total_bytes)

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
