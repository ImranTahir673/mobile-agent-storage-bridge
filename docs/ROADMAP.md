# Project Roadmap & Implementation Phases

## 1. Overview

This roadmap details the engineering phases required to evolve the **Mobile Agent Storage Bridge** from a prototype into an enterprise-grade, fail-safe autonomous mobile filesystem management platform.

```
+-----------------------------------------------------------------------------------------+
| Phase 1: Hardening server.py (Gatekeeper, SQLite Ledger, Batch Plans) [CURRENT FOCUS]   |
+-----------------------------------------------------------------------------------------+
                                           |
                                           v
+-----------------------------------------------------------------------------------------+
| Phase 2: Client & Decision Engine Evolution (Action Plan Generator & CLI)               |
+-----------------------------------------------------------------------------------------+
                                           |
                                           v
+-----------------------------------------------------------------------------------------+
| Phase 3: Android Daemon Resiliency & Named Cloudflare Tunnels                           |
+-----------------------------------------------------------------------------------------+
                                           |
                                           v
+-----------------------------------------------------------------------------------------+
| Phase 4: On-Device Hybrid LLMs & Web Audit Dashboard                                    |
+-----------------------------------------------------------------------------------------+
```

---

## 2. Phase 1: Hardening `server.py` & Bridge Engine (Immediate Priority)

The immediate objective is eliminating vulnerabilities in the mobile Flask server and replacing ad-hoc tool endpoints with the Policy Gatekeeper and SQLite Rollback Ledger.

### 2.1 Tasks & Deliverables

- [ ] **1.1 Strict Path Canonicalization & Containment**
  - Replace raw string containment in `resolve_safe_path` with `os.path.realpath` to protect against symlink traversal and Unicode normalization attacks.
  - Return standardized HTTP 403 error codes for boundary violations.

- [ ] **1.2 Policy Gatekeeper Integration**
  - Implement storage blacklist enforcement (`/Android`, `.agent_trash`, system roots, root dot-files).
  - Implement blast radius validation: Reject any batch exceeding 20 actions or 500 MB cumulative size.
  - Implement collision detection modes (`FAIL`, `RENAME_NUMERIC`, `RENAME_TIMESTAMP`, `SKIP`).

- [ ] **1.3 SQLite Action Ledger (`ledger.db`) Implementation**
  - Implement SQLite database initialization with WAL mode (`PRAGMA journal_mode = WAL`).
  - Create `batches`, `action_ledger`, and `trash_index` tables per `SCHEMA_SPEC.md`.
  - Ensure pre-logging of operations with calculated inverted undo vectors prior to disk writes.

- [ ] **1.4 Batch Execution Endpoint (`/execute_plan`)**
  - Add `POST /execute_plan` accepting the declarative JSON Action Plan.
  - Support `dry_run=true` returning an impact report and diff summary without modifying storage.
  - Wrap sequential step executions in a failure-trap that auto-triggers batch rollback if any step throws an unhandled error.

- [ ] **1.5 Rollback Endpoint (`/rollback_batch`)**
  - Add `POST /rollback_batch` accepting a `batch_id`.
  - Revert operations in reverse order (`ORDER BY step_index DESC`).
  - Restore soft-deleted files from `.agent_trash/` back to their original locations.

- [ ] **1.6 Production Server Runtime**
  - Replace development `Flask.run()` with a production WSGI runner (`Waitress` or multi-worker `Gunicorn` via Termux) to prevent single-threaded connection stalls.

---

## 3. Phase 2: Client & Decision Engine Evolution

Transition the client from single-action imperative tool calls to structured batch Action Plan generation.

### 2.2 Tasks & Deliverables

- [x] **2.1 Two-Stage Agent Protocol**
  - **Stage 1 (Reconnaissance)**: Read-only directory listing, text snippet inspection, and metadata gathering.
  - **Stage 2 (Plan Synthesis)**: Compile all proposed mutations into a single `ActionPlan` submitted to `/execute_plan`.

- [x] **2.2 Interactive Human-in-the-Loop CLI**
  - Render terminal table diffs showing:
    - Target files
    - Source $\to$ Destination paths
    - File size
    - Action type (`MOVE`, `TRASH`, `MKDIR`)
  - Require explicit user confirmation `[Y/n]` before firing non-dry-run batches.

