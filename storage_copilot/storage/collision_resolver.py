import os
import re
from datetime import datetime, timezone

def resolve_collision(target_path: str, strategy: str) -> str:
    """Resolves destination collisions according to policy."""
    if not os.path.exists(target_path):
        return target_path

    if strategy == "FAIL":
        raise FileExistsError(f"Destination collision: '{target_path}' already exists")
    elif strategy == "SKIP":
        return ""
    elif strategy == "RENAME_NUMERIC":
        dirname = os.path.dirname(target_path)
        basename, ext = os.path.splitext(os.path.basename(target_path))
        counter = 1
        while True:
            candidate = os.path.join(dirname, f"{basename} ({counter}){ext}")
            if not os.path.exists(candidate):
                return candidate
            counter += 1
    elif strategy == "RENAME_TIMESTAMP":
        dirname = os.path.dirname(target_path)
        basename, ext = os.path.splitext(os.path.basename(target_path))
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        return os.path.join(dirname, f"{basename}_{ts}{ext}")
    else:
        raise ValueError(f"Unsupported collision strategy: {strategy}")

def ensure_unique_plan_destinations(actions: list) -> list:
    """
    Enforces strict destination uniqueness across all actions in a single batch.
    Prevents duplicate destination collisions by disambiguating with clean tokens or sequential counters.
    Never slices word fragments like '_e_copy' or '_ecture'.
    """
    seen_destinations = set()
    cleaned_actions = []

    for act in actions:
        act_copy = dict(act)
        act_type = act_copy.get("type", "")

        if act_type in ("move", "copy"):
            dst = (act_copy.get("destination") or "").replace("\\", "/").strip()
            if dst:
                base_dir = os.path.dirname(dst)
                base_name = os.path.basename(dst)
                stem, ext = os.path.splitext(base_name)
                src_path = act_copy.get("source") or act_copy.get("path") or ""
                src_stem = os.path.splitext(os.path.basename(src_path))[0]

                cand_dst = dst
                counter = 1
                while cand_dst.lower() in seen_destinations:
                    # Disambiguate using explicit trailing alphanumeric ID (e.g. WA0025) or clean sequential counter
                    token = None
                    if counter == 1 and src_stem:
                        m = re.search(r"(WA\d+|\d+)$", src_stem, re.I)
                        if m and m.group(1) != "1":
                            token = m.group(1)

                    if token:
                        cand_dst = f"{base_dir}/{stem}_{token}{ext}" if base_dir else f"{stem}_{token}{ext}"
                    else:
                        cand_dst = f"{base_dir}/{stem}_{counter + 1}{ext}" if base_dir else f"{stem}_{counter + 1}{ext}"
                    counter += 1

                seen_destinations.add(cand_dst.lower())
                act_copy["destination"] = cand_dst
        cleaned_actions.append(act_copy)
    return cleaned_actions
