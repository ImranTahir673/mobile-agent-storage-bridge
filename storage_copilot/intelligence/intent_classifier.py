# storage_copilot/intelligence/intent_classifier.py
import re
from enum import Enum
from typing import Dict, Any, List, Optional

from storage_copilot.execution.plan_builder import normalize_user_prompt
from storage_copilot.intelligence.document_classifier import parse_query_intent
from storage_copilot.intelligence.gemini_client import call_gemini_micro_classifier
from storage_copilot.advice.advisor import is_format_sorting_query


class UserIntent(str, Enum):
    CONVERSATION = "CONVERSATION"
    QUERY_INFO = "QUERY_INFO"
    FILE_OPERATION = "FILE_OPERATION"


AFFIRMATIVE_WORDS = {
    "yes", "yeah", "yep", "sure", "ok", "okay", "go ahead",
    "proceed", "do it", "please do", "lets do it", "let's do it",
    "yes move all files", "yes, move all files"
}

GREETING_AND_CHAT_EXACT = {
    "hi", "hii", "hiii", "hello", "hey", "heyy", "greetings",
    "good morning", "good afternoon", "good evening", "thanks",
    "thank you", "cool", "awesome", "great", "nice", "cancel", "no", "nope",
    "what else", "what else?", "tell me more", "who are you", "who are you?",
    "what can you do", "what can you do?", "help", "storage advice"
}

CONVERSATION_PATTERNS = [
    r"^what\s+else\b",
    r"^what\s+(?:other\s+things\s+)?can\s+you\s+do\b",
    r"^who\s+are\s+you\b",
    r"^tell\s+me\s+more\b",
    r"\b(?:suggestion|suggestions|recommend|recommendation|recommendations|ideas|advice)\b",
    r"^how\s+(?:should|can|do|could)\s+(?:i|we)\s+(?:organize|sort|clean|manage|structure)\b",
    r"^what\s+(?:can|should|could)\s+(?:i|we)\s+do\s+(?:to|with)\b",
    r"^what\s+do\s+you\s+(?:recommend|suggest)\b",
    # Out-of-scope external actions (email, sms, whatsapp, call, browser)
    r"^(?:send|write|compose)\s+(?:an?\s+)?(?:email|mail|message|sms|whatsapp|text)\b",
    r"^(?:call|phone|dial|ring)\s+",
    r"^(?:open|launch|start)\s+(?:chrome|browser|whatsapp|youtube|settings|camera)\b",
    r"^(?:browse|search\s+the\s+web|google)\b",
]

MUTATION_VERBS = (
    "organize", "sort", "clean", "cleanup", "tidy", "deduplicate",
    "move", "relocate", "transfer", "rename", "trash", "delete",
    "remove", "wipe", "archive", "separate", "isolate", "group",
    "arrange", "consolidate"
)

EXPLICIT_QUERY_PREFIXES = (
    "where is", "where are", "where's", "find", "search", "look for",
    "locate", "do i have", "is there", "list", "show me", "show all",
    "show files", "give me list", "give me a list", "give me the list",
    "can you give me the list", "can you find", "can you show",
    "can you list", "could you find", "could you list", "what university",
    "what lectures", "what files", "what documents", "what is inside",
    "what's inside", "whats inside", "what is my latest", "what is the latest",
    "what is my oldest", "what is the oldest", "what's the oldest",
    "whats the oldest", "what are my oldest", "what are my latest",
    "did i download", "what did i receive", "what did i download",
    "summarize", "explain", "read", "inspect", "view organizedfolder",
    "view organized folders", "see organized folders", "show organized folders",
    "check organized folders", "list organized folders", "inspect organized folders"
)


def _extract_last_assistant_message(conversation_history: Optional[List[Dict[str, Any]]]) -> str:
    if not conversation_history:
        return ""
    for turn in reversed(conversation_history):
        role = (turn.get("role") or "").lower()
        if role in ("assistant", "copilot", "bot"):
            return (turn.get("content") or turn.get("text") or turn.get("message") or "").strip()
    return ""


