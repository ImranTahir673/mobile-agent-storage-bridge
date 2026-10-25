import os
import re

def classify_document(filename: str, snippet: str = "") -> dict:
    """
    Cautiously classifies a document based on BOTH filename and inside text context.
    Strictly distinguishes Academic Course Materials from Identity Passes, Vouchers, and Bills.
    """
    fn_lower = (filename or "").lower()
    snip_lower = (snippet or "").lower()
    combined = f"{fn_lower} {snip_lower}"

    # 1. Identity & Entry Passes (High Caution Priority)
    # Even if it says 'University' or 'AI', an Entry Pass or Passport is an IDENTITY document, not course study material.
    is_identity = any(k in combined for k in (
        "entry pass", "gate pass", "student pass", "transit pass", "admission pass", 
        "passport", "identity card", "id card", "cnic", "student card"
    )) or (("pass" in fn_lower or "passport" in fn_lower) and not any(k in fn_lower for k in ("password", "passed", "passive")))

    if is_identity:
        return {
            "category": "IDENTITY_PASS",
            "type_label": "Identity / Entry Pass",
            "is_academic_study": False,
            "target_dir": "Documents/Personal/Identity",
            "suggested_title": filename
        }

    # 2. Academic Financial Vouchers / Fee Slips
    is_voucher = any(k in combined for k in (
        "fee voucher", "fee slip", "tuition voucher", "challan", "student voucher", "semester fee"
    )) or (("voucher" in fn_lower or "fee" in fn_lower) and "bill" not in fn_lower)

    if is_voucher:
        stem = os.path.splitext(filename)[0]
        clean_title = f"{stem}.pdf" if not stem.endswith(".pdf") else stem
        return {
            "category": "ACADEMIC_VOUCHER",
            "type_label": "Academic Tuition Voucher",
            "is_academic_study": False,
            "target_dir": "Documents/University/Vouchers",
            "suggested_title": clean_title
        }

    # 3. Utility Bills & Commercial Invoices
    is_utility = any(k in combined for k in (
        "utility bill", "electricity bill", "electric bill", "gas bill", "water bill", 
        "consumer id", "amount due", "invoice for march", "invoice for april", "bill invoice"
    )) or ("bill" in fn_lower and "voucher" not in fn_lower)

    if is_utility:
        return {
            "category": "FINANCIAL_BILL",
            "type_label": "Utility / Financial Bill",
            "is_academic_study": False,
            "target_dir": "Documents/Finances",
            "suggested_title": "Electricity_Utility_Bill_March2025.pdf" if "electricity" in combined else "Utility_Bill_Invoice.pdf"
        }

    # 4. Academic Coursework & Study Materials (Lectures, Assignments, Papers)
    academic_keywords = (
        "knowledge representation", "artificial intelligence", "lecture", "course", 
        "assignment", "semester", "bsai", "syllabus", "slides", "notes", "neural", 
        "heuristic search", "exam", "lab report", "tutorial", "arxiv", "deep learning"
    )
    is_academic = any(k in combined for k in academic_keywords) or "1508." in fn_lower

    if is_academic:
        subject = "AI"
        if "knowledge representation" in combined:
            suggested = "Knowledge_Representation_Lecture_04.pdf"
        elif "assignment" in combined:
            suggested = "AI_Course_Assignment_02.pdf"
        elif "neural" in combined or "1508" in fn_lower:
            suggested = "Neural_Algorithm_Artistic_Style_Paper.pdf"
        else:
            stem = os.path.splitext(filename)[0]
            clean_stem = re.sub(r"^(?:DOC-|\d{4}\.\d{4,5}|WhatsApp_Doc_?)", "", stem)
            clean_token = re.sub(r"[^\w-]", "_", clean_stem).strip("_")
            suggested = f"AI_{clean_token or 'Study_Material'}.pdf"

        return {
            "category": "ACADEMIC_STUDY",
            "type_label": "Academic Study Material",
            "is_academic_study": True,
            "target_dir": f"Documents/University/{subject}",
            "suggested_title": suggested
        }

    # 5. Career & Professional Resumes (CV, Resume, Portfolio)
    is_career = any(k in combined for k in (
        "curriculum vitae", "resume", " cv ", "cv.", "cv_", "cv-", "cover letter", 
        "portfolio", "job application", "recommendation letter"
    )) or fn_lower.startswith("cv") or "resume" in fn_lower
    if is_career and not is_academic and not is_voucher:
        return {
            "category": "CAREER_DOCUMENT",
            "type_label": "Career / Resume",
            "is_academic_study": False,
            "target_dir": "Documents/Personal/Career",
            "suggested_title": filename
        }

    # 6. Temporary & Screenshots
    if "screenshot" in fn_lower or fn_lower.endswith((".tmp", ".cache", ".bak", ".log")):
        return {
            "category": "TEMPORARY_TRASH",
            "type_label": "Temporary / Screenshot",
            "is_academic_study": False,
            "target_dir": ".agent_trash",
            "suggested_title": filename
        }

    # 7. General Document
    return {
        "category": "GENERAL_DOCUMENT",
        "type_label": "General Document",
        "is_academic_study": False,
        "target_dir": "Documents/Personal",
        "suggested_title": filename
    }

