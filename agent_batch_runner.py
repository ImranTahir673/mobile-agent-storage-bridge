#!/usr/bin/env python3
"""
Mobile Agent Storage Bridge - Phase 2: Autonomous Agent Batch Runner
====================================================================
Implements the Phase 2 Decision Engine and Human-in-the-Loop CLI:
- Connects to the mobile bridge via --url (local IP or Cloudflare tunnel).
- Executes the Two-Stage Agent Protocol (Reconnaissance -> Plan Synthesis).
- Exposes read-only inspection tools: list_files(path) and read_file_snippet(path, max_chars).
- Employs Gemini reasoning to identify messy files and formulate a strict Action Plan.
- Validates the Action Plan against docs/SCHEMA_SPEC.md and docs/POLICY_RULES.md.
- Step A: Dry-run submission to POST /execute_plan.
- Step B: CLI diff table rendering (Source -> Proposed Destination).
- Step C: Interactive human confirmation ([y/N] or --auto-approve).
- Step D: Live atomic execution with SQLite pre-logging on the device.
- Step E: Immediate 1-tap rollback prompt (Type 'undo' to revert batch).
"""

import os
import sys
import json
import uuid
import time
import re
import argparse
import tempfile
import threading
from typing import Dict, Any, List, Optional, Tuple
import requests

# Optional Google GenAI SDK import
try:
    from google import genai
    from google.genai import types
    HAVE_GENAI_SDK = True
except ImportError:
    HAVE_GENAI_SDK = False

# ==============================================================================
# Environment Configuration & CLI Argument Parsing
# ==============================================================================

def load_environment():
    """Loads environment variables from .env if present."""
    env_path = os.path.join(os.path.dirname(__file__), ".env")
    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, val = line.split("=", 1)
                    os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))

