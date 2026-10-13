import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tempfile
import threading
from datetime import datetime
import requests
from werkzeug.serving import make_server

# Set up test environment
test_storage_dir = tempfile.mkdtemp(prefix="storage_user_test_")
os.environ["STORAGE_BASE_DIR"] = test_storage_dir
os.environ["LEDGER_DB_PATH"] = os.path.join(test_storage_dir, ".ledger.db")

import server
server.BASE_STORAGE_DIR = os.path.realpath(test_storage_dir)
server.BASE_DIR = server.BASE_STORAGE_DIR
server.TRASH_DIR = os.path.join(server.BASE_DIR, ".agent_trash")
server.LEDGER_DB_PATH = os.path.join(server.BASE_DIR, ".ledger.db")
os.makedirs(server.BASE_DIR, exist_ok=True)
os.makedirs(server.TRASH_DIR, exist_ok=True)
download_dir = os.path.join(server.BASE_DIR, "Download")
os.makedirs(download_dir, exist_ok=True)
telegram_dir = os.path.join(download_dir, "Telegram")
os.makedirs(telegram_dir, exist_ok=True)
os.makedirs(os.path.join(server.BASE_DIR, "Documents"), exist_ok=True)
server.init_db()

# Start test server
srv = make_server("127.0.0.1", 0, server.app)
thread = threading.Thread(target=srv.serve_forever, daemon=True)
thread.start()
BASE_URL = f"http://127.0.0.1:{srv.server_port}"

def seed_user_files():
    # 1. 2021 file
    f1 = os.path.join(download_dir, "Zulfiqar Ali Hussaini - Dar e Nabi Per.mp3")
    with open(f1, "w") as f: f.write("audio")
    t1 = datetime(2021, 4, 15, 12, 0).timestamp()
    os.utime(f1, (t1, t1))

    # 2. 2023 file (48 laws of power)
    f2 = os.path.join(download_dir, "the-48-laws-of-power-robert-greene-2000--_--1160432.pdf")
    with open(f2, "w") as f: f.write("Law 1: Never outshine the master. Law 2: Never put too much trust in friends.")
    t2 = datetime(2023, 4, 19, 14, 0).timestamp()
    os.utime(f2, (t2, t2))

    # 3. 2023 file (Java Complete Reference on Sep 12, 2023)
    f3 = os.path.join(download_dir, "JavaTheCompleteReference.pdf")
    with open(f3, "w") as f: f.write("Java The Complete Reference Eleventh Edition Herbert Schildt.")
    t3 = datetime(2023, 9, 12, 10, 30).timestamp()
    os.utime(f3, (t3, t3))

    # 4. File in subdirectory Download/Telegram (testing deep 1st-level scanning)
    f4 = os.path.join(telegram_dir, "Telegram_Lecture_Deep_Learning.pdf")
    with open(f4, "w") as f: f.write("Deep Learning Notes downloaded via Telegram.")
    t4 = datetime(2024, 1, 10, 11, 0).timestamp()
    os.utime(f4, (t4, t4))

    # 5. Newer files (2024 - 2026)
    f5 = os.path.join(download_dir, "the-beekeeper-2024-english-ylfy-596836.zip")
    with open(f5, "w") as f: f.write("subtitles")
    t5 = datetime(2024, 4, 20, 15, 0).timestamp()
    os.utime(f5, (t5, t5))

    f6 = os.path.join(download_dir, "today_ai_notes.pdf")
    with open(f6, "w") as f: f.write("Today AI")
    t6 = datetime.now().timestamp()
    os.utime(f6, (t6, t6))

seed_user_files()

def run_tests():
    print("\n--- TEST: '48 law of powers' ---")
    r1 = requests.post(f"{BASE_URL}/propose_plan", json={"prompt": "48 law of powers", "target_folder": "Download"}).json()
    print("Type:", r1.get("type"))
    print("Model source:", r1.get("model_source"))
    print("Is LLM:", r1.get("is_llm"))
    print("Message preview:\n", r1.get("message")[:300])
    assert r1.get("type") in ("query_info", "conversation")
    assert "48-laws-of-power" in r1.get("message", "").lower() or "48 laws" in r1.get("message", "").lower()
    assert bool(r1.get("model_source"))
    assert "model_source" in r1
    print("[PASS] Successfully found 'the-48-laws-of-power' file!")

    print("\n--- TEST: 'Whats the oldest file you can see?' ---")
    r2 = requests.post(f"{BASE_URL}/propose_plan", json={"prompt": "Whats the oldest file you can see?", "target_folder": "Download"}).json()
    print("Type:", r2.get("type"))
    print("Model source:", r2.get("model_source"))
    print("Message preview:\n", r2.get("message")[:300])
    assert r2.get("type") in ("query_info", "conversation")
    # Oldest file is Zulfiqar Ali Hussaini from 2021
    assert "2021" in r2.get("message", "") or "zulfiqar" in r2.get("message", "").lower()
    print("[PASS] Successfully identifies 2021 file as the oldest file!")

    print("\n--- TEST: 'Can you give me the list of files on 12 sep 2023.' ---")
    r3 = requests.post(f"{BASE_URL}/propose_plan", json={"prompt": "Can you give me the list of files on 12 sep 2023.", "target_folder": "Download"}).json()
    print("Type:", r3.get("type"))
    print("Actions length:", len(r3.get("actions", [])))
    print("Model source:", r3.get("model_source"))
    print("Message preview:\n", r3.get("message")[:300])
    assert r3.get("type") == "query_info", f"Expected query_info, got {r3.get('type')}"
    assert len(r3.get("actions", [])) == 0, "Expected ZERO actions for read-only date list query!"
    assert "javathecompletereference" in r3.get("message", "").lower()
    print("[PASS] Successfully listed 'JavaTheCompleteReference.pdf' for September 12, 2023 with ZERO false actions!")

    print("\n--- TEST: Deep Subdirectory Scanning ('find telegram deep learning') ---")
    r4 = requests.post(f"{BASE_URL}/propose_plan", json={"prompt": "find telegram deep learning", "target_folder": "Download"}).json()
    print("Type:", r4.get("type"))
    print("Message preview:\n", r4.get("message")[:300])
    assert "telegram_lecture_deep_learning" in r4.get("message", "").lower()
    print("[PASS] Successfully found file inside Download/Telegram subdirectory!")

    print("\n=======================================================")
    print(" ALL USER REAL-WORLD VALIDATION TESTS PASSED (4/4)!")
    print("=======================================================\n")

if __name__ == "__main__":
    try:
        run_tests()
    finally:
        srv.shutdown()