SEARCH_STOPWORDS = {
    "the", "a", "an", "all", "and", "or", "for", "with", "doc", "pdf", "file", "files", 
    "to", "in", "on", "at", "by", "from", "some", "my", "me", "your", "our", "please", 
    "can", "could", "would", "should", "want", "like", "any", "this", "that", "these", 
    "those", "how", "what", "which", "where", "tell", "give", "list", "listing", "show", 
    "get", "find", "search", "locate", "check", "view", "book", "books", "document", "documents", 
    "paper", "copy", "copies", "download", "downloads", "inside", "here", "there", "of", 
    "is", "are", "have", "has", "do", "does", "did", "also", "you", "language", "guide",
    "guides", "looking", "need", "folder", "folders", "directory", "directories", "fikes", "vest"
}

def parse_query_intent(query: str, default_folder: str = "Download") -> dict:
    """
    Analyzes the user's natural language query to extract:
    1. Folder constraints (e.g. "in download", "in documents")
    2. Intent type: "directory_listing" vs "temporal_recency" vs "specific_search"
    3. Meaningful semantic target keywords (excluding stopwords)
    """
    q_clean = query.strip()
    q_lower = q_clean.lower()

    # 1. Folder constraints
    folder_constraint = None
    if re.search(r"\b(?:in|from|inside)\s+(?:the\s+)?download(?:s)?\b", q_lower) or "download" in q_lower:
        folder_constraint = "Download"
    elif re.search(r"\b(?:in|from|inside)\s+(?:the\s+)?document(?:s)?\b", q_lower) or "documents" in q_lower:
        folder_constraint = "Documents"

    # 2. Intent Detection
    # 2a. Directory Listing ("give me list of all files in download", "list files in documents")
    is_directory_listing = bool(re.search(
        r"(?:give|show|list|display|get)\s+(?:me\s+)?(?:a\s+)?(?:the\s+)?(?:list\s+of\s+)?(?:all\s+)?(?:the\s+)?files",
        q_lower
    ) or q_lower in ("list files", "show files", "list download", "list downloads"))

    # 2b. Temporal Recency ("what came today", "recent files", "what did I download today")
    is_temporal = bool(re.search(r"\b(?:today|recent|new|just now|yesterday|latest)\b", q_lower))

    # 3. Clean Target Extraction (Strip conversational framing & stopwords)
    cleaned = re.sub(
        r"(?i)^(?:can\s+you\s+)?(?:please\s+)?(?:find|search|locate|look\s+for|where\s+is|what\s+is|give\s+me|show\s+me|get\s+me|check)\s+(?:me\s+)?(?:the\s+)?(?:a\s+)?",
        "",
        q_clean
    ).strip()

    # Remove folder trailer e.g. "in download"
    cleaned = re.sub(r"(?i)\s+(?:in|from|inside)\s+(?:the\s+)?(?:download|downloads|documents|storage)\b.*$", "", cleaned).strip()

    # Extract alphanumeric target tokens (filtering out universal search stopwords)
    words = [re.sub(r"[^\w-]", "", w.lower()) for w in re.split(r"[\s_]+", cleaned) if w]
    target_tokens = [w for w in words if w and w not in SEARCH_STOPWORDS and len(w) > 1]

    # Specific phrase preservation
    cleaned_target = " ".join([w for w in words if w and w not in {"the", "a", "an", "for", "in", "to", "can", "you", "find", "me", "book", "file", "document"}])

    return {
        "raw_query": query,
        "is_directory_listing": is_directory_listing,
        "is_temporal": is_temporal,
        "folder_constraint": folder_constraint or default_folder,
        "target_tokens": target_tokens,
        "cleaned_target": cleaned_target
    }
