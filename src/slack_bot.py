"""
SecondMe Slack Bot — responds as 승빈 using claude-code-api (Pro subscription).
Modes: draft (default), auto, on-demand
Uses RAG-based persona retrieval + per-channel conversation memory.
"""

import os
import re
import json
import time
import logging
import threading
import subprocess
import signal
import requests
import random
import sys
from pathlib import Path
from collections import defaultdict
from dotenv import load_dotenv
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from persona_rag import get_system_prompt, CORE_IDENTITY
from skills.conversation.session_orchestrator import ConversationSessionOrchestrator
from skills.types import LLMRunRequest
from tools.claude_runtime import ClaudeRuntimeClient

# ─── Setup ──────────────────────────────────────────────────────────

load_dotenv(Path(__file__).parent.parent / ".env")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("secondme")
app: App | None = None

# claude-code-api settings
CLAUDE_API_URL = os.environ.get("CLAUDE_API_URL", "http://localhost:8080")
CLAUDE_API_KEY = os.environ.get("CLAUDE_API_KEY", "sk-secondme-key-12345")
API_DIR = Path(__file__).parent.parent / "claude-code-api"
API_ENV_PATH = API_DIR / ".env"
API_LOG_PATH = Path(__file__).parent.parent / ".claude-code-api.log"
RUNTIME_CLIENT = ClaudeRuntimeClient(CLAUDE_API_URL, CLAUDE_API_KEY)

VALID_MODELS = {
    "sonnet": "claude-sonnet-4-6",
    "opus": "claude-opus-4-6",
    "haiku": "claude-haiku-4-5-20251001",
}

OWNER_USER_ID = os.environ["SLACK_USER_ID"]

# Track API server process for model switching
_api_process: subprocess.Popen | None = None

# ─── Conversation Memory ────────────────────────────────────────────

class ChannelMemory:
    """Per-channel conversation memory with context caching."""

    def __init__(self, max_messages: int = 30, session_timeout: int = 1800):
        # {channel_id: [{"role": "user"|"승빈", "sender": str, "text": str, "ts": float}]}
        self.history: dict[str, list[dict]] = defaultdict(list)
        # {channel_id: {"prompt": str, "ts": float}} — cached system prompt per channel
        self.cached_prompts: dict[str, dict] = {}
        self.max_messages = max_messages
        self.session_timeout = session_timeout  # 30 min session window

    def add_message(self, channel_id: str, sender: str, text: str, is_bot: bool = False):
        """Record a message in channel history."""
        self.history[channel_id].append({
            "role": "승빈" if is_bot else "user",
            "sender": sender if not is_bot else "승빈",
            "text": text,
            "ts": time.time(),
        })
        # Trim to max
        if len(self.history[channel_id]) > self.max_messages:
            self.history[channel_id] = self.history[channel_id][-self.max_messages:]

    def get_conversation(self, channel_id: str) -> str:
        """Get formatted conversation history for this channel."""
        msgs = self.history.get(channel_id, [])
        if not msgs:
            return ""

        # Only include messages within session timeout
        cutoff = time.time() - self.session_timeout
        recent = [m for m in msgs if m["ts"] > cutoff]

        if not recent:
            return ""

        lines = []
        for m in recent:
            lines.append(f"[{m['sender']}] {m['text']}")
        return "\n".join(lines)

    def get_cached_prompt(self, channel_id: str) -> str | None:
        """Get cached system prompt if still fresh (within 5 min)."""
        cached = self.cached_prompts.get(channel_id)
        if cached and (time.time() - cached["ts"]) < 300:  # 5 min cache
            return cached["prompt"]
        return None

    def cache_prompt(self, channel_id: str, prompt: str):
        """Cache the system prompt for this channel."""
        self.cached_prompts[channel_id] = {"prompt": prompt, "ts": time.time()}

    def clear_channel(self, channel_id: str):
        """Clear history for a channel."""
        self.history.pop(channel_id, None)
        self.cached_prompts.pop(channel_id, None)


memory = ChannelMemory()

# ─── State ──────────────────────────────────────────────────────────

class BotState:
    mode: str = "auto"
    channel_tones: dict = {}
    monitored_channels: set = set()
    pending_drafts: dict = {}

