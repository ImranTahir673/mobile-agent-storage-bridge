import os
import re
from storage_copilot.intelligence.document_classifier import parse_query_intent, classify_document

def score_document_match(query: str, doc: dict) -> tuple[float, str]:
    """
    Calculates semantic relevance score (0.0 - 200.0) for a document.
    Enforces robust token matching so meaningful keywords (like 'Java') in filenames match with high confidence.
    """
    parsed = parse_query_intent(query)
    target_tokens = parsed["target_tokens"]
    cleaned_target = parsed["cleaned_target"]

    if not target_tokens and not cleaned_target:
        return 0.0, ""

    fn = doc.get("filename", "").lower()
    fn_stem = os.path.splitext(fn)[0].lower()
    fn_normalized = re.sub(r"[-_.]+", " ", fn_stem).strip()
    snip = (doc.get("snippet") or doc.get("text_snippet") or "").lower()
    score = 0.0
    match_reason = ""

    # 1. Exact or Clean Substring Match
    if cleaned_target and (fn == cleaned_target or fn_stem == cleaned_target or fn_normalized == cleaned_target):
        score += 150.0
        match_reason = "Exact title match"
    elif cleaned_target and len(cleaned_target) >= 3 and (cleaned_target in fn or cleaned_target in fn_stem or cleaned_target in fn_normalized):
        score += 120.0
        match_reason = f"Title contains '{cleaned_target}'"
    elif target_tokens:
        matched_fn_tokens = [t for t in target_tokens if t in fn or t in fn_normalized]
        if matched_fn_tokens:
            fn_coverage = len(matched_fn_tokens) / len(target_tokens)
            if fn_coverage == 1.0:
                score += 120.0 + (15.0 * len(matched_fn_tokens))
                match_reason = f"All query keywords match: {', '.join(matched_fn_tokens)}"
            else:
                score += 75.0 + (25.0 * len(matched_fn_tokens)) + (20.0 * fn_coverage)
                match_reason = f"Filename keyword match: {', '.join(matched_fn_tokens)}"

    # 1b. Folder Constraint Alignment Boost
    if parsed.get("folder_constraint"):
        fc = parsed["folder_constraint"].lower()
        doc_p = doc.get("path", "").lower()
        if doc_p.startswith(fc + "/") or (fc == "download" and not doc_p.startswith("documents/")):
            score += 25.0

    # 2. Content Snippet Matching
    if snip and snip != "none (binary/image)":
        if cleaned_target and len(cleaned_target) >= 4 and cleaned_target in snip:
            score += 60.0
            if not match_reason:
                match_reason = f"Content mentions '{cleaned_target}'"
        elif target_tokens:
            matched_snip_tokens = [t for t in target_tokens if t in snip]
            snip_coverage = len(matched_snip_tokens) / len(target_tokens)
            if snip_coverage >= 0.6:
                score += 40.0 * snip_coverage
                if not match_reason:
                    match_reason = f"Content keywords: {', '.join(matched_snip_tokens)}"

    # 3. Document Category & Topic Alignment
    classification = classify_document(doc.get("filename", ""), snip)
    doc_cat = classification.get("category", "")

    if any(t in target_tokens for t in ("pass", "entry", "passport")):
        if doc_cat == "IDENTITY_PASS":
            score += 40.0
    elif any(t in target_tokens for t in ("bill", "electricity", "utility", "invoice")):
        if doc_cat == "FINANCIAL_BILL":
            score += 40.0
    elif any(t in target_tokens for t in ("voucher", "fee", "challan", "slip")):
        if doc_cat == "ACADEMIC_VOUCHER":
            score += 40.0
    elif any(t in target_tokens for t in ("lecture", "notes", "assignment", "neural", "course")):
        if doc_cat == "ACADEMIC_STUDY":
            score += 40.0
        elif doc_cat == "IDENTITY_PASS":
            score = 0.0  # Cautionary penalty: never treat identity pass as academic lecture

    # Filter out weak incidental noise (must achieve meaningful semantic threshold)
    if score < 45.0:
        return 0.0, ""

    return max(0.0, score), match_reason

def compute_query_relevance_score(prompt: str, item: dict) -> float:
    """
    Computes an intelligent relevance score between a user's prompt and a file item
    to guarantee that searched files (by name, keyword, or date) are prioritized
    at the very top of candidate slices sent to Gemini and offline fallback.
    """
    if not prompt or not item:
        return 0.0

    p_clean = prompt.lower()
    fn = item.get("filename", item.get("name", "")).lower()
    fn_stem = os.path.splitext(fn)[0]
    clean_stem = re.sub(r"[-_.]+", " ", fn_stem)
    snip = (item.get("snippet") or item.get("text_snippet") or "").lower()
    mod_date = item.get("modified_date", "")
    date_str = item.get("date_str", "")

    score = 0.0

    # 1. Exact phrase or stem match
    if fn in p_clean or fn_stem in p_clean:
        score += 250.0

    # 2. Key phrase tokens (e.g. '48 laws', 'complete reference', 'snapinsta')
    p_tokens = [w for w in re.findall(r"\w+", p_clean) if len(w) > 2 and w not in ("find", "search", "show", "list", "what", "where", "which", "file", "files", "download", "documents", "cant", "cannot", "give", "could")]
    matched_tokens = 0
    for tok in p_tokens:
        if tok in fn or tok in clean_stem:
            score += 60.0
            matched_tokens += 1
        elif tok in snip:
            score += 25.0

    # Strong boost if multiple query tokens match the filename
    if matched_tokens >= 2:
        score += 100.0

    # 3. Specific Date matching (e.g., '12 sep 2023', 'sep 12 2023', '2023-09-12', '12 sep', 'sep 2023')
    months = {
        "jan": "01", "feb": "02", "mar": "03", "apr": "04", "may": "05", "jun": "06",
        "jul": "07", "aug": "08", "sep": "09", "oct": "10", "nov": "11", "dec": "12"
    }
    date_pattern = re.search(r"(\d{1,2})?\s*([a-zA-Z]{3,9})\s*(\d{2,4})?", p_clean)
    if date_pattern:
        day_g, month_g, year_g = date_pattern.groups()
        month_prefix = (month_g or "")[:3].lower()
        if month_prefix in months:
            m_num = months[month_prefix]
            if day_g and year_g:
                day_padded = day_g.zfill(2)
                expected_iso = f"{year_g}-{m_num}-{day_padded}"
                if expected_iso in mod_date or expected_iso in date_str:
                    score += 300.0
                elif f"{year_g}-{m_num}" in mod_date or f"{year_g}-{m_num}" in date_str:
                    score += 150.0
            elif year_g and f"{year_g}-{m_num}" in (mod_date or date_str):
                score += 150.0

    iso_match = re.search(r"\b(\d{4}-\d{2}-\d{2})\b", p_clean)
    if iso_match and iso_match.group(1) in (mod_date or date_str):
        score += 300.0

    return score
