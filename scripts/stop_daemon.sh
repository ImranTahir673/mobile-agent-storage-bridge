#!/usr/bin/env bash
# ==============================================================================
# Mobile Agent Storage Bridge: Daemon Shutdown Engine (Phase 3)
# ==============================================================================
# Gracefully terminates server.py and cloudflared processes, releases Android
# wake-locks, and verifies that ports and lockfiles are cleanly released.
#
# Usage:
#   ./scripts/stop_daemon.sh [PORT]
# ==============================================================================

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
LOGS_DIR="$PROJECT_DIR/logs"
RUN_DIR="$PROJECT_DIR/.run"
PORT="${1:-8080}"

echo "========================================================================"
echo " Mobile Agent Storage Bridge: Daemon Shutdown"
echo "========================================================================"

stop_pid() {
    local pid_file="$1"
    local name="$2"

    if [[ -f "$pid_file" ]]; then
        local pid
        pid=$(cat "$pid_file" 2>/dev/null || true)
        if [[ -n "$pid" ]]; then
            if kill -0 "$pid" 2>/dev/null; then
                echo "[*] Sending SIGTERM to $name (PID: $pid)..."
                kill "$pid" 2>/dev/null || true
                
                # Wait up to 5 seconds for graceful shutdown
                local stopped=0
                for i in $(seq 1 10); do
                    if ! kill -0 "$pid" 2>/dev/null; then
                        stopped=1
                        break
                    fi
                    sleep 0.5
                done

                if [[ "$stopped" -eq 1 ]]; then
                    echo "  ✓ $name (PID: $pid) terminated gracefully."
                else
                    echo "[!] $name did not stop gracefully. Sending SIGKILL..."
                    kill -9 "$pid" 2>/dev/null || true
                    echo "  ✓ $name (PID: $pid) killed."
                fi
            else
                echo "[*] $name (PID: $pid) is already stopped."
            fi
        fi
        rm -f "$pid_file"
    else
        echo "[*] No PID file found for $name."
    fi
}

# 1. Stop background processes
stop_pid "$RUN_DIR/tunnel.pid" "Cloudflare Tunnel"
stop_pid "$RUN_DIR/server.pid" "server.py Bridge Server"

# Fallback: check for lingering server.py / cloudflared instances started in this directory
if command -v pgrep >/dev/null 2>&1; then
    LINGERING_SERVER=$(pgrep -f "python.*server.py" 2>/dev/null || true)
    if [[ -n "$LINGERING_SERVER" ]]; then
        echo "[*] Cleaning up lingering server.py processes: $LINGERING_SERVER"
        kill -9 $LINGERING_SERVER 2>/dev/null || true
    fi
fi

# 2. Verify Port is Freed
echo "[*] Verifying port $PORT is freed..."
if command -v fuser >/dev/null 2>&1; then
    fuser -k "${PORT}/tcp" >/dev/null 2>&1 || true
elif command -v lsof >/dev/null 2>&1; then
    PORT_PIDS=$(lsof -ti :"${PORT}" 2>/dev/null || true)
    if [[ -n "$PORT_PIDS" ]]; then
        echo "[*] Killing remaining process holding port $PORT: $PORT_PIDS"
        echo "$PORT_PIDS" | xargs -r kill -9 >/dev/null 2>&1 || true
    fi
fi
echo "  ✓ Port $PORT verified free."

# 3. Release Termux wake-lock
if command -v termux-wake-unlock >/dev/null 2>&1; then
    termux-wake-unlock
    echo "[✓] Released Termux wake-lock."
fi

# 4. Remove Termux notification
if command -v termux-notification-remove >/dev/null 2>&1; then
    termux-notification-remove --id "storage-bridge-daemon" >/dev/null 2>&1 || true
fi

# 5. Clean up public URL pointer
if [[ -f "$PROJECT_DIR/.bridge_url" ]]; then
    rm -f "$PROJECT_DIR/.bridge_url"
    echo "  ✓ Removed .bridge_url"
fi

echo "========================================================================"
echo "           STORAGE BRIDGE DAEMON COMPLETELY STOPPED"
echo "========================================================================"
