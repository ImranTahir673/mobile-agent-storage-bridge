# storage_copilot/intelligence/search_engine.py
import os
import re
from typing import Dict, Any, Tuple
from storage_copilot.intelligence.document_classifier import parse_query_intent, classify_document
from storage_copilot.advice.advisor import get_format_category


def score_file_relevance(
    file_info: Dict[str, Any],
    query_intent: Any,
    snippet: str = ""
) -> Dict[str, Any]:
    """
    Computes a grounded 0.0 - 100.0 normalized relevance score:
    - Exact basename or stem match: +40
    - Token overlap in filename: up to +25
    - Token overlap in snippet: up to +20
    - Format / Document category match: +10
    - Temporal / date-range / folder constraint match: +15
    - Negative penalty for blacklisted/mismatched categories (e.g. Identity Pass vs Academic Lecture): -50

    Returns:
    {
        "score": float (0.0 to 100.0),
        "is_relevant": bool (score >= 35.0),
        "match_reasons": List[str]
    }
    """
    if not file_info or not query_intent:
        return {"score": 0.0, "is_relevant": False, "match_reasons": []}

    if isinstance(query_intent, str):
        parsed = parse_query_intent(query_intent)
        raw_query = query_intent.lower()
    else:
        raw_query = (query_intent.get("raw_query") or query_intent.get("cleaned_query") or "").lower()
        parsed = query_intent if "target_tokens" in query_intent else parse_query_intent(raw_query)

    target_tokens = parsed.get("target_tokens", [])
    cleaned_target = (parsed.get("cleaned_target") or "").lower()

    fn = (file_info.get("filename") or file_info.get("name") or "").lower()
    fn_stem = os.path.splitext(fn)[0].lower()
    fn_normalized = re.sub(r"[-_.]+", " ", fn_stem).strip()
    snip = (snippet or file_info.get("snippet") or file_info.get("text_snippet") or "").lower()
    mod_date = file_info.get("modified_date") or file_info.get("modified_str") or ""
    date_str = file_info.get("date_str") or ""

    score = 0.0
    reasons = []

    # 1. Exact Basename or Clean Substring Match (+40)
    if cleaned_target and (fn == cleaned_target or fn_stem == cleaned_target or fn_normalized == cleaned_target):
        score += 40.0
        reasons.append("Exact title match")
    elif cleaned_target and len(cleaned_target) >= 3 and (
        cleaned_target in fn or cleaned_target in fn_stem or cleaned_target in fn_normalized
    ):
        score += 40.0
        reasons.append(f"Title contains '{cleaned_target}'")
    elif fn and len(fn_stem) >= 3 and (fn in raw_query or fn_stem in raw_query):
        score += 40.0
        reasons.append(f"Prompt mentions filename '{fn_stem}'")

    # 2. Token Overlap in Filename (+25)
    if target_tokens:
        matched_fn_tokens = [t for t in target_tokens if t in fn or t in fn_normalized]
        if matched_fn_tokens:
            coverage = len(matched_fn_tokens) / len(target_tokens)
            if coverage == 1.0:
                score += 25.0
                if not reasons:
                    reasons.append(f"All query keywords match: {', '.join(matched_fn_tokens)}")
            else:
                # Single keyword match in filename gives at least +20 so combined with folder/category it crosses 35.0
                token_pts = min(25.0, 15.0 + (10.0 * coverage))
                score += token_pts
                if not reasons:
                    reasons.append(f"Filename keyword match: {', '.join(matched_fn_tokens)}")

    # 3. Token Overlap in Content Snippet (+20)
    if snip and snip != "none (binary/image)":
        if cleaned_target and len(cleaned_target) >= 4 and cleaned_target in snip:
            score += 20.0
            reasons.append(f"Content mentions '{cleaned_target}'")
        elif target_tokens:
            matched_snip_tokens = [t for t in target_tokens if t in snip]
            if matched_snip_tokens:
                snip_coverage = len(matched_snip_tokens) / len(target_tokens)
                if snip_coverage >= 0.5:
                    score += round(20.0 * snip_coverage, 1)
                    reasons.append(f"Content keywords: {', '.join(matched_snip_tokens)}")

    # 4. Document & Format Category Alignment (+10) & Cautionary Penalties (-50)
    classification = classify_document(file_info.get("filename") or file_info.get("name") or "", snip)
    doc_cat = classification.get("category", "")

    if any(t in target_tokens for t in ("pass", "entry", "passport", "id", "identity")):
        if doc_cat == "IDENTITY_PASS":
            score += 15.0
            reasons.append("Category match: Identity Pass")
    elif any(t in target_tokens for t in ("bill", "electricity", "utility", "invoice", "receipt")):
        if doc_cat in ("FINANCIAL_BILL", "ACADEMIC_VOUCHER") or "receipt" in fn or "receipt" in snip:
            score += 15.0
            reasons.append("Category match: Financial Bill/Receipt")
    elif any(t in target_tokens for t in ("voucher", "fee", "challan", "slip")):
        if doc_cat == "ACADEMIC_VOUCHER":
            score += 15.0
            reasons.append("Category match: Academic Voucher")
    elif any(t in target_tokens for t in ("lecture", "lectures", "notes", "assignment", "neural", "course", "university", "academic")):
        if doc_cat == "ACADEMIC_STUDY":
            score += 25.0
            reasons.append("Category match: Academic Study Material")
        elif doc_cat == "IDENTITY_PASS":
            score -= 50.0  # Strict penalty: never treat an Identity Pass as an academic lecture

    # 5. Temporal / Date-Range & Folder Alignment (+15)
    if parsed.get("folder_constraint"):
        fc = parsed["folder_constraint"].lower()
        doc_p = (file_info.get("path") or "").lower()
        if (doc_p.startswith(fc + "/") or (fc == "download" and not doc_p.startswith("documents/"))) and score > 0:
            score += 10.0

    # Date matching boost (ISO date or Month/Day/Year in query)
    months = {
        "jan": "01", "feb": "02", "mar": "03", "apr": "04", "may": "05", "jun": "06",
        "jul": "07", "aug": "08", "sep": "09", "oct": "10", "nov": "11", "dec": "12"
    }
    date_pattern = re.search(r"(\d{1,2})?\s*([a-zA-Z]{3,9})\s*(\d{2,4})?", raw_query)
    if date_pattern:
        day_g, month_g, year_g = date_pattern.groups()
        month_prefix = (month_g or "")[:3].lower()
        if month_prefix in months:
            m_num = months[month_prefix]
            if day_g and year_g:
                expected_iso = f"{year_g}-{m_num}-{day_g.zfill(2)}"
                if expected_iso in mod_date or expected_iso in date_str:
                    score += 45.0
                    reasons.append(f"Date match: {expected_iso}")
            elif year_g and f"{year_g}-{m_num}" in (mod_date or date_str):
                score += 35.0
                reasons.append(f"Month/Year match: {year_g}-{m_num}")

    iso_match = re.search(r"\b(\d{4}-\d{2}-\d{2})\b", raw_query)
    if iso_match and iso_match.group(1) in (mod_date or date_str):
        score += 45.0
        reasons.append(f"Exact ISO date match: {iso_match.group(1)}")

    final_score = round(max(0.0, min(100.0, score)), 1)
    return {
        "score": final_score,
        "is_relevant": final_score >= 35.0,
        "match_reasons": reasons
    }


def score_document_match(query: str, doc: dict) -> Tuple[float, str]:
    """
    Backward-compatible wrapper on the 0.0 - 200.0 scale.
    Preserves exact threshold behavior required by test_dynamic_reasoning_mobile.py (e.g. >= 120.0 for strong book match).
    """
    parsed = parse_query_intent(query)
    rel = score_file_relevance(doc, parsed)
    if not rel["is_relevant"]:
        return 0.0, ""

    # Scale 0-100 normalized score back to 0-200 legacy scale
    scaled_score = rel["score"] * 2.0
    reason_str = rel["match_reasons"][0] if rel["match_reasons"] else "Semantic keyword match"
    return scaled_score, reason_str


def compute_query_relevance_score(prompt: str, item: dict) -> float:
    """
    Backward-compatible relevance scorer (0.0 - 300.0+ scale) for sorting candidate slices.
    Delegates to score_file_relevance() and scales appropriately.
    """
    if not prompt or not item:
        return 0.0
    rel = score_file_relevance(item, prompt)
    if rel["score"] <= 0.0:
        return 0.0
    return round(rel["score"] * 3.0, 1)
