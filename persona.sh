#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
API_DIR="$SCRIPT_DIR/claude-code-api"
BOTS_DIR="$SCRIPT_DIR/bots"

# Load shared model pins (single source of truth for model versions).
if [[ -f "$SCRIPT_DIR/.env.models" ]]; then
    set -a
    source "$SCRIPT_DIR/.env.models"
    set +a
fi

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

    if [[ -n "$BOT_PID" ]] && is_pid_alive "$BOT_PID"; then
        log_err "[$name] bot already running (PID $BOT_PID). Use 'restart' or 'stop' first."
        return 1
    fi

    local b_log
    b_log=$(bot_log "$name")

    # ─ Ensure global claude-code-api server is healthy (managed by launchd) ─
    if ! command -v ccapi >/dev/null 2>&1; then
        log_err "[$name] 'ccapi' not in PATH. Install: ln -sf ~/.local/share/claude-code-api/bin/ccapi ~/.local/bin/ccapi"
        return 1
    fi
    local ccapi_url
    if ! ccapi_url=$(ccapi ensure 2>&1); then
        log_err "[$name] ccapi ensure failed:"
        echo "$ccapi_url"
        log_err "[$name] If not yet installed, run: ccapi install"
        return 1
    fi
    log_ok "[$name] claude-code-api healthy at $ccapi_url"
    API_PID="global"

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
    echo "  API: global (managed by launchd; logs: ccapi logs)"
    echo "  Bot log: $b_log"
}

# ─── Stop a single bot ──────────────────────────────────────────

do_stop_bot() {
    local name="$1"
    read_bot_pids "$name"
    local stopped=false

    # Note: claude-code-api is now managed globally by launchd (com.claudecodeapi).
    # We never kill it from here. Use 'launchctl unload ~/Library/LaunchAgents/com.claudecodeapi.plist'
    # if you really need to stop the API.
    if [[ -n "$BOT_PID" ]] && is_pid_alive "$BOT_PID"; then
        kill "$BOT_PID" 2>/dev/null && log_ok "[$name] Stopped bot (PID $BOT_PID)" || true
        stopped=true
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

    # API — global ccapi server (managed by launchd)
    local ccapi_url http_code
    if command -v ccapi >/dev/null 2>&1 && ccapi_url=$(ccapi url 2>/dev/null); then
        http_code=$(curl -s -o /dev/null -w "%{http_code}" "$ccapi_url/health" 2>/dev/null || echo "000")
        case "$http_code" in
            200) log_ok    "    API: running ($ccapi_url, healthy)" ;;
            503) log_warn  "    API: running ($ccapi_url, warming up)" ;;
            000) log_err   "    API: not reachable at $ccapi_url" ;;
            *)   log_warn  "    API: running ($ccapi_url, status $http_code)" ;;
        esac
    else
        log_err "    API: ccapi not installed (run: ccapi install)"
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