- [ ] **2.3 Automated End-to-End Test Suite**
  - Create test scripts validating:
    - Directory boundary escape attacks.
    - Blacklist violation rejection.
    - Collision resolution behavior.
    - 100% byte-for-byte fidelity after rollback of multi-file moves and trash actions.

---

## 4. Phase 3: Android Daemon Resiliency & Named Tunnels

Ensure the phone server runs reliably in the background without being killed by Android's aggressive memory and battery managers.

### 4.1 Tasks & Deliverables

- [x] **3.1 Termux Daemonization & Wake-Lock**
  - Integrate `termux-wake-lock` commands in startup scripts to prevent CPU sleep during OTA operations.
  - Configure `termux-notification` to display a persistent foreground service status.
  - Implement graceful process management, PID tracking, and port cleanup (`scripts/start_daemon.sh`, `scripts/stop_daemon.sh`).

- [x] **3.2 Named Cloudflare Tunnel Configuration**
  - Support both quick tunnels (`trycloudflare.com`) and dedicated named Cloudflare tunnels with static domains (`cloudflared tunnel run --token <TOKEN>`).
  - Automatically parse live tunnel endpoints and write to `.bridge_url` for consumption by client agents.

- [x] **3.3 Termux Boot Auto-Start**
  - Enable automatic background launching upon device power-on via Termux:Boot integration.
  - Provide daemon status inspection tool reporting PID, memory footprint, port state, and tunnel connectivity (`scripts/status_daemon.sh`).

### 4.2 Daemon Management Guide

#### Starting the Daemon

```bash
# 1. Quick Tunnel Mode (Automatic ephemeral trycloudflare.com URL)
./scripts/start_daemon.sh

# 2. Named Cloudflare Tunnel (Static dedicated domain)
./scripts/start_daemon.sh --token <TUNNEL_TOKEN> --hostname agent.yourdomain.com

# 3. Local-Only Mode (No tunnel, runs on 127.0.0.1:8080)
./scripts/start_daemon.sh --no-tunnel

# 4. Custom Local Port
./scripts/start_daemon.sh --port 9090
```

#### Checking Daemon Status

```bash
./scripts/status_daemon.sh
```

Displays:
- **Server Process**: State (`RUNNING` / `OFFLINE`), PID, RSS memory consumption.
- **Local Health**: HTTP 200 verification, engine version, storage base directory.
- **Cloudflare Tunnel**: PID, memory RSS, and live connectivity status.
- **Public URL**: Resolved endpoint loaded from `.bridge_url`.

#### Stopping the Daemon

```bash
./scripts/stop_daemon.sh
```

- Sends `SIGTERM` followed by `SIGKILL` if necessary to both `server.py` and `cloudflared`.
- Releases the Termux CPU wake-lock (`termux-wake-unlock`).
- Removes the persistent Android status notification.
- Verifies local port 8080 is freed and cleans up `.bridge_url` and PID files.

#### Termux:Boot Integration (Auto-Start on Device Boot)

To run the storage bridge automatically every time your Android device powers on:

1. Install the **Termux:Boot** add-on APK from F-Droid.
2. Launch the Termux:Boot app once to register its broadcast receiver.
3. In Termux, create the boot scripts directory:
   ```bash
   mkdir -p ~/.termux/boot
   ```
4. Create `~/.termux/boot/start-bridge.sh`:
   ```bash
   #!/data/data/com.termux/files/usr/bin/bash
   # Wait for networking to initialize
   sleep 10
   cd ~/storage/shared/mobile-agent-storage-bridge || cd ~/mobile-agent-storage-bridge
   ./scripts/start_daemon.sh
   ```
5. Make the boot script executable:
   ```bash
   chmod +x ~/.termux/boot/start-bridge.sh
   ```

---

## 5. Phase 4: On-Device Hybrid LLMs & Web Audit Dashboard

Bring intelligent edge computing directly onto the phone and provide visual observability.

### 5.1 Tasks & Deliverables

- [ ] **4.1 On-Device Local SLM Execution**
  - Support running lightweight quantized models (e.g. Gemma 2B, Qwen 2.5 1.5B via `llama.cpp` on Termux) for 100% offline categorization.
  - Implement a **Hybrid Split-Brain Router**: Local model performs fast file inspection; Remote Gemini handles complex multi-step reasoning.

- [ ] **4.2 Mobile Bridge Audit Dashboard**
  - Lightweight web dashboard hosted on the phone (`/dashboard`).
  - View historical batches, inspect trashed items, visualize storage health, and trigger one-click rollbacks from any browser.
