import os
from collections import defaultdict
from typing import Dict, Any, List, Optional

from storage_copilot import config
from storage_copilot.execution.common import _load_profile_context, _format_conversation_history
from storage_copilot.storage.scanner import resolve_target_directory
from storage_copilot.intelligence.gemini_client import (
    call_gemini_text_with_meta,
    is_gemini_available,
    get_last_fallback_info
)
from storage_copilot.intelligence.session_context import session_context_store
from storage_copilot.advice.advisor import (
    FORMAT_CATEGORIES,
    get_format_category,
    get_unorganized_download_files,
    generate_dynamic_storage_advice
)


def handle_conversation_turn(
    prompt: str,
    classification: Dict[str, Any],
    target_folder: str = "Download",
    conversation_history: Optional[List[Dict[str, Any]]] = None
) -> Dict[str, Any]:
    """
    Tier 1 Conversation Handler:
    - Provides warm, dynamic responses grounded in live filesystem state.
    - Resolves formatting proposals into markdown breakdown tables before mutations.
    - Respects privacy/security boundary guards (blocks web/SMS/email commands).
    - Ensures explicit diagnostics: sets degraded_fallback, fallback_reason, and fallback_notice.
    """
    ctx = _load_profile_context()
    owner_name = ctx["owner_name"]
    peer_names = ctx["peer_names"]
    sub_intent = classification.get("sub_intent", "")
    raw_prompt = classification.get("raw_prompt", prompt)
    lower_p = raw_prompt.lower()
    fb_diag = get_last_fallback_info()

    if sub_intent == "format_sorting_proposal":
        target_dir = resolve_target_directory(target_folder)
        unorg_files = get_unorganized_download_files(target_dir)
        if not unorg_files:
            return {
                "type": "conversation",
                "intent": "CONVERSATION",
                "message": f"✨ **All files in `{target_folder}/` are already organized!**\n\nNo loose unorganized files remain in the root directory.",
                "analysis": f"All files in `{target_folder}/` are already organized.",
                "version": config.APP_VERSION,
                "model_source": "deterministic-engine",
                "is_llm": False,
                "degraded_fallback": True,
                "fallback_reason": fb_diag["fallback_reason"],
                "fallback_notice": fb_diag["fallback_notice"],
                "actions": [],
                "plan": [],
                "badges": ["Chat", "Advice"],
                "quick_chips": [
                    "View organized folders",
                    "🧹 Clean Duplicates",
                    "📋 Sort Student Vouchers"
                ]
            }

        cat_counts = defaultdict(int)
        for f in unorg_files:
            cat = get_format_category(f.name)
            cat_counts[cat["folder"]] += 1

        rows = []
        for cat_name, info in FORMAT_CATEGORIES.items():
            cnt = cat_counts.get(info["folder"], 0)
            if cnt > 0:
                rows.append(f"| {info['emoji']} **{info['label']}** | `{target_folder}/{info['folder']}/` | **{cnt} files** |")
        if cat_counts.get("Other", 0) > 0:
            rows.append(f"| 📁 **Other Files** | `{target_folder}/Other/` | **{cat_counts['Other']} files** |")

        session_context_store.update_context(
            target_folder=target_folder,
            last_intent="CONVERSATION",
            last_sub_intent="format_sorting_proposal",
            unorganized_count=len(unorg_files)
        )
        table_md = "\n".join(rows)
        msg = f"""Hello {owner_name}. I have analyzed your `{target_folder}/` directory and identified **{len(unorg_files)} unorganized files**.

Here is the breakdown of files by format and their destination subfolders:

| Format | Destination | Files Count |
| :--- | :--- | :--- |
{table_md}

**Total:** **{len(unorg_files)} files** ready to organize.

To ensure system stability and safety, files will be moved in batches (25 files per batch) with 1-Tap Undo available for every batch.

**Would you like me to proceed with moving these files into their respective folders?**"""

        return {
            "type": "conversation",
            "intent": "CONVERSATION",
            "message": msg,
            "analysis": msg,
            "actions": [],
            "plan": [],
            "badges": ["Chat", "Format Breakdown"],
            "format_sorting_active": True,
            "origin_badge": "🛡️ Offline Heuristic",
            "version": config.APP_VERSION,
            "model_source": "deterministic-engine",
            "is_llm": False,
            "degraded_fallback": True,
            "fallback_reason": fb_diag["fallback_reason"],
            "fallback_notice": fb_diag["fallback_notice"],
            "quick_chips": [
                "✅ Yes, Move All Files",
                "View organized folders",
                "❌ Cancel"
            ]
        }

    target_dir = resolve_target_directory(target_folder)
    shallow_filenames = []
    if os.path.exists(target_dir) and os.path.isdir(target_dir):
        try:
            shallow_filenames = [
                e.name for e in os.scandir(target_dir)
                if e.is_file() and not e.name.startswith(".")
            ]
        except Exception:
            pass

    if any(w in lower_p for w in ("email", "mail", "sms", "whatsapp", "call", "browse", "web")):
        msg = (
            f"I operate strictly as an **on-device local storage copilot** for your Android filesystem, {owner_name}. "
            "For your privacy and security, I cannot send emails, transmit messages, or browse the external web.\n\n"
            f"However, I can help you find, inspect, or organize documents in `{target_folder}/` ({len(shallow_filenames)} files currently detected)—such as locating professor notes, sorting university lectures, or isolating peer files."
        )
        return {
            "type": "conversation",
            "intent": "CONVERSATION",
            "message": msg,
            "analysis": msg,
            "version": config.APP_VERSION,
            "model_source": "deterministic-engine",
            "is_llm": False,
            "degraded_fallback": True,
            "fallback_reason": fb_diag["fallback_reason"],
            "fallback_notice": fb_diag["fallback_notice"],
            "actions": [],
            "plan": [],
            "badges": ["Chat", "Boundary Guard"],
            "quick_chips": [
                "📚 List university lectures",
                "📋 Sort Student Vouchers",
                "👥 Separate Peer Documents",
                "🧹 Clean Duplicates"
            ]
        }

    conv_history_str = _format_conversation_history(conversation_history)
    sys_prompt = f"""You are Storage Copilot, an articulate on-device AI storage assistant running locally on Android for {owner_name} (IDs: {ctx['identifiers']}).
Target Folder: '{target_folder}/' ({len(shallow_filenames)} files present: {', '.join(shallow_filenames[:15]) if shallow_filenames else 'Empty'}).
Known Peers: {', '.join(peer_names)}.
Recent Conversation:
{conv_history_str}

Respond directly and warmly to {owner_name} in concise Markdown.
- NEVER use a static canned greeting template like '👋 Hello Imran Tahir! I am Storage Copilot, running locally on your Android device via Termux'.
- Ground your advice in the actual filenames present above.
- Explain your capabilities clearly (local storage reconnaissance, in-memory PII redaction, academic vs. peer routing, SHA-256 & 3-gram Jaccard deduplication, and 1-tap SQLite WAL rollback).
"""
    llm_text, used_model = call_gemini_text_with_meta(sys_prompt, raw_prompt, task_tier="fast")
    if llm_text:
        m_src = used_model or classification.get("model_source") or "gemini-3.1-flash-lite"
        return {
            "type": "conversation",
            "intent": "CONVERSATION",
            "message": llm_text,
            "analysis": llm_text,
            "version": config.APP_VERSION,
            "model_source": m_src,
            "is_llm": True,
            "degraded_fallback": False,
            "fallback_reason": None,
            "fallback_notice": None,
            "origin_badge": f"⚡ Gemini AI ({m_src})",
            "actions": [],
            "plan": [],
            "badges": [f"⚡ Gemini AI ({m_src})", "Chat", "Advice"],
            "quick_chips": [
                "📋 Sort Student Vouchers",
                "👥 Separate Peer Documents",
                "🏷️ Clean Download Names",
                "🧹 Clean Duplicates"
            ]
        }

    advice_msg = generate_dynamic_storage_advice(
        config.BASE_STORAGE_DIR,
        target_folder=target_folder,
        owner_name=owner_name,
        peer_names=peer_names
    )
    return {
        "type": "conversation",
        "intent": "CONVERSATION",
        "message": advice_msg,
        "analysis": advice_msg,
        "version": config.APP_VERSION,
        "model_source": "offline-heuristic",
        "is_llm": False,
        "degraded_fallback": True,
        "fallback_reason": fb_diag["fallback_reason"],
        "fallback_notice": fb_diag["fallback_notice"],
        "actions": [],
        "plan": [],
        "badges": ["Chat", "Advice"],
        "quick_chips": [
            "📋 Sort Student Vouchers",
            "👥 Separate Peer Documents",
            "🏷️ Clean Download Names",
            "🗑️ Cleanup Old Screenshots"
        ]
    }
