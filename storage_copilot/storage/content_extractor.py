import os
import re
import zipfile

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
                page_limit = 15 if max_chars > 2000 else 4
                parts = []
                cur_len = 0
                for p in reader.pages[:page_limit]:
                    txt = p.extract_text() or ""
                    parts.append(txt)
                    cur_len += len(txt)
                    if cur_len >= max_chars:
                        break
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
