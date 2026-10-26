# Storage Copilot — Comprehensive Manual Testing Runbook

This step-by-step runbook walks you through testing every feature of **Storage Copilot (v0.6.7-live)** hands-on through the Mobile Web UI—either directly on your Android phone via Termux or on your laptop/desktop browser.

---

## 1. Starting the Server

### Option A: Local Desktop / Laptop (Windows, Linux, macOS)
Open a terminal in the repository root and run:
```bash
python server.py
```
- The server starts on **`http://127.0.0.1:8080`** using the multi-threaded Waitress/Flask WSGI runner.
- Open **`http://127.0.0.1:8080/?ui=1`** (or simply **`http://127.0.0.1:8080`**) in Chrome, Edge, or Firefox.

### Option B: On Android via Termux (with Optional Cloudflare Tunnel)
```bash
./scripts/start_daemon.sh
```
- Acquires `termux-wake-lock` so Android does not put the CPU to sleep.
- Starts `server.py` on `http://127.0.0.1:8080` and prints your public `https://*.trycloudflare.com` URL (saved in `.bridge_url`).
- To check daemon health at any time: `./scripts/status_daemon.sh`
- To stop the daemon cleanly: `./scripts/stop_daemon.sh`

---

## 2. Pre-Flight UI Checklist & 1-Click Fixture Seeding

When you open the Web UI (`http://127.0.0.1:8080`):
1. **Verify Top Status Indicators**:
   - **SSE Stream Pill**: Should turn green and display **`● Live SSE Connected`** (confirming `EventSource` is connected to `GET /stream`).
   - **AI Mode Pill**: Displays either **`⚡ Gemini Online`** (if `GEMINI_API_KEY` is configured) or **`🛡️ Local Offline Engine`** (deterministic fallback mode). You can click **⚙️ API Key & Auth** in the header to set a Gemini key or Bearer token at any time.
2. **Seed Realistic Mobile Fixtures (1-Click)**:
   - In the top **Storage Tools** bar, check **"Include Duplicates"** and click **`🌱 Seed Demo Fixtures`**.
   - **What to expect**: A toast/chat confirmation appears showing that **12 realistic files** were created in `Download/`:
     - `BSAI-182_fee_voucher_fall.pdf` (Personal university fee voucher)
     - `fawad fee.pdf` (Classmate peer fee slip)
     - `Sumbal pass.pdf` (University entry/identity pass)
     - `1508.06576v2_neural_style.pdf` & `1508.06576v2_neural_style (1).pdf` (ArXiv research paper + exact duplicate)
     - `DOC-20250413-WA0017..pdf` (WhatsApp Knowledge Representation Lecture 4)
     - `DOC-20250413-WA0018..pdf` (WhatsApp AI Course Assignment 2)
     - `DOC-20250413-WA0019..pdf` & `DOC-20250413-WA0019 (1)..pdf` (WhatsApp Electricity Utility Bill + exact duplicate)
     - `WhatsApp_Doc_Ahmed_Receipt.pdf` (Peer library receipt for Ahmed)
     - `Screenshot_20241001-142210.png` (Orphaned screen capture)
     - `temp_cache_sync.tmp` (Temporary sync cache file)

---

## 3. Ten End-to-End Scenarios to Test in the Chat

Test each of the following 10 scenarios in order. Watch the **Live Telemetry Bar** as each prompt runs!

### Scenario 1: Conversational Greeting & Dynamic Storage Audit (`CONVERSATION` Tier)
- **Type in Chat**:
  ```text
  What can you do? Give me some suggestions for my files.
  ```
- **Expected Result**:
  - Intent badge shows **`CONVERSATION`**.
  - Zero file mutations are proposed (`0 actions`).
  - The assistant audits your live `Download/` directory and gives personalized recommendations citing real files (`fawad fee.pdf`, `DOC-20250413-WA0017..pdf`, duplicate count, etc.)—never a static canned template.

---

