# Storage Copilot — Phase 3: Security Hardening & Zero-Trust Boundary
## Architectural Specification & Implementation Plan

> **Target Phase**: Phase 3 of 8  
> **Status**: Ready for Implementation  
> **Pre-requisite**: Phase 1 (Modular Decomposition) & Phase 2 (Three-Tier Intent Classifier) completed. All 170+ existing tests pass.

---

## 1. Executive Summary & Objective

In **Phase 1** and **Phase 2**, Storage Copilot was decomposed into a modular package (`storage_copilot/`) and upgraded to a Three-Tier Intent Architecture (`CONVERSATION`, `QUERY_INFO`, `FILE_OPERATION`).

However, four critical security and reliability vulnerabilities remain before the daemon can safely operate across untrusted networks or mobile browsers:
1. **Unauthenticated Mutation Endpoints**: Endpoints that modify disk state or configuration (`/execute_plan`, `/write_file`, `/configure_api_key`, `/trash_file`, `/move_file`, `/make_directory`, `/rollback_batch`, `/rollback_last`, `POST /user_profile`) currently accept unauthenticated requests.
2. **Wildcard CORS (`*`) Exposure**: `app.py` emits `Access-Control-Allow-Origin: *`, allowing any malicious third-party website open in the phone's browser to issue Cross-Site Request Forgery (CSRF) calls against `http://127.0.0.1:8080`.
3. **PII Regex Over-Matching (False Positives)**: `pii_sanitizer.py` uses an unanchored standalone regex (`\b[A-Za-z]{1,2}[0-9]{7,8}\b` and `\b\d{13}\b`) that accidentally redacts university registration IDs, course serials, or 13-digit Unix millisecond timestamps (`1713000000000`) while lacking redaction for explicit `password:` / `secret:` / `api_key:` tokens.
4. **SQLite WAL Corruption Risk on Android FUSE**: Storing `.ledger.db` directly on `/storage/emulated/0` (mounted via Android FUSE / `sdcardfs`) risks `sqlite3.OperationalError: disk I/O error` because SQLite Write-Ahead Logging (`WAL`) requires POSIX `mmap` shared memory (`-shm`).

**Phase 3 Goal**:
Establish a **Zero-Trust Security Boundary** with Dual-Mode Bearer Token authentication, strict CORS origin allowlisting, precision context-aware PII regex scrubbing, and FUSE-safe SQLite ledger placement—while maintaining **100% backward compatibility** with all 170+ existing automated tests.

---

## 2. Component Architecture

### 2.1 Dual-Mode Bearer Authentication (`storage_copilot/security/auth.py`)

A 256-bit cryptographic token (`secrets.token_urlsafe(32)`) is generated on first boot and persisted to `~/.storage_copilot_token` with `0600` permissions (or read from `COPILOT_AUTH_TOKEN`).

#### Protected Mutation Endpoints:
- `POST /execute_plan`
- `POST /write_file`
- `POST /configure_api_key`
- `POST /trash_file`
- `POST /move_file`
- `POST /make_directory`
- `POST /rollback_batch`
- `POST /rollback_last`
- `POST /user_profile`

#### Verification Rules:
1. **Explicit Token Validation**: If an `Authorization: Bearer <token>` or `X-Copilot-Token: <token>` header is present on *any* request to a protected endpoint, it is verified in constant time via `hmac.compare_digest()`. If invalid, the server immediately aborts with **`401 Unauthorized`**.
2. **Remote / Cloudflare Tunnel Traffic**: If a request originates from a non-loopback IP (`remote_addr not in ("127.0.0.1", "::1", "localhost")`) OR carries proxy/tunnel headers (`Cf-Connecting-Ip`, `X-Forwarded-For`, `Cf-Ray`), a valid Bearer token is **strictly mandatory**. Missing tokens return **`401 Unauthorized`**.
3. **Strict Local Enforcement Mode (`COPILOT_ENFORCE_AUTH=1`)**: When `config.ENFORCE_LOCAL_AUTH` is enabled (via `COPILOT_ENFORCE_AUTH=1`), even local loopback (`127.0.0.1`) requests must supply the Bearer token.
4. **Local Loopback Test Compatibility (`COPILOT_ENFORCE_AUTH=0` default)**: Direct local loopback requests without tunnel headers and without a foreign `Origin` header are permitted when `ENFORCE_LOCAL_AUTH` is `False`, ensuring all 170+ existing test suites pass without modification.