def parse_cli_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Phase 2 Autonomous Storage Organization Agent (Gemini Batch Runner)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python agent_batch_runner.py --url http://192.168.1.50:8080 --target-folder Download
  python agent_batch_runner.py --url https://agent.yourdomain.com --auto-approve
  python agent_batch_runner.py --local --auto-approve --test-rollback
        """
    )
    parser.add_argument(
        "--url",
        type=str,
        default=None,
        help="Target bridge URL (e.g. http://192.168.1.50:8080 or https://...trycloudflare.com)"
    )
    parser.add_argument(
        "--find", "-f",
        type=str,
        default=None,
        help="Deterministic ledger historical lookup: search previous moves, renames, or trashed files (bypasses LLM)"
    )
    parser.add_argument(
        "--gather",
        type=str,
        default=None,
        help="Semantic gathering query across directories (e.g. 'Gather all machine learning papers')"
    )
    parser.add_argument(
        "--to",
        type=str,
        default="Documents/Gathered",
        help="Destination folder for gathered files (default: 'Documents/Gathered')"
    )
    parser.add_argument(
        "--copy",
        action="store_true",
        help="Formulate 'copy' actions instead of 'move' actions when gathering files"
    )
    parser.add_argument(
        "--prompt", "-p",
        type=str,
        default=None,
        help="Custom natural-language goal or instructions (e.g. 'Only organize receipts and fee vouchers')"
    )
    parser.add_argument(
        "--target-folder",
        type=str,
        default="Download",
        help="Target folder on phone storage to inspect and reorganize (default: 'Download')"
    )
    parser.add_argument(
        "--model",
        type=str,
        default="gemini-3.5-flash-lite",
        help="Gemini model to use (default: 'gemini-3.5-flash-lite', e.g. 'gemini-2.5-flash', 'gemini-3.8-flash')"
    )
    parser.add_argument(
        "--auto-approve",
        action="store_true",
        help="Skip interactive confirmation [y/N] and execute plan automatically (useful for testing)"
    )
    parser.add_argument(
        "--dry-run-only",
        action="store_true",
        help="Perform inspection and dry-run validation only; do not prompt for live execution"
    )
    parser.add_argument(
        "--test-rollback",
        action="store_true",
        help="Immediately trigger rollback after execution (for automated end-to-end testing)"
    )
    parser.add_argument(
        "--local",
        action="store_true",
        help="Spin up an isolated local mock bridge with synthetic fixtures for sandbox testing"
    )
    parser.add_argument(
        "--profile",
        type=str,
        default=None,
        help="Path to user_profile.json (defaults to local user_profile.json or queries bridge /user_profile)"
    )
    parser.add_argument(
        "--max-iterations",
        type=int,
        default=16,
        help="Maximum agent reasoning turns before terminating (default: 16)"
    )
    return parser.parse_args()


# ==============================================================================
# Bridge Client: Communication with Android Storage Bridge
# ==============================================================================

class BridgeClient:
    """Encapsulates HTTP communication with the Mobile Agent Storage Bridge."""

    def __init__(self, base_url: str):
        url = (base_url or "").strip().rstrip("/")
        if url:
            # Handle possible scheme typos or missing scheme (e.g., https:/domain or plain domain)
            if url.startswith("https:/") and not url.startswith("https://"):
                url = "https://" + url[7:].lstrip("/")
            elif url.startswith("http:/") and not url.startswith("http://"):
                url = "http://" + url[6:].lstrip("/")
            elif not url.startswith("http://") and not url.startswith("https://"):
                url = "https://" + url
        self.base_url = url

    def health(self, timeout: int = 15) -> Dict[str, Any]:
        resp = requests.get(f"{self.base_url}/", timeout=timeout)
        if resp.status_code != 200:
            raise ConnectionError(f"Health check failed (HTTP {resp.status_code}): {resp.text}")
        data = resp.json()
        if data.get("status") != "running":
            raise ValueError(f"Unexpected bridge status: {data}")
        return data

    def list_files(self, path: str = "") -> Dict[str, Any]:
        """Tool 1: Read-only directory listing relative to device storage root."""
        resp = requests.post(
            f"{self.base_url}/list_files",
            json={"path": path},
            timeout=30
        )
        if not resp.ok:
            return {"error": f"HTTP {resp.status_code}: {resp.text.strip()}"}
        return resp.json()

    def read_file_snippet(self, path: str, max_chars: int = 1000) -> Dict[str, Any]:
        """Tool 2: Read-only file snippet / PDF preview inspection."""
        resp = requests.post(
            f"{self.base_url}/read_file_snippet",
            json={"path": path, "max_chars": max_chars},
            timeout=30
        )
        if not resp.ok:
            return {"error": f"HTTP {resp.status_code}: {resp.text.strip()}"}
        return resp.json()

    def execute_plan(self, plan: Dict[str, Any], dry_run: bool = False) -> Dict[str, Any]:
        """Submits an Action Plan for validation or live execution."""
        payload = dict(plan)
        payload["dry_run"] = dry_run
        resp = requests.post(
            f"{self.base_url}/execute_plan",
            json=payload,
            timeout=45
        )
        try:
            body = resp.json()
        except Exception:
            body = {"raw": resp.text}

        if not resp.ok:
            err_msg = body.get("error", resp.text)
            raise RuntimeError(f"Bridge execution error (HTTP {resp.status_code}): {err_msg}")
        return body

    def rollback_batch(self, batch_id: str) -> Dict[str, Any]:
        """Rolls back an executed batch using SQLite inverse undo vectors."""
        resp = requests.post(
            f"{self.base_url}/rollback_batch",
            json={"batch_id": batch_id},
            timeout=30
        )
        try:
            body = resp.json()
        except Exception:
            body = {"raw": resp.text}

        if not resp.ok:
            err_msg = body.get("error", resp.text)
            raise RuntimeError(f"Bridge rollback error (HTTP {resp.status_code}): {err_msg}")
        return body

    def lookup_history(self, query: str) -> Dict[str, Any]:
        """Tool / Endpoint: Deterministic lookup of file mutation history from the SQLite ledger."""
        resp = requests.post(
            f"{self.base_url}/lookup_history",
            json={"query": query},
            timeout=25
        )
        if not resp.ok:
            return {"error": f"HTTP {resp.status_code}: {resp.text.strip()}", "matches": []}
        return resp.json()

    def get_user_profile(self) -> Dict[str, Any]:
        """Tool / Endpoint: Fetches device-local user profile and peer mapping from the bridge."""
        try:
            resp = requests.get(f"{self.base_url}/user_profile", timeout=15)
            if resp.ok:
                return resp.json().get("profile", {})
        except Exception:
            pass
        return {}


# ==============================================================================
# Inspection Tools Schemas (Read-Only)
# ==============================================================================

INSPECTION_TOOLS = [
    {
        "function_declarations": [
            {
                "name": "list_files",
                "description": "Lists files and subdirectories inside a given path relative to storage root. Returns names, directory flags, and byte sizes.",
                "parameters": {
                    "type": "OBJECT",
                    "properties": {
                        "path": {
                            "type": "STRING",
                            "description": "Folder path relative to storage root (e.g. 'Download' or 'Documents')"
                        }
                    },
                    "required": ["path"]
                }
            },
            {
                "name": "read_file_snippet",
                "description": "Reads text content or extracted PDF text snippet from a file to inspect its true identity, topic, and contents.",
                "parameters": {
                    "type": "OBJECT",
                    "properties": {
                        "path": {
                            "type": "STRING",
                            "description": "Target file path relative to storage root (e.g. 'Download/1508.06576v2.pdf')"
                        },
                        "max_chars": {
                            "type": "INTEGER",
                            "description": "Maximum number of characters to extract (default: 1000)"
                        }
                    },
                    "required": ["path"]
                }
            }
        ]
    }
]


## ==============================================================================
# Fail-Safe Privacy Shield & Local PII Sanitizer
# ==============================================================================

PII_PATTERNS = [
    (re.compile(r"\b\d{5}-\d{7}-\d\b"), "[REDACTED_CNIC]"),
    (re.compile(r"(?:\+92[- ]?|0)?3\d{2}[- ]?\d{7}\b"), "[REDACTED_PHONE]"),
    (re.compile(r"\b(?:\d{4}[- ]?){3}\d{4}\b"), "[REDACTED_CARD]"),
    (re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"), "[REDACTED_EMAIL]"),
]

def sanitize_pii(text: str) -> str:
    """Pre-flight local scrubbing of sensitive personally identifiable information."""
    if not text:
        return ""
    clean = text
    for pattern, replacement in PII_PATTERNS:
        clean = pattern.sub(replacement, clean)
    return clean


# ==============================================================================
# Agent System Prompt & Schema Specification
# ==============================================================================

SYSTEM_PROMPT_TEMPLATE = """You are an autonomous Mobile Storage Management & Semantic Copilot operating on an Android device storage bridge (/storage/emulated/0).

Operating Role & Zero-Cloud Invariants:
- Role: You are a strictly local file management and semantic storage copilot. You organize, sanitize, find, and categorize files within user-designated storage zones on Android internal storage.
- Zero Cloud Storage: User files, SQLite action ledgers (.ledger.db), and soft-delete trash (.agent_trash) remain strictly on the physical device. You NEVER upload, mirror, or transmit raw file content or database files to cloud storage.
- Fail-Closed Storage Boundaries: You are permanently blocked from reading or modifying system paths, application private databases, /Android/data, /Android/obb, .git, or hidden system trees.

Fail-Safe Privacy Shield & PII Protection:
- Zero PII Invariants: You must NEVER infer, reconstruct, or output personally identifiable information (PII)—including CNICs/Gov IDs, phone numbers, payment cards, email addresses, or names of specific individuals—in your reasoning steps, chain-of-thought, action descriptions, or destination filenames.
- Categorical Interpretation: Treat all text snippets purely as semantic category indicators (e.g. 'Fee Voucher', 'Assignment', 'Bank Statement', 'Lecture Slides') without attributing them to specific individuals or account numbers.
- Zero Raw Image Uploads: Image and multimedia processing is strictly metadata-driven (filenames, modification timestamps, EXIF year/month). Never process or request raw image pixels or video frames.
{user_profile_block}
Reconnaissance (Stage 1) & Plan Synthesis (Stage 2):
1. Inspect the target directory '{target_folder}'.
2. Identify messy, cryptic, uninformative, or unstructured filenames:
   - Academic / Research papers (e.g. arXiv IDs '1508.06576v2.pdf' -> identify topic/title and move to 'Documents/Research/...').
   - Administrative / Academic receipts (e.g. 'DOC-20220503-WA0103.pdf' -> identify topic and move to 'Documents/...').
   - Cryptic hash names, raw download names, and temporary junk files (.tmp, session tokens -> soft-delete via 'trash').