state = BotState()

# ─── Helpers ────────────────────────────────────────────────────────

def get_channel_tone(channel_id: str) -> str:
    return state.channel_tones.get(channel_id, "casual")

def get_current_model() -> str:
    """Read current CLAUDE_MODEL from API .env file."""
    try:
        for line in API_ENV_PATH.read_text().splitlines():
            if line.startswith("CLAUDE_MODEL="):
                return line.split("=", 1)[1].strip()
    except Exception:
        pass
    return "claude-sonnet-4-6"

def get_model_short_name(full_name: str) -> str:
    """Get short name for display."""
    for short, full in VALID_MODELS.items():
        if full == full_name:
            return short
    return full_name

def switch_model(new_model_id: str) -> bool:
    """Update .env and restart claude-code-api with new model."""
    global _api_process

    # Update .env file
    env_content = API_ENV_PATH.read_text()
    lines = env_content.splitlines()
    updated = False
    for i, line in enumerate(lines):
        if line.startswith("CLAUDE_MODEL="):
            lines[i] = f"CLAUDE_MODEL={new_model_id}"
            updated = True
            break
    if not updated:
        lines.append(f"CLAUDE_MODEL={new_model_id}")
    API_ENV_PATH.write_text("\n".join(lines) + "\n")

    # Kill existing API server
    try:
        # Find and kill processes running the API server
        result = subprocess.run(
            ["pgrep", "-f", "tsx server/agent-server.ts"],
            capture_output=True, text=True
        )
        for pid in result.stdout.strip().split("\n"):
            if pid:
                os.kill(int(pid), signal.SIGTERM)
        time.sleep(2)
    except Exception as e:
        logger.error(f"Failed to stop API server: {e}")

    # Start new API server with updated env
    try:
        env = os.environ.copy()
        # Load API .env into env
        for line in API_ENV_PATH.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k] = v

        log_file = open(API_LOG_PATH, "w")
        _api_process = subprocess.Popen(
            ["pnpm", "start"],
            cwd=str(API_DIR),
            env=env,
            stdout=log_file,
            stderr=log_file,
        )
        logger.info(f"API server restarting with model={new_model_id} (PID {_api_process.pid})")

        # Wait for health check
        for _ in range(20):
            time.sleep(1)
            try:
                r = requests.get(f"{CLAUDE_API_URL}/health", timeout=2)
                if r.status_code == 200:
                    logger.info("API server healthy after model switch")
                    return True
            except Exception:
                continue

        logger.warning("API health check timed out after model switch (may still be warming up)")
        return True  # Still return True — server is starting, just pool not ready yet

    except Exception as e:
        logger.error(f"Failed to start API server: {e}")
        return False

# ─── Question Classification ─────────────────────────────────────

# Keywords that signal a research/lookup question (real-time info needed)
RESEARCH_KEYWORDS = re.compile(
    r"(시세|시가|종가|주가|얼마|몇\s?달러|몇\s?원|환율|금리|금값"
    r"|나스닥|코스피|코스닥|다우|S&P|s&p|비트코인|이더리움|BTC|ETH|솔라나"
    r"|뉴스|소식|최근|최신|요즘|오늘|어제|이번\s?주|실적|발표"
    r"|날씨|기온|비\s?오|미세먼지"
    r"|찾아봐|검색해|알아봐|조사해|확인해|서치|search"
    r"|출시|업데이트|패치|언제\s?나|런칭|발매"
    r"|경기|스코어|순위|몇\s?대\s?몇|이겼|졌)"
    r"", re.IGNORECASE
)

def classify_question(text: str) -> str:
    """Classify a message as 'chat' (quick) or 'research' (needs tool use)."""
    return ConversationSessionOrchestrator.classify_question(text)

