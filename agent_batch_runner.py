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
        "--max-iterations",
        type=int,
        default=8,
        help="Maximum agent reasoning turns before terminating (default: 8)"
    )
    return parser.parse_args()


# ==============================================================================
# Bridge Client: Communication with Android Storage Bridge
# ==============================================================================

class BridgeClient:
    """Encapsulates HTTP communication with the Mobile Agent Storage Bridge."""

    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")

    def health(self, timeout: int = 10) -> Dict[str, Any]:
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
            timeout=15
        )
        if not resp.ok:
            return {"error": f"HTTP {resp.status_code}: {resp.text.strip()}"}
        return resp.json()

    def read_file_snippet(self, path: str, max_chars: int = 1000) -> Dict[str, Any]:
        """Tool 2: Read-only file snippet / PDF preview inspection."""
        resp = requests.post(
            f"{self.base_url}/read_file_snippet",
            json={"path": path, "max_chars": max_chars},
            timeout=15
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
            timeout=20
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
            timeout=20
        )
        try:
            body = resp.json()
        except Exception:
            body = {"raw": resp.text}

        if not resp.ok:
            err_msg = body.get("error", resp.text)
            raise RuntimeError(f"Bridge rollback error (HTTP {resp.status_code}): {err_msg}")
        return body


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


# ==============================================================================
# Agent System Prompt & Schema Specification
# ==============================================================================

SYSTEM_PROMPT_TEMPLATE = """You are an autonomous Mobile Storage Management Agent operating on an Android device storage bridge.

Your mission is to perform Stage 1 (Reconnaissance) and synthesize Stage 2 (Action Plan):
1. Inspect the target directory '{target_folder}'.
2. Identify messy, cryptic, uninformative, or unstructured filenames:
   - arXiv / Academic paper IDs (e.g. '1508.06576v2.pdf' -> identify author/title and move to 'Documents/Research/...').
   - Messaging / WhatsApp download receipts (e.g. 'DOC-20220503-WA0103.pdf' -> identify invoice/document topic and move or rename).
   - Cryptic hash names, raw download names, and temporary junk files (.tmp, session tokens, obsolete receipts -> soft-delete via 'trash').
3. Use 'read_file_snippet' to inspect file content whenever a filename does not clearly convey its content.
4. When finished inspecting, output a STRICT JSON Action Plan adhering to docs/SCHEMA_SPEC.md:

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
- Blacklisted Targets: NEVER touch or reference '/Android', '.agent_trash', '.ledger.db', or root dotfiles.
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

def render_diff_table(plan: Dict[str, Any], dry_run_summary: Optional[List[Dict[str, Any]]] = None) -> None:
    """
    Renders a clear, formatted CLI table showing original paths -> proposed targets.
    """
    print("\n" + "=" * 95)
    print("                      ACTION PLAN IMPACT REPORT (DRY-RUN)")
    print("=" * 95)
    print(f" Plan ID:            {plan.get('plan_id')}")
    print(f" Description:        {plan.get('description')}")
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
            return self.bridge.read_file_snippet(path=path, max_chars=max_chars)
        else:
            return {"error": f"Unknown tool '{name}'"}

    def run(self, target_folder: str, max_iterations: int = 8) -> Dict[str, Any]:
        """
        Executes the Two-Stage Agent Protocol:
        Stage 1 (Reconnaissance) -> Stage 2 (Plan Synthesis)
        """
        system_prompt = SYSTEM_PROMPT_TEMPLATE.format(target_folder=target_folder)
        initial_prompt = (
            f"Please inspect the '{target_folder}' directory on the device storage. "
            f"Use 'list_files' to inspect contents, and 'read_file_snippet' on any cryptic or unstructured files "
            f"(such as academic papers, WhatsApp receipts, or temporary cache files). "
            f"Synthesize an organized folder structure and output a strict JSON Action Plan."
        )

        print("\n" + "=" * 70)
        print(" [Stage 1: Reconnaissance] Launching Gemini Decision Engine...")
        print(f" Target Folder:  {target_folder}")
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

    def _run_with_genai_sdk(self, system_prompt: str, user_prompt: str, max_turns: int) -> Dict[str, Any]:
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

    def _run_with_rest_client(self, system_prompt: str, user_prompt: str, max_turns: int) -> Dict[str, Any]:
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

            resp = requests.post(endpoint, json=payload, timeout=25)
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
        f"{target_folder}/DOC-20220503-WA0103.pdf": (
            "ACME CLOUD HOSTING - OFFICIAL TAX INVOICE\n"
            "Invoice Number: INV-2022-05-9981\n"
            "Billing Period: April 2022\n"
            "Total Amount: $49.00 USD\n"
            "Status: Paid in Full via Credit Card."
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

    # 1. API Key Verification
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        print("\n[FATAL ERROR] GEMINI_API_KEY is not set.")
        print("Please configure it in your .env file or environment.")
        sys.exit(1)

    local_srv = None
    temp_dir_obj = None

    # 2. Bridge Connection Resolution
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

    # 3. Health Check
    print("[*] Checking bridge connectivity...")
    try:
        health_info = bridge.health(timeout=10)
        print(f"  ✓ Bridge Online: engine='{health_info.get('engine')}', base='{health_info.get('base_dir')}'")
    except Exception as exc:
        print(f"\n[FATAL ERROR] Could not connect to bridge at {bridge_url}:")
        print(f"  {exc}")
        print("\nPlease check that the phone server is running and Cloudflare tunnel / IP is accessible.")
        if local_srv:
            local_srv.shutdown()
        if temp_dir_obj:
            temp_dir_obj.cleanup()
        sys.exit(1)

    try:
        # 4. Reconnaissance & Plan Synthesis (Two-Stage Agent Protocol)
        runner = GeminiAgentRunner(api_key=api_key, model_name=args.model, bridge=bridge)
        action_plan = runner.run(target_folder=args.target_folder, max_iterations=args.max_iterations)

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
        render_diff_table(action_plan, dry_run_res.get("summary"))

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
