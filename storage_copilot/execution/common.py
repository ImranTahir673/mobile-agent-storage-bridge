import os
import json
from typing import Dict, Any, List, Optional
from storage_copilot.api.profile_routes import get_user_profile_path


def _load_profile_context() -> Dict[str, Any]:
    p_path = get_user_profile_path()
    profile = {}
    if os.path.exists(p_path):
        try:
            with open(p_path, "r", encoding="utf-8") as pf:
                profile = json.load(pf)
        except Exception:
            pass

    user_id = profile.get("user_identity", {})
    owner_name = user_id.get("primary_name", "Imran Tahir")
    identifiers = user_id.get("identifiers", ["BSAI-182", "182"])
    peers = profile.get("known_peers", [])
    peer_names = [p.get("name") for p in peers if p.get("name")] or ["Fawad", "Sumbal", "Ahmed", "Yousaf"]
    peer_matrix_lines = "\n".join(
        f"- Peer '{p.get('name')}': designated folder '{p.get('designated_folder', f'Documents/Peers/{p.get('name')}')}'"
        for p in peers
    ) if peers else "\n".join(f"- Peer '{p}': designated folder 'Documents/Peers/{p}'" for p in peer_names)

    return {
        "profile": profile,
        "owner_name": owner_name,
        "identifiers": identifiers,
        "peer_names": peer_names,
        "peer_matrix_lines": peer_matrix_lines
    }


def _format_conversation_history(conversation_history: Optional[List[Dict[str, Any]]]) -> str:
    if not conversation_history:
        return "None (New conversation)"
    lines = []
    for turn in conversation_history[-4:]:
        role = (turn.get("role") or "user").upper()
        content = (turn.get("content") or turn.get("text") or turn.get("message") or "").strip()
        if content:
            lines.append(f"{role}: {content[:300].replace(chr(10), ' ')}")
    return "\n".join(lines) if lines else "None (New conversation)"
