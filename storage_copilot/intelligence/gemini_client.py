import os
import time
import requests
from storage_copilot import config
from storage_copilot.execution.plan_builder import extract_json_plan

GEMINI_MODELS = [
    "gemini-3.5-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
    "gemini-3.7-flash",
    "gemini-3.8-flash",
    "gemini-flash-latest"
]

def call_gemini_json(system_prompt: str, user_prompt: str) -> dict:
    """
    Sends structured JSON prompt to Gemini API with robust model fallback and backoff retry.
    Fast-switches to local deterministic engine if daily project quota (20 req/day) is exhausted.
    """
    if time.time() < config.GEMINI_QUOTA_BLOCKED_UNTIL:
        return {}

    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        print("[Storage Copilot] GEMINI_API_KEY not configured. Running in Local Offline Intelligence mode.")
        return {}

    payload = {
        "system_instruction": {"parts": [{"text": system_prompt}]},
        "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
        "generationConfig": {"temperature": 0.1, "responseMimeType": "application/json"}
    }

    for model in GEMINI_MODELS:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
        try:
            resp = requests.post(url, json=payload, timeout=20)
            if resp.status_code == 200:
                data = resp.json()
                candidates = data.get("candidates", [])
                if candidates:
                    parts = candidates[0].get("content", {}).get("parts", [])
                    if parts:
                        text = parts[0].get("text", "")
                        try:
                            parsed = extract_json_plan(text)
                            if parsed:
                                parsed["_model_used"] = model
                                parsed["_is_llm"] = True
                                print(f"[Storage Copilot Gemini] Successfully received response via model '{model}'.")
                                return parsed
                        except Exception:
                            continue
            elif resp.status_code == 429:
                resp_text = resp.text
                if "GenerateRequestsPerDay" in resp_text or "quotaId" in resp_text or "RESOURCE_EXHAUSTED" in resp_text:
                    print(f"[Storage Copilot Gemini] Daily free-tier quota (20 req/day) reached for project on '{model}'. Fast-switching to deterministic local engine.")
                    config.GEMINI_QUOTA_BLOCKED_UNTIL = time.time() + 900
                    return {}
                print(f"[Storage Copilot Gemini] Model '{model}' returned HTTP 429 (throttled). Retrying with 1s backoff...")
                time.sleep(1.0)
                try:
                    retry_resp = requests.post(url, json=payload, timeout=20)
                    if retry_resp.status_code == 200:
                        data = retry_resp.json()
                        candidates = data.get("candidates", [])
                        if candidates:
                            parts = candidates[0].get("content", {}).get("parts", [])
                            if parts:
                                text = parts[0].get("text", "")
                                parsed = extract_json_plan(text)
                                if parsed:
                                    parsed["_model_used"] = model
                                    parsed["_is_llm"] = True
                                    print(f"[Storage Copilot Gemini] Successfully received response on retry via model '{model}'.")
                                    return parsed
                except Exception:
                    pass
                continue
            elif resp.status_code == 503:
                print(f"[Storage Copilot Gemini] Model '{model}' temporarily unavailable (503). Trying next model...")
                continue
            else:
                print(f"[Storage Copilot Gemini] Model '{model}' returned HTTP {resp.status_code}.")
                continue
        except Exception as e:
            print(f"[Storage Copilot Gemini] Model '{model}' request failed: {e}")
            continue
    return {}

def call_gemini_text(system_prompt: str, user_prompt: str) -> str:
    """
    Sends conversational/markdown prompt to Gemini API with robust model fallback and returns raw text.
    Fast-switches to local deterministic engine if daily project quota (20 req/day) is exhausted.
    """
    if time.time() < config.GEMINI_QUOTA_BLOCKED_UNTIL:
        return ""

    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        print("[Storage Copilot] GEMINI_API_KEY not configured. Running in Local Offline Intelligence mode.")
        return ""

    payload = {
        "system_instruction": {"parts": [{"text": system_prompt}]},
        "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
        "generationConfig": {"temperature": 0.2}
    }

    for model in GEMINI_MODELS:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
        try:
            resp = requests.post(url, json=payload, timeout=20)
            if resp.status_code == 200:
                data = resp.json()
                candidates = data.get("candidates", [])
                if candidates:
                    parts = candidates[0].get("content", {}).get("parts", [])
                    if parts:
                        text = parts[0].get("text", "")
                        if text:
                            print(f"[Storage Copilot Gemini Text] Successfully received response via model '{model}'.")
                            return text
            elif resp.status_code == 429:
                resp_text = resp.text
                if "GenerateRequestsPerDay" in resp_text or "quotaId" in resp_text or "RESOURCE_EXHAUSTED" in resp_text:
                    print(f"[Storage Copilot Gemini Text] Daily free-tier quota (20 req/day) reached for project on '{model}'. Fast-switching to deterministic local engine.")
                    config.GEMINI_QUOTA_BLOCKED_UNTIL = time.time() + 900
                    return ""
                print(f"[Storage Copilot Gemini Text] Model '{model}' returned HTTP 429 (throttled). Retrying with 1s backoff...")
                time.sleep(1.0)
                try:
                    retry_resp = requests.post(url, json=payload, timeout=20)
                    if retry_resp.status_code == 200:
                        data = retry_resp.json()
                        candidates = data.get("candidates", [])
                        if candidates:
                            parts = candidates[0].get("content", {}).get("parts", [])
                            if parts:
                                text = parts[0].get("text", "")
                                if text:
                                    print(f"[Storage Copilot Gemini Text] Successfully received response on retry via model '{model}'.")
                                    return text
                except Exception:
                    pass
                continue
            elif resp.status_code == 503:
                print(f"[Storage Copilot Gemini Text] Model '{model}' temporarily unavailable (503). Trying next model...")
                continue
            else:
                print(f"[Storage Copilot Gemini Text] Model '{model}' returned HTTP {resp.status_code}.")
                continue
        except Exception as e:
            print(f"[Storage Copilot Gemini Text] Model '{model}' request failed: {e}")
            continue
    return ""

def call_gemini_api(system_prompt: str, user_prompt: str) -> dict:
    """Legacy alias for call_gemini_json."""
    return call_gemini_json(system_prompt, user_prompt)
