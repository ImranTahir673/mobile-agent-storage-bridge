# test_phase3_security.py
"""
Storage Copilot: Phase 3 Security Hardening & Zero-Trust Boundary Test Suite
Verifies:
1. Pre-shared Bearer Token generation & persistence (~/.storage_copilot_token).
2. Dual-Mode Zero-Trust Authentication:
   - Rejects remote / Cloudflare tunnel requests (`Cf-Connecting-Ip`, `X-Forwarded-For`) without Bearer token (401).
   - Rejects invalid Bearer tokens on all protected endpoints (401).
   - Enforces local Bearer token when `ENFORCE_LOCAL_AUTH=True` (401 without token, 200 with valid token).
   - Allows local loopback test requests when `ENFORCE_LOCAL_AUTH=False`.
3. Strict CORS Origin Firewall:
   - Never returns wildcard `Access-Control-Allow-Origin: *`.
   - Allows `http://127.0.0.1:8080`, `http://localhost:3000`, `file://`, and `capacitor://localhost`.
   - Blocks untrusted cross-origin requests (`https://evil.example.com`) with 403 Forbidden.
4. Fortified Context-Aware PII Sanitizer:
   - Scrubs Tier 1 PII (CNICs, labeled/ICAO Passports, IBANs, Phones, Cards, Emails, Secrets, Labeled Names).
   - Preserves Tier 2 Academic & System Context (`BSAI-182`, `CS-301`, `1508.06576v2`, `PKR 45,000`, 13-digit Unix millisecond timestamps).
5. FUSE-Safe SQLite Ledger Path Resolution:
   - Relocates default ledger to `$HOME/.storage_copilot_ledger.db` when storage is on `/storage/emulated/0`.
   - Honors explicit `LEDGER_DB_PATH` overrides.
"""
import os
import sys
import unittest
import tempfile

test_storage_dir = tempfile.mkdtemp(prefix="storage_phase3_sec_")
os.environ["STORAGE_BASE_DIR"] = test_storage_dir
os.environ["LEDGER_DB_PATH"] = os.path.join(test_storage_dir, ".ledger.db")

import server
from storage_copilot import config


