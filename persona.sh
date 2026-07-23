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
restart_marker() { echo "$SCRIPT_DIR/.persona.${1}.last_restart"; }
# Manual-stop sentinel: `stop` drops this so the watchdog won't resurrect the
# bot; `start`/`restart` clears it to hand the bot back to the watchdog.
disabled_marker() { echo "$SCRIPT_DIR/.persona.${1}.disabled"; }

bot_llm_provider() {
    python3 -c "import json; print(json.load(open('$1/config.json')).get('llm', {}).get('provider', 'ccapi'))" 2>/dev/null || echo "ccapi"
}

bot_llm_url() {
    python3 -c "import json; print(json.load(open('$1/config.json')).get('llm', {}).get('url', ''))" 2>/dev/null
}

ensure_llm_gateway() {
    local name="$1"
    local bot_dir="$2"
    local provider url
    provider=$(bot_llm_provider "$bot_dir")

    if [[ "$provider" == "gsapi" ]]; then
        url=$(bot_llm_url "$bot_dir")
        url=${url:-${GSAPI_URL:-http://127.0.0.1:8081}}
        local http_code
        http_code=$(curl -s -o /dev/null -w "%{http_code}" "$url/health" 2>/dev/null || echo "000")
        if [[ "$http_code" != "200" ]]; then
            log_err "[$name] gsapi is not healthy at $url (status $http_code)."
            log_err "[$name] Start ../gpt-service-api on that URL, then retry."
            return 1
        fi
        log_ok "[$name] gsapi healthy at $url"
        return 0
    fi

    if [[ "$provider" != "ccapi" ]]; then
        log_err "[$name] unknown llm.provider '$provider' (use ccapi or gsapi)"
        return 1
    fi

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
    log_ok "[$name] ccapi healthy at $ccapi_url"
}

# Watchdog tuning (override via env)
WATCHDOG_WINDOW_MIN=${WATCHDOG_WINDOW_MIN:-5}      # look-back window in minutes
WATCHDOG_ERROR_THRESHOLD=${WATCHDOG_ERROR_THRESHOLD:-20}  # restart if [ERROR] count exceeds this
WATCHDOG_COOLDOWN_SEC=${WATCHDOG_COOLDOWN_SEC:-600}  # min seconds between watchdog restarts per bot

# Log rotation tuning
LOG_ROTATE_MIN_MB=${LOG_ROTATE_MIN_MB:-50}
LOG_ROTATE_KEEP_DAYS=${LOG_ROTATE_KEEP_DAYS:-7}

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

    # An explicit start/restart re-enables watchdog management: clear any
    # manual-stop sentinel left by a prior `stop`.
    local dm
    dm=$(disabled_marker "$name")
    if [[ -f "$dm" ]]; then
        rm -f "$dm"
        log_info "[$name] cleared watchdog-disabled flag (will be auto-managed again)"
    fi

    read_bot_pids "$name"

    if [[ -n "$BOT_PID" ]] && is_pid_alive "$BOT_PID"; then
        log_err "[$name] bot already running (PID $BOT_PID). Use 'restart' or 'stop' first."
        return 1
    fi

    local b_log
    b_log=$(bot_log "$name")

    # ─ Ensure configured LLM gateway is healthy ─
    ensure_llm_gateway "$name" "$bot_dir" || return 1
    API_PID="external"

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
    echo "  LLM gateway: $(bot_llm_provider "$bot_dir")"
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

    # API — selected per bot, so reporter can use gsapi while another bot uses ccapi.
    local provider gateway_url http_code
    provider=$(bot_llm_provider "$bot_dir")
    if [[ "$provider" == "gsapi" ]]; then
        gateway_url=$(bot_llm_url "$bot_dir")
        gateway_url=${gateway_url:-${GSAPI_URL:-http://127.0.0.1:8081}}
    elif command -v ccapi >/dev/null 2>&1 && gateway_url=$(ccapi url 2>/dev/null); then
        :
    else
        log_err "    API: ccapi not installed (run: ccapi install)"
        gateway_url=""
    fi
    if [[ -n "$gateway_url" ]]; then
        http_code=$(curl -s -o /dev/null -w "%{http_code}" "$gateway_url/health" 2>/dev/null || echo "000")
        case "$http_code" in
            200) log_ok    "    API: $provider running ($gateway_url, healthy)" ;;
            503) log_warn  "    API: $provider running ($gateway_url, warming up)" ;;
            000) log_err   "    API: $provider not reachable at $gateway_url" ;;
            *)   log_warn  "    API: $provider running ($gateway_url, status $http_code)" ;;
        esac
    fi

    # Bot
    if [[ -n "$BOT_PID" ]] && is_pid_alive "$BOT_PID"; then
        log_ok "    Bot: running (PID $BOT_PID)"
    else
        log_err "    Bot: not running"
    fi

    if [[ -f "$(disabled_marker "$name")" ]]; then
        log_warn "    Watchdog: DISABLED (manual stop; run 'start'/'restart' to re-enable)"
    fi
}

# ─── Watchdog: detect broken-loop and auto-restart ──────────────

