#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
API_DIR="$SCRIPT_DIR/claude-code-api"
BOTS_DIR="$SCRIPT_DIR/bots"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
CYAN='\033[0;36m'
NC='\033[0m'

log_ok()   { echo -e "${GREEN}[OK]${NC} $1"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_err()  { echo -e "${RED}[ERROR]${NC} $1"; }
log_info() { echo -e "${CYAN}[INFO]${NC} $1"; }

is_pid_alive() {
    kill -0 "$1" 2>/dev/null
}

pid_file()  { echo "$SCRIPT_DIR/.persona.${1}.pids"; }
api_log()   { echo "$SCRIPT_DIR/.${1}-api.log"; }
bot_log()   { echo "$SCRIPT_DIR/.${1}-bot.log"; }

# ─── List available bots ────────────────────────────────────────

list_bots() {
    local bots=()
    for d in "$BOTS_DIR"/*/; do
        if [[ -f "$d/config.json" ]]; then
            bots+=("$(basename "$d")")
        fi
    done
    echo "${bots[@]}"
}

# ─── Read PIDs for a bot ────────────────────────────────────────

read_bot_pids() {
    local name="$1"
    API_PID=""
    BOT_PID=""
    local pf
    pf=$(pid_file "$name")
    if [[ -f "$pf" ]]; then
        source "$pf"
    fi
    # Fallback: check legacy PID file (.persona.pids) for seungbin only
    if [[ -z "$API_PID" && -z "$BOT_PID" && "$name" == "seungbin" && -f "$SCRIPT_DIR/.persona.pids" ]]; then
        source "$SCRIPT_DIR/.persona.pids"
    fi
}

# ─── Start a single bot ─────────────────────────────────────────

do_start_bot() {
    local name="$1"
    local bot_dir="$BOTS_DIR/$name"

    if [[ ! -d "$bot_dir" ]]; then
        log_err "Bot '$name' not found in $BOTS_DIR"
        return 1
    fi
    if [[ ! -f "$bot_dir/config.json" ]]; then
        log_err "No config.json found for bot '$name'"
        return 1
    fi
    if [[ ! -f "$bot_dir/.env" ]]; then
        log_err "No .env found for bot '$name'"
        return 1
    fi

    read_bot_pids "$name"

    # If PID file has an API_PID that's alive, check if it's actually our API
    # or a shared API from another bot. If the port is healthy, treat as shared.
    if [[ -n "$API_PID" && "$API_PID" != "shared" ]] && is_pid_alive "$API_PID"; then
        local check_port
        check_port=$(grep '^API_PORT=' "$bot_dir/.env" 2>/dev/null | cut -d= -f2 || echo "8080")
        if curl -sf "http://localhost:${check_port}/health" > /dev/null 2>&1; then
            log_info "[$name] API PID $API_PID is alive and port $check_port healthy — treating as shared"
            API_PID="shared"
        else
            log_err "[$name] claude-code-api already running (PID $API_PID). Use 'restart' or 'stop' first."
            return 1
        fi
    fi
    if [[ -n "$BOT_PID" ]] && is_pid_alive "$BOT_PID"; then
        log_err "[$name] bot already running (PID $BOT_PID). Use 'restart' or 'stop' first."
        return 1
    fi

    # Read bot .env for port and model info
    local api_port api_keys claude_model
    api_port=$(grep '^API_PORT=' "$bot_dir/.env" | cut -d= -f2 || echo "8080")
    api_keys=$(python3 -c "import json; print(json.load(open('$bot_dir/config.json')).get('api_keys',''))" 2>/dev/null || echo "")
    claude_model=$(grep '^CLAUDE_MODEL=' "$bot_dir/.env" | cut -d= -f2 || echo "claude-sonnet-4-6")

    local health_url="http://localhost:${api_port}/health"
    local a_log b_log
    a_log=$(api_log "$name")
    b_log=$(bot_log "$name")

    # ─ Start claude-code-api (or reuse existing) ─
    # Check if API is already running on this port
    if curl -sf "$health_url" > /dev/null 2>&1; then
        log_ok "[$name] API already running on port $api_port — reusing"
        API_PID="shared"
    else
        echo "[$name] Starting claude-code-api on port $api_port..."
        cd "$API_DIR"

        # Build env: base API .env + bot overrides
        set -a
        source "$API_DIR/.env"
        set +a
        export PORT="$api_port"
        if [[ -n "$api_keys" ]]; then
            export API_KEYS="$api_keys"
        fi
        if [[ -n "$claude_model" ]]; then
            export CLAUDE_MODEL="$claude_model"
        fi

        echo -e "\n=== [$name] API start $(date) ===" >> "$a_log"
        nohup pnpm start >> "$a_log" 2>&1 &
        API_PID=$!
        cd "$SCRIPT_DIR"

        # Wait for health
        echo "[$name] Waiting for API health check (port $api_port)..."
        local retries=0 max_retries=15
        while (( retries < max_retries )); do
            if curl -sf "$health_url" > /dev/null 2>&1; then
                log_ok "[$name] claude-code-api healthy (PID $API_PID, port $api_port)"
                break
            fi
            if ! is_pid_alive "$API_PID"; then
                log_err "[$name] claude-code-api exited unexpectedly. Check $a_log"
                return 1
            fi
            sleep 1
            (( retries++ ))
        done

        if (( retries >= max_retries )); then
            log_warn "[$name] Health check timed out — API may still be starting (PID $API_PID)"
        fi
    fi

    # ─ Start bot ─
    echo "[$name] Starting bot..."
    echo -e "\n=== [$name] Bot start $(date) ===" >> "$b_log"
    BOT_DIR="$bot_dir" \
    nohup "$SCRIPT_DIR/.venv/bin/python" "$SCRIPT_DIR/src/bot.py" >> "$b_log" 2>&1 &
    BOT_PID=$!

    sleep 1
    if ! is_pid_alive "$BOT_PID"; then
        log_err "[$name] bot exited unexpectedly. Check $b_log"
        echo "API_PID=$API_PID" > "$(pid_file "$name")"
        echo "BOT_PID=" >> "$(pid_file "$name")"
        return 1
    fi
    log_ok "[$name] bot started (PID $BOT_PID)"

    # Save PIDs
    cat > "$(pid_file "$name")" <<EOF
API_PID=$API_PID
BOT_PID=$BOT_PID
EOF

    echo ""
    log_ok "[$name] All services started"
    echo "  API log: $a_log"
    echo "  Bot log: $b_log"
}

# ─── Stop a single bot ──────────────────────────────────────────

do_stop_bot() {
    local name="$1"
    local bot_dir="$BOTS_DIR/$name"
    read_bot_pids "$name"
    local stopped=false

    if [[ -n "$API_PID" && "$API_PID" != "shared" ]] && is_pid_alive "$API_PID"; then
        kill "$API_PID" 2>/dev/null && log_ok "[$name] Stopped claude-code-api (PID $API_PID)" || true
        stopped=true
    elif [[ "$API_PID" == "shared" ]]; then
        log_info "[$name] API is shared — not stopping"
    fi

    if [[ -n "$BOT_PID" ]] && is_pid_alive "$BOT_PID"; then
        kill "$BOT_PID" 2>/dev/null && log_ok "[$name] Stopped bot (PID $BOT_PID)" || true
        stopped=true
    fi

    # Fallback: kill any process still on the bot's API port
    if [[ -f "$bot_dir/.env" ]]; then
        local api_port
        api_port=$(grep '^API_PORT=' "$bot_dir/.env" | cut -d= -f2 || echo "")
        if [[ -n "$api_port" ]]; then
            local port_pid
            port_pid=$(lsof -ti ":$api_port" 2>/dev/null || true)
            if [[ -n "$port_pid" ]]; then
                kill $port_pid 2>/dev/null && log_warn "[$name] Killed orphan process on port $api_port (PID $port_pid)" || true
                stopped=true
            fi
        fi
    fi

    rm -f "$(pid_file "$name")"
    # Clean up legacy PID file
    rm -f "$SCRIPT_DIR/.persona.pids"

    if $stopped; then
        log_ok "[$name] All services stopped"
    else
        echo "[$name] No running services found"
    fi
}

# ─── Status for a single bot ────────────────────────────────────

do_status_bot() {
    local name="$1"
    local bot_dir="$BOTS_DIR/$name"
    read_bot_pids "$name"

    local display_name persona_type
    display_name=$(python3 -c "import json; print(json.load(open('$bot_dir/config.json')).get('display_name','$name'))" 2>/dev/null || echo "$name")
    persona_type=$(python3 -c "import json; print(json.load(open('$bot_dir/config.json')).get('persona_type','unknown'))" 2>/dev/null || echo "unknown")

    echo -e "  ${CYAN}$name${NC} ($display_name, $persona_type)"

    # API — check health endpoint directly (handles shared API)
    local api_port
    api_port=$(grep '^API_PORT=' "$bot_dir/.env" 2>/dev/null | cut -d= -f2 || echo "8080")
    local health_url="http://localhost:${api_port}/health"
    local http_code
    http_code=$(curl -s -o /dev/null -w "%{http_code}" "$health_url" 2>/dev/null || echo "000")

    if [[ "$http_code" == "200" ]]; then
        if [[ "$API_PID" == "shared" ]]; then
            log_ok "    API: running (shared, port $api_port, healthy)"
        elif [[ -n "$API_PID" ]] && is_pid_alive "$API_PID"; then
            log_ok "    API: running (PID $API_PID, port $api_port, healthy)"
        else
            log_ok "    API: running (shared, port $api_port, healthy)"
        fi
    elif [[ "$http_code" == "503" ]]; then
        log_warn "    API: running (port $api_port, warming up)"
    elif [[ "$http_code" != "000" ]]; then
        log_warn "    API: running (port $api_port, status $http_code)"
    else
        log_err "    API: not running"
    fi

    # Bot
    if [[ -n "$BOT_PID" ]] && is_pid_alive "$BOT_PID"; then
        log_ok "    Bot: running (PID $BOT_PID)"
    else
        log_err "    Bot: not running"
    fi
}

# ─── Resolve bot names (handle 'all') ───────────────────────────

resolve_bots() {
    local target="$1"
    if [[ "$target" == "all" ]]; then
        list_bots
    else
        echo "$target"
    fi
}

# ─── Main dispatch ──────────────────────────────────────────────

cmd="${1:-}"
target="${2:-}"

case "$cmd" in
    start)
        if [[ -z "$target" ]]; then
            echo "Usage: $0 start <bot-name|all>"
            echo "Available bots: $(list_bots)"
            exit 1
        fi
        for bot in $(resolve_bots "$target"); do
            do_start_bot "$bot"
            echo ""
        done
        ;;

    stop)
        if [[ -z "$target" ]]; then
            echo "Usage: $0 stop <bot-name|all>"
            echo "Available bots: $(list_bots)"
            exit 1
        fi
        for bot in $(resolve_bots "$target"); do
            do_stop_bot "$bot"
        done
        ;;

    restart)
        if [[ -z "$target" ]]; then
            echo "Usage: $0 restart <bot-name|all>"
            echo "Available bots: $(list_bots)"
            exit 1
        fi
        for bot in $(resolve_bots "$target"); do
            do_stop_bot "$bot"
            echo ""
            do_start_bot "$bot"
            echo ""
        done
        ;;

    status)
        echo "=== Persona Services Status ==="
        echo ""
        local_bots=$(list_bots)
        if [[ -z "$local_bots" ]]; then
            echo "No bots configured in $BOTS_DIR"
        else
            for bot in $local_bots; do
                do_status_bot "$bot"
                echo ""
            done
        fi
        ;;

    list)
        echo "Available bots:"
        for bot in $(list_bots); do
            bot_dir="$BOTS_DIR/$bot"
            display_name=$(python3 -c "import json; print(json.load(open('$bot_dir/config.json')).get('display_name','$bot'))" 2>/dev/null || echo "$bot")
            persona_type=$(python3 -c "import json; print(json.load(open('$bot_dir/config.json')).get('persona_type','unknown'))" 2>/dev/null || echo "unknown")
            echo "  $bot ($display_name, $persona_type)"
        done
        ;;

    *)
        echo "Usage: $0 {start|stop|restart|status|list} [bot-name|all]"
        echo ""
        echo "Commands:"
        echo "  start <name|all>    Start bot(s) and their API server(s)"
        echo "  stop <name|all>     Stop bot(s)"
        echo "  restart <name|all>  Restart bot(s)"
        echo "  status              Show status of all bots"
        echo "  list                List available bots"
        echo ""
        echo "Available bots: $(list_bots)"
        exit 1
        ;;
esac