### Scenario 2: Read-Only Semantic & Category Lookup (`QUERY_INFO` Tier)
- **Type in Chat**:
  ```text
  List university lectures in Download
  ```
- **Expected Result**:
  - Intent badge shows **`QUERY_INFO`** (`Read-Only`).
  - Cites `DOC-20250413-WA0017..pdf` (*Knowledge Representation Lecture 4*), `DOC-20250413-WA0018..pdf` (*AI Assignment 2*), and `1508.06576v2_neural_style.pdf`.
  - **Crucial Safety Check**: Verify that `Sumbal pass.pdf` (an Identity/Entry Pass) is **NOT** listed as a university lecture, and **zero** move/trash actions are staged.

---

### Scenario 3: Specific Document Search & Identity Pass Disambiguation (`QUERY_INFO` Tier)
- **Type in Chat**:
  ```text
  Find me Sumbal pass
  ```
- **Expected Result**:
  - Routes to **`QUERY_INFO`** with 0 mutations.
  - Locates **`Sumbal pass.pdf`** (`Download/Sumbal pass.pdf`), classifies it as **`Identity / Entry Pass`**, and shows its PII-sanitized preview (`Student Name: [REDACTED_NAME]`).

---

### Scenario 4: Temporal Recency & Calendar Date Grounding (`QUERY_INFO` Tier)
- **Type in Chat**:
  ```text
  Did I download anything today? What is my latest download?
  ```
- **Expected Result**:
  - Routes to **`QUERY_INFO`** with 0 mutations.
  - Lists the files seeded today along with their sizes and modification timestamps.

---

### Scenario 5: Content-Derived Renaming & Academic Taxonomy (`FILE_OPERATION` Tier)
- **Type in Chat**:
  ```text
  Clean Download Names
  ```
- **Expected Result**:
  - Intent badge shows **`FILE_OPERATION`**.
  - Renders an interactive **Action Plan Diff Review Card** with individual checkboxes for each proposed action:
    - `DOC-20250413-WA0017..pdf` $	o$ `Documents/University/AI/Knowledge_Representation_Lecture_04.pdf` (`RENAME`)
    - `DOC-20250413-WA0018..pdf` $	o$ `Documents/University/AI/AI_Course_Assignment_02.pdf` (`RENAME`)
    - `1508.06576v2_neural_style.pdf` $	o$ `Documents/University/AI/Neural_Algorithm_Artistic_Style_Paper.pdf` (`RENAME`)
    - `DOC-20250413-WA0019..pdf` $	o$ `Documents/Finances/Electricity_Bill_Invoice.pdf` (`RENAME`)
    - `WhatsApp_Doc_Ahmed_Receipt.pdf` $	o$ `Documents/Peers/Ahmed/Ahmed_Receipt.pdf` (`PEER_MOVE`)
  - **Test Selective Checkboxes**:
    1. Uncheck one action (e.g., `WhatsApp_Doc_Ahmed_Receipt.pdf`).
    2. Notice the button label dynamically updates (e.g., `Execute Selected (9 of 10)`).
    3. Re-check it, or click **Execute Selected** to run the subset.
  - **Test 1-Tap Rollback**:
    - Once executed, look at the top **Recent Batch Undo Banner** and click **`⏪ 1-Tap Undo Batch`**.
    - All moved/renamed files are atomically restored back to `Download/`!

---

### Scenario 6: Peer Separation & Tuition Voucher Sorting (`FILE_OPERATION` Tier)
- **Type in Chat**:
  ```text
  Sort student vouchers
  ```
- **Expected Result**:
  - `BSAI-182_fee_voucher_fall.pdf` (owner Imran Tahir) is routed to `Documents/University/Vouchers/BSAI-182_fee_voucher_fall.pdf`.
  - `fawad fee.pdf` (peer Fawad) is isolated into `Documents/Peers/Fawad/fawad fee.pdf` (`PEER_MOVE` badge)—never mixed into your personal vouchers folder!
  - Click **Execute Selected**, then click **⏪ 1-Tap Undo Batch** to restore.

---

