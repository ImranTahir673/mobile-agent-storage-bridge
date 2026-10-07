#!/usr/bin/env bash
# ==============================================================================
# Mobile Agent Storage Bridge: Daemon Startup Engine (Phase 3)
# ==============================================================================
# Starts server.py and cloudflared tunnel in the background with Termux wake-lock
# and robust process management.
#
# Usage:
#   ./scripts/start_daemon.sh [OPTIONS]
#
# Options:
#   --port <PORT>        Local server port (default: 8080 or $PORT)
#   --token <TOKEN>      Cloudflare named tunnel token (or $CLOUDFLARE_TUNNEL_TOKEN)
#   --hostname <DOMAIN>  Named tunnel public hostname (e.g. agent.yourdomain.com)
#   --quick              Force quick tunnel (trycloudflare.com) even if token is configured
#   --no-tunnel          Start only the local server.py without cloudflared
#   --help, -h           Show this help message
# ==============================================================================

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
LOGS_DIR="$PROJECT_DIR/logs"
RUN_DIR="$PROJECT_DIR/.run"

mkdir -p "$LOGS_DIR" "$RUN_DIR"

# 1. Load environment variables from .env if present
ENV_FILE="$PROJECT_DIR/.env"
if [[ -f "$ENV_FILE" ]]; then
    # Export non-comment lines containing '='
    while IFS= read -r line || [[ -n "$line" ]]; do
        line=$(echo "$line" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')
        if [[ -n "$line" && ! "$line" =~ ^# && "$line" =~ = ]]; then
            key="${line%%=*}"
            val="${line#*=}"
            # Strip surrounding quotes
            val=$(echo "$val" | sed -e 's/^["'"'"']//' -e 's/["'"'"']$//')
            export "$key=$val"
        fi
    done < "$ENV_FILE"
fi

# 2. Defaults & CLI Argument Parsing
PORT="${PORT:-8080}"
TUNNEL_TOKEN="${CLOUDFLARE_TUNNEL_TOKEN:-}"
TUNNEL_HOSTNAME="${CLOUDFLARE_HOSTNAME:-}"
FORCE_QUICK=0
START_TUNNEL=1

while [[ $# -gt 0 ]]; do
    case "$1" in
        --port)
            PORT="$2"
            shift 2
            ;;
        --token|-t)
            TUNNEL_TOKEN="$2"
            shift 2
            ;;
        --hostname)
            TUNNEL_HOSTNAME="$2"
            shift 2
            ;;
        --quick)
            FORCE_QUICK=1
            shift
            ;;
        --no-tunnel)
            START_TUNNEL=0
            shift
            ;;
        --help|-h)
            echo "Usage: ./scripts/start_daemon.sh [--port <PORT>] [--token <TOKEN>] [--hostname <DOMAIN>] [--quick] [--no-tunnel]"
            exit 0
            ;;
        *)
            echo "[!] Warning: Unknown option '$1'"
            shift
            ;;
    esac
done

if [[ "$FORCE_QUICK" -eq 1 ]]; then
    TUNNEL_TOKEN=""
fi

echo "========================================================================"
echo " Mobile Agent Storage Bridge: Daemon Startup (Phase 3)"
echo "========================================================================"
echo "[*] Working directory: $PROJECT_DIR"
echo "[*] Local port:        $PORT"
echo "[*] Logs directory:    $LOGS_DIR"

# 3. Clean up existing or stalled instances
SERVER_PID_FILE="$RUN_DIR/server.pid"
TUNNEL_PID_FILE="$RUN_DIR/tunnel.pid"

cleanup_stale_pid() {
    local pid_file="$1"
    local proc_name="$2"
    if [[ -f "$pid_file" ]]; then
        local pid
        pid=$(cat "$pid_file" 2>/dev/null || true)
        if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
            echo "[*] Terminating existing $proc_name instance (PID: $pid)..."
            kill "$pid" 2>/dev/null || true
            sleep 1
            if kill -0 "$pid" 2>/dev/null; then
                kill -9 "$pid" 2>/dev/null || true
            fi
        fi
        rm -f "$pid_file"
    fi
}

