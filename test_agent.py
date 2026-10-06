import os
import requests

# Load .env if present
env_path = os.path.join(os.path.dirname(__file__), ".env")
if os.path.exists(env_path):
    with open(env_path) as f:
        for line in f:
            if line.strip() and not line.startswith("#") and "=" in line:
                k, v = line.strip().split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

# 1. Config
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
if not GEMINI_API_KEY:
    raise ValueError("GEMINI_API_KEY is not set. Please set it in your .env file or environment.")
PHONE_URL = os.getenv("PHONE_URL", "https://tion-hampshire-feel-ted.trycloudflare.com")
GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-3.5-flash-lite:generateContent?key={GEMINI_API_KEY}"

# 2. Tool Wrappers
def _phone_post(endpoint, data=None):
    res = requests.post(f"{PHONE_URL}{endpoint}", json=data or {}, timeout=10)
    if not res.ok:
        raise RuntimeError(f"HTTP {res.status_code} from bridge: {res.text[:150].strip()}")
    return res.json()

def list_files(path=""):
    return _phone_post("/list_files", {"path": path})

def read_file_snippet(path="", max_chars=1000):
    return _phone_post("/read_file_snippet", {"path": path, "max_chars": max_chars})

def make_directory(path=""):
    return _phone_post("/make_directory", {"path": path})

def move_file(source="", destination=""):
    return _phone_post("/move_file", {"source": source, "destination": destination})

def trash_file(path=""):
    return _phone_post("/trash_file", {"path": path})

def rollback_last():
    return _phone_post("/rollback_last")

TOOL_FUNCTIONS = {
    "list_files": list_files,
    "read_file_snippet": read_file_snippet,
    "make_directory": make_directory,
    "move_file": move_file,
    "trash_file": trash_file,
    "rollback_last": rollback_last,
}

# 3. Tool Schemas for Gemini
TOOLS_SCHEMA = [
    {
        "function_declarations": [
            {
                "name": "list_files",
                "description": "Lists files and folders inside a given path relative to phone storage root.",
                "parameters": {
                    "type": "OBJECT",
                    "properties": {"path": {"type": "STRING", "description": "Folder path (e.g. 'Download')"}},
                    "required": ["path"]
                }
            },
            {
                "name": "read_file_snippet",
                "description": "Reads text contents or extracted PDF text of a file to understand what it contains.",
                "parameters": {
                    "type": "OBJECT",
                    "properties": {
                        "path": {"type": "STRING", "description": "Relative file path (e.g. 'Download/document.pdf')"},
                        "max_chars": {"type": "INTEGER", "description": "Maximum characters to read"}
                    },
                    "required": ["path"]
                }
            },
            {
                "name": "make_directory",
                "description": "Creates a new folder at the given path.",
                "parameters": {
                    "type": "OBJECT",
                    "properties": {"path": {"type": "STRING", "description": "Target folder path"}},
                    "required": ["path"]
                }
            },
            {
                "name": "move_file",
                "description": "Moves or renames a file.",
                "parameters": {
                    "type": "OBJECT",
                    "properties": {
                        "source": {"type": "STRING", "description": "Source path"},
                        "destination": {"type": "STRING", "description": "Destination path"}
                    },
                    "required": ["source", "destination"]
                }
            },
            {
                "name": "trash_file",
                "description": "Safely moves a file to the .agent_trash folder on the phone.",
                "parameters": {
                    "type": "OBJECT",
                    "properties": {"path": {"type": "STRING", "description": "Path to trash"}},
                    "required": ["path"]
                }
            },
            {
                "name": "rollback_last",
                "description": "Reverts the most recent move or trash action.",
                "parameters": {"type": "OBJECT", "properties": {}}
            }
        ]
    }
]

# 4. Prompt: Instruct agent to find a cryptic document and inspect its content
user_query = (
    "Inspect the 'Download' folder. Find one PDF or document that has an uninformative name "
    "(such as an arXiv number like '1508.06576v2.pdf' or a WhatsApp file like 'DOC-20251003-WA0018.'). "
    "Read a snippet of its text, explain what the document actually is, "
    "and suggest a clean, descriptive name for it."
)

conversation_history = [{"role": "user", "parts": [{"text": user_query}]}]

print("Executing Content Inspection Agent...\n" + "="*50)

while True:
    payload = {"contents": conversation_history, "tools": TOOLS_SCHEMA}
    res = requests.post(GEMINI_URL, json=payload).json()
    
    if "candidates" not in res:
        print("API Error:", res)
        break

    candidate = res["candidates"][0]["content"]
    conversation_history.append(candidate)
    
    function_calls = [part["functionCall"] for part in candidate.get("parts", []) if "functionCall" in part]
    
    if not function_calls:
        for part in candidate.get("parts", []):
            if "text" in part:
                print("\nAgent Analysis & Verdict:\n" + part["text"])
        break

    tool_responses = []
    for call in function_calls:
        name = call["name"]
        args = call.get("args", {})
        print(f"\n[AI Calling Tool]: {name}({args})")
        
        try:
            result = TOOL_FUNCTIONS[name](**args)
            preview = str(result)[:200] + "..." if len(str(result)) > 200 else str(result)
            print(f"[Phone Response]: {preview}")
        except Exception as e:
            result = {"error": f"Failed to connect to phone ({PHONE_URL}): {e}"}
            print(f"[Phone Connection Error]: {e}")
        
        tool_responses.append({
            "functionResponse": {
                "name": name,
                "response": {"result": result}
            }
        })
    
    conversation_history.append({"role": "user", "parts": tool_responses})