### Scenario 7: Exact SHA-256 & 3-Gram Jaccard Near-Duplicate Detection (Phase 6)
1. **Via Toolbar**:
   - Check the **`Near-Duplicates (Jaccard ≥85%)`** toggle in the top toolbar and click **`🔍 Scan Duplicates`**.
   - **Expected Result**: Displays a structured audit of duplicate groups showing both `EXACT_SHA256` matches and any `NEAR_JACCARD` revised drafts, preserving the clean canonical filename as **Keeper** and staging the `(1)` / `_edit` copies for soft-delete.
2. **Via Chat & Multi-Turn Confirmation**:
   - Type **`Yes`** (or **`Clean duplicate downloads`**) in the chat.
   - The multi-turn affirmative classifier resolves your `"Yes"` directly into a `FILE_OPERATION` deduplication plan!
   - Click **Execute Selected** to move the redundant copies to `.agent_trash/`, then test **⏪ 1-Tap Undo Batch** to verify byte-for-byte restoration from `.agent_trash/`.

---

### Scenario 8: "Forgotten Files" Finder & Cleanup (Phase 6)
1. **Click `🕰️ Forgotten Files`** in the toolbar (or type `"Find forgotten files in Download"`).
   - **Expected Result**: Identifies `temp_cache_sync.tmp` (`TEMPORARY_FILE`) and `Screenshot_20241001-142210.png` (`ORPHANED_SCREENSHOT`) plus any files $\ge 90$ days old, while leaving active study materials untouched.
2. **Type in Chat**:
   ```text
   Clean forgotten files
   ```
   - Generates a `TRASH` Action Plan for the forgotten items. Execute and rollback to verify.

---

### Scenario 9: Format-Based Sorting & Micro-Batch Execution
- **Type in Chat**:
  ```text
  Sort all the file types together in the downloads
  ```
- **Expected Result**:
  - First turn (`CONVERSATION`): Shows a Markdown table breaking down loose files in `Download/` by format (`PDF Documents`, `Photos & Images`, `Word & Office Docs`) and asks for confirmation.
  - Click the quick chip **`✅ Yes, Move All Files`** (or type `"Yes"`).
  - Second turn (`FILE_OPERATION`): Stages a micro-batch of up to 25 files moving into `Download/PDFs/`, `Download/Images/`, etc.

---

### Scenario 10: Live SSE Progress Bar & In-Flight Cancellation (Phase 7)
- When executing any multi-file plan, watch the **Live Execution Progress Bar** above the chat input:
  - It updates in real time via SSE (`Chunk 1/2 — 50%`) as each micro-batch commits to SQLite WAL with a 50ms OS watchdog yield.
  - Clicking **`⏹ Stop Execution`** mid-batch sends `POST /cancel_execution`, stopping the batch cleanly after the current file finishes and allowing immediate 1-tap rollback of the completed prefix.

---

## 4. Where to Verify Files & Ledger State on Disk

| Artifact | Path on Desktop / Laptop | Path on Android (Termux) | Purpose |
| :--- | :--- | :--- | :--- |
| **Downloads Root** | `./fixtures/Download/` | `/storage/emulated/0/Download/` | Incoming unorganized files & format subfolders (`PDFs/`, `Images/`). |
| **Organized Taxonomy** | `./fixtures/Documents/` | `/storage/emulated/0/Documents/` | `University/AI/`, `University/Vouchers/`, `Peers/<Name>/`, `Finances/`. |
| **Soft-Delete Vault** | `./fixtures/.agent_trash/` | `/storage/emulated/0/.agent_trash/` | Timestamped + UUID-prefixed trashed files awaiting rollback or purge. |
| **SQLite WAL Ledger** | `./fixtures/.ledger.db` | `~/.storage_copilot_ledger.db` | `batches`, `action_ledger` (with inverted undo vectors), and `trash_index`. |
| **Bearer Token** | `~/.storage_copilot_token` | `~/.storage_copilot_token` | 256-bit pre-shared token (`0600` permissions) for zero-trust auth. |