cleanup_stale_pid "$SERVER_PID_FILE" "server.py"
cleanup_stale_pid "$TUNNEL_PID_FILE" "cloudflared"

# Free port if something else is binding it
free_port() {
    local p="$1"
    if command -v fuser >/dev/null 2>&1; then
        fuser -k "${p}/tcp" >/dev/null 2>&1 || true
    elif command -v lsof >/dev/null 2>&1; then
        local pids
        pids=$(lsof -ti :"${p}" 2>/dev/null || true)
        if [[ -n "$pids" ]]; then
            echo "$pids" | xargs -r kill -9 >/dev/null 2>&1 || true
        fi
    fi
}
free_port "$PORT"

# 4. Termux wake-lock & notification acquisition
if command -v termux-wake-lock >/dev/null 2>&1; then
    termux-wake-lock
    echo "[✓] Acquired Termux wake-lock (Android CPU sleep prevented)."
else
    echo "[*] Notice: termux-wake-lock not found (running in standard Linux/host environment)."
fi

if command -v termux-notification >/dev/null 2>&1; then
    termux-notification \
        --id "storage-bridge-daemon" \
        --title "Storage Bridge Running" \
        --content "Active on port $PORT" \
        --priority high \
        --ongoing >/dev/null 2>&1 || true
fi

# 5. Launch server.py in background
PYTHON_BIN="python3"
if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
    PYTHON_BIN="python"
fi

if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
    echo "[FATAL ERROR] Neither python3 nor python found in PATH."
    exit 1
fi

SERVER_LOG="$LOGS_DIR/server.log"
echo "[*] Launching server.py on 127.0.0.1:$PORT..."
PORT="$PORT" nohup "$PYTHON_BIN" "$PROJECT_DIR/server.py" > "$SERVER_LOG" 2>&1 &
SERVER_PID=$!
echo "$SERVER_PID" > "$SERVER_PID_FILE"
echo "  ✓ server.py started in background (PID: $SERVER_PID, Log: logs/server.log)"

# 6. Wait for local server health check
HEALTH_URL="http://127.0.0.1:${PORT}/"
echo "[*] Waiting for bridge health check ($HEALTH_URL)..."

HEALTH_SUCCESS=0
for i in $(seq 1 40); do
    if ! kill -0 "$SERVER_PID" 2>/dev/null; then
        echo "\n[FATAL ERROR] server.py terminated unexpectedly. Showing recent logs:"
        tail -n 25 "$SERVER_LOG"
        exit 1
    fi

    if command -v curl >/dev/null 2>&1; then
        HTTP_BODY=$(curl -s --max-time 1 "$HEALTH_URL" 2>/dev/null || true)
        if echo "$HTTP_BODY" | grep -q '"status":\s*"running"'; then
            HEALTH_SUCCESS=1
            break
        fi
    elif command -v python >/dev/null 2>&1 || command -v python3 >/dev/null 2>&1; then
        if "$PYTHON_BIN" -c "import urllib.request, json; resp=json.loads(urllib.request.urlopen('$HEALTH_URL', timeout=1).read()); exit(0 if resp.get('status')=='running' else 1)" 2>/dev/null; then
            HEALTH_SUCCESS=1
            break
        fi
    fi
    sleep 0.5
done

if [[ "$HEALTH_SUCCESS" -ne 1 ]]; then
    echo "\n[FATAL ERROR] server.py failed health check after 20 seconds."
    tail -n 25 "$SERVER_LOG"
    exit 1
fi

echo "  ✓ Local bridge health check PASSED (status: running)"