def generate_interim_message(channel_id: str, channel_tone: str) -> str:
    """Generate a natural 승빈-style 'hold on, let me look' message via Claude."""
    conversation = memory.get_conversation(channel_id)
    last_msg = conversation.strip().split("\n")[-1] if conversation else ""

    prompt = (
        f"너는 승빈이야. 친구가 방금 이렇게 말했어:\n"
        f"{last_msg}\n\n"
        f"이건 좀 찾아봐야 하는 질문이야. "
        f"'잠만 찾아볼게' 뉘앙스의 짧은 대답을 승빈 말투로 해줘. "
        f"매번 다르게, 자연스럽게. 한 문장만. 예시 느낌: '오 잠만 그거 나도 궁금하네 찾아볼게', '웅 잠만 볼게', '흠 잠만 확인해봄' 등. "
        f"하지만 예시를 그대로 쓰지 말고 상황에 맞게 새로 만들어."
    )

    result = _call_api(prompt, timeout_ms=10000)
    return result if result else "잠만 찾아볼게"

def generate_fallback_message(channel_id: str, channel_tone: str) -> str:
    """Generate a natural 승빈-style 'couldn't find it' message via Claude."""
    conversation = memory.get_conversation(channel_id)
    last_msgs = conversation.strip().split("\n")[-3:] if conversation else []
    context = "\n".join(last_msgs)

    prompt = (
        f"너는 승빈이야. 친구 질문에 대해 찾아봤는데 잘 안 찾아졌어.\n"
        f"최근 대화:\n{context}\n\n"
        f"'잘 못 찾겠다 / 나중에 다시 해볼게' 뉘앙스의 짧은 대답을 승빈 말투로 해줘. "
        f"한 문장만. 자연스럽게."
    )

    result = _call_api(prompt, timeout_ms=10000)
    return result if result else "흠 잘 안 찾아지네 나중에 다시 해볼게"

def split_response(text: str) -> list[str]:
    """Split response into individual Slack messages."""
    return ConversationSessionOrchestrator.split_response(text)

def sync_from_slack(client, channel_id: str, limit: int = 15):
    """Sync recent Slack messages into memory (cold start / catch up)."""
    if memory.history.get(channel_id):
        # Already have history, skip cold-start sync
        return

    try:
        result = client.conversations_history(channel=channel_id, limit=limit)
        messages = result.get("messages", [])
        messages.reverse()  # oldest first

        for msg in messages:
            uid = msg.get("user", "")
            text = msg.get("text", "")
            if not text:
                continue
            is_bot = bool(msg.get("bot_id"))
            try:
                user_info = client.users_info(user=uid)
                name = user_info["user"]["profile"].get("display_name") or user_info["user"]["real_name"]
            except Exception:
                name = uid
            memory.add_message(channel_id, name, text, is_bot=is_bot)
    except Exception as e:
        logger.error(f"Failed to sync from Slack: {e}")

def _build_system_prompt(channel_id: str, channel_tone: str) -> str:
    """Build or retrieve cached system prompt."""
    cached = memory.get_cached_prompt(channel_id)
    if cached:
        logger.info(f"  💾 Using cached system prompt")
        return cached
    conversation = memory.get_conversation(channel_id)
    system_prompt = get_system_prompt(conversation, channel_tone)
    memory.cache_prompt(channel_id, system_prompt)
    logger.info(f"  🔍 RAG: built new system prompt")
    return system_prompt

def _call_api(prompt: str, timeout_ms: int) -> str:
    """Call claude-code-api and return the response text, or empty string on failure."""
    logger.info(f"  📝 Prompt size: {len(prompt)} chars, timeout: {timeout_ms}ms")
    result = RUNTIME_CLIENT.run(LLMRunRequest(prompt=prompt, timeout_ms=timeout_ms))
    if not result.success:
        logger.error(f"claude-code-api request failed: {result.raw}")
        return ""
    text = result.output.strip()
    logger.info(f"  📨 Raw response ({result.duration_ms}ms): {text[:200]}")
    if text == "[SKIP]" or not text:
        return ""
    if "소프트웨어 개발" in text or "도움이 필요하시면" in text or "I can help" in text:
        logger.warning(f"  ⚠️ Claude Code assistant mode detected, filtering")
        return ""
    return text

