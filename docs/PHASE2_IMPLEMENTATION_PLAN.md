# Storage Copilot — Phase 2: Three-Tier Intent Classifier & Grounded Reasoning Engine
## Architectural Specification & Implementation Plan

> **Target Phase**: Phase 2 of 8  
> **Status**: Ready for Implementation  
> **Pre-requisite**: Phase 1 (Structural Decomposition into `storage_copilot/`) completed and committed (`66693bc`). All 162 existing tests pass.

---

## 1. Executive Summary & Objective

In **Phase 1**, `server.py` (3,759 lines) was successfully decomposed into a modular package (`storage_copilot/`) while maintaining 100% backward compatibility via a proxy shim.

However, the core planning logic (`storage_copilot/api/plan_routes.py:propose_plan`) remains a monolithic **~1,290-line function**. It suffers from several critical architectural flaws:
1. **Conflated Intent Routing**: Read-only queries (*"List university lectures"*), open-ended chat (*"What else can you do?"*), and filesystem mutations (*"Organize my downloads"*) are parsed through tangled heuristic branches and fallbacks.
2. **Context Collapse & False Mutations**: Prompts that should only query or chat occasionally leak into the Action Plan generator, producing empty or unintentional file move proposals.
3. **Triplicated Heuristic Logic**: Keyword detection and offline query extractors are repeated in 3 separate locations within `propose_plan()`.
4. **Dual Divergent Relevance Scoring Engines**: Two independent scoring functions exist (`score_document_match` in `search_engine.py` on a 0–200 scale and `compute_query_relevance_score` on a 0–300+ scale) with conflicting weights.

**Phase 2 Goal**:
Evolve `propose_plan()` into a clean, decoupled **Three-Tier Intent Architecture** with a deterministic fast-path gate, an ultra-cheap micro-LLM intent classifier, dedicated tier handlers, and unified search relevance scoring.

---

## 2. Target Three-Tier Architecture

```
                    ┌──────────────────────────────────────────────┐
                    │            POST /propose_plan                │
                    │             (User Prompt)                    │
                    └──────────────────────┬───────────────────────┘
                                           │
                                           ▼
                    ┌──────────────────────────────────────────────┐
                    │      Deterministic Fast-Path Gate            │
                    │ (Greeting / Status / Explicit Query Regex)   │
                    └───────┬──────────────────────────────┬───────┘
                            │ Matched                      │ Unmatched
                            ▼                              ▼
                 [Skip LLM Classifier]        ┌────────────────────────────┐
                            │                 │ Lightweight Intent Router  │
                            │                 │ (gemini-3.5-flash-lite /   │
                            │                 │  temperature: 0.0, ~50 tok)│
                            │                 └────────────┬───────────────┘
                            │                              │
                            ├──────────────────────────────┘
                            ▼
              ┌───────────────────────────┐
              │ Classify into EXACT Intent│
              └───────┬─────────┬─────────┘
                      │         │
       ┌──────────────┘         └──────────────┐
       ▼                                       ▼
┌──────────────┐                       ┌──────────────┐
│ CONVERSATION │                       │  QUERY_INFO  │
│              │                       │              │
│ Pure Chat    │                       │ Read-Only    │
│ Zero Moves   │                       │ File Search  │
│ Dynamic Adv. │                       │ Citations    │
└──────┬───────┘                       └──────┬───────┘
       │                                      │
       │               ┌──────────────────────┘
       ▼               ▼
┌──────────────────────────────┐       ┌──────────────────────────────┐
│   handle_conversation()      │       │     handle_query_info()      │
│ - Return conversational text │       │ - Unified Search Engine      │
│ - Dynamic storage tips       │       │ - Read snippets & cite       │
│ - plan: [] (guaranteed empty)│       │ - plan: [] (guaranteed empty)│
└──────────────────────────────┘       └──────────────────────────────┘
                                                      │
                                                      │ Unambiguous Mutative Goal
                                                      ▼
                                       ┌──────────────────────────────┐
                                       │        FILE_OPERATION        │
                                       │                              │
                                       │  handle_file_operation()     │
                                       │  - Relevance filter          │
                                       │  - Deep content inspect      │
                                       │  - Plan synthesis (Gemini)   │
                                       │  - Collision-free validation │
                                       │  - Return ActionPlan diff    │
                                       └──────────────────────────────┘
```

---

## 3. Component Breakdown & Module Blueprint

### 3.1 New Module: `storage_copilot/intelligence/intent_classifier.py`

