import json
from server import app

app.testing = True
client = app.test_client()

history = []

def send_chat(prompt_text):
    print(f"\n=======================================================")
    print(f"USER: \"{prompt_text}\"")
    print(f"=======================================================")
    
    payload = {
        "prompt": prompt_text,
        "target_folder": "Download",
        "conversation_history": history
    }
    
    r = client.post("/propose_plan", json=payload)
    data = r.json
    
    resp_type = data.get("type")
    msg = data.get("message")
    actions = data.get("actions", [])
    
    print(f"ASSISTANT (type: {resp_type}):")
    print(msg)
    
    if actions:
        print(f"\nSTAGED ACTIONS ({len(actions)}):")
        for a in actions:
            act_type = a.get("type")
            if act_type == "make_dir":
                print(f"  [CREATE FOLDER] {a.get('path')}")
            elif act_type == "move":
                print(f"  [MOVE/RENAME] {a.get('source')} -> {a.get('destination')} ({a.get('rationale')})")
            elif act_type == "trash":
                print(f"  [TRASH] {a.get('path')} ({a.get('rationale')})")
                
    history.append({"role": "user", "content": prompt_text})
    history.append({"role": "assistant", "content": msg})
    return data

# TEST 1: Suggestions Request
t1 = send_chat("What suggestions do you have for my files?")
assert t1.get("type") == "conversation", f"Expected 'conversation', got {t1.get('type')}"
assert len(t1.get("actions", [])) == 0, "Suggestions should not stage file mutations"

# TEST 2: Multi-Turn Affirmation
t2 = send_chat("Yes")
assert t2.get("type") == "plan", f"Expected 'plan', got {t2.get('type')}"
assert len(t2.get("actions", [])) > 0, "Affirmative 'Yes' must stage an action plan!"

# TEST 3: Live Audit Query
t3 = send_chat("Any more sorting for the download?")
assert t3.get("type") == "query_info", f"Expected 'query_info', got {t3.get('type')}"
assert len(t3.get("actions", [])) == 0, "Audit status query should not mutate files"

# TEST 4: Specific Book Search (48 laws of power)
t4 = send_chat("Can you find me the book for 48 laws of power?")
assert t4.get("type") == "query_info", f"Expected 'query_info', got {t4.get('type')}"
assert "48 laws of power" in t4.get("message", "").lower() or "robert greene" in t4.get("message", "").lower(), "Must locate 48 laws of power book!"

# TEST 5: Specific Book Search with Conversational Phrasing (Java Language)
t5 = send_chat("Also can you find me a book for Java Language in the downloads?")
assert t5.get("type") == "query_info", f"Expected 'query_info', got {t5.get('type')}"
assert "java" in t5.get("message", "").lower(), "Must locate Java book in message!"

# TEST 6: Deep Content Summarization (Neural Style Paper)
t6 = send_chat("Can you summarize the main findings of the neural style paper?")
assert t6.get("type") == "query_info", f"Expected 'query_info', got {t6.get('type')}"
assert "neural" in t6.get("message", "").lower() or "gatys" in t6.get("message", "").lower(), "Must summarize neural style paper!"

print("\n\n>>> ALL 6 CHAT & DEEP INSPECTION INTERACTIONS TESTED AND VERIFIED PERFECTLY! <<<")
