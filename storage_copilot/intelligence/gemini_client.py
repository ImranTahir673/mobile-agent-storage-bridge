import os
import time
import requests
from typing import Any, Dict, List, Optional, Tuple
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

# Fine-grained failure diagnostics for honest fallback notices
_last_fallback_info: Dict[str, Any] = {
    "reason": "no_api_key",
    "notice": "AI unavailable right now, using on-device fallback"
}


def set_last_fallback_reason(reason: str, notice: Optional[str] = None) -> None:
    """Sets the fine-grained fallback reason and human-readable notice."""
    global _last_fallback_info
    notices = {
        "no_api_key": "AI unavailable right now, using on-device fallback",
        "no_internet": "AI unavailable right now (no internet connection), using on-device fallback",
        "daily_quota_exceeded": "AI unavailable right now (daily Gemini quota reached), using on-device fallback",
        "rate_limited": "AI unavailable right now (temporary rate limit), using on-device fallback",
    }
    _last_fallback_info = {
        "reason": reason,
        "notice": notice or notices.get(reason, f"AI unavailable right now ({reason}), using on-device fallback")
    }


def get_last_fallback_info() -> Dict[str, Any]:
    """Returns fine-grained fallback diagnostics: degraded_fallback, fallback_reason, and fallback_notice."""
    global _last_fallback_info
    api_key = (os.getenv("GEMINI_API_KEY") or "").strip()
    if not api_key or len(api_key) < 5:
        return {
            "degraded_fallback": True,
            "fallback_reason": "no_api_key",
            "fallback_notice": "AI unavailable right now, using on-device fallback"
        }
    return {
        "degraded_fallback": True,
        "fallback_reason": _last_fallback_info.get("reason", "rate_limited"),
        "fallback_notice": _last_fallback_info.get("notice", "AI unavailable right now, using on-device fallback")
    }


def mark_ai_success() -> None:
    """Resets fallback reason on successful AI response."""
    global _last_fallback_info
    _last_fallback_info = {
        "reason": "none",
        "notice": ""
    }


def reset_model_cooldowns() -> None:
    """Clears all per-model cooldowns and resets global quota lock."""
    MODEL_COOLDOWN_UNTIL.clear()
    config.GEMINI_QUOTA_BLOCKED_UNTIL = 0.0


def mark_model_cooldown(model: str, seconds: float = 120.0) -> None:
    """Marks a single model as rate-limited without blinding working sibling models."""
    MODEL_COOLDOWN_UNTIL[model] = time.time() + seconds
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
    for m in GEMINI_MODELS:
        if m not in healthy and MODEL_COOLDOWN_UNTIL.get(m, 0.0) <= now:
            healthy.append(m)
    return healthy


def is_gemini_available() -> bool:
    """Returns True if GEMINI_API_KEY is configured and at least one model is healthy."""
    api_key = (os.getenv("GEMINI_API_KEY") or "").strip()
    if not api_key or len(api_key) < 5:
        set_last_fallback_reason("no_api_key")
        return False
    healthy = get_healthy_models("fast")
    if not healthy:
        now = time.time()
        if any(MODEL_COOLDOWN_UNTIL.get(m, 0.0) - now > 180.0 for m in GEMINI_MODELS):
            set_last_fallback_reason("daily_quota_exceeded")
        else:
            set_last_fallback_reason("rate_limited")
        return False
    return True


class InferenceProvider:
    """Abstract interface seam for LLM inference providers."""
    def generate_json(self, system_prompt: str, user_prompt: str, task_tier: str = "deep", timeout: int = 18) -> dict:
        raise NotImplementedError

    def generate_text_with_meta(self, system_prompt: str, user_prompt: str, task_tier: str = "fast", timeout: int = 15) -> Tuple[str, Optional[str]]:
        raise NotImplementedError

    def classify_micro(self, prompt: str, recent_context: str = "", session_state: Optional[Dict[str, Any]] = None) -> dict:
        raise NotImplementedError


