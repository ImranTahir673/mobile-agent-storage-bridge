import re
from enum import Enum
from typing import Dict, Any, List, Optional

from storage_copilot.execution.plan_builder import normalize_user_prompt
from storage_copilot.intelligence.document_classifier import parse_query_intent
from storage_copilot.intelligence.gemini_client import (
    call_gemini_micro_classifier,
    is_gemini_available
)
from storage_copilot.intelligence.session_context import session_context_store
from storage_copilot.advice.advisor import is_format_sorting_query


class UserIntent(str, Enum):
    CONVERSATION = "CONVERSATION"
    QUERY_INFO = "QUERY_INFO"
    FILE_OPERATION = "FILE_OPERATION"


TRIVIAL_REFLEX_EXACT = {
    "hi", "hii", "hiii", "hello", "hey", "heyy", "greetings",
    "thanks", "thank you", "cancel", "no", "nope"
}

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


def _resolve_continuity_sub_intent(
    lower_norm: str,
    lower_asst: str,
    session_ctx: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    """
    Resolves multi-turn follow-ups ('Yes', 'Proceed', 'Yes clean all the duplicate files', 'Clean them')
    using BOTH conversation_history AND the stateful SessionContextStore.
    """
    last_sub = session_ctx.get("last_sub_intent") or ""
    inc_near = bool(session_ctx.get("include_near") or session_ctx.get("near_duplicates_count", 0) > 0)

    # Check if user explicitly mentions duplicates or confirms after a duplicate audit
    if any(k in lower_norm for k in ("duplicate", "duplicates", "deduplicate")):
        is_clean = any(k in lower_norm for k in ("clean", "delete", "trash", "remove", "wipe", "sweep", "clear", "yes", "proceed"))
        is_near = inc_near or any(k in lower_norm for k in ("near", "similar", "fuzzy", "revised", "jaccard", "deep")) or ("near-duplicate" in lower_asst) or ("jaccard" in lower_asst)
        if is_clean:
            return {
                "intent": UserIntent.FILE_OPERATION,
                "sub_intent": "deduplicate_near_clean" if is_near else "deduplicate_clean",
                "resolved_goal": "clean near duplicate downloads" if is_near else "clean duplicate downloads",
                "include_near": is_near,
            }

    # Check if user confirms with 'Yes' / 'Proceed' / 'Clean them'
    clean_p_start = re.sub(r"^[^\w]+", "", lower_norm).strip()
    is_affirmative_or_pronoun = (
        clean_p_start in AFFIRMATIVE_WORDS
        or bool(re.search(r"^(?:yes|yeah|yep|sure|ok|okay|go ahead|proceed|do it|please do)\b", clean_p_start))
        or bool(re.search(r"\b(?:clean|trash|delete|remove|move|sort)\s+(?:them|those|all\s+of\s+them|these)\b", lower_norm))
    )

    if not is_affirmative_or_pronoun:
        return None

    # If user attached an explicit mutation command after 'Yes' (e.g. 'yes clean all the files in the downloads')
    remainder = re.sub(
        r"^(?:yes|yeah|yep|sure|ok|okay|go ahead|proceed|do it|please do)[\s,!.]*",
        "",
        clean_p_start
    ).strip()

    if remainder and remainder not in ("move all files", "clean them", "clean those", "do it"):
        if any(k in remainder for k in ("duplicate", "duplicates", "deduplicate")):
            is_near = inc_near or ("near" in remainder) or ("near-duplicate" in lower_asst) or ("jaccard" in lower_asst)
            return {
                "intent": UserIntent.FILE_OPERATION,
                "sub_intent": "deduplicate_near_clean" if is_near else "deduplicate_clean",
                "resolved_goal": "clean near duplicate downloads" if is_near else "clean duplicate downloads",
                "include_near": is_near,
            }
        if any(k in remainder for k in ("forgotten", "stale")):
            return {
                "intent": UserIntent.FILE_OPERATION,
                "sub_intent": "forgotten_clean",
                "resolved_goal": "clean forgotten files",
                "include_near": False,
            }
        if any(v in remainder for v in MUTATION_VERBS):
            return {
                "intent": UserIntent.FILE_OPERATION,
                "sub_intent": "general_mutation",
                "resolved_goal": remainder,
                "include_near": inc_near,
            }

    if "move all files" in lower_norm:
        return {
            "intent": UserIntent.FILE_OPERATION,
            "sub_intent": "format_batch_execute",
            "resolved_goal": "process next batch of files in download",
            "include_near": False,
        }

    # Check assistant message or session_ctx from previous turn
    if "forgotten files audit" in lower_asst or "clean forgotten files" in lower_asst or last_sub == "forgotten_audit":
        return {
            "intent": UserIntent.FILE_OPERATION,
            "sub_intent": "forgotten_clean",
            "resolved_goal": "clean forgotten files",
            "include_near": False,
        }
    if (
        "duplicate file audit" in lower_asst
        or "clean up these redundant copies" in lower_asst
        or "redundant duplicate" in lower_asst
        or last_sub in ("deduplicate_audit", "deduplicate_near_audit")
    ):
        is_near = inc_near or ("near-duplicate" in lower_asst) or ("jaccard" in lower_asst) or (last_sub == "deduplicate_near_audit")
        return {
            "intent": UserIntent.FILE_OPERATION,
            "sub_intent": "deduplicate_near_clean" if is_near else "deduplicate_clean",
            "resolved_goal": "clean near duplicate downloads" if is_near else "clean duplicate downloads",
            "include_near": is_near,
        }
    if any(p in lower_asst for p in ("peer document", "peer files", "separate peer", "classmate", "peer separation")):
        return {
            "intent": UserIntent.FILE_OPERATION,
            "sub_intent": "peer_separation",
            "resolved_goal": "separate peer documents",
            "include_near": False,
        }
    if any(p in lower_asst for p in ("tuition voucher", "fee voucher", "fee slip", "student voucher", "student vouchers")):
        return {
            "intent": UserIntent.FILE_OPERATION,
            "sub_intent": "voucher_sorting",
            "resolved_goal": "sort student vouchers",
            "include_near": False,
        }
    if any(p in lower_asst for p in ("file format", "format subfolders", "group by format", "by format", "respective folders", "unorganized files", "files ready to organize")) or last_sub == "format_sorting_proposal":
        return {
            "intent": UserIntent.FILE_OPERATION,
            "sub_intent": "format_batch_execute",
            "resolved_goal": "process next batch of files in download",
            "include_near": False,
        }
    if any(p in lower_asst for p in ("university", "academic", "organize", "recommend", "plan", "voucher")):
        return {
            "intent": UserIntent.FILE_OPERATION,
            "sub_intent": "academic_organization",
            "resolved_goal": "organize all my university files",
            "include_near": False,
        }

    return None


def classify_intent(
    prompt: str,
    conversation_history: Optional[List[Dict[str, Any]]] = None,
    recent_context: Optional[str] = None,
    available_files_count: int = 0,
    target_folder: str = "Download"
) -> Dict[str, Any]:
    """
    Phase 9 AI-First Intent Orchestrator with Conversational Continuity & Offline Safety Net:
    1. Normalizes mobile typos ('cleen', 'teh', 'fils', 'downlods').
    2. Loads multi-turn session state from `session_context_store`.
    3. When Gemini API key is configured & healthy, consults the AI-First Orchestrator (`flash-lite` cascade)
       for nuanced natural language & multi-turn context while honoring strict safety invariants.
    4. Falls back seamlessly to deterministic heuristics when offline or in unit-test mode.
    """
    raw_prompt = (prompt or "").strip()
    normalized_prompt = normalize_user_prompt(raw_prompt)
    lower_raw = raw_prompt.lower()
    lower_norm = normalized_prompt.lower()

    clean_p_exact = re.sub(r"[^\w\s?]", "", lower_norm).strip()
    last_asst = _extract_last_assistant_message(conversation_history) or (recent_context or "")
    lower_asst = last_asst.lower()

    session_ctx = session_context_store.get_context(target_folder)

    # 1. Trivial 1-word Greeting / Cancel Reflex (0ms)
    if not raw_prompt or clean_p_exact in TRIVIAL_REFLEX_EXACT:
        return {
            "intent": UserIntent.CONVERSATION,
            "confidence": 1.0,
            "raw_prompt": raw_prompt,
            "normalized_prompt": normalized_prompt,
            "cleaned_query": normalized_prompt,
            "resolved_goal": normalized_prompt,
            "sub_intent": "greeting_or_capability",
            "include_near": False,
            "fast_path_matched": True,
            "is_llm": False,
            "model_source": "offline-heuristic",
            "extracted_entity": "",
            "is_affirmative": False
        }

    # 2. AI-First Orchestration Mode (Active whenever Gemini API Key is configured & healthy)
    if is_gemini_available():
        llm_res = call_gemini_micro_classifier(
            normalized_prompt,
            recent_context=last_asst[:350] if last_asst else "",
            session_state=session_ctx
        )
        if llm_res and isinstance(llm_res, dict) and llm_res.get("intent"):
            raw_intent_str = str(llm_res.get("intent", "")).upper().strip()
            if raw_intent_str in ("CONVERSATION", "QUERY_INFO", "FILE_OPERATION"):
                intent_enum = UserIntent(raw_intent_str)
                sub_intent = str(llm_res.get("sub_intent") or "").strip()
                resolved_goal = str(llm_res.get("resolved_goal") or normalized_prompt).strip()
                target_ent = str(llm_res.get("target") or "").strip()
                inc_near = bool(
                    llm_res.get("include_near", False)
                    or session_ctx.get("include_near", False)
                    or ("near" in lower_norm)
                )
                model_used = llm_res.get("_model_used", "gemini-3.1-flash-lite")

                # Safety guard: format sorting initial request always proposes breakdown table first
                if is_format_sorting_query(raw_prompt) or is_format_sorting_query(normalized_prompt):
                    if not any(w in lower_norm for w in ("yes", "proceed", "next batch")):
                        intent_enum = UserIntent.CONVERSATION
                        sub_intent = "format_sorting_proposal"

                return {
                    "intent": intent_enum,
                    "confidence": 0.96,
                    "raw_prompt": raw_prompt,
                    "normalized_prompt": normalized_prompt,
                    "cleaned_query": target_ent or resolved_goal,
                    "resolved_goal": resolved_goal,
                    "sub_intent": sub_intent or "llm_classified",
                    "include_near": inc_near,
                    "fast_path_matched": False,
                    "is_llm": True,
                    "model_source": model_used,
                    "extracted_entity": target_ent,
                    "is_affirmative": False
                }

    # =========================================================================
    # 3. Deterministic Continuity & Offline Safety Net (When API key is absent or all models 429)
    # =========================================================================
    continuity_match = _resolve_continuity_sub_intent(lower_norm, lower_asst, session_ctx)
    if continuity_match is not None:
        return {
            "intent": continuity_match["intent"],
            "confidence": 1.0,
            "raw_prompt": raw_prompt,
            "normalized_prompt": continuity_match["resolved_goal"],
            "cleaned_query": continuity_match["resolved_goal"],
            "resolved_goal": continuity_match["resolved_goal"],
            "sub_intent": continuity_match["sub_intent"],
            "include_near": continuity_match.get("include_near", False),
            "fast_path_matched": True,
            "is_llm": False,
            "model_source": "continuity-engine",
            "extracted_entity": continuity_match["resolved_goal"],
            "is_affirmative": True
        }

    # =========================================================================
    # 4. Deterministic Offline Safety Net (When API key is absent or all models 429)
    # =========================================================================
    if clean_p_exact in GREETING_AND_CHAT_EXACT or lower_norm in GREETING_AND_CHAT_EXACT:
        return {
            "intent": UserIntent.CONVERSATION,
            "confidence": 1.0,
            "raw_prompt": raw_prompt,
            "normalized_prompt": normalized_prompt,
            "cleaned_query": normalized_prompt,
            "resolved_goal": normalized_prompt,
            "sub_intent": "greeting_or_capability",
            "include_near": False,
            "fast_path_matched": True,
            "is_llm": False,
            "model_source": "offline-heuristic",
            "extracted_entity": "",
            "is_affirmative": False
        }

    clean_p_start = re.sub(r"^[^\w]+", "", lower_raw).strip()
    is_pure_affirmative = (
        clean_p_start in AFFIRMATIVE_WORDS
        or lower_norm in AFFIRMATIVE_WORDS
        or clean_p_exact in AFFIRMATIVE_WORDS
        or bool(re.search(r"^(?:yes|yeah|yep|sure|ok|okay|go ahead|proceed|do it|please do)\b", clean_p_start))
    )
    if is_pure_affirmative:
        return {
            "intent": UserIntent.CONVERSATION,
            "confidence": 0.95,
            "raw_prompt": raw_prompt,
            "normalized_prompt": normalized_prompt,
            "cleaned_query": normalized_prompt,
            "resolved_goal": normalized_prompt,
            "sub_intent": "unanchored_affirmative",
            "include_near": False,
            "fast_path_matched": True,
            "is_llm": False,
            "model_source": "offline-heuristic",
            "extracted_entity": "",
            "is_affirmative": True
        }

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
                "include_near": False,
                "fast_path_matched": True,
                "is_llm": False,
                "model_source": "offline-heuristic",
                "extracted_entity": "",
                "is_affirmative": False
            }

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
            "include_near": False,
            "fast_path_matched": True,
            "is_llm": False,
            "model_source": "offline-heuristic",
            "extracted_entity": "organized folders",
            "is_affirmative": False
        }

    if any(k in lower_norm for k in (
        "forgotten file", "forgotten files", "forgotten download", "forgotten downloads",
        "stale file", "stale files", "stale download", "stale downloads", "orphaned screenshot", "orphaned screenshots"
    )):
        is_clean_forgotten = any(k in lower_norm for k in ("clean", "cleanup", "delete", "trash", "remove", "wipe", "sweep", "clear"))
        return {
            "intent": UserIntent.FILE_OPERATION if is_clean_forgotten else UserIntent.QUERY_INFO,
            "confidence": 1.0,
            "raw_prompt": raw_prompt,
            "normalized_prompt": normalized_prompt,
            "cleaned_query": "clean forgotten files" if is_clean_forgotten else "find forgotten files",
            "resolved_goal": "clean forgotten files" if is_clean_forgotten else "find forgotten files",
            "sub_intent": "forgotten_clean" if is_clean_forgotten else "forgotten_audit",
            "include_near": False,
            "fast_path_matched": True,
            "is_llm": False,
            "model_source": "offline-heuristic",
            "extracted_entity": "forgotten files",
            "is_affirmative": False
        }

    if any(k in lower_norm for k in ("duplicate", "duplicates", "deduplicate", "repeated file", "repeated files", "near duplicate", "near duplicates")):
        is_clean_dup = any(k in lower_norm for k in ("clean", "delete", "trash", "remove", "wipe", "sweep", "clear"))
        is_near_dup = (
            any(k in lower_norm for k in ("near", "similar", "fuzzy", "revised", "jaccard", "deep"))
            or bool(session_ctx.get("include_near") and is_clean_dup)
        )
        if is_clean_dup:
            return {
                "intent": UserIntent.FILE_OPERATION,
                "confidence": 1.0,
                "raw_prompt": raw_prompt,
                "normalized_prompt": normalized_prompt,
                "cleaned_query": "clean near duplicate downloads" if is_near_dup else "clean duplicate downloads",
                "resolved_goal": "clean near duplicate downloads" if is_near_dup else "clean duplicate downloads",
                "sub_intent": "deduplicate_near_clean" if is_near_dup else "deduplicate_clean",
                "include_near": is_near_dup,
                "fast_path_matched": True,
                "is_llm": False,
                "model_source": "offline-heuristic",
                "extracted_entity": "duplicates",
                "is_affirmative": False
            }
        else:
            return {
                "intent": UserIntent.QUERY_INFO,
                "confidence": 1.0,
                "raw_prompt": raw_prompt,
                "normalized_prompt": normalized_prompt,
                "cleaned_query": "find near duplicate files" if is_near_dup else "find duplicate files",
                "resolved_goal": "find near duplicate files" if is_near_dup else "find duplicate files",
                "sub_intent": "deduplicate_near_audit" if is_near_dup else "deduplicate_audit",
                "include_near": is_near_dup,
                "fast_path_matched": True,
                "is_llm": False,
                "model_source": "offline-heuristic",
                "extracted_entity": "duplicates",
                "is_affirmative": False
            }

    if any(k in lower_norm for k in ("next batch", "process next batch", "continue batch", "run next batch")):
        return {
            "intent": UserIntent.FILE_OPERATION,
            "confidence": 1.0,
            "raw_prompt": raw_prompt,
            "normalized_prompt": normalized_prompt,
            "cleaned_query": "process next batch of files in download",
            "resolved_goal": "process next batch of files in download",
            "sub_intent": "format_batch_execute",
            "include_near": False,
            "fast_path_matched": True,
            "is_llm": False,
            "model_source": "offline-heuristic",
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
        return {
            "intent": UserIntent.CONVERSATION,
            "confidence": 1.0,
            "raw_prompt": raw_prompt,
            "normalized_prompt": normalized_prompt,
            "cleaned_query": normalized_prompt,
            "resolved_goal": "propose format sorting breakdown",
            "sub_intent": "format_sorting_proposal",
            "include_near": False,
            "fast_path_matched": True,
            "is_llm": False,
            "model_source": "offline-heuristic",
            "extracted_entity": "Download",
            "is_affirmative": False
        }

    has_mutation_verb = any(re.search(rf"\b{re.escape(v)}\b", lower_norm) for v in MUTATION_VERBS)
    starts_with_query = any(lower_norm.startswith(q) for q in EXPLICIT_QUERY_PREFIXES)
    has_temporal_query_marker = any(k in lower_norm for k in (
        "latest download", "newest download", "oldest file", "oldest files",
        "newest file", "latest file", "download anything today", "receive yesterday",
        "files from today", "files from yesterday", "past 7 days", "list of files"
    ))

    if has_mutation_verb:
        return {
            "intent": UserIntent.FILE_OPERATION,
            "confidence": 1.0,
            "raw_prompt": raw_prompt,
            "normalized_prompt": normalized_prompt,
            "cleaned_query": normalized_prompt,
            "resolved_goal": normalized_prompt,
            "sub_intent": "general_mutation",
            "include_near": False,
            "fast_path_matched": True,
            "is_llm": False,
            "model_source": "offline-heuristic",
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
            "include_near": False,
            "fast_path_matched": True,
            "is_llm": False,
            "model_source": "offline-heuristic",
            "extracted_entity": parsed_q.get("cleaned_target", ""),
            "is_affirmative": False
        }

    parsed_q = parse_query_intent(normalized_prompt)
    if parsed_q.get("is_directory_listing") or parsed_q.get("is_temporal") or parsed_q.get("target_tokens"):
        return {
            "intent": UserIntent.QUERY_INFO,
            "confidence": 0.85,
            "raw_prompt": raw_prompt,
            "normalized_prompt": normalized_prompt,
            "cleaned_query": parsed_q.get("cleaned_target") or normalized_prompt,
            "resolved_goal": normalized_prompt,
            "sub_intent": "read_only_search",
            "include_near": False,
            "fast_path_matched": True,
            "is_llm": False,
            "model_source": "offline-heuristic",
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
        "include_near": False,
        "fast_path_matched": True,
        "is_llm": False,
        "model_source": "offline-heuristic",
        "extracted_entity": "",
        "is_affirmative": False
    }