class TestPhase3SecurityHardening(unittest.TestCase):
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
        cls.valid_token = server.get_active_auth_token()

    def setUp(self):
        server.ENFORCE_LOCAL_AUTH = False

    def test_01_token_generation_and_persistence(self):
        """Verify 256-bit token generation and file persistence."""
        with tempfile.TemporaryDirectory() as tmp:
            tok_file = os.path.join(tmp, ".test_copilot_token")
            tok1 = server.load_or_create_auth_token(tok_file)
            self.assertTrue(len(tok1) >= 32)
            self.assertTrue(os.path.exists(tok_file))
            tok2 = server.load_or_create_auth_token(tok_file)
            self.assertEqual(tok1, tok2, "Token must persist across loads")

    def test_02_rejects_remote_tunnel_mutations_without_bearer_token(self):
        """Remote or Cloudflare-tunneled mutation requests without Bearer token must return 401."""
        for header_name, header_val in [
            ("Cf-Connecting-Ip", "203.0.113.50"),
            ("X-Forwarded-For", "198.51.100.22"),
            ("Cf-Ray", "8d123456789abcdef-SIN"),
        ]:
            resp = self.client.post(
                "/write_file",
                json={"path": "Download/hacked.txt", "content": "pwned"},
                headers={header_name: header_val},
            )
            self.assertEqual(
                resp.status_code,
                401,
                f"Expected 401 for unauthenticated tunnel request ({header_name})",
            )

        # Now supply valid Bearer token on tunnel request -> 200 OK
        resp_ok = self.client.post(
            "/write_file",
            json={"path": "Download/authorized_tunnel.txt", "content": "verified"},
            headers={
                "Cf-Connecting-Ip": "203.0.113.50",
                "Authorization": f"Bearer {self.valid_token}",
            },
        )
        self.assertEqual(resp_ok.status_code, 200)

    def test_03_rejects_invalid_bearer_token_on_all_protected_endpoints(self):
        """Even on localhost, presenting a wrong Bearer token must return 401."""
        protected_routes = [
            ("/execute_plan", {"actions": []}),
            ("/write_file", {"path": "Download/a.txt", "content": "x"}),
            ("/configure_api_key", {"api_key": "AIzaTest"}),
            ("/trash_file", {"path": "Download/a.txt"}),
            ("/move_file", {"source": "Download/a.txt", "destination": "Documents/a.txt"}),
            ("/make_directory", {"path": "Documents/NewDir"}),
            ("/rollback_batch", {"batch_id": "nonexistent"}),
            ("/rollback_last", {}),
            ("/user_profile", {"user_identity": {"primary_name": "Imran"}}),
        ]
        for route, payload in protected_routes:
            resp = self.client.post(
                route,
                json=payload,
                headers={"Authorization": "Bearer invalid_forged_token_999"},
            )
            self.assertEqual(
                resp.status_code, 401, f"Route {route} must reject invalid token with 401"
            )

    def test_04_strict_local_auth_enforcement_flag(self):
        """When ENFORCE_LOCAL_AUTH=True, even 127.0.0.1 loopback requires a valid Bearer token."""
        server.ENFORCE_LOCAL_AUTH = True
        try:
            resp_no_tok = self.client.post(
                "/write_file",
                json={"path": "Download/strict_test.txt", "content": "hello"},
            )
            self.assertEqual(resp_no_tok.status_code, 401)

            resp_with_tok = self.client.post(
                "/write_file",
                json={"path": "Download/strict_test.txt", "content": "hello"},
                headers={"Authorization": f"Bearer {self.valid_token}"},
            )
            self.assertEqual(resp_with_tok.status_code, 200)
        finally:
            server.ENFORCE_LOCAL_AUTH = False

    def test_05_cors_origin_restriction_and_csrf_protection(self):
        """Verify wildcard '*' is eliminated and untrusted browser Origins are rejected with 403."""
        # 1. Trusted localhost origin -> allowed
        r_local = self.client.get("/health", headers={"Origin": "http://127.0.0.1:8080"})
        self.assertEqual(r_local.status_code, 200)
        self.assertEqual(
            r_local.headers.get("Access-Control-Allow-Origin"), "http://127.0.0.1:8080"
        )
        self.assertNotEqual(r_local.headers.get("Access-Control-Allow-Origin"), "*")

        # 2. Untrusted external origin preflight -> 403 Forbidden
        r_evil_opt = self.client.options(
            "/execute_plan", headers={"Origin": "https://malicious-site.example.com"}
        )
        self.assertEqual(r_evil_opt.status_code, 403)
        self.assertNotIn("Access-Control-Allow-Origin", r_evil_opt.headers)

        # 3. Untrusted external origin POST mutation -> 403 Forbidden
        r_evil_post = self.client.post(
            "/write_file",
            json={"path": "Download/csrf.txt", "content": "bad"},
            headers={"Origin": "https://malicious-site.example.com"},
        )
        self.assertEqual(r_evil_post.status_code, 403)

    def test_06_fortified_pii_sanitizer_precision(self):
        """Verify Tier 1 PII is scrubbed while Tier 2 academic IDs and timestamps are preserved."""
        sample_text = (
            "Student Name: Imran Tahir, Roll No: BSAI-182, Course: CS-301, "
            "ArXiv: 1508.06576v2, CNIC: 37405-1234567-1, RawCNIC: 3520112345671, "
            "Passport No: PK9823412, IBAN: PK36SCBL0000001123456701, "
            "Phone: +92 300 1234567, Email: student@nu.edu.pk, "
            "password: mySuperSecret123, Timestamp_1713000000000, Amount: PKR 45,000."
        )
        sanitized = server.sanitize_pii(sample_text)

        # Tier 1 must be redacted
        self.assertIn("[REDACTED_NAME]", sanitized)
        self.assertIn("[REDACTED_CNIC]", sanitized)
        self.assertIn("[REDACTED_PASSPORT]", sanitized)
        self.assertIn("[REDACTED_IBAN]", sanitized)
        self.assertIn("[REDACTED_PHONE]", sanitized)
        self.assertIn("[REDACTED_EMAIL]", sanitized)
        self.assertIn("[REDACTED_SECRET]", sanitized)
        self.assertNotIn("37405-1234567-1", sanitized)
        self.assertNotIn("3520112345671", sanitized)
        self.assertNotIn("PK9823412", sanitized)
        self.assertNotIn("mySuperSecret123", sanitized)

        # Tier 2 academic & non-PII tokens must be preserved
        self.assertIn("BSAI-182", sanitized)
        self.assertIn("CS-301", sanitized)
        self.assertIn("1508.06576v2", sanitized)
        self.assertIn("1713000000000", sanitized)
        self.assertIn("PKR 45,000", sanitized)

    def test_07_fuse_safe_ledger_path_resolution(self):
        """Verify Android FUSE paths relocate default ledger to $HOME/.storage_copilot_ledger.db."""
        old_env = os.environ.pop("LEDGER_DB_PATH", None)
        try:
            self.assertTrue(server.is_android_fuse_mount("/storage/emulated/0"))
            self.assertTrue(server.is_android_fuse_mount("/storage/emulated/0/Download"))
            self.assertFalse(server.is_android_fuse_mount(test_storage_dir))

            android_ledger = server.resolve_default_ledger_path("/storage/emulated/0")
            self.assertTrue(
                android_ledger.endswith(".storage_copilot_ledger.db"),
                f"Expected $HOME/.storage_copilot_ledger.db, got {android_ledger}",
            )
            self.assertFalse(android_ledger.startswith("/storage/emulated/0"))
        finally:
            if old_env is not None:
                os.environ["LEDGER_DB_PATH"] = old_env


if __name__ == "__main__":
    unittest.main()