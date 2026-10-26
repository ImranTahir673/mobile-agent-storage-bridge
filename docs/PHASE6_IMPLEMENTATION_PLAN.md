# Storage Copilot — Phase 6: Deep Deduplication & "Forgotten Files" Finder
## Architectural Specification & Implementation Plan

> **Target Phase**: Phase 6 of 8  
> **Status**: Ready for Implementation  
> **Pre-requisite**: Phases 1–5 completed. All 187 existing tests pass.

---

## 1. Executive Summary & Objectives

In **Phases 1–5**, Storage Copilot gained a modular package structure, a Three-Tier Intent Classifier, Zero-Trust Bearer & Origin Security, Two-Stage Lazy Manifest Indexing with SSE Streaming, and the `ContentDerivedNamer` engine.

However, two storage hygiene gaps remain on real-world mobile devices:
1. **Exact-Hash Blind Spot for Edited/Revised Documents**: `find_duplicate_clusters()` in `deduplication.py` only groups files with identical byte sizes and identical SHA-256 hashes. If a user has slightly modified drafts or re-saved copies of the same document (e.g., `SRS IR SOLUTION.docx` and `SRS IR SOLUTION edit.docx` or two text/PDF exports with minor header/whitespace changes), SHA-256 treats them as completely unrelated.
2. **Stale & Orphaned Storage Bloat ("Forgotten Files")**: Users accumulate stale downloads (>90 days old), ephemeral `.tmp`/`.cache`/`.bak`/`.crdownload` artifacts, and orphaned old screenshots (`Screenshot_*.png`) that silently consume storage.

**Phase 6 Goal**:
Upgrade the deduplication and storage reconnaissance engine with:
1. **3-Gram Jaccard Near-Duplicate Detection ($	ext{similarity} \ge 0.85$)** alongside exact SHA-256 clustering.
2. **"Forgotten Files Finder" (`find_forgotten_files`)** identifying stale downloads ($> 90$ days default), temporary cache artifacts, and orphaned screenshots, exposed via both natural-language prompts and dedicated REST endpoints (`/scan_duplicates` and `/scan_forgotten`).

---

## 2. Core Components & Architecture

### 2.1 Near-Duplicate 3-Gram Jaccard Engine (`storage_copilot/storage/deduplication.py`)

1. **Shingle Extraction (`compute_text_shingles`)**:
   - Normalizes extracted text (lowercased, punctuation stripped, whitespace collapsed).
   - Generates character/word 3-grams (default: word 3-grams with character 3-gram fallback for short texts).
2. **Jaccard Similarity (`compute_jaccard_similarity`)**:
   - Computes $J(A, B) = rac{|S_A \cap S_B|}{|S_A \cup S_B|}$ on shingle sets.
   - Enforces a strict default threshold of **`0.85`** ($\ge 85\%$ overlap).
3. **Two-Pass Unified Duplicate Clustering (`find_duplicate_clusters`)**:
   - **Pass 1 (Exact SHA-256)**: Identical byte size + identical SHA-256 hash (`match_type: "EXACT_SHA256"`, `similarity: 1.0`).
   - **Pass 2 (Near-Duplicate 3-Gram Jaccard)**: When `include_near=True`, inspects remaining non-exact text/document files, extracts PII-sanitized snippets via the Phase 4 RAM cache, and groups pairs with $	ext{Jaccard} \ge 0.85$ (`match_type: "NEAR_JACCARD"`).
   - **100% Backward Compatibility**: `find_duplicate_clusters(base_dir, target_folder)` keeps `include_near: bool = False` as the default when called without arguments so `test_phase1_deduplication.py` exact cluster counts remain 100% unchanged, while `find_near_duplicate_clusters()` and `find_duplicate_clusters(..., include_near=True)` return both exact and near-duplicate clusters!

### 2.2 "Forgotten Files" Finder (`find_forgotten_files` in `storage_copilot/storage/deduplication.py`)

Scans `target_folder` (default `Download`) and categorizes forgotten bloat into three actionable buckets:
1. **`stale_downloads`**: Regular files in `Download/` with `age_days >= stale_days` (default `90.0` days).
2. **`temporary_files`**: Ephemeral sync/updater/partial files matching `.tmp`, `.cache`, `.bak`, `.log`, `.crdownload`, `.part`, or `temp_*`.
3. **`orphaned_screenshots`**: Screen captures matching `screenshot*` or `screen_shot*` sitting unorganized in `Download/`.

Returns structured metrics (`total_forgotten`, `reclaimable_bytes`, `reclaimable_human`, category lists, and unified `items` list).

### 2.3 API & Intent Integration

1. **`storage_copilot/api/dedup_routes.py`**:
   - `GET/POST /scan_duplicates`: Accepts optional `include_near=1` and `threshold=0.85` parameters.
   - `GET/POST /scan_forgotten`: New endpoint returning `find_forgotten_files()` audit report (with configurable `stale_days`, default `90`).
2. **`storage_copilot/intelligence/intent_classifier.py` & `tier_handlers.py`**:
   - Recognizes natural-language queries for forgotten/stale files (*"Find forgotten files"*, *"Show stale downloads"* $	o$ `QUERY_INFO` with `sub_intent="forgotten_audit"`) and cleanup commands (*"Clean forgotten files"*, *"Trash forgotten files"* $	o$ `FILE_OPERATION` with `sub_intent="forgotten_clean"`).
   - Recognizes near-duplicate queries (*"Find near duplicates"*, *"Check similar duplicate files"*) and cleanup commands.
