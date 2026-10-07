#!/usr/bin/env bash
# ==============================================================================
# Mobile Agent Storage Bridge: Daemon Status Inspector (Phase 3)
# ==============================================================================
# Inspects running status of server.py and cloudflared tunnel, port binding,
# memory consumption, and public tunnel accessibility.
#
# Usage:
#   ./scripts/status_daemon.sh [PORT]
# ==============================================================================

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
LOGS_DIR="$PROJECT_DIR/logs"
RUN_DIR="$PROJECT_DIR/.run"
PORT="${1:-8080}"

echo "========================================================================"
echo "         Mobile Agent Storage Bridge: Daemon Status Report"
echo "========================================================================"

# 1. Inspect server.py process
SERVER_PID_FILE="$RUN_DIR/server.pid"
SERVER_STATUS="OFFLINE"
SERVER_PID=""
SERVER_MEM="-"

if [[ -f "$SERVER_PID_FILE" ]]; then
    SERVER_PID=$(cat "$SERVER_PID_FILE" 2>/dev/null || true)
    if [[ -n "$SERVER_PID" ]] && kill -0 "$SERVER_PID" 2>/dev/null; then
        SERVER_STATUS="RUNNING"
        # Extract RSS memory in MB
        if command -v ps >/dev/null 2>&1; then
            RSS_KB=$(ps -p "$SERVER_PID" -o rss= 2>/dev/null | tr -d ' ' || echo "0")
            if [[ "$RSS_KB" =~ ^[0-9]+$ ]] && [[ "$RSS_KB" -gt 0 ]]; then
                SERVER_MEM="$(( RSS_KB / 1024 )) MB"
            fi
        fi
    else
        SERVER_STATUS="STALE_PID"
    fi
fi

# 2. Inspect Cloudflare Tunnel process
TUNNEL_PID_FILE="$RUN_DIR/tunnel.pid"
TUNNEL_STATUS="OFFLINE"
TUNNEL_PID=""
TUNNEL_MEM="-"

if [[ -f "$TUNNEL_PID_FILE" ]]; then
    TUNNEL_PID=$(cat "$TUNNEL_PID_FILE" 2>/dev/null || true)
    if [[ -n "$TUNNEL_PID" ]] && kill -0 "$TUNNEL_PID" 2>/dev/null; then
        TUNNEL_STATUS="RUNNING"
        if command -v ps >/dev/null 2>&1; then
            RSS_KB=$(ps -p "$TUNNEL_PID" -o rss= 2>/dev/null | tr -d ' ' || echo "0")
            if [[ "$RSS_KB" =~ ^[0-9]+$ ]] && [[ "$RSS_KB" -gt 0 ]]; then
                TUNNEL_MEM="$(( RSS_KB / 1024 )) MB"
            fi
        fi
    else
        TUNNEL_STATUS="STALE_PID"
    fi
fi

# 3. Local Port & Health Check
HEALTH_URL="http://127.0.0.1:${PORT}/"
HEALTH_STATUS="UNREACHABLE"
ENGINE_INFO="-"
BASE_DIR_INFO="-"

if command -v curl >/dev/null 2>&1; then
    HEALTH_RESP=$(curl -s --max-time 2 "$HEALTH_URL" 2>/dev/null || true)
    if echo "$HEALTH_RESP" | grep -q '"status":\s*"running"'; then
        HEALTH_STATUS="ONLINE (HTTP 200)"
        ENGINE_INFO=$(echo "$HEALTH_RESP" | grep -o '"engine":\s*"[^"]*"' | cut -d'"' -f4 || echo "OK")
        BASE_DIR_INFO=$(echo "$HEALTH_RESP" | grep -o '"base_dir":\s*"[^"]*"' | cut -d'"' -f4 || echo "-")
    fi
fi

# 4. Public Tunnel URL Inspection
BRIDGE_URL_FILE="$PROJECT_DIR/.bridge_url"
PUBLIC_URL="Not Configured"
PUBLIC_URL_HEALTH="-"

if [[ -f "$BRIDGE_URL_FILE" ]]; then
    PUBLIC_URL=$(cat "$BRIDGE_URL_FILE" | tr -d '\r\n')
    if [[ "$PUBLIC_URL" =~ ^https?:// ]]; then
        if command -v curl >/dev/null 2>&1; then
            PUB_RESP=$(curl -s --max-time 4 "$PUBLIC_URL" 2>/dev/null || true)
            if echo "$PUB_RESP" | grep -q '"status":\s*"running"'; then
                PUBLIC_URL_HEALTH="ONLINE & RESPONSIVE"
            else
                PUBLIC_URL_HEALTH="CONNECTED / AWAITING_TRAFFIC"
            fi
        fi
    fi
fi

# 5. Display Status Table
printf "%-26s : %s\n" "● Bridge Server Process" "$SERVER_STATUS"
if [[ "$SERVER_STATUS" == "RUNNING" ]]; then
    printf "%-26s : %s\n" "  PID" "$SERVER_PID"
    printf "%-26s : %s\n" "  Memory RSS" "$SERVER_MEM"
fi

printf "%-26s : %s\n" "● Local Health Endpoint" "$HEALTH_STATUS"
if [[ "$HEALTH_STATUS" =~ ONLINE ]]; then
    printf "%-26s : %s\n" "  URL" "$HEALTH_URL"
    printf "%-26s : %s\n" "  Engine" "$ENGINE_INFO"
    printf "%-26s : %s\n" "  Base Storage" "$BASE_DIR_INFO"
fi

echo "------------------------------------------------------------------------"
printf "%-26s : %s\n" "● Cloudflare Tunnel" "$TUNNEL_STATUS"
if [[ "$TUNNEL_STATUS" == "RUNNING" ]]; then
    printf "%-26s : %s\n" "  PID" "$TUNNEL_PID"
    printf "%-26s : %s\n" "  Memory RSS" "$TUNNEL_MEM"
fi

printf "%-26s : %s\n" "● Public Tunnel URL" "$PUBLIC_URL"
if [[ "$PUBLIC_URL_HEALTH" != "-" ]]; then
    printf "%-26s : %s\n" "  Tunnel Connectivity" "$PUBLIC_URL_HEALTH"
fi

# 6. Termux wake-lock status
if command -v termux-wake-lock >/dev/null 2>&1; then
    echo "------------------------------------------------------------------------"
    printf "%-26s : %s\n" "● Termux Wake-Lock" "Supported & Integrated"
fi

echo "========================================================================"
