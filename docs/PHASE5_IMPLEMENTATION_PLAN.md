# Storage Copilot — Phase 5: Content-Derived Collision-Free Naming Engine
## Architectural Specification & Implementation Plan

> **Target Phase**: Phase 5 of 8  
> **Status**: Completed & Verified  
> **Pre-requisite**: Phases 1–4 completed. All 182 existing tests pass.

---

## 1. Executive Summary & Objectives

In **Phases 1–4**, we modularized `storage_copilot/`, implemented the Three-Tier Intent Classifier, added Dual-Mode Zero-Trust Security, and built the Two-Stage Lazy Manifest Indexer with Server-Sent Events (SSE).

However, document renaming in `document_classifier.py` and `tier_handlers.py` still relied on static, hardcoded sequence numbers (`Knowledge_Representation_Lecture_04.pdf`, `AI_Course_Assignment_02.pdf`). If a user downloaded *Lecture 7* or *Lab 3*, the legacy heuristic either forced `_04` or fell back to a generic token.

**Phase 5 Goal**:
Build a deterministic, regex-driven **`ContentDerivedNamer`** engine (`storage_copilot/storage/namer.py`) that dynamically extracts document types, sequence numbers (`Lecture 7` -> `07`, `Assignment 3` -> `03`, `Lab 5` -> `05`), academic topics, research paper titles, and financial billing periods directly from inspected snippets while supporting user-configurable naming templates in `user_profile.json` and 100% backward compatibility with all 182 existing tests.

---

## 2. Core Components & Architecture

### 2.1 `storage_copilot/storage/namer.py` (`ContentDerivedNamer`)

1. **Dynamic Sequence Extraction (`extract_sequence_info`)**:
   - Detects sequence markers across natural-language and shorthand formats:
     - `Lecture 4`, `Lec #7`, `Lect 12` -> `("Lecture", 4)`
     - `Assignment 2`, `Assign 3`, `HW 1`, `Homework 5` -> `("Assignment", 2)`
     - `Lab 3`, `Lab Report 6`, `Practical 2` -> `("Lab", 3)`
     - `Chapter 1`, `Ch. 8`, `Week 5`, `Module 3`, `Tutorial 4`, `Quiz 2` -> `("<Type>", <int>)`
   - Zero-pads integers to 2 digits (`01`, `02`, `04`, `10`).

2. **Academic Topic & Research Paper Title Extraction (`extract_academic_topic`)**:
   - Parses course subjects (`AI`, `CS`, `SE`, `DS`, `Math`) and core topic titles before colons or headers (e.g., `"Knowledge Representation and Reasoning Lecture 4: First-Order..."` -> topic `"Knowledge_Representation"`).
   - Extracts clean titles from ArXiv / research papers (`"A Neural Algorithm of Artistic Style by Leon A. Gatys..."` -> `"Neural_Algorithm_Artistic_Style_Paper"`).
   - Never emits generic `Document_YYYY-MM-DD.pdf` names for inspected files.

3. **Financial & Utility Bill Entity + Period Extraction (`extract_financial_bill_title`)**:
   - Extracts utility/provider types (`Electricity`, `Gas`, `Water`, `Internet`, `Tuition`) and billing periods (`March 2025` -> `March2025`, `Oct 2026` -> `Oct2026`).
   - Supports both concise mode (`Electricity_Bill_Invoice.pdf` for Clean Download Names) and detailed period mode (`Electricity_Utility_Bill_March2025.pdf` for general classification).

4. **User-Configurable Naming Templates (`user_profile.json`)**:
   - Reads optional `naming_templates` from `user_profile.json`:
     ```json
     "naming_templates": {
       "lecture": "{topic}_Lecture_{sequence:02d}",
       "assignment": "{course}_Course_Assignment_{sequence:02d}",
       "lab": "{course}_Lab_{sequence:02d}",
       "paper": "{topic}_Paper",
       "financial_bill": "{utility}_Utility_Bill_{period}",
       "peer_receipt": "{peer}_Receipt"
     }
     ```
   - Safely interpolates placeholders with clean fallback when a placeholder (`{sequence}` or `{period}`) is absent in a document.

5. **Collision-Free Batch & Disk Guarantee**:
   - Integrates directly with `collision_resolver.py` (`ensure_unique_plan_destinations` and `resolve_collision`) so two lectures or bills in the same batch never collide on destination path.
