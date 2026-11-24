# Storage Copilot — Architectural Questions, Suggestions & Decision Ledger
> **Target Version**: `v0.8.0` (Phase 9.3 Post-Retest Hardening Complete)  
> **Status**: Ready for User Review & Sign-Off  
> **Maintained By**: Antigravity Assistant & Engineering Team

---

## 1. Executive Summary of Phase 9.x & 9.3 Hardening

All P0, P1, P2, and Product Pillar requirements from the owner's v2 specification and Phase 9.3 hardening directives have been implemented, verified, and backed by automated test suites:

### Phase 9.3 Key Completions
1. **Purged `user_profile.json` from Git History**:
   - Executed `git filter-repo --path user_profile.json --invert-paths --force`.
   - Verified `git log --all -- user_profile.json` is completely empty across all historical commits.
   - Re-linked remote origin to `https://github.com/ImranTahir673/mobile-agent-storage-bridge.git`.
   - **Notice**: Because commit hashes were rewritten during git history scrubbing, pushing to GitHub will require a force push (`git push origin main --force`).
2. **Seed-Fixture Isolation & Cleanup**:
   - `POST /seed_fixtures` writes only to `<BASE_DIR>/.demo_sandbox/Download/` (never polluting real `/storage/emulated/0/Download/`).
   - Added `FIXTURE_SIGNATURES` (exact SHA-256 hashes of the 12 seed stubs).
   - Scanner and deduplication engines automatically exclude `.demo_sandbox/` and files matching `FIXTURE_SIGNATURES`.
   - Added `GET /scan_seed_fixtures` (detects any seed stubs accidentally placed in real storage and returns standard `trash` plan) and `POST /clear_demo_sandbox` to purge demo files.
3. **AI-First Always & Specific Diagnostics**:
   - Removed all pre-AI greeting/regex shortcuts (`TRIVIAL_REFLEX_EXACT`). Gemini micro-classifier runs on every prompt when API key is configured.
   - When offline, rate-limited, daily quota exhausted, or unconfigured, sets explicit diagnostic fields: `degraded_fallback: True`, `fallback_reason` (`no_api_key`, `no_internet`, `daily_quota_exceeded`, `rate_limited`), and user-facing `fallback_notice`.
4. **Corrupt File Detection & Binary Clean Snippets**:
   - Added `inspect_file_health(file_path)`: flags 0-byte files, stub/truncated PDFs (missing `%PDF-` header or unparseable), and corrupt zip/docx files with `is_corrupt=True`.
   - Organise turns route damaged files into `Documents/Review/Damaged/` quarantine bucket.
   - Added `is_human_readable_text()`: intercepts raw `%PDF-` streams or >20% binary non-printable content, returning `"Scanned document — text not extractable"` with `is_scanned_or_binary=True`.
5. **Search Ranking & Quarantine Penalties**:
   - Added `-80.0` penalty for `META-INF/` files (`.sf`, `.rsa`, `.dsa`, `.mf`).
   - Added `-25.0` penalty for `Documents/Peers/*` when the query does not explicitly name that peer.
   - Added `-40.0` penalty for `is_scanned_or_binary=True` on document content queries.
6. **Persistent SQLite Content Index & Blast Radius Limits**:
   - Added `file_content_index` table in `.ledger.db` with indexed mtime cache for instant warm searches (< 2.0s over 500 files).
   - Enforced default 20 actions per batch (unless `allow_large_batch=True`) and 500 MB cumulative size cap.
   - Unknown batch cancel requests return 404; completed batches return 409.
7. **Modular Tier Handlers**:
   - Monolithic `tier_handlers.py` decomposed into `conversation_handler.py`, `query_handler.py`, and `operation_handler.py`, with `tier_handlers.py` serving as a thin facade.
8. **One-Click Local Boot URL**:
   - Server prints `[Storage Copilot] One-Click Local Boot URL: http://127.0.0.1:<PORT>/?token=<TOKEN>` on boot.
   - `web/app.js` captures `?token=` parameter on load, stores it in `localStorage` and `state.bearerToken`, and strips it from the address bar via `window.history.replaceState`.

---

## 2. Structured Questions & Suggestions for User Review (Active Items)

