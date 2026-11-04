# Storage Copilot — Architectural Questions, Suggestions & Decision Ledger
> **Target Version**: `v0.7.0-live` (Phase 9.x Production Hardening & v2 Audit Complete)  
> **Status**: Ready for User Review & Sign-Off  
> **Maintained By**: Antigravity Assistant & Engineering Team

---

## 1. Executive Summary of Phase 9.x Hardening (Parts 1 & 2)

All P0, P1, P2, and Product Pillar requirements from the v2 specification have been implemented, verified, and backed by automated test suites (100% green across all 55 pytest tests, 29 temporal tests, 51 deduplication tests, 37 accuracy/privacy tests, and batch rollback verification):

### Phase 9.x Part 1 Completions
1. **P0 #1 (Type Safety)**: Fixed missing `Any` import in `storage_copilot/intelligence/gemini_client.py`.
2. **P1 #10 (Zero-Trust Security for Read Endpoints)**: Read endpoints (`/list_files`, `/read_file_snippet`, `/lookup_history`, `/scan_duplicates`, `/scan_forgotten`, `/recent_batch`, `/propose_plan`, `/user_profile`, `/api_status`, `/storage_overview`, `/stream_ticket`) are secured under Bearer authentication (`ALL_PROTECTED_PATHS`) while keeping static UI assets and `/health` public.
3. **P0 #4 & #5 (Recursive Subtrees & Full Storage Search)**: Manifest scanner and search engine now recursively traverse nested subdirectories (e.g., `Documents/University/AI/`) and automatically search across both `Download/` and `Documents/` trees. Background index warm-up indexes both roots on server start.
4. **P0 #6 (Ranked Relevance Lists with Snippets & Match Reasons)**: Search query endpoints return structured match items containing `rank`, `name`, `path`, `size_kb`, `relevance_score`, `match_reasons`, `category`, and PII-sanitized `snippet`.
5. **P0 #2 & #7 (Zero-Match Action Guards & Pre-Filtering)**: Pre-filtering by extension and category is enforced on `--gather` and general reorganization queries. If 0 candidate files match, the system returns a safe No-Op conversation reply and prevents staging plans with 0 file mutations.
6. **P0 #3 & P1 #13 (Segregated Duplicates & 'CANCELLED' Batch Status)**: Exact duplicate cleanups (SHA-256) and near-duplicate cleanups (3-gram Jaccard) are strictly segregated. Batch cancellations commit `status = 'CANCELLED'` to SQLite WAL, and `/recent_batch` queries include cancelled batches to permit 1-tap rollback.
7. **Repository Hygiene**: Added `requirements.txt`, `pyproject.toml`, Apache 2.0 `LICENSE`, untracked personal `user_profile.json` from git history, and synchronized version strings to `v0.7.0-live`.

### Phase 9.x Part 2 Completions
8. **P1 #18 (Collision Suffix Disambiguation)**: Completely removed `clean_token[-6:]` word slicing in `storage_copilot/storage/collision_resolver.py`. Batches with colliding destination stems now cleanly disambiguate using sequential numbering (`_2`, `_3`) or full trailing alphanumeric IDs (`WA0025`), eliminating word fragments like `_e_copy` or `_ecture`.
9. **Product Pillar #2 (Fixed General-Niche Taxonomy & Review Bucket)**: In `storage_copilot/intelligence/document_classifier.py`, every classification returns a `confidence: float`. High-confidence categories return `0.90`. Top-level format buckets added for `MEDIA` (`Documents/Media`, 0.85), `ARCHIVE` (`Documents/Archives`, 0.85), and `APK_PACKAGE` (`Documents/Apps_and_APKs`, 0.85). Ambiguous fallback documents route safely to `Documents/Review` with `type_label = "Needs Review"` and `confidence = 0.40`.
10. **Product Pillar #3 (AI Inference Seam & Honest Fallback Notice)**: Introduced `InferenceProvider` interface and `DirectGeminiProvider` in `gemini_client.py`. When offline, rate-limited, or unconfigured, system degrades gracefully to local heuristics and displays an explicit notice: `"AI unavailable right now, using on-device fallback"`, with UI banner rendering.
11. **P0 #8 (Folder Inventory Queries)**: In `document_classifier.py`, folder queries like `"what is in my download folder?"` match directory listing regex and clear target tokens to list contents rather than filtering by keyword.
12. **P0 #6 (Search Relevance Penalties & Boosts)**: In `storage_copilot/intelligence/search_engine.py`, added `-50.0` penalty for binary/media files on document queries, word-boundary category matching (`\b`), certificate boost (+35.0) for documents, and cross-folder screenshot boost (+40.0).
13. **P0 #9 (Temporal Month/Year Filtering)**: In `tier_handlers.py`, added `<MonthName> <YYYY>` / `<YYYY-MM>` parsing (e.g., `"October 2026"`) with direct manifest date filtering.
14. **P1 #11 & #12 (`/write_file` SQLite WAL Pre-Logging & 500 MB Cap)**: Full ledger integration for `write_file` actions with atomic rollback via `.agent_trash` (`untrash` on overwrite, `trash` on newly written files) and enforcement of a 500 MB cumulative batch size cap.
15. **P2 #22 (PII Sanitizer Refinement)**: Preserves generic `"name":` in JSON/code payloads while redacting formal administrative identity labels (`Student Name:`, `Fee Slip Name:`, etc.).
16. **P2 #21 (Adaptive Shingling for Near-Dedup)**: Uses character 4-grams for short texts (< 40 words) and word 3-grams for long texts (>= 40 words), ensuring short documents with minor edits achieve >= 0.85 Jaccard similarity.
17. **Ephemeral SSE Stream Tickets & Storage Overview**: Implemented `POST /stream_ticket` issuing 30-second single-use tickets (`?ticket=`) for secure EventSource connections, and `GET /storage_overview` providing file distribution stats on boot to power the Web UI welcome card.