def generate_chat_response(channel_id: str, channel_tone: str) -> str:
    """Quick chat response — no tool use, fast timeout."""
    conversation = memory.get_conversation(channel_id)
    if not conversation:
        return ""

    system_prompt = _build_system_prompt(channel_id, channel_tone)

    prompt = (
        f"{system_prompt}\n\n"
        f"---\n\n"
        f"아래는 Slack 대화야. [승빈]은 네가 이전에 한 말이야:\n\n"
        f"{conversation}\n\n"
        f"위 대화에 승빈으로서 자연스럽게 대답해. 코드나 개발 도움이 아니라 일상 대화야. "
        f"짧게 한국어 반말로. 이전 답변과 일관성 유지. 할 말 없으면 [SKIP]만.\n\n"
        f"도구 사용 없이 바로 대답해."
    )

    return _call_api(prompt, timeout_ms=30000)

def generate_research_response(channel_id: str, channel_tone: str) -> str:
    """Research response — uses WebSearch tool, longer timeout."""
    conversation = memory.get_conversation(channel_id)
    if not conversation:
        return ""

    system_prompt = _build_system_prompt(channel_id, channel_tone)

    prompt = (
        f"{system_prompt}\n\n"
        f"---\n\n"
        f"아래는 Slack 대화야. [승빈]은 네가 이전에 한 말이야:\n\n"
        f"{conversation}\n\n"
        f"위 대화의 마지막 질문에 대해 승빈으로서 대답해.\n\n"
        f"중요 지시사항:\n"
        f"1. WebSearch 도구를 사용해서 최신 정보를 검색해.\n"
        f"2. 검색 결과를 승빈 말투로 자연스럽게 전달해 (짧은 한국어 반말).\n"
        f"3. 숫자/데이터는 정확하게, 하지만 말투는 캐주얼하게.\n"
        f"4. 출처 URL은 붙이지 마. 친구한테 말하듯이.\n"
        f"5. 할 말 없으면 [SKIP]만."
    )

    return _call_api(prompt, timeout_ms=120000)

def _handle_draft(client, channel_id: str, sender_name: str, original_text: str, response: str, thread_ts: str = None):
    """Queue a draft response and DM the owner for approval."""
    state.pending_drafts[channel_id] = {
        "text": response,
        "thread_ts": thread_ts,
    }
    memory.add_message(channel_id, "승빈", response, is_bot=True)

    try:
        ch_info = client.conversations_info(channel=channel_id)
        ch_name = ch_info["channel"]["name"]
    except Exception:
        ch_name = channel_id

    client.chat_postMessage(
        channel=OWNER_USER_ID,
        text=(
            f"*Draft for #{ch_name}*\n"
            f"In response to: _{sender_name}: {original_text[:100]}_\n\n"
            f"```{response}```\n\n"
            f"→ Go to <#{channel_id}> and use `/secondme send` to approve, or `/secondme skip` to discard."
        ),
    )

def send_response(client, channel_id: str, response: str, thread_ts: str = None):
    """Send response as split messages to Slack channel and record in memory."""
    for line in split_response(response):
        client.chat_postMessage(
            channel=channel_id,
            text=line,
            thread_ts=thread_ts,
        )
    # Record bot's own response in memory
    memory.add_message(channel_id, "승빈", response, is_bot=True)
    # Invalidate cached prompt so next RAG considers the new conversation state
    memory.cached_prompts.pop(channel_id, None)

# ─── Command Handlers ──────────────────────────────────────────────

