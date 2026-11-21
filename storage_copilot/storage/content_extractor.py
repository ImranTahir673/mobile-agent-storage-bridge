import os
import re
import zipfile
from typing import Tuple, Dict, Any


def is_human_readable_text(text: str) -> Tuple[bool, str, bool]:
    """
    Evaluates whether an extracted string is human-readable prose:
    1. Detects raw '%PDF-' stream binary headers or PDF stream operators.
    2. Flags text where > 20% of characters are non-printable control characters.
    
    Returns:
        (is_readable: bool, sanitized_text: str, is_scanned_or_binary: bool)
    """
    if not text:
        return True, "", False

    # Check for raw %PDF- header / stream operator leaking into string
    if "%PDF-" in text or "/FlateDecode" in text or "endstream" in text or "/Contents" in text:
        return False, "Scanned document — text not extractable", True

    # Count non-printable characters (excluding newline, carriage return, and tab)
    total_chars = len(text)
    if total_chars == 0:
        return True, "", False

    non_printable = sum(
        1 for c in text
        if (ord(c) < 32 and c not in ("\n", "\r", "\t")) or (0x7F <= ord(c) <= 0x9F)
    )

    if (non_printable / total_chars) > 0.20:
        return False, "Scanned document — text not extractable", True

    return True, text, False


def inspect_file_health(file_path: str) -> Dict[str, Any]:
    """
    Validates file integrity and flags corrupted, damaged, or empty artifacts:
    - 0-byte files -> is_corrupt=True
    - Broken / stub PDFs (missing %PDF- header or failing pypdf parser) -> is_corrupt=True
    - Corrupted or unreadable Zip / DOCX / PPTX / APK archives -> is_corrupt=True
    
    Returns:
        {"is_corrupt": bool, "reason": str, "size_bytes": int}
    """
    if not file_path or not os.path.exists(file_path):
        return {"is_corrupt": True, "reason": "File does not exist", "size_bytes": 0}

    if not os.path.isfile(file_path):
        return {"is_corrupt": False, "reason": "Directory", "size_bytes": 0}

    try:
        size = os.path.getsize(file_path)
    except Exception as e:
        return {"is_corrupt": True, "reason": f"Cannot read file size: {e}", "size_bytes": 0}

    if size == 0:
        return {"is_corrupt": True, "reason": "Zero-byte empty file", "size_bytes": 0}

    ext = os.path.splitext(file_path)[1].lower()

    if ext == ".pdf":
        try:
            with open(file_path, "rb") as fh:
                head = fh.read(1024)
            if not head.startswith(b"%PDF-"):
                return {"is_corrupt": True, "reason": "Invalid or stub PDF (missing %PDF- header)", "size_bytes": size}

            # Attempt full structural parse with pypdf
            import pypdf
            reader = pypdf.PdfReader(file_path)
            _ = len(reader.pages)
        except Exception as e:
            return {"is_corrupt": True, "reason": f"Corrupt PDF structure: {str(e)}", "size_bytes": size}

    elif ext in (".docx", ".pptx", ".xlsx", ".zip", ".apk", ".xapk"):
        if not zipfile.is_zipfile(file_path):
            return {"is_corrupt": True, "reason": f"Corrupt or broken zip/office archive ({ext})", "size_bytes": size}
        try:
            with zipfile.ZipFile(file_path, "r") as z:
                bad_file = z.testzip()
                if bad_file:
                    return {"is_corrupt": True, "reason": f"Corrupt entry in archive: {bad_file}", "size_bytes": size}
        except Exception as e:
            return {"is_corrupt": True, "reason": f"Broken zip/office archive: {str(e)}", "size_bytes": size}

    return {"is_corrupt": False, "reason": "Healthy", "size_bytes": size}


def extract_file_snippet(file_path: str, max_chars: int = 400) -> str:
    """
    Intelligently extracts textual preview across text files, PDFs, Word docx, PowerPoint pptx, and archives.
    Guarantees zero raw binary streams by filtering via is_human_readable_text().
    """
    ext = os.path.splitext(file_path)[1].lower()
    raw_content = ""
    try:
        if ext == ".pdf":
            try:
                import pypdf
                reader = pypdf.PdfReader(file_path)
                page_limit = 15 if max_chars > 2000 else 4
                parts = []
                cur_len = 0
                for p in reader.pages[:page_limit]:
                    txt = p.extract_text() or ""
                    parts.append(txt)
                    cur_len += len(txt)
                    if cur_len >= max_chars:
                        break
                raw_content = "\n".join(parts)
            except Exception:
                try:
                    with open(file_path, "r", encoding="utf-8", errors="replace") as fh:
                        raw_content = fh.read(max_chars)
                except Exception:
                    raw_content = ""
        elif ext in (".txt", ".md", ".json", ".csv", ".tmp", ".log", ".xml", ".html", ".tex", ".cpp", ".py", ".vcf", ".ics"):
            try:
                with open(file_path, "r", encoding="utf-8", errors="replace") as fh:
                    raw_content = fh.read(max_chars)
            except Exception:
                raw_content = ""
        elif ext in (".docx", ".pptx", ".xlsx", ".zip", ".apk", ".xapk"):
            try:
                with zipfile.ZipFile(file_path, "r") as z:
                    names = z.namelist()
                    if ext == ".docx" and "word/document.xml" in names:
                        xml = z.read("word/document.xml").decode("utf-8", errors="ignore")
                        text = re.sub(r"<[^>]+>", " ", xml)
                        text = re.sub(r"\s+", " ", text).strip()
                        raw_content = f"[Word Doc: {text}]"
                    elif ext == ".pptx" and any(n.startswith("ppt/slides/slide") for n in names):
                        slide_files = [n for n in names if n.startswith("ppt/slides/slide") and n.endswith(".xml")][:3]
                        text_parts = []
                        for sf in slide_files:
                            xml = z.read(sf).decode("utf-8", errors="ignore")
                            s_text = re.sub(r"<[^>]+>", " ", xml)
                            text_parts.append(s_text)
                        full_t = re.sub(r"\s+", " ", " ".join(text_parts)).strip()
                        raw_content = f"[Presentation: {full_t}]"
                    elif ext in (".apk", ".xapk"):
                        has_manifest = "AndroidManifest.xml" in names
                        clean_names = [n for n in names if not n.startswith("META-INF")][:6]
                        raw_content = f"[Android Package: {len(names)} files, manifest={'yes' if has_manifest else 'no'}, entries: {', '.join(clean_names)}]"
                    else:
                        clean_names = [n for n in names if not n.startswith("__MACOSX")][:8]
                        raw_content = f"[Archive with {len(names)} files: {', '.join(clean_names)}]"
            except Exception:
                raw_content = ""
    except Exception:
        raw_content = ""

    is_readable, clean_text, _ = is_human_readable_text(raw_content)
    return clean_text[:max_chars]