---

### 2.2 Strict CORS & Origin Firewall (`storage_copilot/security/auth.py`)

Wildcard `Access-Control-Allow-Origin: *` is removed.
- **Trusted Origins**:
  - `http://127.0.0.1` (any port)
  - `https://127.0.0.1` (any port)
  - `http://localhost` (any port)
  - `https://localhost` (any port)
  - `http://[::1]` (any port)
  - `file://` and `capacitor://localhost` (Android WebView / hybrid containers)
  - Any custom domain configured via `CLOUDFLARE_HOSTNAME` in `.env`
- **Cross-Origin Blocking**:
  - If a request carries an `Origin` header that does **not** match the trusted allowlist (e.g. `Origin: https://evil.example.com`), CORS headers are omitted and any preflight (`OPTIONS`) or state-mutating (`POST`, `PUT`, `DELETE`) request is immediately rejected with **`403 Forbidden`**.

---

### 2.3 Context-Aware PII Sanitizer (`storage_copilot/security/pii_sanitizer.py`)

Upgrades `PII_PATTERNS` and adds `contains_tier1_pii()` with strict word-boundary and context guards:
- **Tier 1 Redactions (Zero-Tolerance)**:
  - **CNIC**: Hyphenated `\b\d{5}-\d{7}-\d\b`, labeled `CNIC: \d+`, and unhyphenated 13-digit Pakistani CNICs (`\b[1-8]\d{12}\b` with negative lookarounds excluding Unix millisecond timestamps `17\d{11}` or `16\d{11}` when preceded by timestamp markers).
  - **Passports**: Labeled passport numbers (`Passport No: PK9823412`) and standard passport formats (`\b[A-PR-WY][1-9]\d\s?\d{4}[1-9]\b` or 2-letter + 7-digit passports) while explicitly preserving university roll numbers (`BSAI-182`, `21K-3819`), course codes (`CS-301`), and ArXiv IDs (`1508.06576v2`).
  - **IBAN & Bank Accounts**: `[REDACTED_IBAN]` and `Account: [REDACTED_ACCOUNT]`.
  - **Phone Numbers**: Pakistani (`+923...`, `03...`) and international phone formats $\to$ `[REDACTED_PHONE]`.
  - **Payment Cards**: 16-digit card numbers $\to$ `[REDACTED_CARD]`.
  - **Email Addresses**: `[REDACTED_EMAIL]`.
  - **Credentials & Secrets**: `(password|passwd|pin|secret|token|api_key)\s*[:=]\s*\S+` $\to$ `\1: [REDACTED_SECRET]`.
  - **Formal Administrative Name Labels**: `Student Name: ...`, `Name: ...`, `receipt for ...` $\to$ `[REDACTED_NAME]`.

---

### 2.4 FUSE-Safe SQLite Ledger Path (`storage_copilot/config.py`)

- Adds `resolve_default_ledger_path(base_storage_dir: str) -> str`:
  1. If `LEDGER_DB_PATH` environment variable is explicitly set, uses `os.path.realpath(env_ledger)`.
  2. If running on Android emulated storage (`/storage/emulated/0` or Termux FUSE mount), defaults to `os.path.expanduser("~/.storage_copilot_ledger.db")` in Termux's native POSIX home directory to prevent SQLite WAL `-shm` mmap corruption.
  3. On desktop/sandbox environments, defaults to `os.path.join(base_storage_dir, ".ledger.db")`.

---

## 3. Verification Gates

| Gate | Test Suite | Expected Outcome |
|---|---|---|
| **3.1** | `python test_phase3_security.py` | 100% pass across Bearer Auth, Tunnel enforcement, CORS blocking, PII precision, and FUSE path resolution. |
| **3.2** | Existing 7 test suites | All 170+ existing tests pass with 0 regressions. |