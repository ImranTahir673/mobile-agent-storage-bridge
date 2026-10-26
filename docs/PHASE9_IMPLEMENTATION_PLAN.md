# Storage Copilot — Phase 9: AI-First Intent Orchestrator & Conversational Continuity Engine
## Architectural Specification & Implementation Plan

> **Target Phase**: Phase 9 (AI-First Architecture & Multi-Turn Continuity Track)  
> **Status**: Ready for Implementation  
> **Pre-requisite**: Phases 1–8 completed + Commit `88dff30` hotfix applied. All 197 existing tests pass.

---

## 1. Executive Summary & Root-Cause Analysis

During real-world physical hardware testing on Android Termux (`/storage/emulated/0`), three critical architectural anti-patterns were exposed:

1. **The "Heuristic Inversion" Trap**:
   - In Phase 2, the **Deterministic Fast-Path Gate** was placed *ahead* of the LLM to save tokens. In practice, natural human prompts with mobile typos (e.g., *"yes cleen all teh fils in the downlods"*) or nuanced conversational follow-ups either missed the rigid regexes or were hijacked by the wrong regex branch before Gemini was ever consulted.
   - **Phase 9 Fix (AI-First Orchestration)**: Invert the control flow. Whenever a Gemini API key is configured and at least one model in the pool has available quota, **the LLM is the primary brain** for intent classification, typo tolerance, and sub-intent parameter extraction. Deterministic regexes are demoted strictly to:
     a) **Zero-Cost Trivial Reflexes** (exact single-word `"hi"`, `"hello"`, `"cancel"`), and
     b) **Silent Offline Safety Net** when `GEMINI_API_KEY` is unconfigured or *all* models in the pool are simultaneously exhausted.

2. **Global Lockout on Single-Model 429 (`GEMINI_QUOTA_BLOCKED_UNTIL`)**:
   - Previously, hitting a `429 RESOURCE_EXHAUSTED` on `gemini-3.5-flash` set a global 15-minute lockout (`config.GEMINI_QUOTA_BLOCKED_UNTIL = time.time() + 900`), blinding the entire application even when `gemini-3.1-flash-lite` and `gemini-3.5-flash-lite` had abundant quota!
   - **Phase 9 Fix (Per-Model Circuit Breaker & Token-Weight Routing)**: Replace the single global lockout with a per-model health ledger (`MODEL_COOLDOWN_UNTIL[model_name]`). Route lightweight tasks (< 600 input tokens: intent classification, affirmative resolution, conversational replies) to the `flash-lite` tier, and heavy reasoning tasks (multi-file semantic taxonomy synthesis) to the highest-capacity healthy model, cascading cleanly across all models before ever touching the offline fallback.

3. **Stateless Turn Amnesia (The 67 Near-Duplicates Gap)**:
   - When the user ran a scan that discovered **67 near-duplicates** (`NEAR_JACCARD`) and 0 exact duplicates, and then replied *"Yes clean all the duplicate files"*, the next HTTP request to `/propose_plan` had no structured memory of the prior scan's parameters (`include_near=True`, `near_clusters_count=67`). It defaulted to `include_near=False` and reported *"No duplicate files found"*.
   - **Phase 9 Fix (Conversational Continuity & Session State Ledger)**: Introduce `SessionContextStore` (`storage_copilot/intelligence/session_context.py`). Every audit, scan, or proposal records a structured **Turn Artifact** (`last_audit_type`, `include_near`, `candidate_count`, `target_folder`, `pending_sub_intent`). When the user replies with a follow-up (*"Yes clean them"*, *"Do it"*, *"Clean those 67 files"*), both the AI Orchestrator and `tier_handlers.py` inherit the exact state from the prior turn.

---

## 2. Target Phase 9 Architecture