This module is responsible for classifying the raw user prompt into one of three strict intents:
- `CONVERSATION`: Pure discussion, greetings, capabilities, advice, storage explanations. Must produce **zero file mutations**.
- `QUERY_INFO`: Read-only queries, finding files, summarizing notes, locating lectures/receipts. Must produce **zero file mutations**.
- `FILE_OPERATION`: Reorganizing, moving, deduplicating, grouping, trashing, or archiving files. Produces an **Action Plan with diffs**.

#### API Contract:
```python
from enum import Enum
from typing import Dict, Any, Optional

class UserIntent(str, Enum):
    CONVERSATION = "CONVERSATION"
    QUERY_INFO = "QUERY_INFO"
    FILE_OPERATION = "FILE_OPERATION"

def classify_intent(
    prompt: str,
    recent_context: Optional[str] = None,
    available_files_count: int = 0
) -> Dict[str, Any]:
    """
    Returns:
    {
        "intent": UserIntent,
        "confidence": float,
        "cleaned_query": str,        # Stripped of command syntax
        "fast_path_matched": bool,    # True if resolved via deterministic heuristics
        "extracted_entity": str       # Target entity or keyword (if query_info)
    }
    """
```

#### Deterministic Fast-Path Heuristics (Zero-Latency / Offline-Proof):
1. **Greetings & Open Chat**:
   - Matches: `"hello"`, `"hi"`, `"hey"`, `"what else"`, `"what can you do"`, `"help"`, `"who are you"`, `"storage advice"`.
   - Routing: `UserIntent.CONVERSATION` (Confidence: 1.0)
2. **Explicit Read-Only Search**:
   - Matches: `"where is *"`, `"find *"`, `"search *"`, `"look for *"`, `"do i have *"`, `"list *"`, `"show me *"`, `"what university lectures *"`.
   - Routing: `UserIntent.QUERY_INFO` (Confidence: 1.0)
3. **Explicit Storage Mutation Commands**:
   - Matches: `"organize *"`, `"sort *"`, `"clean *"`, `"deduplicate *"`, `"move * to *"`, `"trash *"`, `"archive *"`, `"group * by *"`.
   - Routing: `UserIntent.FILE_OPERATION` (Confidence: 1.0)

#### Cheap LLM Classifier Fallback (When Ambiguous):
- If fast-path does not trigger with high confidence ($\ge 0.95$):
  - Model: `gemini-3.5-flash-lite` (or fallback to `gemini-2.5-flash`)
  - Config: `temperature: 0.0`, `max_output_tokens: 60`, `responseMimeType: "application/json"`
  - System prompt:
    ```
    Classify the user's intent regarding their mobile phone storage:
    - CONVERSATION: greetings, asking for capabilities, advice, storage health questions.
    - QUERY_INFO: finding files, searching content, reading notes, listing documents without moving them.
    - FILE_OPERATION: moving, renaming, sorting, cleaning, grouping, deleting files.
    Return JSON: {"intent": "CONVERSATION" | "QUERY_INFO" | "FILE_OPERATION", "target": "extracted subject"}
    ```

---

### 3.2 Unify Relevance Scoring in `storage_copilot/intelligence/search_engine.py`

#### Current Problem:
- `search_engine.py` contains `score_document_match()` (lines 14–98, 0–200 point scale).
- `search_engine.py` also contains `compute_query_relevance_score()` (lines 101–180, 0–300+ point scale).
- `plan_routes.py` calls both at different times with inconsistent thresholding.

#### Solution:
Create a single, authoritative unified scoring function:
```python
def score_file_relevance(
    file_info: Dict[str, Any],
    query_intent: Dict[str, Any],
    snippet: str = ""
) -> Dict[str, Any]:
    """
    Computes a grounded 0-100 normalized relevance score:
    - Exact basename match: +40
    - Token overlap in filename: +25
    - Token overlap in snippet: +20
    - Format category match (e.g. audio vs doc): +10
    - Temporal / date-range match: +15
    - Negative penalty for blacklist or irrelevant extensions: -50

    Returns:
    {
        "score": float (0.0 to 100.0),
        "is_relevant": bool (score >= 35.0),
        "match_reasons": List[str]
    }
    """
```

---

### 3.3 Modular Tier Handlers in `storage_copilot/execution/tier_handlers.py`

Extract the three massive execution branches out of `propose_plan()` into dedicated handlers:

1. **`handle_conversation_turn(prompt, context)`**:
   - Calls `generate_dynamic_storage_advice()` or `call_gemini_text()`.
   - Never inspects candidate mutation paths.
   - Guaranteed response contract:
     ```json
     {
       "analysis": "...",
       "plan": [],
       "badges": ["Chat", "Advice"],
       "intent": "CONVERSATION"
     }
     ```