# 7. Launch Cloudflare Tunnel
BRIDGE_URL_FILE="$PROJECT_DIR/.bridge_url"
TUNNEL_LOG="$LOGS_DIR/tunnel.log"
PUBLIC_URL=""

if [[ "$START_TUNNEL" -eq 1 ]]; then
    if ! command -v cloudflared >/dev/null 2>&1; then
        echo "[!] Warning: 'cloudflared' binary not found in PATH."
        echo "    Install on Termux via: pkg install cloudflared"
        echo "    Bridge remains available locally at: $HEALTH_URL"
        echo "$HEALTH_URL" > "$BRIDGE_URL_FILE"
    else
        echo "[*] Launching Cloudflare tunnel..."
        if [[ -n "$TUNNEL_TOKEN" ]]; then
            echo "  * Tunnel Mode: Named Cloudflare Tunnel"
            nohup cloudflared tunnel run --token "$TUNNEL_TOKEN" > "$TUNNEL_LOG" 2>&1 &
            TUNNEL_PID=$!
            echo "$TUNNEL_PID" > "$TUNNEL_PID_FILE"

            if [[ -n "$TUNNEL_HOSTNAME" ]]; then
                PUBLIC_URL="https://$TUNNEL_HOSTNAME"
            else
                PUBLIC_URL="https://[named-tunnel-active]"
            fi
            echo "$PUBLIC_URL" > "$BRIDGE_URL_FILE"
            echo "  ✓ Named tunnel process running (PID: $TUNNEL_PID)"
        else
            echo "  * Tunnel Mode: Ephemeral Quick Tunnel (trycloudflare.com)"
            nohup cloudflared tunnel --protocol http2 --url "http://127.0.0.1:$PORT" > "$TUNNEL_LOG" 2>&1 &
            TUNNEL_PID=$!
            echo "$TUNNEL_PID" > "$TUNNEL_PID_FILE"
            echo "  ✓ Cloudflare quick tunnel running (PID: $TUNNEL_PID)"

            echo "[*] Resolving public URL from tunnel logs..."
            for j in $(seq 1 40); do
                if [[ -f "$TUNNEL_LOG" ]]; then
                    PUBLIC_URL=$(grep -o 'https://[-a-zA-Z0-9\.]*\.trycloudflare\.com' "$TUNNEL_LOG" | tail -n 1 || true)
                    if [[ -n "$PUBLIC_URL" ]]; then
                        break
                    fi
                fi
                sleep 0.5
            done

            if [[ -n "$PUBLIC_URL" ]]; then
                echo "$PUBLIC_URL" > "$BRIDGE_URL_FILE"
            else
                echo "[!] Warning: Could not detect quick tunnel URL within 20s. Check logs/tunnel.log."
                PUBLIC_URL="$HEALTH_URL"
                echo "$PUBLIC_URL" > "$BRIDGE_URL_FILE"
            fi
        fi
    fi
else
    echo "[*] Cloudflare tunnel skipped (--no-tunnel passed)."
    PUBLIC_URL="$HEALTH_URL"
    echo "$PUBLIC_URL" > "$BRIDGE_URL_FILE"
fi

# 8. Output Startup Summary Banner
echo ""
echo "========================================================================"
echo "           STORAGE BRIDGE DAEMON ONLINE & OPERATIONAL"
echo "========================================================================"
echo " Local Endpoint:      $HEALTH_URL"
if [[ -n "$PUBLIC_URL" ]]; then
echo " Public Tunnel URL:   $PUBLIC_URL"
fi
echo " Public URL Saved:    .bridge_url"
echo " Server Log:          logs/server.log (PID: $SERVER_PID)"
if [[ -f "$TUNNEL_PID_FILE" ]]; then
echo " Tunnel Log:          logs/tunnel.log (PID: $(cat "$TUNNEL_PID_FILE"))"
fi
echo " Control Commands:    ./scripts/status_daemon.sh"
echo "                      ./scripts/stop_daemon.sh"
echo "========================================================================"
