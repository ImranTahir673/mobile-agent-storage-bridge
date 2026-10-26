import os
import re
import json

def extract_json_plan(response_text: str) -> dict:
    """Extracts JSON Action Plan from LLM response text."""
    clean_text = (response_text or "").strip()
    fenced_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", clean_text, re.DOTALL)
    if fenced_match:
        json_candidate = fenced_match.group(1)
    else:
        start_idx = clean_text.find("{")
        end_idx = clean_text.rfind("}")
        if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
            json_candidate = clean_text[start_idx:end_idx + 1]
        else:
            raise ValueError(f"Could not locate JSON in response:\n{clean_text}")
    return json.loads(json_candidate)

def format_plan_badges(plan_actions: list, peer_names: list, manifest_lookup: dict = None) -> list:
    """Post-processes action steps, annotates visual UI badges, and ensures PII inspection attributes."""
    formatted_actions = []
    step_num = 1
    manifest_lookup = manifest_lookup or {}

    for act in plan_actions:
        act_copy = dict(act)
        act_type = act_copy.get("type", "")
        src_path = act_copy.get("source") or act_copy.get("path") or ""
        base_name = os.path.basename(src_path)
        manifest_item = manifest_lookup.get(src_path.lower()) or manifest_lookup.get(base_name.lower())

        if act_type == "make_dir":
            dir_path = (act_copy.get("path") or "").replace("\\", "/").strip().lstrip("/")
            if dir_path in (".agent_trash", ".agent_trash/", "") or dir_path.startswith(".agent_trash/"):
                continue
            act_copy["action_id"] = f"step-{step_num}"
            act_copy["badge"] = "FOLDER_CREATE"
            act_copy["badge_label"] = "Create Folder"
            act_copy["ai_inspected"] = False
            act_copy["inspected_preview"] = "Directory creation"
            formatted_actions.append(act_copy)
            step_num += 1

        elif act_type in ("move", "copy"):
            dst = (act_copy.get("destination") or "").replace("\\", "/").strip().lstrip("/")
            src = act_copy.get("source", "")
            if dst == ".agent_trash" or dst.startswith(".agent_trash/"):
                act_copy["type"] = "trash"
                act_copy["path"] = src
                act_copy.pop("destination", None)
                act_copy["action_id"] = f"step-{step_num}"
                act_copy["badge"] = "TRASH"
                act_copy["badge_label"] = "Soft Delete"
            else:
                peer_target = act_copy.get("peer_name")
                is_peer_dest = "documents/peers/" in dst.lower() or "peers/" in dst.lower()
                if not peer_target and is_peer_dest:
                    for p_name in peer_names:
                        if p_name.lower() in dst.lower() or p_name.lower() in src.lower():
                            peer_target = p_name
                            break

                if peer_target and is_peer_dest:
                    act_copy["badge"] = "PEER_MOVE"
                    act_copy["badge_label"] = "Peer Move"
                    act_copy["peer_name"] = peer_target
                elif os.path.basename(src).lower() != os.path.basename(dst).lower():
                    act_copy["badge"] = "RENAME"
                    act_copy["badge_label"] = "Rename / Move"
                else:
                    act_copy["badge"] = "FILE_MOVE"
                    act_copy["badge_label"] = "Move File"

            # Annotate PII inspection attributes
            if "ai_inspected" not in act_copy or act_copy["ai_inspected"] is None:
                if manifest_item and manifest_item.get("has_content"):
                    act_copy["ai_inspected"] = True
                    raw_snip = manifest_item.get("snippet") or manifest_item.get("text_snippet") or ""
                    act_copy["inspected_preview"] = raw_snip[:100] if raw_snip else "Content inspected"
                else:
                    act_copy["ai_inspected"] = False
                    act_copy["inspected_preview"] = "No readable text extracted (scanned image or binary document)"
            else:
                if act_copy["ai_inspected"] and (not act_copy.get("inspected_preview") or act_copy.get("inspected_preview") == "..."):
                    if manifest_item:
                        act_copy["inspected_preview"] = (manifest_item.get("snippet") or manifest_item.get("text_snippet") or "")[:100]
                elif not act_copy["ai_inspected"]:
                    if not act_copy.get("inspected_preview"):
                        act_copy["inspected_preview"] = "No readable text extracted (scanned image or binary document)"

            formatted_actions.append(act_copy)
            step_num += 1

        elif act_type == "trash":
            act_copy["action_id"] = f"step-{step_num}"
            act_copy["badge"] = "TRASH"
            act_copy["badge_label"] = "Soft Delete"

            if "ai_inspected" not in act_copy or act_copy["ai_inspected"] is None:
                if manifest_item and manifest_item.get("has_content"):
                    act_copy["ai_inspected"] = True
                    raw_snip = manifest_item.get("snippet") or manifest_item.get("text_snippet") or ""
                    act_copy["inspected_preview"] = raw_snip[:100] if raw_snip else "Content inspected"
                else:
                    act_copy["ai_inspected"] = False
                    act_copy["inspected_preview"] = "No readable text extracted (scanned image or binary document)"
            else:
                if not act_copy.get("inspected_preview"):
                    act_copy["inspected_preview"] = "No readable text extracted (scanned image or binary document)"

            formatted_actions.append(act_copy)
            step_num += 1

    return formatted_actions

def normalize_user_prompt(text: str) -> str:
    """
    Normalizes common mobile typos, abbreviations, and colloquialisms for robust intent understanding:
    - 'fikes' -> 'files'
    - 'vest' -> 'best'
    - 'oragnize' -> 'organize'
    - 'doenload' -> 'download'
    - 'reciept' -> 'receipt'
    - 'dups' / 'dupes' / 'dupe' -> 'duplicates'
    - 'dedup' -> 'deduplicate'
    """
    if not text:
        return ""
    t = text.strip()
    t = re.sub(r"\b(?:fikes|fils|fiel|fiels)\b", "files", t, flags=re.I)
    t = re.sub(r"\bcleen\b", "clean", t, flags=re.I)
    t = re.sub(r"\bteh\b", "the", t, flags=re.I)
    t = re.sub(r"\bvest\b", "best", t, flags=re.I)
    t = re.sub(r"\b(?:oragnize|organise|orginize)\b", "organize", t, flags=re.I)
    t = re.sub(r"\b(?:doenload|downlods|dawnload|dwnload|downlod)\b", "download", t, flags=re.I)
    t = re.sub(r"\breciept\b", "receipt", t, flags=re.I)
    t = re.sub(r"\b(?:dups|dupes|dupe|dupplicates)\b", "duplicates", t, flags=re.I)
    t = re.sub(r"\bdedup\b", "deduplicate", t, flags=re.I)
    t = re.sub(r"\b(?:vouchr|vaucher)\b", "voucher", t, flags=re.I)
    t = re.sub(r"\b(?:lectur|letcure)\b", "lecture", t, flags=re.I)
    return t