---

## 2. Questions & Architectural Suggestions for User Approval

### Item 1: Bearer Token Auto-Handshake on Mobile Termux
- **Current State**: When accessing the Web UI locally on Android (`http://127.0.0.1:8080`), local requests are permitted without authentication when `ENFORCE_LOCAL_AUTH=False`. However, when accessed remotely over Cloudflare Tunnel (`COPILOT_ENFORCE_AUTH=1`), the browser requests Bearer token authentication via the top-right Token pill.
- **Proposal**: Would you like the backend to provide a local one-time bootstrap token URL (e.g. `http://127.0.0.1:8080/?token=...`) printed in Termux console on server boot, so opening the link automatically authenticates the mobile browser session without manual token copy-pasting?
  - [ ] **Option A (Recommended)**: Auto-print one-click authenticated URL in console output on server startup.
  - [ ] **Option B**: Keep current manual token input modal in the Web UI.

### Item 2: Android FUSE Inotify / Watchdog File Watcher
- **Current State**: Directory changes are invalidated using O(1) directory signature comparisons (`mtime` + file count check). This is very lightweight and avoids keeping open file descriptors.
- **Proposal**: Should we integrate a background `watchdog` daemon thread for `/storage/emulated/0/Download` to push SSE `scan_progress` events to the Web UI in real time as new files are downloaded from Telegram, WhatsApp, or Chrome?
  - [ ] **Option A**: Add optional `watchdog` background listener that triggers SSE notification when files land in `Download/`.
  - [ ] **Option B (Recommended)**: Keep current lightweight signature check to preserve Android battery life and avoid FUSE inotify lockups.

### Item 3: Near-Duplicate 3-Gram Jaccard Threshold Slider
- **Current State**: The near-duplicate clustering engine uses a fixed Jaccard similarity threshold of `0.85` (85% text shingle overlap).
- **Proposal**: Should we expose a sensitivity slider in the Web UI (e.g., between 70% and 95%) in the Duplicates toolbar?
  - [ ] **Option A**: Add a sensitivity slider (70% - 95%) in the Web UI toolbar.
  - [ ] **Option B (Recommended)**: Retain 85% default with an optional query parameter `?threshold=0.85`.

### Item 4: Native Android APK (Kotlin / Jetpack Compose) Track
- **Current State**: Phase 8 and Phase 9 provide a responsive Mobile Web UI and Python bridge server running in Termux.
- **Proposal**: Are you ready to begin designing the native Android Kotlin/Compose wrapper (Phase 10) to run the server in a foreground Android Service with notification tray controls?
  - [ ] **Option A**: Proceed to Phase 10 Native Android Service & Jetpack Compose UI.
  - [ ] **Option B**: Focus on further stress-testing and refining the Web UI on physical hardware first.

---

## 3. Decision Log
*(Record user answers here once reviewed)*
- **Decision on Item 1**: Pending user response
- **Decision on Item 2**: Pending user response
- **Decision on Item 3**: Pending user response
- **Decision on Item 4**: Pending user response