```
                    ┌──────────────────────────────────────────────────┐
                    │              POST /propose_plan                  │
                    │  (Raw User Prompt + Conversation History)        │
                    └────────────────────────┬─────────────────────────┘
                                             │
                                             ▼
                    ┌──────────────────────────────────────────────────┐
                    │        SessionContextStore (Continuity)          │
                    │  Loads active Turn Artifact:                     │
                    │  - last_audit_type (e.g. near_dedup, forgotten)  │
                    │  - include_near=True, candidate_count=67         │
                    │  - target_folder, last_proposed_sub_intent       │
                    └────────────────────────┬─────────────────────────┘
                                             │
                                             ▼
                    ┌──────────────────────────────────────────────────┐
                    │       Is Gemini API Key Configured & Healthy?    │
                    └───────────┬──────────────────────────┬───────────┘
                                │ YES (Online AI-First)    │ NO (Offline / All Models 429)
                                ▼                          ▼
        ┌───────────────────────────────────────────┐   ┌──────────────────────────────────┐
        │   Tiered Model Router (gemini_client.py)  │   │  Offline Heuristic Safety Net    │
        │   - Per-Model Cooldown Circuit Breaker    │   │  - Fuzzy Levenshtein typo normal │
        │   - Tier A (Lite): gemini-3.1-flash-lite  │   │  - Session-aware affirmative     │
        │                    gemini-3.5-flash-lite  │   │  - Regex fallback classifier     │
        │   - Tier B (Deep): gemini-3.5-flash       │   └─────────────────┬────────────────┘
        └───────────────────────┬───────────────────┘                     │
                                │                                         │
                                ▼                                         │
        ┌───────────────────────────────────────────┐                     │
        │   AI-First Intent & Continuity Resolver   │                     │
        │   - Understands arbitrary typos natively  │                     │
        │   - Resolves multi-turn "Yes clean them"  │                     │
        │     against SessionContextStore           │                     │
        │   - Outputs strict JSON:                  │                     │
        │     {intent, sub_intent, include_near,    │                     │
        │      stale_days, resolved_goal, target}   │                     │
        └───────────────────────┬───────────────────┘                     │
                                │                                         │
                                └────────────────────┬────────────────────┘
                                                     ▼
                                ┌─────────────────────────────────────────┐
                                │      Dispatch to Tier Handlers          │
                                │  - Persists new Turn Artifact in Store  │
                                │  - stamps `origin_badge`:               │
                                │    "⚡ Gemini AI (<model>)" vs          │
                                │    "🛡️ Offline Heuristic"               │
                                └─────────────────────────────────────────┘
```

---

## 3. Detailed Module Specifications

### 3.1 New Module: `storage_copilot/intelligence/session_context.py`

Maintains an in-memory, thread-safe continuity ledger per `target_folder` / session so multi-turn interactions never lose state between `QUERY_INFO` audits and `FILE_OPERATION` executions.

- **Tracked State Fields**:
  - `last_intent`: `"CONVERSATION" | "QUERY_INFO" | "FILE_OPERATION"`
  - `last_sub_intent`: e.g., `"deduplicate_near_audit"`, `"deduplicate_audit"`, `"forgotten_audit"`, `"format_sorting_proposal"`
  - `include_near`: `bool` (True if near-duplicates were scanned or found in the last audit)
  - `exact_duplicates_count`: `int`
  - `near_duplicates_count`: `int`
  - `forgotten_files_count`: `int`
  - `stale_days`: `float`
  - `unorganized_count`: `int`
  - `timestamp`: `float` (TTL: 30 minutes)

- **Continuity Resolution Rule**:
  - If a duplicate scan finds `exact_duplicates_count == 0` and `near_duplicates_count > 0` (or if the user explicitly scanned with `include_near=True`), `session_context` sets `include_near = True`.
  - When the user subsequently says *"Yes"*, *"Clean duplicate downloads"*, *"Yes clean all the duplicate files"*, or *"Clean them"*, `handle_file_operation_turn` automatically reads `session_context.include_near` (and also auto-upgrades to `include_near=True` whenever `exact_duplicates == 0`), guaranteeing that the 67 near-duplicates are swept into `.agent_trash`!

---

### 3.2 Upgraded Quota-Aware Model Router: `storage_copilot/intelligence/gemini_client.py`

