import os
import time
import requests
from typing import Dict, List, Optional, Tuple
from storage_copilot import config

# Phase 9 Priority Order: Fast, high-quota Flash-Lite models first, cascading to full Flash models
GEMINI_MODELS = [
    "gemini-3.1-flash-lite",
    "gemini-3.5-flash-lite",
    "gemini-3.5-flash",
    "gemini-3.6-flash",
    "gemini-3.7-flash",
    "gemini-3.8-flash",
    "gemini-flash-latest"
]

FAST_TIER_MODELS = [
    "gemini-3.1-flash-lite",
    "gemini-3.5-flash-lite",
    "gemini-3.5-flash",
    "gemini-flash-latest"
]

DEEP_TIER_MODELS = [
    "gemini-3.1-flash-lite",
    "gemini-3.5-flash-lite",
    "gemini-3.5-flash",
    "gemini-3.6-flash",
    "gemini-3.7-flash",
    "gemini-3.8-flash",
    "gemini-flash-latest"
]

# Per-model cooldown circuit breaker: model_name -> unix_timestamp_unblocked
MODEL_COOLDOWN_UNTIL: Dict[str, float] = {}


def reset_model_cooldowns() -> None:
    """Clears all per-model cooldowns and resets global quota lock."""
    MODEL_COOLDOWN_UNTIL.clear()
    config.GEMINI_QUOTA_BLOCKED_UNTIL = 0.0


def mark_model_cooldown(model: str, seconds: float = 120.0) -> None:
    """Marks a single model as rate-limited without blinding working sibling models."""
    MODEL_COOLDOWN_UNTIL[model] = time.time() + seconds
    # Only set global GEMINI_QUOTA_BLOCKED_UNTIL if EVERY model in GEMINI_MODELS is currently on cooldown
    now = time.time()
    if all(MODEL_COOLDOWN_UNTIL.get(m, 0.0) > now for m in GEMINI_MODELS):
        earliest_unblock = min(MODEL_COOLDOWN_UNTIL.get(m, now + 60.0) for m in GEMINI_MODELS)
        config.GEMINI_QUOTA_BLOCKED_UNTIL = earliest_unblock


def get_healthy_models(task_tier: str = "fast") -> List[str]:
    """
    Returns ordered list of models not currently in cooldown for the requested task tier.
    If all models are in cooldown but global lock was manually cleared, returns the base list.
    """
    now = time.time()
    if now < config.GEMINI_QUOTA_BLOCKED_UNTIL:
        return []

    base_pool = FAST_TIER_MODELS if task_tier == "fast" else DEEP_TIER_MODELS
    healthy = [m for m in base_pool if MODEL_COOLDOWN_UNTIL.get(m, 0.0) <= now]
    # Include any remaining models from GEMINI_MODELS not in base_pool as final fallback
    for m in GEMINI_MODELS:
        if m not in healthy and MODEL_COOLDOWN_UNTIL.get(m, 0.0) <= now:
            healthy.append(m)
    return healthy


def is_gemini_available() -> bool:
    """Returns True if GEMINI_API_KEY is configured and at least one model is healthy."""
    api_key = (os.getenv("GEMINI_API_KEY") or "").strip()
    if not api_key or len(api_key) < 5:
        return False
    return len(get_healthy_models("fast")) > 0