3. Semantic Search & Gathering: If the user provides a search or gathering directive (e.g. 'Find my OS lab' or 'Gather ML papers into Documents/Research/ML'), locate relevant files matching the query and formulate clean 'copy' or 'move' actions into the designated target folder. You may inspect multiple directories (e.g. 'Download', 'Documents') using 'list_files' to discover matching candidate files.
4. Reconnaissance Efficiency & Snippet Budget:
   - Use filename cues first to filter and identify the most probable candidate files.
   - Limit 'read_file_snippet' inspections strictly to only the most probable candidate files (maximum 4–5 snippet inspections). Do NOT exhaust reasoning turns inspecting every single file in the directory.
   - Once candidate files are identified or rule-out is clear, synthesize and emit the vetted Action Plan JSON immediately without exhausting the turn budget.
{custom_goal_block}
5. When finished inspecting, output a STRICT JSON Action Plan adhering to docs/SCHEMA_SPEC.md:

```json
{{
  "plan_id": "<UUIDv4>",
  "version": "1.0",
  "description": "<Human-readable summary of what this batch accomplishes>",
  "collision_strategy": "RENAME_NUMERIC",
  "actions": [
    {{
      "action_id": "step-1",
      "type": "make_dir",
      "path": "Documents/Research/Computer_Vision"
    }},
    {{
      "action_id": "step-2",
      "type": "move",
      "source": "Download/1508.06576v2.pdf",
      "destination": "Documents/Research/Computer_Vision/Neural_Algorithm_of_Artistic_Style_Gatys.pdf"
    }},
    {{
      "action_id": "step-3",
      "type": "trash",
      "path": "Download/temp_cache.tmp"
    }}
  ]
}}
```