1. **Per-Model Circuit Breaker (`MODEL_COOLDOWN_UNTIL`)**:
   - Tracks cooldown timestamps individually per model name (`Dict[str, float]`).
   - If `gemini-3.5-flash` returns HTTP `429`, **only `gemini-3.5-flash` is placed on cooldown** (e.g., 300s for daily quota, 15s for RPM rate limit), and the router immediately cascades to `gemini-3.1-flash-lite` and `gemini-3.5-flash-lite` in the *same* turn!
   - `config.GEMINI_QUOTA_BLOCKED_UNTIL` is only set if **every** model in `GEMINI_MODELS` is simultaneously on cooldown.

2. **Token-Weight Dynamic Routing (`select_models_for_task`)**:
   - `task_tier="fast"` (Intent orchestration, typo resolution, short chat):
     Prioritizes `["gemini-3.1-flash-lite", "gemini-3.5-flash-lite", "gemini-3.5-flash", "gemini-3.6-flash", "gemini-flash-latest"]`.
   - `task_tier="deep"` (Complex multi-file Action Plan synthesis & deep 12,000-char document Q&A):
     Prioritizes healthy flash/lite models with automatic cascade.

---

### 3.3 Upgraded AI-First Orchestrator: `storage_copilot/intelligence/intent_classifier.py`

1. **Fuzzy Typo Normalizer (`normalize_user_prompt` in `plan_builder.py`)**:
   - Expands normalization to catch real-world mobile keyboard typos observed in testing:
     - `cleen` / `clen` / `claen` $	o$ `clean`
     - `teh` / `hte` $	o$ `the`
     - `fils` / `fiels` / `fikes` / `fls` $	o$ `files`
     - `downlods` / `dowloads` / `doenload` / `downlaods` $	o$ `downloads`
     - `duplicat` / `duplicats` / `dups` / `dupes` $	o$ `duplicates`
     - `orgnize` / `oragnize` / `organise` $	o$ `organize`
     - `forgt` / `forgoten` $	o$ `forgotten`
2. **AI-First Resolution Mode (When `GEMINI_API_KEY` is present & healthy)**:
   - Sends the user prompt + last 4 conversation turns + current `SessionContextStore` snapshot to `call_gemini_micro_classifier()`.
   - The LLM resolves typos, conversational pronouns (*"clean them"*, *"sort those"*), and sub-intents (`deduplicate_near_clean`, `forgotten_clean`, `voucher_sorting`, `peer_separation`, `academic_organization`, `format_batch_execute`, `read_only_search`, `advice_or_chat`).
   - If the LLM call succeeds, its structured classification is used directly with `is_llm=True` and `model_source=<model_used>`.
3. **Deterministic Continuity & Offline Safety Net**:
   - If `GEMINI_API_KEY` is empty (as in the 197 offline unit tests) or all models are on 429 cooldown, the classifier uses the fuzzy-normalized prompt + `SessionContextStore` + deterministic rules so all 197 existing tests continue to pass at 0ms latency.

---

### 3.4 Near-Duplicate Sweep & Origin Badges in `storage_copilot/execution/tier_handlers.py`

1. **Automatic Near-Duplicate Fallback & Continuity in `deduplicate_clean`**:
   - When executing `deduplicate_clean` or `deduplicate_near_clean`:
     - First checks if `sub_intent == "deduplicate_near_clean"` or `session_context.include_near` is True.
     - Furthermore, if an exact-only scan returns `total_duplicates == 0`, it **automatically runs a second pass with `include_near=True`** before ever claiming *"No duplicate files detected"*!
     - This permanently eliminates the bug where 67 near-duplicates were found during audit but ignored during cleanup.
2. **Explicit Origin Badges (`⚡ Gemini AI` vs `🛡️ Offline Heuristic`)**:
   - Every response from `handle_conversation_turn`, `handle_query_info_turn`, and `handle_file_operation_turn` includes:
     - `"is_llm": True | False`
     - `"model_source": "<model_name>" | "offline-heuristic"`
     - `"origin_badge": "⚡ Gemini AI (<model>)" | "🛡️ Offline Heuristic"`
     - Appends the origin badge to `response["badges"]` so the Web UI always shows the user whether Gemini AI or the Offline Heuristic engine generated the reply.