def handle_secondme_command(ack, command, client):
    """Slash command to control the bot."""
    ack()
    text = command.get("text", "").strip()
    user_id = command["user_id"]

    if user_id != OWNER_USER_ID:
        client.chat_postEphemeral(
            channel=command["channel_id"],
            user=user_id,
            text="이 봇은 승빈만 제어할 수 있습니다."
        )
        return

    parts = text.split()
    cmd = parts[0] if parts else "help"

    if cmd == "mode":
        if len(parts) < 2:
            client.chat_postEphemeral(
                channel=command["channel_id"],
                user=user_id,
                text=f"Current mode: *{state.mode}*\nUsage: `/secondme mode [draft|auto|on-demand]`"
            )
        elif parts[1] in ("draft", "auto", "on-demand"):
            state.mode = parts[1]
            client.chat_postEphemeral(
                channel=command["channel_id"],
                user=user_id,
                text=f"Mode changed to *{state.mode}*"
            )
        else:
            client.chat_postEphemeral(
                channel=command["channel_id"],
                user=user_id,
                text="Invalid mode. Use: draft, auto, on-demand"
            )

    elif cmd == "tone":
        if len(parts) < 3:
            client.chat_postEphemeral(
                channel=command["channel_id"],
                user=user_id,
                text=f"Current channel tone: *{get_channel_tone(command['channel_id'])}*\nUsage: `/secondme tone [casual|formal]`"
            )
        else:
            tone = parts[1]
            if tone in ("casual", "formal"):
                state.channel_tones[command["channel_id"]] = tone
                client.chat_postEphemeral(
                    channel=command["channel_id"],
                    user=user_id,
                    text=f"This channel set to *{tone}* tone"
                )

    elif cmd == "send":
        channel_id = command["channel_id"]
        draft = state.pending_drafts.get(channel_id)
        if draft:
            send_response(client, channel_id, draft["text"], draft.get("thread_ts"))
            del state.pending_drafts[channel_id]
            client.chat_postEphemeral(channel=channel_id, user=user_id, text="Draft sent!")
        else:
            client.chat_postEphemeral(channel=channel_id, user=user_id, text="No pending draft for this channel.")

    elif cmd == "skip":
        channel_id = command["channel_id"]
        state.pending_drafts.pop(channel_id, None)
        client.chat_postEphemeral(channel=channel_id, user=user_id, text="Draft discarded.")

    elif cmd == "status":
        drafts = len(state.pending_drafts)
        channels = len(state.monitored_channels) or "all"
        ch_id = command["channel_id"]
        mem_count = len(memory.history.get(ch_id, []))
        has_cache = ch_id in memory.cached_prompts
        client.chat_postEphemeral(
            channel=ch_id,
            user=user_id,
            text=(
                f"*SecondMe Status*\n"
                f"Mode: {state.mode}\n"
                f"Pending drafts: {drafts}\n"
                f"Monitored channels: {channels}\n"
                f"This channel tone: {get_channel_tone(ch_id)}\n"
                f"Memory: {mem_count} messages\n"
                f"Prompt cache: {'active' if has_cache else 'none'}"
            )
        )

    else:
        client.chat_postEphemeral(
            channel=command["channel_id"],
            user=user_id,
            text=(
                "*SecondMe Commands*\n"
                "• `/secondme mode [draft|auto|on-demand]` — switch mode\n"
                "• `/secondme tone [casual|formal]` — set channel tone\n"
                "• `/secondme send` — approve & send pending draft\n"
                "• `/secondme skip` — discard pending draft\n"
                "• `/secondme status` — show current state"
            )
        )


# ─── Message Handler ───────────────────────────────────────────────

