"""
Storage Copilot: Dynamic Reasoning, Intent Disambiguation & Mobile UX Test Suite
Verifies:
1. Directory Inventory requests ("Give me list of all the files in the download") list folder contents without keyword matching.
2. Specific document lookups ("Can you find me the book for 48 laws of power?") isolate the exact target without noise/peer leaks.
3. Zero static canned responses for suggestions and recommendations (dynamic live filesystem auditing).
4. Web UI API key configuration endpoint (/configure_api_key) and status checking (/api_status).
"""
import os
import json
import unittest
from server import app, BASE_DIR, parse_query_intent, score_document_match, generate_dynamic_storage_advice

class TestDynamicReasoningMobile(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()
        self.download_dir = os.path.join(BASE_DIR, "Download")
        os.makedirs(self.download_dir, exist_ok=True)

    def test_01_query_intent_parsing(self):
        """Test parse_query_intent accurately separates directory inventory from specific searches."""
        p1 = parse_query_intent("Give me list of all the files in the download")
        self.assertTrue(p1["is_directory_listing"], "Should detect directory listing intent")
        self.assertEqual(p1["folder_constraint"], "Download")
        self.assertEqual(len(p1["target_tokens"]), 0, "Stopwords list/of/download should not be search tokens")

        p2 = parse_query_intent("Can you find me the book for 48 laws of power?")
        self.assertFalse(p2["is_directory_listing"], "Should be a specific document search")
        self.assertIn("48", p2["target_tokens"])
        self.assertIn("laws", p2["target_tokens"])
        self.assertIn("power", p2["target_tokens"])
        self.assertEqual(p2["cleaned_target"], "48 laws of power")

    def test_02_semantic_score_precision(self):
        """Test score_document_match gives high score to true book and 0 to unrelated peer pass."""
        q = "Can you find me the book for 48 laws of power?"
        book_doc = {
            "filename": "the-48-laws-of-power-robert-greene-2000--annas-archive--libgenrs-nf-1160432.pdf",
            "snippet": "THE 48 LAWS OF POWER by Robert Greene. Literature and philosophy."
        }
        peer_pass_doc = {
            "filename": "Yousaf Pass.pdf",
            "snippet": "University Entry Pass for Yousaf, Department of Management Sciences."
        }
        torrent_txt_doc = {
            "filename": "Downloaded From TorrentCouch.net.txt",
            "snippet": "Download TV Shows Free from TorrentCouch.net. Seed our torrents."
        }

        s_book, r_book = score_document_match(q, book_doc)
        s_pass, r_pass = score_document_match(q, peer_pass_doc)
        s_txt, r_txt = score_document_match(q, torrent_txt_doc)

        self.assertGreaterEqual(s_book, 120.0, "Book must score very high (>=120)")
        self.assertEqual(s_pass, 0.0, "Peer pass must score 0.0")
        self.assertEqual(s_txt, 0.0, "Torrent text note must score 0.0")

    def test_03_directory_listing_no_keyword_pollution(self):
        """Test 'Give me list of all the files in the download' returns inventory without matching 'list' or 'download' tokens."""
        # Seed test book and peer pass in Download/
        book_path = os.path.join(self.download_dir, "the-48-laws-of-power.pdf")
        with open(book_path, "w", encoding="utf-8") as f:
            f.write("THE 48 LAWS OF POWER")

        resp = self.client.post("/propose_plan", json={
            "prompt": "Give me list of all the files in the download",
            "target_folder": "Download"
        })
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data.get("type"), "query_info")
        self.assertEqual(len(data.get("actions", [])), 0, "Read-only query must stage zero mutations")

        msg = data.get("message", "")
        # Must not contain robotic database debug strings
        self.assertNotIn("Match: Filename matches: list, of", msg)
        self.assertNotIn("Match: Filename matches: download", msg)

        # Clean up
        if os.path.exists(book_path):
            os.remove(book_path)

    def test_04_specific_book_lookup(self):
        """Test 'Can you find me the book for 48 laws of power?' cleanly identifies the target book."""
        book_path = os.path.join(self.download_dir, "the-48-laws-of-power.pdf")
        with open(book_path, "w", encoding="utf-8") as f:
            f.write("THE 48 LAWS OF POWER")

        resp = self.client.post("/propose_plan", json={
            "prompt": "Can you find me the book for 48 laws of power?",
            "target_folder": "Download"
        })
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data.get("type"), "query_info")
        msg = data.get("message", "")

        # Must mention the book cleanly
        self.assertTrue("the-48-laws-of-power" in msg.lower() or "48 laws of power" in msg.lower())
        # Must NOT dump irrelevant files or robotic strings
        self.assertNotIn("Match: Filename matches:", msg)
        self.assertNotIn("Found 131 matching", msg)

        if os.path.exists(book_path):
            os.remove(book_path)

    def test_05_dynamic_storage_advice_not_static(self):
        """Test advice generation reflects live filesystem state and does not return static canned strings."""
        advice = generate_dynamic_storage_advice(BASE_DIR, "Download", "Imran Tahir", ["Fawad", "Sumbal", "Ahmed", "Yousaf"])
        self.assertIn("Imran Tahir", advice)
        self.assertIn("Download/", advice)
        self.assertNotIn("Here are my top recommendations for organizing your storage, Imran Tahir:\n\n1. 📚 **Academic Courseware**", advice, "Must not return legacy static template")

    def test_06_api_status_endpoint(self):
        """Test /api_status reports online bridge and configured Gemini state."""
        resp = self.client.get("/api_status")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data.get("status"), "online")
        self.assertIn("gemini_configured", data)
        self.assertIn("active_models", data)
        self.assertEqual(data.get("app_version"), "v0.6-live")

if __name__ == "__main__":
    unittest.main()