### Item 1: AI-Fallback Quality Bar
- **Current State**: When Gemini is offline, rate-limited, or unconfigured, the system degrades to deterministic local heuristics with an explicit notice (`fallback_notice`: `"AI unavailable right now, using on-device fallback"`). Local heuristics classify core queries (receipts, course codes, forgotten files, exact & near duplicates, format sorting) reliably.
- **Proposal**: For complex multi-intent conversational queries during an AI outage, should the offline fallback:
  - [ ] **Option A (Recommended)**: Propose safe read-only reconnaissance (QUERY_INFO) or prompt the user to clarify, never guessing destructive file operations while degraded.
  - [ ] **Option B**: Attempt aggressive heuristic pattern extraction for multi-step mutations even while degraded.

### Item 2: `InferenceProvider` Backend-Seam Readiness
- **Current State**: Phase 9.x introduced the abstract `InferenceProvider` interface and `DirectGeminiProvider`. Currently, calls go directly to the Google Gemini API.
- **Proposal**: As the prototype matures towards Phase 10, how should the backend seam evolve?
  - [ ] **Option A (Recommended)**: Retain `DirectGeminiProvider` for direct on-device execution (BYOK - Bring Your Own Key) while keeping the provider seam open for an optional self-hosted or proxy inference provider later.
  - [ ] **Option B**: Implement an on-device local LLM provider seam (e.g., MediaPipe LLM Inference / Gemma 2B on NPU).

### Item 3: Gated Sample-File Demo-Mode Design
- **Current State**: Seed fixtures are quarantined in `<BASE_DIR>/.demo_sandbox/Download/` with `FIXTURE_SIGNATURES` tracking SHA-256 hashes. Normal scans completely ignore this directory unless in demo mode.
- **Proposal**: How should the Web UI present demo mode vs live production mode?
  - [ ] **Option A (Recommended)**: Add a visual "Demo Mode" toggle pill in the UI header. When active, queries target `.demo_sandbox/Download/`; when inactive, operations target the real `/storage/emulated/0/Download/`.
  - [ ] **Option B**: Keep current behavior where demo fixtures are strictly isolated in `.demo_sandbox/` and only inspected via dedicated `/scan_seed_fixtures` or developer testing.

---

## 3. Decided Archive Section (Phase 9.3 Decisions)

### Item 1: Bearer Token Auto-Handshake on Mobile Termux
- **Proposal**: Auto-print one-time bootstrap token URL (`http://127.0.0.1:<PORT>/?token=...`) on server boot vs manual token modal input.
- **Decision**: **Option A (Decided)** — Implemented in `server.py` and `web/app.js`. Server prints one-click local boot URL on startup; Web UI captures `?token=` parameter, stores it, and cleans the URL bar via `window.history.replaceState`.

### Item 2: Android FUSE Inotify / Watchdog File Watcher
- **Proposal**: Add background `watchdog` daemon thread pushing SSE events vs lightweight directory signature checks (`mtime` + count).
- **Decision**: **Option B (Decided)** — No watchdog daemon. Retained lightweight O(1) signature check to preserve Android battery life and avoid FUSE inotify descriptor lockups.

### Item 3: Near-Duplicate 3-Gram Jaccard Threshold Slider
- **Proposal**: Expose a 70%–95% sensitivity slider in Web UI vs retaining fixed 85% threshold.
- **Decision**: **Option B (Decided)** — No sensitivity slider UI. Retained fixed 85% default with adaptive character 4-gram shingling for short texts.

### Item 4: Native Android APK (Kotlin / Jetpack Compose) Track
- **Proposal**: Begin Phase 10 Native Android Service & Jetpack Compose UI vs continuing Web UI & Termux post-retest hardening.
- **Decision**: **Option B (Decided)** — No Kotlin / Phase 10 yet. Focus on physical Android Termux testing and Web UI hardening first.

---

## 4. Git History Rewrite Notice & Force Push Instructions

> [!WARNING]
> Because `git filter-repo --path user_profile.json --invert-paths --force` was executed to scrub private identity data (`user_profile.json`) from the entire git commit history, commit hashes have diverged from the remote repository.
> When you are ready to publish these changes to GitHub, execute:
> ```bash
> git push origin main --force
> ```
> This will synchronize the clean, redacted git history with the remote repository.