def call_gemini_json(
    system_prompt: str,
    user_prompt: str,
    task_tier: str = "deep",
    timeout: int = 18
) -> dict:
    """
    Sends structured JSON prompt to Gemini API with per-model 429 cascading.
    Never aborts the entire model loop when a single model hits daily quota;
    cascades immediately to the next healthy model (e.g. gemini-3.1-flash-lite).
    """
    api_key = (os.getenv("GEMINI_API_KEY") or "").strip()
    if not api_key:
        return {}

    candidate_models = get_healthy_models(task_tier=task_tier)
    if not candidate_models:
        return {}

    payload = {
        "system_instruction": {"parts": [{"text": system_prompt}]},
        "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
        "generationConfig": {"temperature": 0.1, "responseMimeType": "application/json"}
    }

    from storage_copilot.execution.plan_builder import extract_json_plan

    for model in candidate_models:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
        try:
            resp = requests.post(url, json=payload, timeout=timeout)
            if resp.status_code == 200:
                data = resp.json()
                candidates = data.get("candidates", [])
                if candidates:
                    parts = candidates[0].get("content", {}).get("parts", [])
                    if parts:
                        text = parts[0].get("text", "")
                        try:
                            parsed = extract_json_plan(text)
                            if parsed and isinstance(parsed, dict):
                                parsed["_model_used"] = model
                                parsed["_is_llm"] = True
                                return parsed
                        except Exception:
                            continue
            elif resp.status_code == 429:
                resp_text = resp.text or ""
                if "GenerateRequestsPerDay" in resp_text or "quotaId" in resp_text or "RESOURCE_EXHAUSTED" in resp_text:
                    # Place ONLY this specific model on 5-minute cooldown and cascade to next model!
                    print(f"[Storage Copilot Gemini] Model '{model}' hit quota (429). Cascading to next healthy model...")
                    mark_model_cooldown(model, seconds=300.0)
                    continue
                mark_model_cooldown(model, seconds=15.0)
                continue
            elif resp.status_code in (404, 503):
                mark_model_cooldown(model, seconds=60.0)
                continue
            else:
                continue
        except Exception:
            continue

    return {}


def call_gemini_text_with_meta(
    system_prompt: str,
    user_prompt: str,
    task_tier: str = "fast",
    timeout: int = 15
) -> Tuple[str, Optional[str]]:
    """
    Sends conversational/markdown prompt to Gemini API with per-model 429 cascading.
    Returns (response_text, model_used).
    """
    api_key = (os.getenv("GEMINI_API_KEY") or "").strip()
    if not api_key:
        return "", None

    candidate_models = get_healthy_models(task_tier=task_tier)
    if not candidate_models:
        return "", None

    payload = {
        "system_instruction": {"parts": [{"text": system_prompt}]},
        "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
        "generationConfig": {"temperature": 0.2}
    }

    for model in candidate_models:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
        try:
            resp = requests.post(url, json=payload, timeout=timeout)
            if resp.status_code == 200:
                data = resp.json()
                candidates = data.get("candidates", [])
                if candidates:
                    parts = candidates[0].get("content", {}).get("parts", [])
                    if parts:
                        text = (parts[0].get("text") or "").strip()
                        if text:
                            return text, model
            elif resp.status_code == 429:
                resp_text = resp.text or ""
                if "GenerateRequestsPerDay" in resp_text or "quotaId" in resp_text or "RESOURCE_EXHAUSTED" in resp_text:
                    print(f"[Storage Copilot Gemini Text] Model '{model}' hit quota (429). Cascading to next model...")
                    mark_model_cooldown(model, seconds=300.0)
                    continue
                mark_model_cooldown(model, seconds=15.0)
                continue
            elif resp.status_code in (404, 503):
                mark_model_cooldown(model, seconds=60.0)
                continue
        except Exception:
            continue

    return "", None


def call_gemini_text(system_prompt: str, user_prompt: str) -> str:
    """Backward-compatible wrapper returning text string."""
    text, _ = call_gemini_text_with_meta(system_prompt, user_prompt, task_tier="fast")
    return text


def call_gemini_api(system_prompt: str, user_prompt: str) -> dict:
    """Legacy alias for call_gemini_json."""
    return call_gemini_json(system_prompt, user_prompt)