class DirectGeminiProvider(InferenceProvider):
    """Direct Google Generative Language REST API provider with per-model 429 cascading."""
    def generate_json(
        self,
        system_prompt: str,
        user_prompt: str,
        task_tier: str = "deep",
        timeout: int = 18
    ) -> dict:
        api_key = (os.getenv("GEMINI_API_KEY") or "").strip()
        if not api_key:
            set_last_fallback_reason("no_api_key")
            return {}

        candidate_models = get_healthy_models(task_tier=task_tier)
        if not candidate_models:
            set_last_fallback_reason("rate_limited")
            return {}

        payload = {
            "system_instruction": {"parts": [{"text": system_prompt}]},
            "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
            "generationConfig": {"temperature": 0.1, "responseMimeType": "application/json"}
        }

        from storage_copilot.execution.plan_builder import extract_json_plan

        saw_network_error = False
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
                                    mark_ai_success()
                                    return parsed
                            except Exception:
                                continue
                elif resp.status_code == 429:
                    resp_text = resp.text or ""
                    if "GenerateRequestsPerDay" in resp_text or "quotaId" in resp_text or "RESOURCE_EXHAUSTED" in resp_text:
                        print(f"[Storage Copilot Gemini] Model '{model}' hit quota (429). Cascading to next healthy model...")
                        mark_model_cooldown(model, seconds=300.0)
                        set_last_fallback_reason("daily_quota_exceeded")
                        continue
                    set_last_fallback_reason("rate_limited")
                    mark_model_cooldown(model, seconds=15.0)
                    continue
                elif resp.status_code in (404, 503):
                    mark_model_cooldown(model, seconds=60.0)
                    set_last_fallback_reason("rate_limited")
                    continue
                else:
                    set_last_fallback_reason("rate_limited")
                    continue
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
                saw_network_error = True
                set_last_fallback_reason("no_internet")
                continue
            except Exception:
                saw_network_error = True
                set_last_fallback_reason("no_internet")
                continue

        if saw_network_error:
            set_last_fallback_reason("no_internet")
        return {}

    def generate_text_with_meta(
        self,
        system_prompt: str,
        user_prompt: str,
        task_tier: str = "fast",
        timeout: int = 15
    ) -> Tuple[str, Optional[str]]:
        api_key = (os.getenv("GEMINI_API_KEY") or "").strip()
        if not api_key:
            set_last_fallback_reason("no_api_key")
            return "", None

        candidate_models = get_healthy_models(task_tier=task_tier)
        if not candidate_models:
            set_last_fallback_reason("rate_limited")
            return "", None

        payload = {
            "system_instruction": {"parts": [{"text": system_prompt}]},
            "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
            "generationConfig": {"temperature": 0.2}
        }

        saw_network_error = False
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
                                mark_ai_success()
                                return text, model
                elif resp.status_code == 429:
                    resp_text = resp.text or ""
                    if "GenerateRequestsPerDay" in resp_text or "quotaId" in resp_text or "RESOURCE_EXHAUSTED" in resp_text:
                        print(f"[Storage Copilot Gemini Text] Model '{model}' hit quota (429). Cascading to next model...")
                        mark_model_cooldown(model, seconds=300.0)
                        set_last_fallback_reason("daily_quota_exceeded")
                        continue
                    set_last_fallback_reason("rate_limited")
                    mark_model_cooldown(model, seconds=15.0)
                    continue
                elif resp.status_code in (404, 503):
                    mark_model_cooldown(model, seconds=60.0)
                    set_last_fallback_reason("rate_limited")
                    continue
                else:
                    set_last_fallback_reason("rate_limited")
                    continue
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
                saw_network_error = True
                set_last_fallback_reason("no_internet")
                continue
            except Exception:
                saw_network_error = True
                set_last_fallback_reason("no_internet")
                continue

        if saw_network_error:
            set_last_fallback_reason("no_internet")
        return "", None

    def classify_micro(
        self,
        prompt: str,
        recent_context: str = "",
        session_state: Optional[Dict[str, Any]] = None
    ) -> dict:
        api_key = (os.getenv("GEMINI_API_KEY") or "").strip()
        if not api_key:
            set_last_fallback_reason("no_api_key")
            return {}

        candidate_models = get_healthy_models(task_tier="fast")
        if not candidate_models:
            set_last_fallback_reason("rate_limited")
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

        saw_network_error = False
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
                                mark_ai_success()
                                return parsed
                elif resp.status_code == 429:
                    resp_text = resp.text or ""
                    if "GenerateRequestsPerDay" in resp_text or "quotaId" in resp_text or "RESOURCE_EXHAUSTED" in resp_text:
                        mark_model_cooldown(model, seconds=300.0)
                        set_last_fallback_reason("daily_quota_exceeded")
                        continue
                    set_last_fallback_reason("rate_limited")
                    mark_model_cooldown(model, seconds=15.0)
                    continue
                elif resp.status_code in (404, 503):
                    mark_model_cooldown(model, seconds=60.0)
                    set_last_fallback_reason("rate_limited")
                    continue
                else:
                    set_last_fallback_reason("rate_limited")
                    continue
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
                saw_network_error = True
                set_last_fallback_reason("no_internet")
                continue
            except Exception:
                saw_network_error = True
                set_last_fallback_reason("no_internet")
                continue

        if saw_network_error:
            set_last_fallback_reason("no_internet")
        return {}


_default_provider: InferenceProvider = DirectGeminiProvider()


def get_inference_provider() -> InferenceProvider:
    """Returns the active LLM inference provider."""
    global _default_provider
    return _default_provider


def set_inference_provider(provider: InferenceProvider) -> None:
    """Sets custom LLM inference provider for testing or alternative backends."""
    global _default_provider
    _default_provider = provider


def call_gemini_json(
    system_prompt: str,
    user_prompt: str,
    task_tier: str = "deep",
    timeout: int = 18
) -> dict:
    """Sends structured JSON prompt to Gemini API via active InferenceProvider."""
    return _default_provider.generate_json(system_prompt, user_prompt, task_tier=task_tier, timeout=timeout)


def call_gemini_text_with_meta(
    system_prompt: str,
    user_prompt: str,
    task_tier: str = "fast",
    timeout: int = 15
) -> Tuple[str, Optional[str]]:
    """Sends conversational prompt to Gemini API via active InferenceProvider."""
    return _default_provider.generate_text_with_meta(system_prompt, user_prompt, task_tier=task_tier, timeout=timeout)


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
    """Phase 9 Intent Orchestrator delegating to active InferenceProvider."""
    return _default_provider.classify_micro(prompt, recent_context=recent_context, session_state=session_state)