Strict Policy & Safety Rules:
- Blast Radius Limit: Max 20 actions per batch.
- Storage Boundary: All paths must be relative to storage root without leading slashes.
- Blacklisted Targets: NEVER touch or reference '/Android', '.agent_trash', '.ledger.db', '.git', or root dotfiles.
- Zero Hard-Delete: Only use action 'trash' for removal; physical deletes are prohibited.
- Directory Dependency: If moving a file into a newly proposed directory, include a 'make_dir' step for that directory first.
- Permitted Action Types: 'make_dir', 'move', 'copy', 'trash'.
- Permitted Collision Strategies: 'FAIL', 'RENAME_NUMERIC', 'RENAME_TIMESTAMP', 'SKIP' (Default: 'RENAME_NUMERIC').
- Output Format: Output ONLY the strict JSON Action Plan once reasoning is complete. Do not append explanatory prose outside the JSON block.
"""


# ==============================================================================
# Plan Extraction & Validation
# ==============================================================================

def extract_json_plan(response_text: str) -> Dict[str, Any]:
    """
    Extracts and validates a JSON Action Plan from the model's text response.
    Handles markdown code fences and cleans whitespace.
    """
    clean_text = response_text.strip()
    
    # 1. Check for fenced code blocks
    fenced_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", clean_text, re.DOTALL)
    if fenced_match:
        json_candidate = fenced_match.group(1)
    else:
        # Search for first { and last }
        start_idx = clean_text.find("{")
        end_idx = clean_text.rfind("}")
        if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
            json_candidate = clean_text[start_idx:end_idx + 1]
        else:
            raise ValueError(f"Could not locate JSON Action Plan in agent response:\n{clean_text}")

    try:
        plan = json.loads(json_candidate)
    except json.JSONDecodeError as e:
        raise ValueError(f"Failed to parse Action Plan JSON: {e}\nRaw JSON text:\n{json_candidate}")

    # 2. Schema contract validation
    if not isinstance(plan, dict):
        raise ValueError("Action Plan must be a JSON object")

    if "plan_id" not in plan or not plan["plan_id"]:
        plan["plan_id"] = str(uuid.uuid4())
    else:
        plan["plan_id"] = str(plan["plan_id"])

    plan.setdefault("version", "1.0")
    plan.setdefault("description", "Reorganize unstructured files in storage")
    plan.setdefault("collision_strategy", "RENAME_NUMERIC")

    actions = plan.get("actions")
    if not isinstance(actions, list) or len(actions) == 0:
        raise ValueError("Action Plan must contain a non-empty 'actions' list")

    if len(actions) > 20:
        raise ValueError(f"Blast radius exceeded: Plan contains {len(actions)} actions (max 20)")

    # Validate individual steps
    for i, act in enumerate(actions):
        if not isinstance(act, dict):
            raise ValueError(f"Action at index {i} is not a dictionary")
        act.setdefault("action_id", f"step-{i + 1}")
        act_type = act.get("type")
        if act_type not in ("make_dir", "move", "copy", "trash"):
            raise ValueError(f"Action '{act.get('action_id')}' has invalid type: '{act_type}'")

        if act_type in ("make_dir", "trash"):
            if not act.get("path"):
                raise ValueError(f"Action '{act.get('action_id')}' ({act_type}) missing required 'path'")
        elif act_type in ("move", "copy"):
            if not act.get("source") or not act.get("destination"):
                raise ValueError(f"Action '{act.get('action_id')}' ({act_type}) requires 'source' and 'destination'")

    return plan


# ==============================================================================
# CLI Diff / Table Rendering (Step B)
# ==============================================================================

def render_diff_table(plan: Dict[str, Any], dry_run_summary: Optional[List[Dict[str, Any]]] = None, custom_goal: Optional[str] = None) -> None:
    """
    Renders a clear, formatted CLI table showing original paths -> proposed targets.
    """
    print("\n" + "=" * 95)
    print("                      ACTION PLAN IMPACT REPORT (DRY-RUN)")
    print("=" * 95)
    print(f" Plan ID:            {plan.get('plan_id')}")
    print(f" Description:        {plan.get('description')}")
    if custom_goal:
        print(f" Custom Goal:        {custom_goal}")
    print(f" Collision Policy:   {plan.get('collision_strategy', 'FAIL')}")
    print(f" Total Actions:      {len(plan.get('actions', []))}")
    print("-" * 95)

    headers = ("STEP", "ACTION", "SOURCE / ORIGINAL PATH", "PROPOSED DESTINATION")
    
    rows = []
    actions = plan.get("actions", [])
    for idx, act in enumerate(actions, 1):
        step_id = act.get("action_id", f"step-{idx}")
        act_type = act.get("type", "").upper()
        
        if act_type == "MAKE_DIR":
            src = "-"
            dst = act.get("path", "")
        elif act_type == "TRASH":
            src = act.get("path", "")
            dst = "[.agent_trash soft-delete]"
        elif act_type in ("MOVE", "COPY"):
            src = act.get("source", "")
            dst = act.get("destination", "")
        else:
            src = act.get("source") or act.get("path") or "-"
            dst = act.get("destination") or "-"

        rows.append((str(step_id), act_type, src, dst))

    # Compute column widths
    col_widths = [len(h) for h in headers]
    for row in rows:
        for i, val in enumerate(row):
            col_widths[i] = max(col_widths[i], len(val))

    # Cap widths for clean display
    col_widths[2] = max(col_widths[2], 26)
    col_widths[3] = max(col_widths[3], 32)

    sep_line = "+" + "+".join("-" * (w + 2) for w in col_widths) + "+"
    header_line = "|" + "|".join(f" {headers[i]:<{col_widths[i]}} " for i in range(len(headers))) + "|"

    print(sep_line)
    print(header_line)
    print(sep_line)
    for row in rows:
        line = "|" + "|".join(f" {row[i]:<{col_widths[i]}} " for i in range(len(row))) + "|"
        print(line)
    print(sep_line)
    print("=" * 95)


def render_history_table(query: str, matches: List[Dict[str, Any]]) -> None:
    """
    Prints a clean ASCII table showing the historical trace:
    Original Path -> Current Path | Timestamp | Plan ID
    """
    print("\n" + "=" * 115)
    print(f" DETERMINISTIC HISTORICAL TRACE: Query = '{query}' (Zero-Cost Ledger Resolution)")
    print("=" * 115)
    if not matches:
        print(f"  No historical mutations or ledger records found matching '{query}'.")
        print("=" * 115 + "\n")
        return

    # Header
    print(f" {'Original Path':<36} -> {'Current / Proposed Path':<42} | {'Timestamp':<19} | {'Plan ID'}")
    print("-" * 115)

    for item in matches:
        orig = item.get("source_path") or "-"
        dest = item.get("destination_path") or "-"
        act_type = item.get("type", "")
        ts = item.get("timestamp") or "-"
        pid = item.get("plan_id") or "-"
        is_trashed = item.get("is_trashed", False)
        status = item.get("status", "")

        if is_trashed:
            dest_str = f"[{dest} (TRASHED)]" if dest else "[.agent_trash (TRASHED)]"
        elif status == "REVERTED":
            dest_str = f"{dest} (REVERTED)"
        elif act_type == "make_dir":
            dest_str = f"[Created Directory: {dest or orig}]"
        else:
            dest_str = dest

        orig_disp = (orig[:33] + "...") if len(orig) > 36 else orig
        dest_disp = (dest_str[:39] + "...") if len(dest_str) > 42 else dest_str
        ts_disp = ts[:19]
        pid_disp = (pid[:12] + "...") if len(pid) > 15 else pid

        print(f" {orig_disp:<36} -> {dest_disp:<42} | {ts_disp:<19} | {pid_disp}")

    print("-" * 115)
    print(f" Total records found: {len(matches)}")
    print("=" * 115 + "\n")


# ==============================================================================
# Phase 2.7: Local User Profile & Contextual Routing Resolvers
# ==============================================================================

def load_user_profile(profile_path: Optional[str] = None, bridge: Optional[BridgeClient] = None) -> Dict[str, Any]:
    """
    Loads device-local user profile and peer mapping:
    1. Explicit CLI argument (--profile <path>)
    2. Local file 'user_profile.json' in working/script directory
    3. Remote endpoint on bridge (GET /user_profile)
    4. Fallback default structure
    """
    if profile_path and os.path.exists(profile_path):
        try:
            with open(profile_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"[*] Warning: Could not read profile from {profile_path}: {e}")

    # Check local default in project root
    local_p = os.path.join(os.path.dirname(__file__), "user_profile.json")
    if os.path.exists(local_p):
        try:
            with open(local_p, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass

    # Query bridge
    if bridge:
        try:
            remote_p = bridge.get_user_profile()
            if remote_p:
                return remote_p
        except Exception:
            pass

    return {
        "user_identity": {"primary_name": "User", "aliases": [], "identifiers": [], "organization": ""},
        "known_peers": [],
        "routing_rules": {
            "peer_documents_base": "Documents/Peers",
            "personal_documents_base": "Documents/Personal",
            "academic_base": "Documents/University"
        }
    }


def build_user_profile_prompt_block(profile: Optional[Dict[str, Any]]) -> str:
    """Constructs prompt block defining user identity and strict peer separation rules."""
    if not profile:
        return ""

    identity = profile.get("user_identity", {})
    peers = profile.get("known_peers", [])
    rules = profile.get("routing_rules", {})

    primary_name = identity.get("primary_name", "User")
    aliases = ", ".join(identity.get("aliases", [])) or "None"
    identifiers = ", ".join(identity.get("identifiers", [])) or "None"
    organization = identity.get("organization", "")

    peer_lines = []
    for p in peers:
        p_name = p.get("name", "")
        p_aliases = ", ".join(p.get("aliases", []))
        p_folder = p.get("designated_folder", f"Documents/Peers/{p_name}")
        p_rel = p.get("relation", "Peer")
        peer_lines.append(f"  - '{p_name}' (aliases: [{p_aliases}], relation: {p_rel}) -> Route to '{p_folder}'")
    peer_text = "\n".join(peer_lines) if peer_lines else "  - None registered"

    peer_base = rules.get("peer_documents_base", "Documents/Peers")
    personal_base = rules.get("personal_documents_base", "Documents/Personal")
    academic_base = rules.get("academic_base", "Documents/University")

    block = (
        f"\n*** USER IDENTITY & PEER SEPARATION MATRIX (PHASE 2.7) ***\n"
        f"Primary User Identity (Device Owner):\n"
        f"- Primary Name: '{primary_name}'\n"
        f"- Aliases / Nicknames: [{aliases}]\n"
        f"- Identifiers / Roll Numbers: [{identifiers}]\n"
        f"- Organization: {organization}\n\n"
        f"Known Peers & Third-Party Contacts (MANDATORY PEER SEPARATION):\n"
        f"{peer_text}\n\n"
        f"Contextual Routing Hierarchy:\n"
        f"- Personal Documents Base: '{personal_base}' (EXCLUSIVELY for the primary user '{primary_name}')\n"
        f"- Academic Documents Base: '{academic_base}' (transcripts, assignments, academic results of '{primary_name}')\n"
        f"- Peer Documents Base: '{peer_base}' (all documents belonging to peers or third parties)\n\n"
        f"CRITICAL PEER SEPARATION INVARIANTS:\n"
        f"1. Zero Peer Pollution: NEVER misfile or route third-party or peer documents (e.g. 'fawad fee.pdf', 'Sumbal pass.pdf', 'Ahmed pass.pdf', 'Yousaf Pass.pdf') into '{personal_base}'. '{personal_base}' is strictly reserved for '{primary_name}'.\n"
        f"2. Peer Routing: When a document contains the name, nickname, or alias of a registered peer, route it to that peer's designated folder (e.g., '{peer_base}/<Peer_Name>') instead of personal folders.\n"
        f"3. General / Semantic Gathering Requests: When the user asks to gather files (e.g. 'fee vouchers, receipts, and invoices' to '{personal_base}/Receipts'), ONLY gather files that belong to '{primary_name}'. Do NOT move peer receipts into '{personal_base}/Receipts'; either route peer receipts to their designated peer folder or leave them untouched.\n"
        f"*************************************************************\n"
    )
    return block


# ==============================================================================
# Gemini Reasoning Runner (Two-Stage Agent Protocol)
# ==============================================================================

class GeminiAgentRunner:
    """
    Executes the Gemini reasoning loop with read-only inspection tools.
    Supports both Google GenAI SDK (google.genai) and native REST client fallback.
    """

    def __init__(self, api_key: str, model_name: str, bridge: BridgeClient):
        self.api_key = api_key
        self.model_name = model_name
        self.bridge = bridge

    def execute_tool(self, name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        """Dispatches inspection tool calls to the bridge."""
        if name == "list_files":
            path = args.get("path", "")
            print(f"  [Inspection Tool] list_files(path='{path}')")
            return self.bridge.list_files(path=path)
        elif name == "read_file_snippet":
            path = args.get("path", "")
            max_chars = int(args.get("max_chars", 1000))
            print(f"  [Inspection Tool] read_file_snippet(path='{path}', max_chars={max_chars})")
            raw_res = self.bridge.read_file_snippet(path=path, max_chars=max_chars)
            if isinstance(raw_res, dict) and "snippet" in raw_res:
                raw_res["snippet"] = sanitize_pii(raw_res["snippet"])
            return raw_res
        else:
            return {"error": f"Unknown tool '{name}'"}

    def run(
        self,
        target_folder: str,
        custom_instruction: Optional[str] = None,
        max_iterations: int = 16,
        gather_query: Optional[str] = None,
        gather_target: Optional[str] = None,
        copy_mode: bool = False,
        user_profile: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        Executes the Two-Stage Agent Protocol:
        Stage 1 (Reconnaissance) -> Stage 2 (Plan Synthesis)
        """
        custom_goal_block = ""
        if custom_instruction:
            custom_goal_block = (
                f"\n*** CRITICAL: USER CUSTOM GOAL & PRIORITIZED CRITERIA ***\n"
                f"The user has provided an explicit custom goal for this execution:\n"
                f"\"{custom_instruction}\"\n"
                f"You MUST prioritize this instruction above generic cleanup. Focus your inspection and actions specifically "
                f"on satisfying this criteria. Do not touch or move unrelated files unless requested by this instruction.\n"
                f"*********************************************************"
            )

        user_profile_block = build_user_profile_prompt_block(user_profile)

        system_prompt = SYSTEM_PROMPT_TEMPLATE.format(
            target_folder=target_folder,
            user_profile_block=user_profile_block,
            custom_goal_block=custom_goal_block
        )

        if gather_query:
            target_op = "copy" if copy_mode else "move"
            dst_folder = gather_target or "Documents/Gathered"
            initial_prompt = (
                f"SEMANTIC GATHERING TASK:\n"
                f"- User Intent / Search Topic: '{gather_query}'\n"
                f"- Target Destination Folder: '{dst_folder}'\n"
                f"- Action Type: '{target_op}' (formulate strict '{target_op}' actions)\n\n"
                f"Reconnaissance & Efficiency Directives:\n"
                f"1. Use 'list_files' to inspect '{target_folder}' (and any other relevant directories if needed).\n"
                f"2. Prioritize candidate files using filename cues first. Limit 'read_file_snippet' inspections strictly to only the most probable candidate files (maximum 4-5 snippet inspections).\n"
                f"3. Do NOT exhaust reasoning turns inspecting every unrelated file.\n"
                f"4. Once candidate files are identified or rule-out is clear, synthesize and emit the vetted Action Plan JSON immediately containing a 'make_dir' action for '{dst_folder}' and '{target_op}' actions for all matching files.\n"
                f"5. Leave unrelated files untouched.\n"
                f"6. Output a strict JSON Action Plan adhering to docs/SCHEMA_SPEC.md."
            )
        elif custom_instruction:
            initial_prompt = (
                f"Please inspect the '{target_folder}' directory on the device storage. "
                f"The user has specified an explicit custom goal: '{custom_instruction}'. "
                f"Use 'list_files' to inspect contents, and use 'read_file_snippet' selectively on at most 4-5 key candidate files. "
                f"Once candidates are identified, synthesize an organized folder structure fulfilling this criteria and output a strict JSON Action Plan immediately."
            )
        else:
            initial_prompt = (
                f"Please inspect the '{target_folder}' directory on the device storage. "
                f"Use 'list_files' to inspect contents, and selectively use 'read_file_snippet' on at most 4-5 cryptic or unstructured files. "
                f"Synthesize an organized folder structure and output a strict JSON Action Plan immediately."
            )

        print("\n" + "=" * 70)
        print(" [Stage 1: Reconnaissance] Launching Gemini Decision Engine...")
        print(f" Target Folder:   {target_folder}")
        if user_profile and user_profile.get("user_identity", {}).get("primary_name"):
            owner = user_profile["user_identity"]["primary_name"]
            peer_count = len(user_profile.get("known_peers", []))
            print(f" User Profile:    '{owner}' (Peers Tracked: {peer_count})")
        if gather_query:
            print(f" Semantic Gather: \"{gather_query}\" -> '{gather_target or 'Documents/Gathered'}' (mode: {'copy' if copy_mode else 'move'})")
        elif custom_instruction:
            print(f" Custom Goal:     \"{custom_instruction}\"")
        print(f" Reasoning Model: {self.model_name}")
        print(" Inspection Tools: list_files(path), read_file_snippet(path, max_chars)")
        print("=" * 70)

        # Attempt to use google-genai SDK if available
        if HAVE_GENAI_SDK:
            try:
                return self._run_with_genai_sdk(system_prompt, initial_prompt, max_iterations)
            except Exception as e:
                print(f"[*] Note: Google GenAI SDK encountered '{e}'. Falling back to REST driver...")

        # Fallback to direct REST driver
        return self._run_with_rest_client(system_prompt, initial_prompt, max_iterations)

    def _run_with_genai_sdk(self, system_prompt: str, user_prompt: str, max_turns: int = 16) -> Dict[str, Any]:
        """Runs the loop using google-genai SDK."""
        client = genai.Client(api_key=self.api_key)
        
        # Build tool declarations
        sdk_tools = [
            types.Tool(function_declarations=[
                types.FunctionDeclaration(
                    name="list_files",
                    description="Lists files in a folder relative to storage root.",
                    parameters=types.Schema(
                        type="OBJECT",
                        properties={"path": types.Schema(type="STRING", description="Target path")},
                        required=["path"]
                    )
                ),
                types.FunctionDeclaration(
                    name="read_file_snippet",
                    description="Reads file snippet or PDF text.",
                    parameters=types.Schema(
                        type="OBJECT",
                        properties={
                            "path": types.Schema(type="STRING", description="File path"),
                            "max_chars": types.Schema(type="INTEGER", description="Max chars")
                        },
                        required=["path"]
                    )
                )
            ])
        ]

        chat = client.chats.create(
            model=self.model_name,
            config=types.GenerateContentConfig(
                system_instruction=system_prompt,
                tools=sdk_tools,
                temperature=0.2
            )
        )

        response = chat.send_message(user_prompt)
        for turn in range(1, max_turns + 1):
            if not response.function_calls:
                return extract_json_plan(response.text or "")

            tool_outputs = []
            for call in response.function_calls:
                res = self.execute_tool(call.name, dict(call.args or {}))
                tool_outputs.append(
                    types.Part.from_function_response(
                        name=call.name,
                        response={"result": res}
                    )
                )

            response = chat.send_message(tool_outputs)

        raise TimeoutError(f"Agent exceeded {max_turns} reasoning turns without emitting Action Plan")

    def _run_with_rest_client(self, system_prompt: str, user_prompt: str, max_turns: int = 16) -> Dict[str, Any]:
        """Runs the loop using direct Gemini REST API."""
        models_to_try = [self.model_name]
        if self.model_name != "gemini-3.5-flash-lite":
            models_to_try.append("gemini-3.5-flash-lite")

        active_model = None
        for candidate_model in models_to_try:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{candidate_model}:generateContent?key={self.api_key}"
            try:
                # Test connectivity
                test_res = requests.post(
                    url,
                    json={"contents": [{"parts": [{"text": "ping"}]}]},
                    timeout=8
                )
                if test_res.status_code == 200:
                    active_model = candidate_model
                    break
                elif test_res.status_code == 404:
                    print(f"[*] Model '{candidate_model}' is not available (404). Trying next model...")
            except Exception:
                continue

        if not active_model:
            active_model = self.model_name

        endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/{active_model}:generateContent?key={self.api_key}"
        conversation = [{"role": "user", "parts": [{"text": user_prompt}]}]

        for turn in range(1, max_turns + 1):
            payload = {
                "system_instruction": {"parts": [{"text": system_prompt}]},
                "contents": conversation,
                "tools": INSPECTION_TOOLS,
                "generationConfig": {"temperature": 0.2}
            }

            resp = requests.post(endpoint, json=payload, timeout=90)
            if not resp.ok:
                raise RuntimeError(f"Gemini API error (HTTP {resp.status_code}): {resp.text}")

            data = resp.json()
            candidates = data.get("candidates", [])
            if not candidates:
                raise RuntimeError(f"No candidates returned by Gemini: {data}")

            candidate = candidates[0].get("content", {})
            conversation.append(candidate)

            parts = candidate.get("parts", [])
            func_calls = [p["functionCall"] for p in parts if "functionCall" in p]

            if not func_calls:
                # Completed reasoning; extract JSON
                full_text = "\n".join(p.get("text", "") for p in parts if "text" in p)
                return extract_json_plan(full_text)

            # Process function calls
            tool_responses = []
            for call in func_calls:
                name = call.get("name")
                args = call.get("args", {})
                res = self.execute_tool(name, args)
                tool_responses.append({
                    "functionResponse": {
                        "name": name,
                        "response": {"result": res}
                    }
                })

            conversation.append({
                "role": "user",
                "parts": tool_responses
            })

        raise TimeoutError(f"Agent exceeded {max_turns} reasoning turns without emitting Action Plan")