def classify_intent(
    prompt: str,
    conversation_history: Optional[List[Dict[str, Any]]] = None,
    recent_context: Optional[str] = None,
    available_files_count: int = 0
) -> Dict[str, Any]:
    """
    Classifies a raw user prompt into one of three strict intents:
    - UserIntent.CONVERSATION
    - UserIntent.QUERY_INFO
    - UserIntent.FILE_OPERATION

    Returns:
    {
        "intent": UserIntent,
        "confidence": float,
        "raw_prompt": str,
        "normalized_prompt": str,
        "cleaned_query": str,
        "resolved_goal": str,
        "sub_intent": str,
        "fast_path_matched": bool,
        "extracted_entity": str,
        "is_affirmative": bool
    }
    """
    raw_prompt = (prompt or "").strip()
    normalized_prompt = normalize_user_prompt(raw_prompt)
    lower_raw = raw_prompt.lower()
    lower_norm = normalized_prompt.lower()

    clean_p_start = re.sub(r"^[^\w]+", "", lower_raw).strip()
    clean_p_exact = re.sub(r"[^\w\s?]", "", lower_norm).strip()

    last_asst = _extract_last_assistant_message(conversation_history) or (recent_context or "")
    lower_asst = last_asst.lower()

    # -------------------------------------------------------------------------
    # 1. Empty or Cancellation / Greeting Check
    # -------------------------------------------------------------------------
    if not raw_prompt or clean_p_exact in GREETING_AND_CHAT_EXACT or lower_norm in GREETING_AND_CHAT_EXACT:
        return {
            "intent": UserIntent.CONVERSATION,
            "confidence": 1.0,
            "raw_prompt": raw_prompt,
            "normalized_prompt": normalized_prompt,
            "cleaned_query": normalized_prompt,
            "resolved_goal": normalized_prompt,
            "sub_intent": "greeting_or_capability",
            "fast_path_matched": True,
            "extracted_entity": "",
            "is_affirmative": False
        }

    # -------------------------------------------------------------------------
    # 2. Multi-Turn Affirmative Context Resolution ("Yes", "Proceed", "Do it")
    # -------------------------------------------------------------------------
    is_pure_affirmative = (
        clean_p_start in AFFIRMATIVE_WORDS
        or lower_norm in AFFIRMATIVE_WORDS
        or clean_p_exact in AFFIRMATIVE_WORDS
        or bool(re.search(r"^(?:yes|yeah|yep|sure|ok|okay|go ahead|proceed|do it|please do)\b", clean_p_start))
    )

    if is_pure_affirmative:
        # Check if the user explicitly attached a goal to their "Yes", e.g. "Yes organize all my uni files"
        remainder = re.sub(
            r"^(?:yes|yeah|yep|sure|ok|okay|go ahead|proceed|do it|please do)[\s,!.]*",
            "",
            clean_p_start
        ).strip()

        if remainder and any(v in remainder for v in MUTATION_VERBS) and remainder not in ("move all files",):
            resolved = normalize_user_prompt(remainder)
            return {
                "intent": UserIntent.FILE_OPERATION,
                "confidence": 1.0,
                "raw_prompt": raw_prompt,
                "normalized_prompt": resolved,
                "cleaned_query": resolved,
                "resolved_goal": resolved,
                "sub_intent": "general_mutation",
                "fast_path_matched": True,
                "extracted_entity": resolved,
                "is_affirmative": True
            }

        if clean_p_exact in ("yes move all files", "yes, move all files") or "move all files" in lower_norm:
            return {
                "intent": UserIntent.FILE_OPERATION,
                "confidence": 1.0,
                "raw_prompt": raw_prompt,
                "normalized_prompt": "process next batch of files in download",
                "cleaned_query": "process next batch of files in download",
                "resolved_goal": "process next batch of files in download",
                "sub_intent": "format_batch_execute",
                "fast_path_matched": True,
                "extracted_entity": "Download",
                "is_affirmative": True
            }

        if last_asst:
            if "duplicate file audit" in lower_asst or "clean up these redundant copies" in lower_asst or "redundant duplicate" in lower_asst:
                resolved = "clean duplicate downloads"
                sub = "deduplicate_clean"
            elif any(p in lower_asst for p in ("peer document", "peer files", "separate peer", "classmate", "peer separation")):
                resolved = "separate peer documents"
                sub = "peer_separation"
            elif any(p in lower_asst for p in ("tuition voucher", "fee voucher", "fee slip", "student voucher", "student vouchers")):
                resolved = "sort student vouchers"
                sub = "voucher_sorting"
            elif any(p in lower_asst for p in ("file format", "format subfolders", "group by format", "by format", "respective folders", "unorganized files", "files ready to organize")):
                resolved = "process next batch of files in download"
                sub = "format_batch_execute"
            elif any(p in lower_asst for p in ("university", "academic", "organize", "recommend", "plan", "voucher")):
                resolved = "organize all my university files"
                sub = "academic_organization"
            else:
                return {
                    "intent": UserIntent.CONVERSATION,
                    "confidence": 0.95,
                    "raw_prompt": raw_prompt,
                    "normalized_prompt": normalized_prompt,
                    "cleaned_query": normalized_prompt,
                    "resolved_goal": normalized_prompt,
                    "sub_intent": "unanchored_affirmative",
                    "fast_path_matched": True,
                    "extracted_entity": "",
                    "is_affirmative": True
                }

            return {
                "intent": UserIntent.FILE_OPERATION,
                "confidence": 1.0,
                "raw_prompt": raw_prompt,
                "normalized_prompt": resolved,
                "cleaned_query": resolved,
                "resolved_goal": resolved,
                "sub_intent": sub,
                "fast_path_matched": True,
                "extracted_entity": resolved,
                "is_affirmative": True
            }

        # No prior assistant context -> default to conversation
        return {
            "intent": UserIntent.CONVERSATION,
            "confidence": 0.95,
            "raw_prompt": raw_prompt,
            "normalized_prompt": normalized_prompt,
            "cleaned_query": normalized_prompt,
            "resolved_goal": normalized_prompt,
            "sub_intent": "unanchored_affirmative",
            "fast_path_matched": True,
            "extracted_entity": "",
            "is_affirmative": True
        }

    # -------------------------------------------------------------------------
    # 3. Conversational / Advice / Out-of-Scope Fast-Path
    # -------------------------------------------------------------------------
    for pattern in CONVERSATION_PATTERNS:
        if re.search(pattern, lower_norm):
            return {
                "intent": UserIntent.CONVERSATION,
                "confidence": 1.0,
                "raw_prompt": raw_prompt,
                "normalized_prompt": normalized_prompt,
                "cleaned_query": normalized_prompt,
                "resolved_goal": normalized_prompt,
                "sub_intent": "advice_or_chat",
                "fast_path_matched": True,
                "extracted_entity": "",
                "is_affirmative": False
            }

    # -------------------------------------------------------------------------
    # 4. Special Domain Fast-Paths (Deduplication, Organized Folders, Format Sorting)
    # -------------------------------------------------------------------------
    # 4a. View Organized Folders (Read-only status check)
    if any(k in lower_norm for k in (
        "view organized folders", "view organized folder", "see organized folders",
        "show organized folders", "check organized folders", "list organized folders",
        "inspect organized folders", "organized folders"
    )):
        return {
            "intent": UserIntent.QUERY_INFO,
            "confidence": 1.0,
            "raw_prompt": raw_prompt,
            "normalized_prompt": normalized_prompt,
            "cleaned_query": normalized_prompt,
            "resolved_goal": "view organized folders",
            "sub_intent": "view_organized_folders",
            "fast_path_matched": True,
            "extracted_entity": "organized folders",
            "is_affirmative": False
        }

    # 4b. Deduplication (Audit vs Clean)
    if any(k in lower_norm for k in ("duplicate", "duplicates", "deduplicate", "repeated file", "repeated files")):
        is_clean_dup = any(k in lower_norm for k in ("clean", "delete", "trash", "remove", "wipe", "sweep", "clear"))
        if is_clean_dup:
            return {
                "intent": UserIntent.FILE_OPERATION,
                "confidence": 1.0,
                "raw_prompt": raw_prompt,
                "normalized_prompt": normalized_prompt,
                "cleaned_query": "clean duplicate downloads",
                "resolved_goal": "clean duplicate downloads",
                "sub_intent": "deduplicate_clean",
                "fast_path_matched": True,
                "extracted_entity": "duplicates",
                "is_affirmative": False
            }
        else:
            return {
                "intent": UserIntent.QUERY_INFO,
                "confidence": 1.0,
                "raw_prompt": raw_prompt,
                "normalized_prompt": normalized_prompt,
                "cleaned_query": "find duplicate files",
                "resolved_goal": "find duplicate files",
                "sub_intent": "deduplicate_audit",
                "fast_path_matched": True,
                "extracted_entity": "duplicates",
                "is_affirmative": False
            }

    # 4c. Format-Based Sorting (Next Batch Execution vs Proposal Breakdown)
    if any(k in lower_norm for k in ("next batch", "process next batch", "continue batch", "run next batch")):
        return {
            "intent": UserIntent.FILE_OPERATION,
            "confidence": 1.0,
            "raw_prompt": raw_prompt,
            "normalized_prompt": normalized_prompt,
            "cleaned_query": "process next batch of files in download",
            "resolved_goal": "process next batch of files in download",
            "sub_intent": "format_batch_execute",
            "fast_path_matched": True,
            "extracted_entity": "Download",
            "is_affirmative": False
        }

    format_sorting_keywords = (
        "file types together", "file types", "by format", "by file type", "by type",
        "group by format", "group files by format", "sort files by format",
        "group by type", "sort by format", "sort format", "group format",
        "format subfolders", "group all files by format"
    )
    is_format_sort = (
        is_format_sorting_query(raw_prompt)
        or is_format_sorting_query(normalized_prompt)
        or (
            any(k in lower_norm for k in format_sorting_keywords)
            and any(w in lower_norm for w in ("sort", "group", "organize", "consolidate", "together", "separate", "move", "arrange", "put", "place"))
        )
    )
    if is_format_sort:
        # Format sorting initial request shows the breakdown table and asks for confirmation (type="conversation", actions=[])
        return {
            "intent": UserIntent.CONVERSATION,
            "confidence": 1.0,
            "raw_prompt": raw_prompt,
            "normalized_prompt": normalized_prompt,
            "cleaned_query": normalized_prompt,
            "resolved_goal": "propose format sorting breakdown",
            "sub_intent": "format_sorting_proposal",
            "fast_path_matched": True,
            "extracted_entity": "Download",
            "is_affirmative": False
        }

    # -------------------------------------------------------------------------
    # 5. Hybrid / Explicit Mutation Check vs Explicit Read-Only Query Check
    # -------------------------------------------------------------------------
    has_mutation_verb = any(re.search(rf"\b{re.escape(v)}\b", lower_norm) for v in MUTATION_VERBS)
    starts_with_query = any(lower_norm.startswith(q) for q in EXPLICIT_QUERY_PREFIXES)
    has_temporal_query_marker = any(k in lower_norm for k in (
        "latest download", "newest download", "oldest file", "oldest files",
        "newest file", "latest file", "download anything today", "receive yesterday",
        "files from today", "files from yesterday", "past 7 days", "list of files"
    ))

    # Rule from Phase 2 Section 5 (Trap #2):
    # Any presence of true mutation verbs upgrades hybrid requests ("Find and clean receipts", "Move files older than 30 days") to FILE_OPERATION.
    if has_mutation_verb:
        return {
            "intent": UserIntent.FILE_OPERATION,
            "confidence": 1.0,
            "raw_prompt": raw_prompt,
            "normalized_prompt": normalized_prompt,
            "cleaned_query": normalized_prompt,
            "resolved_goal": normalized_prompt,
            "sub_intent": "general_mutation",
            "fast_path_matched": True,
            "extracted_entity": parse_query_intent(normalized_prompt).get("cleaned_target", ""),
            "is_affirmative": False
        }

    if starts_with_query or has_temporal_query_marker:
        parsed_q = parse_query_intent(normalized_prompt)
        return {
            "intent": UserIntent.QUERY_INFO,
            "confidence": 1.0,
            "raw_prompt": raw_prompt,
            "normalized_prompt": normalized_prompt,
            "cleaned_query": parsed_q.get("cleaned_target") or normalized_prompt,
            "resolved_goal": normalized_prompt,
            "sub_intent": "read_only_search",
            "fast_path_matched": True,
            "extracted_entity": parsed_q.get("cleaned_target", ""),
            "is_affirmative": False
        }

    # Check if prompt is a bare document/topic search query (e.g. "48 laws of power", "Sumbal pass", "Java book")
    parsed_q = parse_query_intent(normalized_prompt)
    if parsed_q.get("is_directory_listing") or parsed_q.get("is_temporal"):
        return {
            "intent": UserIntent.QUERY_INFO,
            "confidence": 0.95,
            "raw_prompt": raw_prompt,
            "normalized_prompt": normalized_prompt,
            "cleaned_query": parsed_q.get("cleaned_target") or normalized_prompt,
            "resolved_goal": normalized_prompt,
            "sub_intent": "read_only_search",
            "fast_path_matched": True,
            "extracted_entity": parsed_q.get("cleaned_target", ""),
            "is_affirmative": False
        }

    # -------------------------------------------------------------------------
    # 6. Ambiguous Prompt -> Cheap Micro-LLM Classifier Fallback
    # -------------------------------------------------------------------------
    llm_res = call_gemini_micro_classifier(normalized_prompt, recent_context=last_asst[:250] if last_asst else "")
    if llm_res and isinstance(llm_res, dict) and llm_res.get("intent"):
        raw_intent_str = str(llm_res.get("intent", "")).upper().strip()
        target_ent = str(llm_res.get("target") or parsed_q.get("cleaned_target") or "").strip()
        if raw_intent_str in ("CONVERSATION", "QUERY_INFO", "FILE_OPERATION"):
            return {
                "intent": UserIntent(raw_intent_str),
                "confidence": 0.9,
                "raw_prompt": raw_prompt,
                "normalized_prompt": normalized_prompt,
                "cleaned_query": target_ent or normalized_prompt,
                "resolved_goal": normalized_prompt,
                "sub_intent": "llm_classified",
                "fast_path_matched": False,
                "extracted_entity": target_ent,
                "is_affirmative": False,
                "model_source": llm_res.get("_model_used", "gemini-3.5-flash-lite")
            }

    # -------------------------------------------------------------------------
    # 7. Offline Fallback when Gemini is Unavailable or Unconfigured
    # -------------------------------------------------------------------------
    if parsed_q.get("target_tokens"):
        return {
            "intent": UserIntent.QUERY_INFO,
            "confidence": 0.75,
            "raw_prompt": raw_prompt,
            "normalized_prompt": normalized_prompt,
            "cleaned_query": parsed_q.get("cleaned_target") or normalized_prompt,
            "resolved_goal": normalized_prompt,
            "sub_intent": "offline_keyword_search",
            "fast_path_matched": True,
            "extracted_entity": parsed_q.get("cleaned_target", ""),
            "is_affirmative": False
        }

    return {
        "intent": UserIntent.CONVERSATION,
        "confidence": 0.7,
        "raw_prompt": raw_prompt,
        "normalized_prompt": normalized_prompt,
        "cleaned_query": normalized_prompt,
        "resolved_goal": normalized_prompt,
        "sub_intent": "fallback_conversation",
        "fast_path_matched": True,
        "extracted_entity": "",
        "is_affirmative": False
    }
