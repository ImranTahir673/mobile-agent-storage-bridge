import json
import os
import requests

# 1. Config
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
PHONE_URL = "http://192.168.100.65:8080"
GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-3.5-flash-lite:generateContent?key={GEMINI_API_KEY}"

# 2. Local Functions that execute on the Phone
def list_files(path=""):
    return requests.post(f"{PHONE_URL}/list_files", json={"path": path}).json()

def make_directory(path=""):
    return requests.post(f"{PHONE_URL}/make_directory", json={"path": path}).json()

def move_file(source="", destination=""):
    return requests.post(f"{PHONE_URL}/move_file", json={"source": source, "destination": destination}).json()

def trash_file(path=""):
    return requests.post(f"{PHONE_URL}/trash_file", json={"path": path}).json()

TOOL_FUNCTIONS = {
    "list_files": list_files,
    "make_directory": make_directory,
    "move_file": move_file,
    "trash_file": trash_file,
}

# 3. Tool Declarations for Gemini
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
            }
        ]
    }
]

# 4. Agent Event Loop
conversation_history = [
    {
        "role": "user",
        "parts": [{
            "text": (
                "Inspect my phone's 'Download' folder. Check what files are there, "
                "create a folder named 'AI_Test_Archive', and move at least one test document or image into it. "
                "Summarize what you did."
            )
        }]
    }
]

print("Sending instructions to Gemini Agent...\n" + "="*50)

while True:
    payload = {
        "contents": conversation_history,
        "tools": TOOLS_SCHEMA
    }
    
    res = requests.post(GEMINI_URL, json=payload).json()
    
    if "candidates" not in res:
        print("API Error:", res)
        break

    candidate = res["candidates"][0]["content"]
    conversation_history.append(candidate)
    
    # Check if Gemini wants to call any tool
    function_calls = [part["functionCall"] for part in candidate.get("parts", []) if "functionCall" in part]
    
    if not function_calls:
        # No more tools called; print final answer
        for part in candidate.get("parts", []):
            if "text" in part:
                print("\nAgent Summary:\n" + part["text"])
        break

    # Execute phone operations
    tool_responses = []
    for call in function_calls:
        name = call["name"]
        args = call.get("args", {})
        print(f"\n[AI Calling Phone Tool]: {name}({args})")
        
        # Run function on phone
        result = TOOL_FUNCTIONS[name](**args)
        print(f"[Phone Response]: {result}")
        
        tool_responses.append({
            "functionResponse": {
                "name": name,
                "response": {"result": result}
            }
        })
    
    # Feed tool output back to Gemini
    conversation_history.append({"role": "user", "parts": tool_responses})