# ==============================================================================
# Local Mock Bridge Support (For Sandbox Testing)
# ==============================================================================

def start_local_mock_bridge(storage_dir: str) -> Tuple[Any, str]:
    """Spins up a local test instance of server.py for sandboxed testing."""
    os.environ["STORAGE_BASE_DIR"] = storage_dir
    os.environ["LEDGER_DB_PATH"] = os.path.join(storage_dir, ".ledger.db")

    import server
    server.BASE_DIR = os.path.realpath(storage_dir)
    server.TRASH_DIR = os.path.join(server.BASE_DIR, ".agent_trash")
    server.LEDGER_DB_PATH = os.path.join(server.BASE_DIR, ".ledger.db")
    os.makedirs(server.BASE_DIR, exist_ok=True)
    os.makedirs(server.TRASH_DIR, exist_ok=True)
    server.init_db()

    from werkzeug.serving import make_server
    srv = make_server("127.0.0.1", 8888, server.app)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, "http://127.0.0.1:8888"

def seed_synthetic_fixtures(storage_dir: str, target_folder: str = "Download") -> Dict[str, str]:
    """Creates realistic messy files for demonstration and testing."""
    fixtures = {
        f"{target_folder}/1508.06576v2.pdf": (
            "A Neural Algorithm of Artistic Style\n"
            "Leon A. Gatys, Alexander S. Ecker, Matthias Bethge\n"
            "arXiv:1508.06576v2 [cs.CV] 2 Sep 2015\n"
            "In fine art, especially painting, humans have mastered the skill to create unique visual experiences "
            "through composing a complex interplay between the content and style of an image..."
        ),
        f"{target_folder}/1706.03762v7.pdf": (
            "Attention Is All You Need\n"
            "Ashish Vaswani, Noam Shazeer, Niki Parmar, Jakob Uszkoreit\n"
            "arXiv:1706.03762v7 [cs.CL] 2 Aug 2017\n"
            "The dominant sequence transduction models are based on complex recurrent or convolutional neural networks. "
            "We propose the Transformer, a novel model architecture based entirely on attention mechanisms..."
        ),
        f"{target_folder}/DOC-20220503-WA0103.pdf": (
            "ACME CLOUD HOSTING - OFFICIAL TAX INVOICE\n"
            "Invoice Number: INV-2022-05-9981\n"
            "Billing Period: April 2022\n"
            "Total Amount: $49.00 USD\n"
            "Status: Paid in Full via Credit Card."
        ),
        f"{target_folder}/fawad fee.pdf": (
            "NATIONAL UNIVERSITY OF SCIENCES AND TECHNOLOGY\n"
            "Student Fee Voucher - Fall Semester\n"
            "Student Name: Fawad Khan\n"
            "Roll No: BSAI-201\n"
            "Total Amount: PKR 145,000\n"
            "Status: Paid via HBL Online."
        ),
        f"{target_folder}/Imran Tahir BSAI-182.pdf": (
            "NATIONAL UNIVERSITY OF SCIENCES AND TECHNOLOGY\n"
            "Student Fee Voucher - Fall Semester\n"
            "Student Name: Imran Tahir\n"
            "Roll No: BSAI-182\n"
            "Total Amount: PKR 145,000\n"
            "Status: Paid via HBL Online."
        ),
        f"{target_folder}/temp_session_cache_09f8a.tmp": (
            "SESSION_ID=9f8a7c2b-expired\n"
            "TIMESTAMP=1651582800\n"
            "EPHEMERAL CACHE DUMP - SAFE TO REMOVE."
        )
    }
    for rel_path, content in fixtures.items():
        abs_p = os.path.join(storage_dir, rel_path)
        os.makedirs(os.path.dirname(abs_p), exist_ok=True)
        with open(abs_p, "w", encoding="utf-8") as f:
            f.write(content)
    return fixtures