def call_gemini_micro_classifier(
    prompt: str,
    recent_context: str = "",
    session_state: Optional[Dict[str, Any]] = None
) -> dict:
    """
    Phase 9 AI-First Intent & Continuity Orchestrator (~80 tokens, temperature: 0.0).
    Uses FAST_TIER_MODELS (`gemini-3.1-flash-lite`, `gemini-3.5-flash-lite`) with automatic 429 cascade.
    Resolves typos, multi-turn affirmations ("Yes clean them"), and exact sub_intents.
    """
    api_key = (os.getenv("GEMINI_API_KEY") or "").strip()
    if not api_key:
        return {}

    candidate_models = get_healthy_models(task_tier="fast")
    if not candidate_models:
        return {}

    session_info = ""
    if session_state:
        session_info = (
            f"\nSession State: last_sub_intent={session_state.get('last_sub_intent')}, "
            f"include_near={session_state.get('include_near')}, "
            f"near_duplicates_count={session_state.get('near_duplicates_count', 0)}, "
            f"exact_duplicates_count={session_state.get('exact_duplicates_count', 0)}, "
            f"forgotten_files_count={session_state.get('forgotten_files_count', 0)}"
        )

    system_prompt = (
        "You are the AI-First Intent Orchestrator for Storage Copilot on Android.\n"
        "Classify the user's request (tolerating any spelling/typing errors and resolving multi-turn 'Yes'/'Proceed'/'Clean them' against Recent Context & Session State):\n"
        "1. intent: 'CONVERSATION' | 'QUERY_INFO' | 'FILE_OPERATION'\n"
        "   - CONVERSATION: greetings, asking for capabilities/suggestions/advice, out-of-scope requests (sending email/SMS/web), or initial request to group files by format ('sort file types together').\n"
        "   - QUERY_INFO: finding/searching/listing files, reading notes, checking dates/oldest/newest, viewing organized folders, or auditing duplicates/forgotten files WITHOUT moving/deleting them.\n"
        "   - FILE_OPERATION: organizing, sorting, cleaning, moving, renaming, archiving, separating peers, trashing duplicates/forgotten files, or confirming a prior proposal ('Yes', 'Proceed', 'Do it').\n"
        "2. sub_intent (pick best match):\n"
        "   - For CONVERSATION: 'greeting_or_capability', 'advice_or_chat', 'format_sorting_proposal'\n"
        "   - For QUERY_INFO: 'read_only_search', 'deduplicate_audit', 'deduplicate_near_audit', 'forgotten_audit', 'view_organized_folders'\n"
        "   - For FILE_OPERATION: 'general_mutation', 'deduplicate_clean', 'deduplicate_near_clean', 'forgotten_clean', 'format_batch_execute', 'peer_separation', 'voucher_sorting', 'academic_organization'\n"
        "3. include_near: true if user mentions near/similar/revised duplicates OR if confirming a duplicate cleanup when Session State has include_near=true or near_duplicates_count > 0.\n"
        'Return strict JSON: {"intent": "...", "sub_intent": "...", "include_near": bool, "resolved_goal": "clean normalized goal", "target": "extracted subject"}'
    )

    user_text = f"User Prompt: {prompt}\nRecent Assistant Turn: {recent_context}{session_info}"

    payload = {
        "system_instruction": {"parts": [{"text": system_prompt}]},
        "contents": [{"role": "user", "parts": [{"text": user_text}]}],
        "generationConfig": {
            "temperature": 0.0,
            "maxOutputTokens": 110,
            "responseMimeType": "application/json"
        }
    }

    from storage_copilot.execution.plan_builder import extract_json_plan

    for model in candidate_models:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
        try:
            resp = requests.post(url, json=payload, timeout=8)
            if resp.status_code == 200:
                data = resp.json()
                candidates = data.get("candidates", [])
                if candidates:
                    parts = candidates[0].get("content", {}).get("parts", [])
                    if parts:
                        parsed = extract_json_plan(parts[0].get("text", ""))
                        if parsed and isinstance(parsed, dict) and "intent" in parsed:
                            parsed["_model_used"] = model
                            parsed["_is_llm"] = True
                            return parsed
            elif resp.status_code == 429:
                resp_text = resp.text or ""
                if "GenerateRequestsPerDay" in resp_text or "quotaId" in resp_text or "RESOURCE_EXHAUSTED" in resp_text:
                    mark_model_cooldown(model, seconds=300.0)
                    continue
                mark_model_cooldown(model, seconds=15.0)
                continue
            elif resp.status_code in (404, 503):
                mark_model_cooldown(model, seconds=60.0)
                continue
        except Exception:
            continue

    return {}