do_watchdog_bot() {
    local name="$1"

    # Honor an explicit manual stop. `persona.sh stop <name>` drops a .disabled
    # sentinel; while it exists the watchdog leaves the bot down (no cold-start,
    # no error-restart). `start`/`restart` removes it. Silent skip — logging here
    # would spam the watchdog log every interval.
    if [[ -f "$(disabled_marker "$name")" ]]; then
        return 0
    fi

    local b_log
    b_log=$(bot_log "$name")
    [[ -f "$b_log" ]] || return 0

    read_bot_pids "$name"
    if [[ -z "${BOT_PID:-}" ]] || ! is_pid_alive "$BOT_PID"; then
        # Bot is dead (crash, OOM, signal). Cold-start it so the watchdog is
        # genuinely self-healing instead of leaving it down indefinitely.
        # NOTE: relies on AbandonProcessGroup=true in com.persona.watchdog.plist
        # so launchd does not reap the freshly-spawned bot when this run exits.
        log_warn "[$name] watchdog: bot not alive (PID ${BOT_PID:-none}), cold-starting"
        echo "=== [$name] watchdog cold-start $(date) (dead PID ${BOT_PID:-none}) ===" >> "$b_log"
        do_start_bot "$name" >/dev/null
        date +%s > "$(restart_marker "$name")"
        return 0
    fi

    # Cooldown guard
    local marker
    marker=$(restart_marker "$name")
    if [[ -f "$marker" ]]; then
        local last_ts now diff
        last_ts=$(cat "$marker" 2>/dev/null || echo 0)
        now=$(date +%s)
        diff=$((now - last_ts))
        if (( diff < WATCHDOG_COOLDOWN_SEC )); then
            return 0
        fi
    fi

    # Count [ERROR] lines newer than (now - WATCHDOG_WINDOW_MIN minutes).
    # Log lines are prefixed with "YYYY-MM-DD HH:MM:SS,mmm", so lexical >= works.
    local since
    since=$(date -v-${WATCHDOG_WINDOW_MIN}M '+%Y-%m-%d %H:%M' 2>/dev/null \
            || date -d "${WATCHDOG_WINDOW_MIN} minutes ago" '+%Y-%m-%d %H:%M')
    local err_count
    err_count=$(tail -n 20000 "$b_log" \
        | awk -v s="$since" 'index($0,"[ERROR]") && $0 >= s' \
        | wc -l | tr -d ' ')

    if (( err_count > WATCHDOG_ERROR_THRESHOLD )); then
        log_warn "[$name] watchdog: $err_count ERRORs in last ${WATCHDOG_WINDOW_MIN}m (threshold ${WATCHDOG_ERROR_THRESHOLD}), restarting PID $BOT_PID"
        echo "=== [$name] watchdog restart $(date) (errors=$err_count window=${WATCHDOG_WINDOW_MIN}m) ===" >> "$b_log"
        do_stop_bot "$name" >/dev/null
        sleep 1
        do_start_bot "$name" >/dev/null
        date +%s > "$marker"
    fi
}

# ─── Log rotation (copytruncate-safe for O_APPEND fds) ──────────

do_rotate_logs() {
    local size_bytes=$((LOG_ROTATE_MIN_MB * 1024 * 1024))
    local stamp
    stamp=$(date '+%Y%m%d-%H%M%S')
    local any=false

    shopt -s nullglob
    for log in "$SCRIPT_DIR"/.*-bot.log "$SCRIPT_DIR"/.*-api.log; do
        local sz
        sz=$(stat -f%z "$log" 2>/dev/null || stat -c%s "$log" 2>/dev/null || echo 0)
        if (( sz > size_bytes )); then
            local rotated="${log}.${stamp}"
            cp "$log" "$rotated"
            : > "$log"          # safe: bot writes via shell O_APPEND
            gzip -f "$rotated"  # creates ${rotated}.gz, removes ${rotated}
            log_ok "Rotated $(basename "$log") ($((sz/1024/1024))MB) → $(basename "${rotated}").gz"
            any=true
        fi
    done
    shopt -u nullglob

    # Cleanup rotations older than retention
    find "$SCRIPT_DIR" -maxdepth 1 \( -name '.*-bot.log.*.gz' -o -name '.*-api.log.*.gz' \) \
        -mtime "+${LOG_ROTATE_KEEP_DAYS}" -print -delete 2>/dev/null \
        | while read -r f; do log_info "Removed old rotation: $(basename "$f")"; done

    $any || log_info "No logs exceeded ${LOG_ROTATE_MIN_MB}MB threshold"
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
            # Drop the manual-stop sentinel so the watchdog won't revive it.
            # Only the CLI `stop` does this; the watchdog's own error-restart
            # path calls do_stop_bot directly and must NOT disable.
            : > "$(disabled_marker "$bot")"
            log_info "[$bot] watchdog disabled until next start/restart"
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

    watchdog)
        for bot in $(resolve_bots "${target:-all}"); do
            do_watchdog_bot "$bot"
        done
        ;;

    rotate-logs)
        do_rotate_logs
        ;;

    *)
        echo "Usage: $0 {start|stop|restart|status|list|watchdog|rotate-logs} [bot-name|all]"
        echo ""
        echo "Commands:"
        echo "  start <name|all>    Start bot(s) and their API server(s)"
        echo "  stop <name|all>     Stop bot(s); watchdog won't revive until start/restart"
        echo "  restart <name|all>  Restart bot(s)"
        echo "  status              Show status of all bots"
        echo "  list                List available bots"
        echo "  watchdog [name|all] Restart bot(s) if recent [ERROR] count exceeds threshold"
        echo "  rotate-logs         Rotate bot/api logs >${LOG_ROTATE_MIN_MB}MB, keep ${LOG_ROTATE_KEEP_DAYS}d gzip'd"
        echo ""
        echo "Available bots: $(list_bots)"
        exit 1
        ;;
esac