2. **`handle_query_info_turn(prompt, parsed_intent, files)`**:
   - Uses `score_file_relevance()` across indexed candidate files.
   - For top 5 matches ($score \ge 35$), extracts snippets via `extract_file_snippet()`.
   - Formulates grounded citation answer: where files are, what is inside them, and links/paths.
   - Guaranteed response contract:
     ```json
     {
       "analysis": "Found 3 matching files:\n- `Documents/AI/Lecture_01.pdf` (matches 'AI lecture')\n...",
       "plan": [],
       "matches": [...],
       "badges": ["Search", "Read-Only"],
       "intent": "QUERY_INFO"
     }
     ```

3. **`handle_file_operation_turn(prompt, files, user_profile, dry_run)`**:
   - Pre-flight PII redaction on extracted snippets.
   - Gathers candidate mutation files ($N \le 30$).
   - Calls Gemini Action Plan generator (or offline heuristic rule engine).
   - Validates plan using `ensure_unique_plan_destinations()` and `resolve_collision()`.
   - Enforces Policy Gatekeeper limits (max 20 actions, protected roots).
   - Response contract:
     ```json
     {
       "analysis": "...",
       "plan": [
         {"action": "move", "source": "...", "destination": "..."},
         ...
       ],
       "badges": ["Action Plan", "12 Moves"],
       "intent": "FILE_OPERATION"
     }
     ```

---

### 3.4 Refactoring `storage_copilot/api/plan_routes.py`

`propose_plan()` shrinks from **~1,290 lines** down to **~80 lines**:
```python
@plan_bp.route("/propose_plan", methods=["POST"])
def propose_plan():
    data = request.get_json(force=True) or {}
    raw_prompt = data.get("prompt", "").strip()
    
    # 1. Normalize prompt
    prompt = normalize_user_prompt(raw_prompt)
    
    # 2. Classify intent via deterministic gate + micro-LLM
    classification = classify_intent(prompt)
    intent = classification["intent"]
    
    # 3. Route to dedicated handler
    if intent == UserIntent.CONVERSATION:
        result = handle_conversation_turn(prompt, classification)
    elif intent == UserIntent.QUERY_INFO:
        files = collect_storage_files(config.BASE_DIR)
        result = handle_query_info_turn(prompt, classification, files)
    else:  # UserIntent.FILE_OPERATION
        files = collect_storage_files(config.BASE_DIR)
        result = handle_file_operation_turn(prompt, classification, files)
        
    return jsonify(result)
```

---

## 4. Implementation Steps & Verification Gates

| Step | Action | Files Touched | Verification Gate |
|---|---|---|---|
| **2.1** | Create `storage_copilot/intelligence/intent_classifier.py` | `intent_classifier.py`, `__init__.py` | Unit tests for 40+ prompt variations (conversational, queries, moves). |
| **2.2** | Unify scoring functions in `search_engine.py` | `search_engine.py` | Run `test_phase35_temporal.py` and `test_v06_live.py`. |
| **2.3** | Implement dedicated tier handlers | `storage_copilot/execution/tier_handlers.py` | Individual handler test with fixture data. |
| **2.4** | Refactor `propose_plan()` in `plan_routes.py` | `plan_routes.py` | All 162 existing tests pass with 0 regressions. |
| **2.5** | Add new end-to-end tests for Three-Tier Routing | `test_three_tier_intent.py` | Verify 0 empty plans generated for chat/query prompts. |

---

## 5. Potential Traps & Edge Cases

1. **"What else" Regressions**:
   - In earlier versions, asking *"What else"* triggered either an explicit query for files named "what" or an unorganized download sweep.
   - *Guard*: Fast-path must intercept conversational follow-ups before any file scan.
2. **Ambiguous Actions like "Find and clean receipts"**:
   - Hybrid intent ("find" + "clean").
   - *Rule*: Any presence of mutation verbs (`clean`, `sort`, `organize`, `move`, `trash`, `delete`) upgrades the intent to `FILE_OPERATION`.
3. **Gemini Quota Exceeded (429/503)**:
   - If Gemini is rate-limited or offline, the classifier must fall back seamlessly to local regex rules without throwing an unhandled 500 error.
4. **Zero-File Scans for Conversational Turns**:
   - `handle_conversation_turn` should NEVER trigger a deep recursive scan of `/storage/emulated/0`, preserving battery and preventing Android I/O stutter.