# ==============================================================================
# Main Execution Pipeline (Steps A through E)
# ==============================================================================

def main():
    load_environment()
    args = parse_cli_args()

    print("\n" + "=" * 70)
    print(" Mobile Agent Storage Bridge: Phase 2 Decision Engine & Runner")
    print("=" * 70)

    local_srv = None
    temp_dir_obj = None

    try:
        # 1. Bridge Connection Resolution
        if args.local:
            print("[*] Mode: Local mock sandbox (--local specified).")
            temp_dir_obj = tempfile.TemporaryDirectory(prefix="agent_bridge_test_")
            storage_dir = os.path.realpath(temp_dir_obj.name)
            seed_synthetic_fixtures(storage_dir, args.target_folder)
            local_srv, bridge_url = start_local_mock_bridge(storage_dir)
            time.sleep(0.4)
            print(f"[*] Local bridge initialized at {bridge_url} (Sandbox: {storage_dir})")
        elif args.url:
            bridge_url = args.url.rstrip("/")
            print(f"[*] Target Bridge URL: {bridge_url}")
        else:
            env_url = os.getenv("PHONE_URL")
            if env_url:
                bridge_url = env_url.rstrip("/")
                print(f"[*] Target Bridge URL (from .env): {bridge_url}")
            else:
                print("\n[FATAL ERROR] No bridge URL specified.")
                print("Provide '--url <URL>' or '--local', or set PHONE_URL in your .env file.")
                sys.exit(1)

        bridge = BridgeClient(bridge_url)

        # 2. Health Check
        print("[*] Checking bridge connectivity...")
        try:
            health_info = bridge.health(timeout=15)
            print(f"  ✓ Bridge Online: engine='{health_info.get('engine')}', base='{health_info.get('base_dir')}'")
        except Exception as exc:
            print(f"\n[FATAL ERROR] Could not connect to bridge at {bridge_url}:")
            print(f"  {exc}")
            print("\nPlease check that the phone server is running and Cloudflare tunnel / IP is accessible.")
            sys.exit(1)

        # 3. Deterministic Historical Lookup (--find / -f)
        # Bypasses LLM reasoning entirely for instant zero-cost resolution
        if args.find:
            print(f"[*] Querying historical ledger on bridge for: '{args.find}'...")
            res = bridge.lookup_history(args.find)
            if "error" in res and not res.get("matches"):
                print(f"[ERROR] Failed querying history: {res.get('error')}")
                sys.exit(1)
            matches = res.get("matches", [])
            render_history_table(args.find, matches)
            return

        # 4. API Key Verification (Only required for LLM reasoning and gathering)
        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            print("\n[FATAL ERROR] GEMINI_API_KEY is not set.")
            print("Please configure it in your .env file or environment.")
            sys.exit(1)

        # 5. Resolve Custom Instruction / Semantic Gathering Directives
        custom_instruction = args.prompt
        if args.gather:
            target_op = "copy" if args.copy else "move"
            gather_directive = (
                f"SEMANTIC GATHERING DIRECTIVE:\n"
                f"- User Intent: {args.gather}\n"
                f"- Target Destination Folder: '{args.to}'\n"
                f"- Action Type: '{target_op}' (Formulate strict '{target_op}' actions for all matching files into '{args.to}')\n"
                f"- Ensure a 'make_dir' action is included for '{args.to}' before moving or copying files into it.\n"
                f"- Leave all non-matching files untouched in their current directories.\n"
                f"- Comply strictly with blast radius limit (max 20 actions)."
            )
            if custom_instruction:
                custom_instruction = f"{gather_directive}\n- Additional Instructions: {custom_instruction}"
            else:
                custom_instruction = gather_directive
        elif not custom_instruction and not args.auto_approve and sys.stdin.isatty():
            try:
                user_input = input("\nCustom Goal / Instruction (press Enter for general cleanup): ").strip()
                if user_input:
                    custom_instruction = user_input
            except (KeyboardInterrupt, EOFError):
                pass

        # 6. Load User Profile & Peer Separation Matrix (Phase 2.7)
        user_profile = load_user_profile(args.profile, bridge)
        if user_profile and user_profile.get("user_identity", {}).get("primary_name"):
            owner = user_profile["user_identity"]["primary_name"]
            peer_count = len(user_profile.get("known_peers", []))
            print(f"[*] Loaded User Profile: Owner='{owner}', Registered Peers={peer_count}")

        # 7. Reconnaissance & Plan Synthesis (Two-Stage Agent Protocol)
        runner = GeminiAgentRunner(api_key=api_key, model_name=args.model, bridge=bridge)
        action_plan = runner.run(
            target_folder=args.target_folder,
            custom_instruction=custom_instruction,
            max_iterations=args.max_iterations,
            gather_query=args.gather,
            gather_target=args.to if args.gather else None,
            copy_mode=args.copy,
            user_profile=user_profile
        )

        print("\n[Stage 2: Plan Synthesis Complete] Vetted Action Plan:")
        print(json.dumps(action_plan, indent=2))

        # ======================================================================
        # Step A: Dry-Run Submission to POST /execute_plan
        # ======================================================================
        print("\n[Step A] Submitting Action Plan to POST /execute_plan (dry_run: true)...")
        try:
            dry_run_res = bridge.execute_plan(action_plan, dry_run=True)
            print(f"  ✓ Gatekeeper Validation Succeeded: {dry_run_res.get('actions_validated')} actions verified.")
        except Exception as gatekeeper_err:
            print(f"\n[POLICY GATEKEEPER VIOLATION] Plan rejected by bridge safety guardrails:")
            print(f"  {gatekeeper_err}\n")
            sys.exit(1)

        # ======================================================================
        # Step B: Render CLI Diff/Table
        # ======================================================================
        print("\n[Step B] Rendering proposed filesystem mutations table...")
        render_diff_table(action_plan, dry_run_res.get("summary"), custom_goal=custom_instruction)

        if args.dry_run_only:
            print("\n[*] Flag '--dry-run-only' specified. Stopping without live execution.")
            return

        # ======================================================================
        # Step C: Interactive User Confirmation ([y/N])
        # ======================================================================
        if args.auto_approve:
            print("\n[Step C] [Auto-Approve Enabled] Automatically confirming plan execution.")
            confirmed = True
        else:
            try:
                prompt_text = "\n[Step C] Execute this Action Plan on device storage? [y/N]: "
                user_input = input(prompt_text).strip().lower()
                confirmed = user_input in ("y", "yes")
            except (KeyboardInterrupt, EOFError):
                confirmed = False

        if not confirmed:
            print("\n[Cancelled] Plan execution aborted by user. Zero filesystem mutations performed.")
            return

        # ======================================================================
        # Step D: Live Execution (POST /execute_plan, dry_run: false)
        # ======================================================================
        print(f"\n[Step D] Sending POST /execute_plan (Live Execution, Batch ID: {action_plan['plan_id']})...")
        live_res = bridge.execute_plan(action_plan, dry_run=False)
        print(f"  ✓ Execution SUCCESS: {live_res.get('executed_actions')} actions executed and pre-logged to SQLite!")

        # ======================================================================
        # Step E: Immediate 1-Tap Rollback Prompt
        # ======================================================================
        plan_id = action_plan["plan_id"]
        trigger_rollback = False

        if args.test_rollback:
            print("\n[Step E] [--test-rollback specified] Triggering automated rollback verification...")
            trigger_rollback = True
        elif args.auto_approve:
            print(f"\n[Step E] [Auto-Approve Enabled] Batch retained. Batch ID for manual rollback: {plan_id}")
        else:
            try:
                rollback_prompt = (
                    f"\n[Step E] [Action Complete] Type 'undo' to revert batch, or press Enter to keep: "
                )
                undo_choice = input(rollback_prompt).strip().lower()
                if undo_choice == "undo":
                    trigger_rollback = True
            except (KeyboardInterrupt, EOFError):
                pass

        if trigger_rollback:
            print(f"\n[*] Triggering POST /rollback_batch for Batch ID '{plan_id}'...")
            rollback_res = bridge.rollback_batch(plan_id)
            print(f"  ✓ Rollback COMPLETED: {rollback_res.get('reverted_actions')} actions reverted.")
            print(f"  ✓ Files untrashed and directory structure restored.")
        else:
            print(f"\n[*] Batch '{plan_id}' permanently applied to device storage.")

        print("\n" + "=" * 70)
        print(" PHASE 2 EXECUTION PIPELINE FINISHED SUCCESSFULLY")
        print("=" * 70)

    finally:
        if local_srv:
            local_srv.shutdown()
        if temp_dir_obj:
            temp_dir_obj.cleanup()

if __name__ == "__main__":
    main()
