# Storage Copilot (Mobile Agent Storage Bridge)

[![Version](https://img.shields.io/badge/version-v0.6.2--live-blue.svg)](file:///d:/Imran%20Tahir/SIDEHUSTLE/bridging-prototype/storage_copilot/config.py)
[![Phase 1](https://img.shields.io/badge/Phase%201-Decomposition%20Complete-green.svg)](file:///d:/Imran%20Tahir/SIDEHUSTLE/bridging-prototype/docs/ROADMAP.md)
[![Phase 2](https://img.shields.io/badge/Phase%202-Three--Tier%20Routing%20Complete%20(168%2B%20Tests)-green.svg)](file:///d:/Imran%20Tahir/SIDEHUSTLE/bridging-prototype/docs/PHASE2_IMPLEMENTATION_PLAN.md)
[![Phase 3](https://img.shields.io/badge/Phase%203-Security%20Hardening%20(Next)-orange.svg)](file:///d:/Imran%20Tahir/SIDEHUSTLE/bridging-prototype/docs/ROADMAP.md)

An intelligent on-device local storage bridge and autonomous file organizer running directly on Android hardware via **Termux** (`/storage/emulated/0`), managed locally through a mobile web UI or remotely via a browser bridge.

---

## 🚀 Current Architecture State (Phase 1 Complete)

The monolithic `server.py` (3,759 lines) has been structurally decomposed into a clean, modular Python package under [`storage_copilot/`](file:///d:/Imran%20Tahir/SIDEHUSTLE/bridging-prototype/storage_copilot):

```
storage_copilot/
├── __init__.py                  # Package exports & create_app() factory
├── app.py                       # Flask application factory, CORS & blueprint registration
├── config.py                    # Environment, directory paths, and global runtime constants
│
├── api/                         # Flask Blueprints (HTTP API Endpoints)
│   ├── core_routes.py           # /, /health, /api_status, /configure_api_key
│   ├── file_routes.py           # /list_files, /read_file_snippet, /write_file, /move_file, /trash_file
│   ├── plan_routes.py           # /propose_plan, /execute_plan, /recent_batch
│   ├── history_routes.py        # /lookup_history, /rollback_batch, /rollback_last
│   ├── profile_routes.py        # /user_profile (GET/POST)
│   ├── dedup_routes.py          # /scan_duplicates
│   └── utility_routes.py        # /seed_fixtures
│
├── storage/                     # Filesystem Operations & Inspections
│   ├── scanner.py               # collect_storage_files() directory traversals
│   ├── content_extractor.py     # extract_file_snippet() for PDF, TXT, DOCX, CSV
│   ├── deduplication.py         # SHA-256 hash clustering & keeper scoring
│   └── collision_resolver.py    # resolve_collision() & ensure_unique_plan_destinations()
│
├── security/                    # Privacy & Path Isolation
│   ├── pii_sanitizer.py         # RAM-level deterministic regex redaction ([REDACTED_CNIC], etc.)
│   └── path_resolver.py         # Canonical realpath resolution & protected roots gatekeeper
│
├── db/                          # Atomic Ledger & Rollback
│   └── ledger.py                # SQLite WAL connection & schema initialization
│
├── intelligence/                # AI Decision Engines & Search
│   ├── gemini_client.py         # Gemini API client with multi-tier quota & model fallback
│   ├── document_classifier.py   # Query intent parser & stopword removal
│   └── search_engine.py         # Keyword, temporal, and semantic scoring engines
│
├── execution/                   # Action Plan Synthesis & Reversal
│   ├── executor.py              # execute_action_plan() atomic batch executor
│   ├── rollback.py              # Inverted undo vector execution (LIFO rollback)
│   └── plan_builder.py          # Action plan JSON extractor & badge formatter
│
└── advice/                      # User Guidance & Heuristics
    └── advisor.py               # Dynamic storage advice generator
```

[`server.py`](file:///d:/Imran%20Tahir/SIDEHUSTLE/bridging-prototype/server.py) remains in place as a 110-line backward-compatibility proxy shim, ensuring that legacy scripts (`agent_batch_runner.py`) and existing test runners work without modification.

---

## 🧪 Automated Test Suite (162 Passing Tests)

All existing regression and behavioral tests pass with 100% green status:

```bash
# Run all test suites
python test_phase1_deduplication.py         # 51 passed
python test_batch.py --local                # 7 passed
python test_accuracy_privacy.py            # 35 passed
python test_phase35_temporal.py             # 29 passed
python test_v06_live.py                     # 34 passed
python test_dynamic_reasoning_mobile.py     # 6 passed
```

---

## 🔒 Core Invariants & Safety Guarantees

1. **Strict Device-Local PII Shield**:
   - Zero unredacted personal identifiers leave the device.
   - Deterministic RAM-level regex scrubs CNIC, mobile numbers, payment cards, and IBANs before sending snippets to any LLM.
2. **Atomic Rollback & SQLite Ledger**:
   - Every file operation (`move`, `trash`, `make_dir`) pre-logs an **inverted undo vector** to `.ledger.db` before disk mutation.
   - Instant 1-tap rollback (`/rollback_batch` or `/rollback_last`) restores files with byte-for-byte fidelity.
3. **Protected Path Gatekeeper**:
   - `/Android`, system directories, root dotfiles, `.ledger.db`, and `.agent_trash` are strictly blacklisted.
4. **Blast Radius Caps**:
   - Max 20 actions per batch execution; max 500 MB cumulative mutation size.

---

## 🎯 Next Steps: Phase 2 Ready

The detailed engineering plan for **Phase 2: Three-Tier Intent Classifier & Grounded Reasoning Engine** is documented in:

📄 **[docs/PHASE2_IMPLEMENTATION_PLAN.md](file:///d:/Imran%20Tahir/SIDEHUSTLE/bridging-prototype/docs/PHASE2_IMPLEMENTATION_PLAN.md)**

### Phase 2 Scope:
- Extract and decompose `storage_copilot/api/plan_routes.py:propose_plan()` (~1,290 lines).
- Build `storage_copilot/intelligence/intent_classifier.py` with fast-path deterministic rules + cheap micro-LLM intent router (`gemini-3.5-flash-lite`).
- Strict intent separation:
  - `CONVERSATION` $\to$ Chat & dynamic advice (0 file mutations).
  - `QUERY_INFO` $\to$ Read-only file search & snippet citations (0 file mutations).
  - `FILE_OPERATION` $\to$ Action Plan synthesis with diff cards.
- Unify dual scoring functions in `search_engine.py`.
- Eliminate triplicated offline fallback blocks.