def handle_message(event, client, logger):
    """Handle incoming messages based on current mode."""
    logger.info(f"📨 Message event received: channel={event.get('channel')} user={event.get('user')} text={event.get('text', '')[:50]}")

    # Ignore bot messages but still record them in memory
    if event.get("bot_id"):
        logger.info("  → Skipped (bot message)")
        return
    if event.get("subtype"):
        logger.info("  → Skipped (subtype)")
        return

    user_id = event.get("user", "")
    text = event.get("text", "")
    channel_id = event["channel"]

    # Resolve display name for this user
    try:
        user_info = client.users_info(user=user_id)
        sender_name = user_info["user"]["profile"].get("display_name") or user_info["user"]["real_name"]
    except Exception:
        sender_name = user_id

    # Record this message in memory
    memory.add_message(channel_id, sender_name, text)

    is_test = False

    # Owner commands
    if user_id == OWNER_USER_ID:
        if text.startswith("!mode "):
            new_mode = text.split(" ", 1)[1].strip()
            if new_mode in ("draft", "auto", "on-demand"):
                state.mode = new_mode
                logger.info(f"  → Mode changed to: {state.mode}")
                client.chat_postMessage(channel=channel_id, text=f"모드 변경: *{state.mode}*")
            return
        if text.startswith("!tone "):
            new_tone = text.split(" ", 1)[1].strip()
            if new_tone in ("casual", "formal"):
                state.channel_tones[channel_id] = new_tone
                logger.info(f"  → Tone changed to: {new_tone}")
                client.chat_postMessage(channel=channel_id, text=f"톤 변경: *{new_tone}*")
            return
        if text == "!send":
            draft = state.pending_drafts.get(channel_id)
            if draft:
                send_response(client, channel_id, draft["text"], draft.get("thread_ts"))
                del state.pending_drafts[channel_id]
            return
        if text == "!skip":
            state.pending_drafts.pop(channel_id, None)
            return
        if text == "!status":
            mem_count = len(memory.history.get(channel_id, []))
            cur_model = get_current_model()
            cur_short = get_model_short_name(cur_model)
            client.chat_postMessage(
                channel=channel_id,
                text=f"Mode: {state.mode} | Model: {cur_short} ({cur_model}) | Tone: {get_channel_tone(channel_id)} | Drafts: {len(state.pending_drafts)} | Memory: {mem_count}msgs"
            )
            return
        if text.startswith("!model"):
            parts = text.split()
            if len(parts) < 2:
                cur_model = get_current_model()
                cur_short = get_model_short_name(cur_model)
                model_list = " / ".join(f"`{s}`" for s in VALID_MODELS.keys())
                client.chat_postMessage(
                    channel=channel_id,
                    text=f"현재 모델: *{cur_short}* (`{cur_model}`)\n사용법: `!model [{model_list}]`"
                )
            else:
                requested = parts[1].lower()
                if requested not in VALID_MODELS:
                    model_list = ", ".join(VALID_MODELS.keys())
                    client.chat_postMessage(channel=channel_id, text=f"잘못된 모델. 사용 가능: {model_list}")
                else:
                    new_model_id = VALID_MODELS[requested]
                    cur_model = get_current_model()
                    if new_model_id == cur_model:
                        client.chat_postMessage(channel=channel_id, text=f"이미 *{requested}* 모델 사용 중")
                    else:
                        client.chat_postMessage(channel=channel_id, text=f"모델 전환 중: *{get_model_short_name(cur_model)}* → *{requested}*... 잠시만 기다려")
                        logger.info(f"  → Model switch: {cur_model} → {new_model_id}")

                        def _switch():
                            success = switch_model(new_model_id)
                            if success:
                                client.chat_postMessage(channel=channel_id, text=f"모델 전환 완료: *{requested}* (`{new_model_id}`)")
                            else:
                                client.chat_postMessage(channel=channel_id, text=f"모델 전환 실패. 로그 확인 필요")

                        threading.Thread(target=_switch, daemon=True).start()
            return
        if text.startswith("!test"):
            is_test = True
            logger.info("  → Owner test mode (will respond directly)")
        else:
            logger.info("  → Owner message (will respond normally)")

    logger.info(f"  → Mode: {state.mode}, Channel tone: {get_channel_tone(channel_id)}, Memory: {len(memory.history.get(channel_id, []))}msgs")

    # On-demand mode: only respond when explicitly triggered
    if state.mode == "on-demand" and not is_test:
        logger.info("  → Skipped (on-demand mode)")
        return

    if state.monitored_channels and channel_id not in state.monitored_channels and not is_test:
        return

    # Cold start: sync from Slack if no memory yet
    sync_from_slack(client, channel_id)

    tone = get_channel_tone(channel_id)
    question_type = classify_question(text)
    thread_ts = event.get("thread_ts") or event.get("ts")
    reply_ts = thread_ts if event.get("thread_ts") else None

    logger.info(f"  ⏳ Generating response for {channel_id} (type={question_type})...")

    if question_type == "research":
        # Two-phase: send interim message first, then research
        if is_test or state.mode == "auto":
            interim = generate_interim_message(channel_id, tone)
            logger.info(f"  📤 Sending interim: {interim}")
            client.chat_postMessage(channel=channel_id, text=interim, thread_ts=reply_ts)
            memory.add_message(channel_id, "승빈", interim, is_bot=True)

            # Run research in background thread to not block Slack event loop
            def _do_research():
                response = generate_research_response(channel_id, tone)
                if response:
                    logger.info(f"  ✅ Research response ready ({len(response)} chars)")
                    send_response(client, channel_id, response, reply_ts)
                else:
                    logger.info(f"  → No research response generated")
                    fallback = generate_fallback_message(channel_id, tone)
                    client.chat_postMessage(channel=channel_id, text=fallback, thread_ts=reply_ts)
                    memory.add_message(channel_id, "승빈", fallback, is_bot=True)

            threading.Thread(target=_do_research, daemon=True).start()
        elif state.mode == "draft":
            # For draft mode, just generate and queue (no interim needed)
            response = generate_research_response(channel_id, tone)
            if response:
                _handle_draft(client, channel_id, sender_name, text, response, reply_ts)
        return

    # Quick chat path
    response = generate_chat_response(channel_id, tone)

    if not response:
        logger.info(f"  → No response generated (empty or SKIP)")
        return

    logger.info(f"  ✅ Response ready ({len(response)} chars)")

    if is_test or state.mode == "auto":
        send_response(client, channel_id, response, reply_ts)

    elif state.mode == "draft":
        _handle_draft(client, channel_id, sender_name, text, response, reply_ts)


