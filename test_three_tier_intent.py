# test_three_tier_intent.py
import os
import sys
import unittest
import tempfile

test_storage_dir = tempfile.mkdtemp(prefix="storage_phase2_intent_")
os.environ["STORAGE_BASE_DIR"] = test_storage_dir
os.environ["LEDGER_DB_PATH"] = os.path.join(test_storage_dir, ".ledger.db")
os.environ["GEMINI_API_KEY"] = ""  # Test deterministic & offline resilience

import server
from storage_copilot.intelligence.intent_classifier import UserIntent, classify_intent
from storage_copilot.intelligence.search_engine import score_file_relevance, score_document_match


class TestPhase2ThreeTierIntent(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        server.BASE_STORAGE_DIR = os.path.realpath(test_storage_dir)
        server.BASE_DIR = server.BASE_STORAGE_DIR
        server.TRASH_DIR = os.path.join(server.BASE_DIR, ".agent_trash")
        server.LEDGER_DB_PATH = os.path.join(server.BASE_DIR, ".ledger.db")
        os.makedirs(os.path.join(server.BASE_DIR, "Download"), exist_ok=True)
        os.makedirs(os.path.join(server.BASE_DIR, "Documents"), exist_ok=True)
        server.init_db()
        cls.client = server.app.test_client()
        cls.client.post("/seed_fixtures")

    def test_01_conversation_fast_path_prompts(self):
        prompts = [
            "Hello", "Hii", "hey", "What else", "What else?",
            "what can you do?", "who are you", "help",
            "Some suggestions for my fikes to organize",
            "What do you recommend I do with my files?",
            "How should I organize my documents?",
            "Give me some ideas for organizing files",
            "What can we do to the files on my phone to organize in the vest way possible",
            "Send an email to my professor"
        ]
        for p in prompts:
            res = classify_intent(p)
            self.assertEqual(res["intent"], UserIntent.CONVERSATION, f"Failed CONVERSATION for: {p}")
            self.assertTrue(res["fast_path_matched"])

    def test_02_query_info_fast_path_prompts(self):
        prompts = [
            "List university lectures",
            "Find receipts",
            "Where is the electricity bill",
            "Find me Sumbal pass",
            "What is inside Download?",
            "Can you find me the book for 48 laws of power?",
            "Give me list of all the files in the download",
            "What is my latest download?",
            "Did I download anything today?",
            "What files did I receive yesterday?",
            "What are my oldest files?",
            "Find duplicate files in Download",
            "View organized folders"
        ]
        for p in prompts:
            res = classify_intent(p)
            self.assertEqual(res["intent"], UserIntent.QUERY_INFO, f"Failed QUERY_INFO for: {p}")
            self.assertTrue(res["fast_path_matched"])

    def test_03_file_operation_fast_path_prompts(self):
        prompts = [
            "Organize all my University files in 1 place",
            "Sort student vouchers",
            "Separate peer documents",
            "Clean Download Names",
            "Clean duplicate downloads",
            "Move files older than 30 days to Documents/Archive/",
            "Sort all the other files like docs and pdfs in the download to their best suited places",
            "Find and clean receipts",  # Hybrid intent upgraded to FILE_OPERATION
            "Process next batch"
        ]
        for p in prompts:
            res = classify_intent(p)
            self.assertEqual(res["intent"], UserIntent.FILE_OPERATION, f"Failed FILE_OPERATION for: {p}")
            self.assertTrue(res["fast_path_matched"])

    def test_04_multi_turn_affirmative_resolution(self):
        # 1. Duplicate audit follow-up
        hist_dup = [{"role": "assistant", "content": "### Duplicate File Audit\nTo clean up these redundant copies, say Clean duplicate downloads."}]
        c1 = classify_intent("Yes", conversation_history=hist_dup)
        self.assertEqual(c1["intent"], UserIntent.FILE_OPERATION)
        self.assertEqual(c1["resolved_goal"], "clean duplicate downloads")

        # 2. Peer separation follow-up
        hist_peer = [{"role": "assistant", "content": "I recommend we separate peer documents into Documents/Peers/."}]
        c2 = classify_intent("Proceed", conversation_history=hist_peer)
        self.assertEqual(c2["intent"], UserIntent.FILE_OPERATION)
        self.assertEqual(c2["resolved_goal"], "separate peer documents")

        # 3. Format batch follow-up
        hist_fmt = [{"role": "assistant", "content": "Total: 10 files ready to organize into their respective folders."}]
        c3 = classify_intent("Go ahead", conversation_history=hist_fmt)
        self.assertEqual(c3["intent"], UserIntent.FILE_OPERATION)
        self.assertEqual(c3["resolved_goal"], "process next batch of files in download")

        # 4. Unanchored affirmative with no prior assistant turn -> CONVERSATION
        c4 = classify_intent("Yes", conversation_history=[])
        self.assertEqual(c4["intent"], UserIntent.CONVERSATION)

    def test_05_unified_relevance_scoring_and_wrappers(self):
        book_doc = {
            "filename": "the-48-laws-of-power-robert-greene.pdf",
            "snippet": "THE 48 LAWS OF POWER by Robert Greene."
        }
        pass_doc = {
            "filename": "Sumbal pass.pdf",
            "snippet": "University Entry Pass: Student Name: [REDACTED_NAME], Department: AI."
        }
        r_book = score_file_relevance(book_doc, "Can you find me the book for 48 laws of power?")
        self.assertTrue(r_book["is_relevant"])
        self.assertGreaterEqual(r_book["score"], 60.0)

        # Cautionary penalty: Identity pass must score 0 for lecture queries
        r_pass_lecture = score_file_relevance(pass_doc, "List university lectures")
        self.assertFalse(r_pass_lecture["is_relevant"])
        self.assertEqual(r_pass_lecture["score"], 0.0)

        # Legacy wrapper check (>= 120.0)
        legacy_score, _ = score_document_match("Can you find me the book for 48 laws of power?", book_doc)
        self.assertGreaterEqual(legacy_score, 120.0)

    def test_06_end_to_end_response_envelope(self):
        # Chat turn -> 0 actions, intent == CONVERSATION
        r_chat = self.client.post("/propose_plan", json={"prompt": "What else?"}).get_json()
        self.assertEqual(r_chat["type"], "conversation")
        self.assertEqual(r_chat["intent"], "CONVERSATION")
        self.assertEqual(r_chat["actions"], [])

        # Query turn -> 0 actions, intent == QUERY_INFO
        r_query = self.client.post("/propose_plan", json={"prompt": "Find me Sumbal pass"}).get_json()
        self.assertEqual(r_query["type"], "query_info")
        self.assertEqual(r_query["intent"], "QUERY_INFO")
        self.assertEqual(r_query["actions"], [])

        # Plan turn -> >0 actions, intent == FILE_OPERATION
        r_plan = self.client.post("/propose_plan", json={"prompt": "Sort student vouchers"}).get_json()
        self.assertEqual(r_plan["type"], "plan")
        self.assertEqual(r_plan["intent"], "FILE_OPERATION")
        self.assertGreater(len(r_plan["actions"]), 0)


if __name__ == "__main__":
    unittest.main()