# ─── App Mention Handler ───────────────────────────────────────────

def handle_mention(event, client):
    """Always respond to @mentions regardless of mode."""
    channel_id = event["channel"]
    tone = get_channel_tone(channel_id)

    # Record mention in memory
    user_id = event.get("user", "")
    text = event.get("text", "")
    try:
        user_info = client.users_info(user=user_id)
        sender_name = user_info["user"]["profile"].get("display_name") or user_info["user"]["real_name"]
    except Exception:
        sender_name = user_id
    memory.add_message(channel_id, sender_name, text)

    # Cold start sync
    sync_from_slack(client, channel_id)

    question_type = classify_question(text)
    thread_ts = event.get("thread_ts") or event.get("ts")

    logger.info(f"  ⏳ Generating @mention response for {channel_id} (type={question_type})...")

    if question_type == "research" and state.mode != "draft":
        # Two-phase response for @mention research
        interim = generate_interim_message(channel_id, tone)
        client.chat_postMessage(channel=channel_id, text=interim, thread_ts=thread_ts)
        memory.add_message(channel_id, "승빈", interim, is_bot=True)

        def _do_research():
            response = generate_research_response(channel_id, tone)
            if response:
                logger.info(f"  ✅ @mention research response ready ({len(response)} chars)")
                send_response(client, channel_id, response, thread_ts)
            else:
                logger.info(f"  → No research response generated")
                fallback = generate_fallback_message(channel_id, tone)
                client.chat_postMessage(channel=channel_id, text=fallback, thread_ts=thread_ts)
                memory.add_message(channel_id, "승빈", fallback, is_bot=True)

        threading.Thread(target=_do_research, daemon=True).start()
        return

    # Quick chat or draft-mode research (single-phase)
    if question_type == "research":
        response = generate_research_response(channel_id, tone)
    else:
        response = generate_chat_response(channel_id, tone)

    if not response:
        logger.info(f"  → No response generated (empty or SKIP)")
        return

    logger.info(f"  ✅ @mention response ready ({len(response)} chars)")

    if state.mode == "draft":
        _handle_draft(client, channel_id, sender_name, text, response, thread_ts)
    else:
        send_response(client, channel_id, response, thread_ts)


# ─── Main ───────────────────────────────────────────────────────────

def create_app() -> App:
    slack_app = App(token=os.environ["SLACK_BOT_TOKEN"])
    slack_app.command("/secondme")(handle_secondme_command)
    slack_app.event("message")(handle_message)
    slack_app.event("app_mention")(handle_mention)
    return slack_app

if __name__ == "__main__":
    app = create_app()
    logger.info(f"SecondMe starting in '{state.mode}' mode...")
    logger.info(f"Owner: {OWNER_USER_ID}")
    logger.info(f"Core identity: {len(CORE_IDENTITY)} chars")
    logger.info(f"RAG persona retrieval + conversation memory enabled")
    logger.info(f"Memory: max {memory.max_messages} msgs/channel, session timeout {memory.session_timeout}s")

    handler = SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"])
    handler.start()
