"""
Multi-persona Slack Bot — config-driven bot that loads persona from BOT_DIR.
Supports persona types: "persona" (seungbin-style RAG chat) and "coder" (dev sessions).
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

# ─── Bot Directory & Config ──────────────────────────────────────

BOT_DIR = Path(os.environ.get("BOT_DIR", Path(__file__).parent.parent / "bots" / "seungbin"))
PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from skills.conversation.session_orchestrator import ConversationSessionOrchestrator
from skills.types import LLMRunRequest
from tools.claude_runtime import ClaudeRuntimeClient
from tools.pdf_extract import extract_pdf_text

# Load bot-specific .env
load_dotenv(BOT_DIR / ".env")

# Load bot config
with open(BOT_DIR / "config.json", "r", encoding="utf-8") as f:
    BOT_CONFIG = json.load(f)

BOT_NAME = BOT_CONFIG["name"]
DISPLAY_NAME = BOT_CONFIG["display_name"]
PERSONA_TYPE = BOT_CONFIG["persona_type"]  # "persona", "coder", "reporter", or "research_pipeline"
DEFAULT_MODE = BOT_CONFIG.get("default_mode", "auto")
DEFAULT_MODEL = BOT_CONFIG.get("default_model", "claude-sonnet-4-6")

# ─── Setup ──────────────────────────────────────────────────────

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(f"bot-{BOT_NAME}")
app: App | None = None

# claude-code-api settings
CLAUDE_API_URL = os.environ.get("CLAUDE_API_URL", "http://localhost:8080")
CLAUDE_API_KEY = os.environ.get("CLAUDE_API_KEY", "sk-secondme-key-12345")
API_PORT = os.environ.get("API_PORT", "8080")
API_DIR = PROJECT_ROOT / "claude-code-api"
API_ENV_PATH = API_DIR / ".env"
RUNTIME_CLIENT = ClaudeRuntimeClient(CLAUDE_API_URL, CLAUDE_API_KEY)

VALID_MODELS = {
    "sonnet": "claude-sonnet-4-6",
    "opus": "claude-opus-4-6",
    "haiku": "claude-haiku-4-5-20251001",
}

OWNER_USER_ID = os.environ["SLACK_USER_ID"]
DEVELOPER_USER_IDS = set(os.environ.get("DEVELOPER_USER_IDS", OWNER_USER_ID).split(","))

# ─── RAG Setup (persona type only) ──────────────────────────────

rag_instance = None
CORE_IDENTITY = ""

if PERSONA_TYPE == "persona":
    from persona_rag import build_rag, DEFAULT_CORE_IDENTITY
    data_dir = BOT_DIR / "data"
    core_id = BOT_CONFIG.get("core_identity", None)
    rag_instance = build_rag(data_dir, core_id)
    CORE_IDENTITY = rag_instance.core_identity
elif PERSONA_TYPE == "coder":
    CORE_IDENTITY = BOT_CONFIG.get("core_identity", "")
elif PERSONA_TYPE in ("reporter", "research_pipeline"):
    CORE_IDENTITY = ""  # Pipeline bots use agent-specific prompts

# ─── Pipeline Setup (reporter / research_pipeline) ──────────────
_pipeline = None
_bot_scheduler = None

if PERSONA_TYPE in ("reporter", "research_pipeline"):
    from scheduler import BotScheduler

# Track API server process for model switching
_api_process: subprocess.Popen | None = None

# ─── Conversation Memory ────────────────────────────────────────

class ChannelMemory:
    """Per-channel conversation memory with context caching."""

    def __init__(self, max_messages: int = 30, session_timeout: int = 1800):
        self.history: dict[str, list[dict]] = defaultdict(list)
        self.cached_prompts: dict[str, dict] = {}
        self.max_messages = max_messages
        self.session_timeout = session_timeout

    def add_message(self, channel_id: str, sender: str, text: str, is_bot: bool = False):
        self.history[channel_id].append({
            "role": DISPLAY_NAME if is_bot else "user",
            "sender": sender if not is_bot else DISPLAY_NAME,
            "text": text,
            "ts": time.time(),
        })
        if len(self.history[channel_id]) > self.max_messages:
            self.history[channel_id] = self.history[channel_id][-self.max_messages:]

    def get_conversation(self, channel_id: str) -> str:
        msgs = self.history.get(channel_id, [])
        if not msgs:
            return ""
        cutoff = time.time() - self.session_timeout
        recent = [m for m in msgs if m["ts"] > cutoff]
        if not recent:
            return ""
        lines = []
        for m in recent:
            lines.append(f"[{m['sender']}] {m['text']}")
        return "\n".join(lines)

    def get_cached_prompt(self, channel_id: str) -> str | None:
        cached = self.cached_prompts.get(channel_id)
        if cached and (time.time() - cached["ts"]) < 300:
            return cached["prompt"]
        return None

    def cache_prompt(self, channel_id: str, prompt: str):
        self.cached_prompts[channel_id] = {"prompt": prompt, "ts": time.time()}

    def clear_channel(self, channel_id: str):
        self.history.pop(channel_id, None)
        self.cached_prompts.pop(channel_id, None)


memory = ChannelMemory()

# ─── State ──────────────────────────────────────────────────────

class BotState:
    mode: str = DEFAULT_MODE
    channel_tones: dict = {}
    monitored_channels: set = set()
    pending_drafts: dict = {}

state = BotState()

# ─── Dev Sessions (coder type only) ─────────────────────────────

class DevSession:
    """Tracks an active dev session for the coder bot."""

    def __init__(self, project: str, workspace: str):
        self.project = project
        self.workspace = workspace
        self.session_id: str | None = None
        self.started_at = time.time()
        self.message_count = 0

    @property
    def project_path(self) -> str:
        return f"{self.workspace}/{self.project}"


# {channel_id: DevSession}
_dev_sessions: dict[str, DevSession] = {}

# ─── Helpers ────────────────────────────────────────────────────

def get_channel_tone(channel_id: str) -> str:
    return state.channel_tones.get(channel_id, "casual")

def get_current_model() -> str:
    try:
        for line in API_ENV_PATH.read_text().splitlines():
            if line.startswith("CLAUDE_MODEL="):
                return line.split("=", 1)[1].strip()
    except Exception:
        pass
    return DEFAULT_MODEL

def get_model_short_name(full_name: str) -> str:
    for short, full in VALID_MODELS.items():
        if full == full_name:
            return short
    return full_name

def switch_model(new_model_id: str) -> bool:
    global _api_process
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

    try:
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

    try:
        env = os.environ.copy()
        for line in API_ENV_PATH.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k] = v

        log_path = PROJECT_ROOT / f".{BOT_NAME}-api.log"
        log_file = open(log_path, "w")
        _api_process = subprocess.Popen(
            ["pnpm", "start"],
            cwd=str(API_DIR),
            env=env,
            stdout=log_file,
            stderr=log_file,
        )
        logger.info(f"API server restarting with model={new_model_id} (PID {_api_process.pid})")

        for _ in range(20):
            time.sleep(1)
            try:
                r = requests.get(f"{CLAUDE_API_URL}/health", timeout=2)
                if r.status_code == 200:
                    logger.info("API server healthy after model switch")
                    return True
            except Exception:
                continue

        logger.warning("API health check timed out after model switch")
        return True

    except Exception as e:
        logger.error(f"Failed to start API server: {e}")
        return False

# ─── Question Classification ─────────────────────────────────────

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
    return ConversationSessionOrchestrator.classify_question(text)

def generate_interim_message(channel_id: str, channel_tone: str) -> str:
    """Generate a natural 'hold on' message."""
    conversation = memory.get_conversation(channel_id)
    last_msg = conversation.strip().split("\n")[-1] if conversation else ""

    if PERSONA_TYPE == "coder":
        prompt = (
            f"너는 코딩봇이야. 사용자가 방금 이렇게 말했어:\n"
            f"{last_msg}\n\n"
            f"작업을 시작한다는 뉘앙스의 짧은 대답을 해줘. "
            f"매번 다르게, 자연스럽게. 한 문장만. 예시 느낌: '잠만 확인해볼게', '오 그거 해볼게 잠만', '작업 시작할게' 등."
        )
    else:
        prompt = (
            f"너는 {DISPLAY_NAME}이야. 친구가 방금 이렇게 말했어:\n"
            f"{last_msg}\n\n"
            f"이건 좀 찾아봐야 하는 질문이야. "
            f"'잠만 찾아볼게' 뉘앙스의 짧은 대답을 {DISPLAY_NAME} 말투로 해줘. "
            f"매번 다르게, 자연스럽게. 한 문장만."
        )

    result = _call_api(prompt, timeout_ms=10000)
    return result if result else "잠만 확인해볼게"

def generate_fallback_message(channel_id: str, channel_tone: str) -> str:
    conversation = memory.get_conversation(channel_id)
    last_msgs = conversation.strip().split("\n")[-3:] if conversation else []
    context = "\n".join(last_msgs)

    if PERSONA_TYPE == "coder":
        prompt = (
            f"너는 코딩봇이야. 요청을 처리하려 했는데 잘 안 됐어.\n"
            f"최근 대화:\n{context}\n\n"
            f"'잘 안 되네 / 다시 해볼게' 뉘앙스의 짧은 대답을 해줘. 한 문장만."
        )
    else:
        prompt = (
            f"너는 {DISPLAY_NAME}이야. 친구 질문에 대해 찾아봤는데 잘 안 찾아졌어.\n"
            f"최근 대화:\n{context}\n\n"
            f"'잘 못 찾겠다 / 나중에 다시 해볼게' 뉘앙스의 짧은 대답을 {DISPLAY_NAME} 말투로 해줘. 한 문장만."
        )

    result = _call_api(prompt, timeout_ms=10000)
    return result if result else "흠 잘 안 되네 다시 해볼게"

def split_response(text: str) -> list[str]:
    return ConversationSessionOrchestrator.split_response(text)

def sync_from_slack(client, channel_id: str, limit: int = 15):
    if memory.history.get(channel_id):
        return
    try:
        result = client.conversations_history(channel=channel_id, limit=limit)
        messages = result.get("messages", [])
        messages.reverse()
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
    if PERSONA_TYPE == "coder":
        return CORE_IDENTITY

    cached = memory.get_cached_prompt(channel_id)
    if cached:
        logger.info(f"  💾 Using cached system prompt")
        return cached
    conversation = memory.get_conversation(channel_id)
    system_prompt = rag_instance.get_system_prompt(conversation, channel_tone)
    memory.cache_prompt(channel_id, system_prompt)
    logger.info(f"  🔍 RAG: built new system prompt")
    return system_prompt

def _call_api(prompt: str, timeout_ms: int, session_id: str | None = None) -> str:
    """Call claude-code-api and return the response text."""
    logger.info(f"  📝 Prompt size: {len(prompt)} chars, timeout: {timeout_ms}ms")
    result = RUNTIME_CLIENT.run(
        LLMRunRequest(prompt=prompt, timeout_ms=timeout_ms, session_id=session_id)
    )
    if not result.success:
        logger.error(f"claude-code-api error: {result.raw}")
        return ""
    text = result.output.strip()
    logger.info(f"  📨 Raw response ({result.duration_ms}ms): {text[:200]}")
    _call_api._last_session_id = result.session_id
    if text == "[SKIP]" or not text:
        return ""
    if PERSONA_TYPE == "persona":
        if "소프트웨어 개발" in text or "도움이 필요하시면" in text or "I can help" in text:
            logger.warning(f"  ⚠️ Claude Code assistant mode detected, filtering")
            return ""
    return text

_call_api._last_session_id = None

def generate_chat_response(channel_id: str, channel_tone: str) -> str:
    conversation = memory.get_conversation(channel_id)
    if not conversation:
        return ""

    system_prompt = _build_system_prompt(channel_id, channel_tone)

    if PERSONA_TYPE == "coder":
        prompt = (
            f"{system_prompt}\n\n"
            f"---\n\n"
            f"아래는 Slack 대화야:\n\n"
            f"{conversation}\n\n"
            f"위 대화에 자연스럽게 대답해. 코딩 관련 질문이면 코드로 답하고, "
            f"일반 대화면 짧게 한국어로 대답해. 할 말 없으면 [SKIP]만."
        )
    else:
        prompt = (
            f"{system_prompt}\n\n"
            f"---\n\n"
            f"아래는 Slack 대화야. [{DISPLAY_NAME}]은 네가 이전에 한 말이야:\n\n"
            f"{conversation}\n\n"
            f"위 대화에 {DISPLAY_NAME}으로서 자연스럽게 대답해. 코드나 개발 도움이 아니라 일상 대화야. "
            f"짧게 한국어 반말로. 이전 답변과 일관성 유지. 할 말 없으면 [SKIP]만.\n\n"
            f"도구 사용 없이 바로 대답해."
        )

    return _call_api(prompt, timeout_ms=30000)

def generate_research_response(channel_id: str, channel_tone: str) -> str:
    conversation = memory.get_conversation(channel_id)
    if not conversation:
        return ""

    system_prompt = _build_system_prompt(channel_id, channel_tone)

    if PERSONA_TYPE == "coder":
        prompt = (
            f"{system_prompt}\n\n"
            f"---\n\n"
            f"아래는 Slack 대화야:\n\n"
            f"{conversation}\n\n"
            f"위 대화의 마지막 질문에 대해 대답해.\n"
            f"WebSearch 도구를 사용해서 최신 정보를 검색한 후 답변해."
        )
    else:
        prompt = (
            f"{system_prompt}\n\n"
            f"---\n\n"
            f"아래는 Slack 대화야. [{DISPLAY_NAME}]은 네가 이전에 한 말이야:\n\n"
            f"{conversation}\n\n"
            f"위 대화의 마지막 질문에 대해 {DISPLAY_NAME}으로서 대답해.\n\n"
            f"중요 지시사항:\n"
            f"1. WebSearch 도구를 사용해서 최신 정보를 검색해.\n"
            f"2. 검색 결과를 {DISPLAY_NAME} 말투로 자연스럽게 전달해 (짧은 한국어 반말).\n"
            f"3. 숫자/데이터는 정확하게, 하지만 말투는 캐주얼하게.\n"
            f"4. 출처 URL은 붙이지 마. 친구한테 말하듯이.\n"
            f"5. 할 말 없으면 [SKIP]만."
        )

    return _call_api(prompt, timeout_ms=120000)

def _handle_draft(client, channel_id: str, sender_name: str, original_text: str, response: str, thread_ts: str = None):
    state.pending_drafts[channel_id] = {
        "text": response,
        "thread_ts": thread_ts,
    }
    memory.add_message(channel_id, DISPLAY_NAME, response, is_bot=True)

    try:
        ch_info = client.conversations_info(channel=channel_id)
        ch_name = ch_info["channel"]["name"]
    except Exception:
        ch_name = channel_id

    client.chat_postMessage(
        channel=OWNER_USER_ID,
        text=(
            f"*Draft for #{ch_name}* ({DISPLAY_NAME})\n"
            f"In response to: _{sender_name}: {original_text[:100]}_\n\n"
            f"```{response}```\n\n"
            f"→ Go to <#{channel_id}> and use `!send` to approve, or `!skip` to discard."
        ),
    )

def send_response(client, channel_id: str, response: str, thread_ts: str = None):
    for line in split_response(response):
        client.chat_postMessage(
            channel=channel_id,
            text=line,
            thread_ts=thread_ts,
        )
    memory.add_message(channel_id, DISPLAY_NAME, response, is_bot=True)
    memory.cached_prompts.pop(channel_id, None)

def send_code_response(client, channel_id: str, response: str, thread_ts: str = None):
    """Send response preserving code blocks (for coder bot)."""
    # If response contains code blocks, send as a single message
    if "```" in response:
        client.chat_postMessage(
            channel=channel_id,
            text=response,
            thread_ts=thread_ts,
        )
    else:
        # Split into lines for non-code responses
        for line in split_response(response):
            client.chat_postMessage(
                channel=channel_id,
                text=line,
                thread_ts=thread_ts,
            )
    memory.add_message(channel_id, DISPLAY_NAME, response, is_bot=True)
    memory.cached_prompts.pop(channel_id, None)

# ─── Dev Session Handlers (coder only) ──────────────────────────

def handle_dev_command(client, channel_id: str, text: str, thread_ts: str = None):
    """Handle !dev commands for the coder bot."""
    parts = text.split()
    subcmd = parts[1] if len(parts) > 1 else "help"

    if subcmd == "start":
        project = parts[2] if len(parts) > 2 else ""
        if not project:
            client.chat_postMessage(
                channel=channel_id,
                text="사용법: `!dev start <project>` — 프로젝트 이름을 입력해줘",
                thread_ts=thread_ts,
            )
            return

        workspace = os.environ.get("DEV_WORKSPACE", "/Users/shinseungbin/workspace")
        session = DevSession(project, workspace)
        _dev_sessions[channel_id] = session

        client.chat_postMessage(
            channel=channel_id,
            text=f"🚀 *Dev 세션 시작*\n프로젝트: `{project}`\n경로: `{session.project_path}`\n\n이제 이 채널에서 코딩 요청을 보내면 해당 프로젝트에서 작업할게.",
            thread_ts=thread_ts,
        )

    elif subcmd == "stop":
        session = _dev_sessions.pop(channel_id, None)
        if session:
            elapsed = int(time.time() - session.started_at)
            mins = elapsed // 60
            client.chat_postMessage(
                channel=channel_id,
                text=f"🛑 *Dev 세션 종료*\n프로젝트: `{session.project}`\n메시지: {session.message_count}개\n시간: {mins}분",
                thread_ts=thread_ts,
            )
        else:
            client.chat_postMessage(
                channel=channel_id,
                text="활성 dev 세션이 없어.",
                thread_ts=thread_ts,
            )

    elif subcmd == "status":
        session = _dev_sessions.get(channel_id)
        if session:
            elapsed = int(time.time() - session.started_at)
            mins = elapsed // 60
            client.chat_postMessage(
                channel=channel_id,
                text=(
                    f"📊 *Dev 세션 정보*\n"
                    f"프로젝트: `{session.project}`\n"
                    f"경로: `{session.project_path}`\n"
                    f"메시지: {session.message_count}개\n"
                    f"시간: {mins}분\n"
                    f"세션ID: `{session.session_id or 'N/A'}`"
                ),
                thread_ts=thread_ts,
            )
        else:
            client.chat_postMessage(
                channel=channel_id,
                text="활성 dev 세션이 없어. `!dev start <project>`로 시작해.",
                thread_ts=thread_ts,
            )

    else:
        client.chat_postMessage(
            channel=channel_id,
            text=(
                "*Dev 명령어*\n"
                "• `!dev start <project>` — dev 세션 시작\n"
                "• `!dev stop` — 세션 종료\n"
                "• `!dev status` — 세션 정보"
            ),
            thread_ts=thread_ts,
        )

def handle_dev_message(client, channel_id: str, text: str, thread_ts: str = None):
    """Process a message within an active dev session."""
    session = _dev_sessions.get(channel_id)
    if not session:
        return

    session.message_count += 1

    # Send interim message
    interim = generate_interim_message(channel_id, "casual")
    client.chat_postMessage(channel=channel_id, text=interim, thread_ts=thread_ts)
    memory.add_message(channel_id, DISPLAY_NAME, interim, is_bot=True)

    def _do_dev_work():
        prompt = (
            f"{CORE_IDENTITY}\n\n"
            f"---\n\n"
            f"프로젝트 경로: {session.project_path}\n\n"
            f"요청:\n{text}\n\n"
            f"위 프로젝트에서 요청된 작업을 수행해줘. "
            f"코드 수정이 필요하면 직접 파일을 읽고 수정해. "
            f"결과를 한국어로 설명해줘."
        )

        result = RUNTIME_CLIENT.run(
            LLMRunRequest(
                prompt=prompt,
                timeout_ms=180000,
                session_id=session.session_id,
                cwd=session.project_path,
            )
        )

        # Update session ID for multi-turn
        if result.session_id:
            session.session_id = result.session_id

        if result.success and result.output.strip():
            response = result.output.strip()
            logger.info(f"  ✅ Dev response ready ({len(response)} chars)")
            send_code_response(client, channel_id, response, thread_ts)
        else:
            # Show actual error instead of generic fallback
            if result.timed_out:
                error_msg = (
                    f":warning: *타임아웃 ({result.duration_ms // 1000}초)*\n"
                    f"작업이 너무 오래 걸렸어. 더 작은 단위로 나눠서 요청하거나 "
                    f"`!team run`으로 팀 파이프라인을 사용해봐."
                )
            elif result.raw and "error" in str(result.raw):
                error_detail = str(result.raw.get("error", result.raw))[:300]
                error_msg = f":x: *API 에러:*\n```\n{error_detail}\n```"
            else:
                error_msg = ":warning: 응답을 생성하지 못했어. 다시 시도해봐."

            client.chat_postMessage(channel=channel_id, text=error_msg, thread_ts=thread_ts)
            memory.add_message(channel_id, DISPLAY_NAME, error_msg, is_bot=True)

    threading.Thread(target=_do_dev_work, daemon=True).start()

# ─── Command Handlers ──────────────────────────────────────────────

SLASH_COMMAND = f"/{BOT_NAME}" if BOT_NAME != "seungbin" else "/secondme"

def handle_secondme_command(ack, command, client):
    _handle_slash_command(ack, command, client)

def handle_coder_command(ack, command, client):
    _handle_slash_command(ack, command, client)

def _handle_slash_command(ack, command, client):
    """Slash command to control the bot."""
    ack()
    text = command.get("text", "").strip()
    user_id = command["user_id"]

    if user_id != OWNER_USER_ID and user_id not in DEVELOPER_USER_IDS:
        client.chat_postEphemeral(
            channel=command["channel_id"],
            user=user_id,
            text=f"이 봇은 {DISPLAY_NAME} 관리자만 제어할 수 있습니다."
        )
        return

    parts = text.split()
    cmd = parts[0] if parts else "help"

    if cmd == "mode":
        if len(parts) < 2:
            client.chat_postEphemeral(
                channel=command["channel_id"],
                user=user_id,
                text=f"Current mode: *{state.mode}*\nUsage: `mode [draft|auto|on-demand]`"
            )
        elif parts[1] in ("draft", "auto", "on-demand"):
            state.mode = parts[1]
            client.chat_postEphemeral(
                channel=command["channel_id"],
                user=user_id,
                text=f"Mode changed to *{state.mode}*"
            )

    elif cmd == "tone":
        if len(parts) < 3:
            client.chat_postEphemeral(
                channel=command["channel_id"],
                user=user_id,
                text=f"Current channel tone: *{get_channel_tone(command['channel_id'])}*\nUsage: `tone [casual|formal]`"
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
            client.chat_postEphemeral(channel=channel_id, user=user_id, text="No pending draft.")

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
        dev_info = ""
        if PERSONA_TYPE == "coder":
            session = _dev_sessions.get(ch_id)
            dev_info = f"\nDev session: {'active (' + session.project + ')' if session else 'none'}"
        client.chat_postEphemeral(
            channel=ch_id,
            user=user_id,
            text=(
                f"*{DISPLAY_NAME} Bot Status*\n"
                f"Type: {PERSONA_TYPE}\n"
                f"Mode: {state.mode}\n"
                f"Pending drafts: {drafts}\n"
                f"Monitored channels: {channels}\n"
                f"This channel tone: {get_channel_tone(ch_id)}\n"
                f"Memory: {mem_count} messages\n"
                f"Prompt cache: {'active' if has_cache else 'none'}"
                f"{dev_info}"
            )
        )

    else:
        help_text = (
            f"*{DISPLAY_NAME} Bot Commands*\n"
            "• `mode [draft|auto|on-demand]` — switch mode\n"
            "• `tone [casual|formal]` — set channel tone\n"
            "• `send` — approve & send pending draft\n"
            "• `skip` — discard pending draft\n"
            "• `status` — show current state"
        )
        if PERSONA_TYPE == "coder":
            help_text += (
                "\n\n*Dev Commands (in chat)*\n"
                "• `!dev start <project>` — start dev session\n"
                "• `!dev stop` — end session\n"
                "• `!dev status` — session info"
            )
        client.chat_postEphemeral(
            channel=command["channel_id"],
            user=user_id,
            text=help_text,
        )


# ─── Reporter & Research Command Handlers ──────────────────────────

def _handle_reporter_command(client, channel_id: str, text: str, thread_ts: str = None):
    """Handle !digest commands for the reporter bot."""
    parts = text.split()
    subcmd = parts[1] if len(parts) > 1 else "help"

    if subcmd == "now":
        client.chat_postMessage(channel=channel_id, text=":newspaper: 뉴스 다이제스트를 수동 실행합니다...", thread_ts=thread_ts)

        def _run():
            if _pipeline:
                _pipeline.run_full_pipeline()
            else:
                client.chat_postMessage(channel=channel_id, text=":warning: 파이프라인이 초기화되지 않았습니다.", thread_ts=thread_ts)

        threading.Thread(target=_run, daemon=True).start()

    elif subcmd == "sources":
        reporter_config = BOT_CONFIG.get("reporter", {})
        queries = reporter_config.get("search_queries", [])
        sources = reporter_config.get("credible_sources", [])
        client.chat_postMessage(
            channel=channel_id,
            text=(
                f"*검색 키워드:* {', '.join(queries)}\n"
                f"*신뢰 출처:* {', '.join(sources)}"
            ),
            thread_ts=thread_ts,
        )

    else:
        client.chat_postMessage(
            channel=channel_id,
            text=(
                "*Reporter 명령어*\n"
                "• `!digest now` — 뉴스 다이제스트 수동 실행\n"
                "• `!digest sources` — 검색 키워드/출처 확인"
            ),
            thread_ts=thread_ts,
        )


def _handle_team_command(client, channel_id: str, text: str, thread_ts: str = None):
    """Handle !team commands for the coder team pipeline."""
    parts = text.split(maxsplit=2)
    subcmd = parts[1] if len(parts) > 1 else "help"

    if subcmd in ("run", "do"):
        request_text = parts[2] if len(parts) > 2 else ""
        if not request_text:
            client.chat_postMessage(
                channel=channel_id,
                text="사용법: `!team run <작업 설명>` — 예: `!team run OAuth 로그인 추가`",
                thread_ts=thread_ts,
            )
            return

        # Need an active dev session for project path
        session = _dev_sessions.get(channel_id)
        if not session:
            client.chat_postMessage(
                channel=channel_id,
                text=":warning: 먼저 `!dev start <project>`로 프로젝트를 설정해줘.",
                thread_ts=thread_ts,
            )
            return

        # Pre-flight checks
        project_path = session.project_path
        if not os.path.isdir(project_path):
            client.chat_postMessage(
                channel=channel_id,
                text=f":x: 프로젝트 경로가 없습니다: `{project_path}`\n"
                     f"`!dev start <프로젝트명>`으로 올바른 프로젝트를 설정하세요.",
                thread_ts=thread_ts,
            )
            return

        if not os.path.isdir(os.path.join(project_path, ".git")):
            client.chat_postMessage(
                channel=channel_id,
                text=f":x: `{session.project}`는 git 저장소가 아닙니다.\n"
                     f"`cd {project_path} && git init && git add -A && git commit -m 'init'`을 먼저 실행하세요.",
                thread_ts=thread_ts,
            )
            return

        # Check git has at least one commit (required for worktree)
        try:
            commit_check = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=project_path, capture_output=True, text=True,
            )
            if commit_check.returncode != 0:
                client.chat_postMessage(
                    channel=channel_id,
                    text=f":x: `{session.project}`에 커밋이 없습니다. 워크트리 생성에 최소 1개 커밋이 필요합니다.\n"
                         f"`cd {project_path} && git add -A && git commit -m 'init'`",
                    thread_ts=thread_ts,
                )
                return
        except Exception:
            pass

        # Check API health
        try:
            health = requests.get(f"{CLAUDE_API_URL}/health", timeout=3)
            if health.status_code != 200:
                client.chat_postMessage(
                    channel=channel_id,
                    text=f":x: claude-code-api가 응답하지 않습니다 (status: {health.status_code}).\n"
                         f"API 서버를 확인하세요: `{CLAUDE_API_URL}`",
                    thread_ts=thread_ts,
                )
                return
        except Exception as e:
            client.chat_postMessage(
                channel=channel_id,
                text=f":x: claude-code-api에 연결할 수 없습니다: `{CLAUDE_API_URL}`\n"
                     f"에러: {str(e)[:200]}",
                thread_ts=thread_ts,
            )
            return

        client.chat_postMessage(
            channel=channel_id,
            text=f":rocket: *팀 작업 시작*\n프로젝트: `{session.project}`\n요청: {request_text}",
            thread_ts=thread_ts,
        )

        def _run():
            if _pipeline:
                try:
                    result = _pipeline.run_task(session.project_path, request_text, channel_id, thread_ts)
                    if not result.get("success"):
                        error = result.get("error", "unknown")
                        client.chat_postMessage(
                            channel=channel_id,
                            text=f":x: 작업 실패: {error}",
                            thread_ts=thread_ts,
                        )
                except Exception as e:
                    logger.error(f"Team pipeline crashed: {e}", exc_info=True)
                    client.chat_postMessage(
                        channel=channel_id,
                        text=f":x: *파이프라인 크래시:*\n```\n{str(e)[:500]}\n```",
                        thread_ts=thread_ts,
                    )

        threading.Thread(target=_run, daemon=True).start()

    elif subcmd == "plan":
        request_text = parts[2] if len(parts) > 2 else ""
        if not request_text:
            client.chat_postMessage(
                channel=channel_id,
                text="사용법: `!team plan <작업 설명>` — 실행 없이 계획만 확인",
                thread_ts=thread_ts,
            )
            return

        session = _dev_sessions.get(channel_id)
        if not session:
            client.chat_postMessage(
                channel=channel_id,
                text=":warning: 먼저 `!dev start <project>`로 프로젝트를 설정해줘.",
                thread_ts=thread_ts,
            )
            return

        def _plan():
            if _pipeline:
                plan = _pipeline.run_plan_only(session.project_path, request_text, channel_id, thread_ts)
                if plan:
                    plan_json = json.dumps(plan, ensure_ascii=False, indent=2)
                    client.chat_postMessage(
                        channel=channel_id,
                        text=f":clipboard: *계획 미리보기*\n```\n{plan_json[:3000]}\n```",
                        thread_ts=thread_ts,
                    )

        threading.Thread(target=_plan, daemon=True).start()

    elif subcmd == "status":
        session = _dev_sessions.get(channel_id)
        if not session:
            client.chat_postMessage(channel=channel_id, text="활성 dev 세션이 없어.", thread_ts=thread_ts)
            return
        # Check for active worktrees
        import glob
        wt_dir = f"{session.project_path}/.worktrees"
        worktrees = glob.glob(f"{wt_dir}/TASK-*") if os.path.isdir(wt_dir) else []
        if worktrees:
            wt_list = "\n".join(f"  • `{os.path.basename(w)}`" for w in worktrees)
            client.chat_postMessage(
                channel=channel_id,
                text=f":hammer_and_wrench: *활성 워크트리*\n{wt_list}",
                thread_ts=thread_ts,
            )
        else:
            client.chat_postMessage(
                channel=channel_id, text="활성 워크트리 없음.", thread_ts=thread_ts,
            )

    else:
        client.chat_postMessage(
            channel=channel_id,
            text=(
                "*Team 명령어*\n"
                "• `!team run <설명>` — 전체 파이프라인 실행 (plan → implement → review → merge)\n"
                "• `!team plan <설명>` — 계획만 미리보기 (실행 안 함)\n"
                "• `!team status` — 활성 워크트리 확인"
            ),
            thread_ts=thread_ts,
        )


def _handle_research_command(client, channel_id: str, text: str, thread_ts: str = None, event: dict = None):
    """Handle !research commands for the research pipeline bot."""
    parts = text.split()
    subcmd = parts[1] if len(parts) > 1 else "help"

    if not _pipeline:
        client.chat_postMessage(channel=channel_id, text=":warning: 파이프라인이 초기화되지 않았습니다.", thread_ts=thread_ts)
        return

    if subcmd == "discover" or subcmd == "run":
        client.chat_postMessage(
            channel=channel_id,
            text=":microscope: 논문 스캔을 시작합니다... 완료 후 `!research select` 로 아이디어를 선택하세요.",
            thread_ts=thread_ts,
        )

        def _run():
            _pipeline.run_full_pipeline()

        threading.Thread(target=_run, daemon=True).start()

    elif subcmd == "select":
        # !research select 1,3,5 추가 힌트 텍스트
        if len(parts) < 3:
            client.chat_postMessage(
                channel=channel_id,
                text=":warning: 사용법: `!research select 1,3,5 \"추가 조사 힌트\"`",
                thread_ts=thread_ts,
            )
            return

        try:
            indices = [int(x.strip()) for x in parts[2].split(",")]
        except ValueError:
            client.chat_postMessage(
                channel=channel_id,
                text=":warning: 번호를 콤마로 구분해주세요. 예: `!research select 1,3,5`",
                thread_ts=thread_ts,
            )
            return

        # Everything after indices is extra hints
        extra_hints = " ".join(parts[3:]).strip().strip('"').strip("'") if len(parts) > 3 else ""

        client.chat_postMessage(
            channel=channel_id,
            text=f":rocket: 아이디어 {parts[2]} 선택 — 딥다이브 → 리포트 → 리뷰를 시작합니다...",
            thread_ts=thread_ts,
        )

        def _run():
            _pipeline.select_ideas(indices, extra_hints)

        threading.Thread(target=_run, daemon=True).start()

    elif subcmd == "dive":
        # !research dive https://arxiv.org/... "힌트"
        # or !research dive (with PDF attachment) "힌트"
        url = ""
        pdf_text = ""
        hint_parts = []

        # Check for URL in parts
        if len(parts) > 2 and parts[2].startswith("http"):
            url = parts[2]
            hint_parts = parts[3:]
        else:
            hint_parts = parts[2:]

        # Check for file attachment
        files = (event or {}).get("files", [])
        if files:
            for f in files:
                if f.get("mimetype", "").startswith("application/pdf") or f.get("name", "").endswith(".pdf"):
                    try:
                        import requests
                        file_url = f.get("url_private_download", f.get("url_private", ""))
                        if file_url:
                            token = os.environ.get("SLACK_BOT_TOKEN", "")
                            resp = requests.get(file_url, headers={"Authorization": f"Bearer {token}"}, timeout=30)
                            pdf_text = extract_pdf_text(resp.content, max_chars=5000)
                    except Exception as e:
                        logger.error(f"PDF download/parse failed: {e}")
                    break

        if not url and not pdf_text:
            client.chat_postMessage(
                channel=channel_id,
                text=":warning: 사용법: `!research dive <arXiv URL> \"조사 힌트\"` 또는 PDF 파일을 첨부하세요.",
                thread_ts=thread_ts,
            )
            return

        user_hint = " ".join(hint_parts).strip().strip('"').strip("'")

        source = url if url else "PDF 첨부"
        client.chat_postMessage(
            channel=channel_id,
            text=f":mag: 논문 딥다이브를 시작합니다: {source}",
            thread_ts=thread_ts,
        )

        def _run():
            _pipeline.dive_paper(url=url, pdf_text=pdf_text, user_hint=user_hint)

        threading.Thread(target=_run, daemon=True).start()

    elif subcmd == "topic":
        # !research topic "tiered memory" "Bertha 아키텍처 적용 가능성"
        if len(parts) < 3:
            client.chat_postMessage(
                channel=channel_id,
                text=':warning: 사용법: `!research topic "주제" "조사 힌트"`',
                thread_ts=thread_ts,
            )
            return

        # Parse topic and hint from quoted or unquoted args
        rest = " ".join(parts[2:])
        # Try to split quoted strings
        import shlex
        try:
            args = shlex.split(rest)
        except ValueError:
            args = rest.split('"')
            args = [a.strip() for a in args if a.strip()]

        topic = args[0] if args else rest
        user_hint = args[1] if len(args) > 1 else ""

        client.chat_postMessage(
            channel=channel_id,
            text=f":mag: 주제 조사를 시작합니다: *{topic}*",
            thread_ts=thread_ts,
        )

        def _run():
            _pipeline.research_topic(topic, user_hint)

        threading.Thread(target=_run, daemon=True).start()

    elif subcmd == "resume":
        try:
            count = int(parts[2]) if len(parts) > 2 else 5
        except ValueError:
            client.chat_postMessage(
                channel=channel_id,
                text=":warning: 사용법: `!research resume 3` 처럼 숫자를 입력해주세요.",
                thread_ts=thread_ts,
            )
            return
        client.chat_postMessage(
            channel=channel_id,
            text=f":rocket: 대기 중인 아이디어 {count}개로 딥다이브 → 리포트 → 리뷰 시작...",
            thread_ts=thread_ts,
        )

        def _run():
            _pipeline.run_from_existing(count)

        threading.Thread(target=_run, daemon=True).start()

    elif subcmd == "status":
        summary = _pipeline.get_status_summary()
        client.chat_postMessage(channel=channel_id, text=summary, thread_ts=thread_ts)

    elif subcmd == "list":
        reports = _pipeline.store.list_reports()
        if not reports:
            client.chat_postMessage(channel=channel_id, text="저장된 리포트가 없습니다.", thread_ts=thread_ts)
        else:
            lines = []
            for r in reports:
                lines.append(f"• `{r.get('report_id', '')}` — {r.get('status', '?')} (리뷰 {r.get('review_count', 0)}회)")
            client.chat_postMessage(channel=channel_id, text="*리포트 목록*\n" + "\n".join(lines), thread_ts=thread_ts)

    elif subcmd == "chat":
        # !research chat <report_id> — start session
        # !research chat end — end session (in thread)
        # !research chat — list chattable reports
        if len(parts) > 2 and parts[2] == "end":
            # End session in current thread
            if thread_ts and _pipeline.has_chat_session(thread_ts):
                msg_count = _pipeline.end_chat_session(thread_ts)
                client.chat_postMessage(
                    channel=channel_id,
                    text=f":wave: 리서치 대화를 종료합니다. (메시지 {msg_count}건)",
                    thread_ts=thread_ts,
                )
            else:
                client.chat_postMessage(
                    channel=channel_id,
                    text=":warning: 이 스레드에 활성 대화 세션이 없습니다.",
                    thread_ts=thread_ts,
                )
            return

        if len(parts) < 3:
            # List chattable reports
            reports = _pipeline.list_chattable_reports()
            if not reports:
                client.chat_postMessage(
                    channel=channel_id,
                    text="대화 가능한 리포트가 없습니다. 리포트 작성이 완료된 후 사용하세요.",
                    thread_ts=thread_ts,
                )
            else:
                lines = [f"*대화 가능한 리포트 ({len(reports)}건)*\n"]
                for r in reports:
                    rid = r.get("report_id", "")
                    status = r.get("status", "?")
                    title = r.get("metadata", {}).get("title", rid)
                    lines.append(f"• `{rid}` — {title} ({status})")
                lines.append(f"\n:point_right: `!research chat <report_id>` 로 대화를 시작하세요.")
                client.chat_postMessage(
                    channel=channel_id,
                    text="\n".join(lines),
                    thread_ts=thread_ts,
                )
            return

        report_id = parts[2]
        # Verify report exists
        report = _pipeline.store.get_report(report_id)
        if not report:
            client.chat_postMessage(
                channel=channel_id,
                text=f":warning: 리포트 `{report_id}`를 찾을 수 없습니다. `!research list`로 확인하세요.",
                thread_ts=thread_ts,
            )
            return

        # Post initial message to create a thread
        title = report.get("metadata", {}).get("title", report_id)
        result = client.chat_postMessage(
            channel=channel_id,
            text=f":microscope: *{title}* 리포트 대화를 시작합니다...\n잠시만 기다려 주세요.",
        )
        chat_thread_ts = result["ts"]

        def _start_chat():
            response = _pipeline.start_chat_session(report_id, channel_id, chat_thread_ts)
            if response:
                client.chat_postMessage(
                    channel=channel_id,
                    text=response,
                    thread_ts=chat_thread_ts,
                )
            else:
                client.chat_postMessage(
                    channel=channel_id,
                    text=":warning: 대화 세션 시작에 실패했습니다. 리포트 아티팩트가 충분한지 확인하세요.",
                    thread_ts=chat_thread_ts,
                )

        threading.Thread(target=_start_chat, daemon=True).start()

    elif subcmd == "sync-discourse":
        client.chat_postMessage(
            channel=channel_id,
            text=":books: Discourse 지식 동기화를 시작합니다...",
            thread_ts=thread_ts,
        )

        def _run():
            _pipeline.sync_discourse()

        threading.Thread(target=_run, daemon=True).start()

    else:
        client.chat_postMessage(
            channel=channel_id,
            text=(
                "*Research 명령어*\n"
                "• `!research discover` — 논문 스캔 + 아이디어 발굴 (선별 대기)\n"
                "• `!research select 1,3,5 \"힌트\"` — 아이디어 선별 → 딥다이브 → 리포트 → 리뷰\n"
                "• `!research dive <URL> \"힌트\"` — 지정 논문 딥다이브\n"
                "• `!research dive` (PDF 첨부) — PDF 논문 딥다이브\n"
                "• `!research topic \"주제\" \"힌트\"` — 주제 기반 조사 → 딥다이브 → 리포트\n"
                "• `!research resume [N]` — 대기 중인 아이디어 N개로 재개\n"
                "• `!research status` — 현황 요약\n"
                "• `!research list` — 리포트 목록\n"
                "• `!research chat <report_id>` — 리포트에 대해 연구원과 대화\n"
                "• `!research chat` — 대화 가능한 리포트 목록\n"
                "• `!research chat end` — (스레드 내) 대화 종료"
            ),
            thread_ts=thread_ts,
        )


# ─── Message Handler ───────────────────────────────────────────────

def handle_message(event, client, logger):
    logger.info(f"📨 Message event received: channel={event.get('channel')} user={event.get('user')} text={event.get('text', '')[:50]}")

    if event.get("bot_id"):
        logger.info("  → Skipped (bot message)")
        return
    if event.get("subtype"):
        logger.info("  → Skipped (subtype)")
        return

    user_id = event.get("user", "")
    text = event.get("text", "")
    channel_id = event["channel"]

    try:
        user_info = client.users_info(user=user_id)
        sender_name = user_info["user"]["profile"].get("display_name") or user_info["user"]["real_name"]
    except Exception:
        sender_name = user_id

    memory.add_message(channel_id, sender_name, text)

    is_test = False
    thread_ts = event.get("thread_ts") or event.get("ts")
    reply_ts = thread_ts if event.get("thread_ts") else None

    # Owner/developer commands
    if user_id == OWNER_USER_ID or user_id in DEVELOPER_USER_IDS:
        # Dev commands (coder only)
        if PERSONA_TYPE == "coder" and text.startswith("!dev"):
            handle_dev_command(client, channel_id, text, reply_ts)
            return

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
            dev_info = ""
            if PERSONA_TYPE == "coder":
                session = _dev_sessions.get(channel_id)
                dev_info = f" | Dev: {'active (' + session.project + ')' if session else 'none'}"
            client.chat_postMessage(
                channel=channel_id,
                text=f"[{DISPLAY_NAME}] Mode: {state.mode} | Model: {cur_short} ({cur_model}) | Tone: {get_channel_tone(channel_id)} | Drafts: {len(state.pending_drafts)} | Memory: {mem_count}msgs{dev_info}"
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
        # Reporter commands
        if PERSONA_TYPE == "reporter" and text.startswith("!digest"):
            _handle_reporter_command(client, channel_id, text, reply_ts)
            return

        # Coder team commands
        if PERSONA_TYPE == "coder" and text.startswith("!team"):
            _handle_team_command(client, channel_id, text, reply_ts)
            return

        # Research pipeline commands
        if PERSONA_TYPE == "research_pipeline" and text.startswith("!research"):
            _handle_research_command(client, channel_id, text, reply_ts, event=event)
            return

        if text.startswith("!test"):
            is_test = True
            logger.info("  → Owner test mode (will respond directly)")
        else:
            logger.info("  → Owner/developer message (will respond normally)")

    logger.info(f"  → Mode: {state.mode}, Channel tone: {get_channel_tone(channel_id)}, Memory: {len(memory.history.get(channel_id, []))}msgs")

    # Research chat: route thread messages to active chat session
    if (
        PERSONA_TYPE == "research_pipeline"
        and _pipeline
        and not text.startswith("!")
        and event.get("thread_ts")
        and _pipeline.has_chat_session(event["thread_ts"])
    ):
        def _continue_chat():
            response = _pipeline.continue_chat(event["thread_ts"], text)
            if response:
                client.chat_postMessage(
                    channel=channel_id,
                    text=response,
                    thread_ts=event["thread_ts"],
                )
            else:
                client.chat_postMessage(
                    channel=channel_id,
                    text=":warning: 응답 생성에 실패했습니다. 다시 시도해 주세요.",
                    thread_ts=event["thread_ts"],
                )

        threading.Thread(target=_continue_chat, daemon=True).start()
        return

    # Coder bot: if dev session is active, route to dev handler
    if PERSONA_TYPE == "coder" and channel_id in _dev_sessions and not text.startswith("!"):
        handle_dev_message(client, channel_id, text, reply_ts)
        return

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

    logger.info(f"  ⏳ Generating response for {channel_id} (type={question_type})...")

    if question_type == "research":
        if is_test or state.mode == "auto":
            interim = generate_interim_message(channel_id, tone)
            logger.info(f"  📤 Sending interim: {interim}")
            client.chat_postMessage(channel=channel_id, text=interim, thread_ts=reply_ts)
            memory.add_message(channel_id, DISPLAY_NAME, interim, is_bot=True)

            def _do_research():
                response = generate_research_response(channel_id, tone)
                if response:
                    logger.info(f"  ✅ Research response ready ({len(response)} chars)")
                    if PERSONA_TYPE == "coder":
                        send_code_response(client, channel_id, response, reply_ts)
                    else:
                        send_response(client, channel_id, response, reply_ts)
                else:
                    logger.info(f"  → No research response generated")
                    fallback = generate_fallback_message(channel_id, tone)
                    client.chat_postMessage(channel=channel_id, text=fallback, thread_ts=reply_ts)
                    memory.add_message(channel_id, DISPLAY_NAME, fallback, is_bot=True)

            threading.Thread(target=_do_research, daemon=True).start()
        elif state.mode == "draft":
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
        if PERSONA_TYPE == "coder":
            send_code_response(client, channel_id, response, reply_ts)
        else:
            send_response(client, channel_id, response, reply_ts)
    elif state.mode == "draft":
        _handle_draft(client, channel_id, sender_name, text, response, reply_ts)


# ─── App Mention Handler ───────────────────────────────────────────

def handle_mention(event, client):
    channel_id = event["channel"]
    tone = get_channel_tone(channel_id)

    user_id = event.get("user", "")
    text = event.get("text", "")
    try:
        user_info = client.users_info(user=user_id)
        sender_name = user_info["user"]["profile"].get("display_name") or user_info["user"]["real_name"]
    except Exception:
        sender_name = user_id
    memory.add_message(channel_id, sender_name, text)

    sync_from_slack(client, channel_id)

    question_type = classify_question(text)
    thread_ts = event.get("thread_ts") or event.get("ts")

    logger.info(f"  ⏳ Generating @mention response for {channel_id} (type={question_type})...")

    # Coder bot: if dev session active, route to dev handler
    if PERSONA_TYPE == "coder" and channel_id in _dev_sessions:
        handle_dev_message(client, channel_id, text, thread_ts)
        return

    if question_type == "research" and state.mode != "draft":
        interim = generate_interim_message(channel_id, tone)
        client.chat_postMessage(channel=channel_id, text=interim, thread_ts=thread_ts)
        memory.add_message(channel_id, DISPLAY_NAME, interim, is_bot=True)

        def _do_research():
            response = generate_research_response(channel_id, tone)
            if response:
                logger.info(f"  ✅ @mention research response ready ({len(response)} chars)")
                if PERSONA_TYPE == "coder":
                    send_code_response(client, channel_id, response, thread_ts)
                else:
                    send_response(client, channel_id, response, thread_ts)
            else:
                logger.info(f"  → No research response generated")
                fallback = generate_fallback_message(channel_id, tone)
                client.chat_postMessage(channel=channel_id, text=fallback, thread_ts=thread_ts)
                memory.add_message(channel_id, DISPLAY_NAME, fallback, is_bot=True)

        threading.Thread(target=_do_research, daemon=True).start()
        return

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
        if PERSONA_TYPE == "coder":
            send_code_response(client, channel_id, response, thread_ts)
        else:
            send_response(client, channel_id, response, thread_ts)


# ─── Main ───────────────────────────────────────────────────────────

def create_app() -> App:
    slack_app = App(token=os.environ["SLACK_BOT_TOKEN"])
    slack_app.command("/secondme")(handle_secondme_command)
    slack_app.command("/coder")(handle_coder_command)
    slack_app.event("message")(handle_message)
    slack_app.event("app_mention")(handle_mention)
    return slack_app

if __name__ == "__main__":
    app = create_app()
    logger.info(f"Bot '{DISPLAY_NAME}' ({PERSONA_TYPE}) starting in '{state.mode}' mode...")
    logger.info(f"Bot dir: {BOT_DIR}")
    logger.info(f"Owner: {OWNER_USER_ID}")
    logger.info(f"API: {CLAUDE_API_URL}")
    logger.info(f"Core identity: {len(CORE_IDENTITY)} chars")
    if PERSONA_TYPE == "persona":
        logger.info(f"RAG persona retrieval + conversation memory enabled")
    elif PERSONA_TYPE == "coder":
        logger.info(f"Coder mode: dev sessions + team pipeline enabled")
        from pipelines.coder_pipeline import CoderPipeline
        _pipeline = CoderPipeline(BOT_CONFIG, app.client, CLAUDE_API_URL, CLAUDE_API_KEY, BOT_DIR)
    elif PERSONA_TYPE == "reporter":
        logger.info(f"Reporter mode: initializing pipeline + scheduler")
        from pipelines.reporter_pipeline import ReporterPipeline
        _pipeline = ReporterPipeline(BOT_CONFIG, app.client, CLAUDE_API_URL, CLAUDE_API_KEY, BOT_DIR)

        schedule_config = BOT_CONFIG.get("schedule", {})
        digest_time = schedule_config.get("digest_time", "02:45")
        digest_days = schedule_config.get("digest_days", [])
        tz = schedule_config.get("timezone", "Asia/Seoul")

        _bot_scheduler = BotScheduler()
        if digest_days:
            _bot_scheduler.add_weekdays(digest_days, digest_time, _pipeline.run_full_pipeline, tz=tz)
        else:
            _bot_scheduler.add_daily(digest_time, _pipeline.run_full_pipeline, tz=tz)
        _bot_scheduler.start()
        logger.info(
            f"Reporter scheduled: time={digest_time} days={digest_days or 'daily'} (tz={tz})"
        )
    elif PERSONA_TYPE == "research_pipeline":
        logger.info(f"Research pipeline mode: initializing pipeline + scheduler")
        from pipelines.research_pipeline import ResearchPipeline
        _pipeline = ResearchPipeline(BOT_CONFIG, app.client, CLAUDE_API_URL, CLAUDE_API_KEY, BOT_DIR)

        schedule_config = BOT_CONFIG.get("schedule", {})
        discovery_time = schedule_config.get("discovery_scan", "22:00")
        discovery_days = schedule_config.get("discovery_days", [])
        auto_report_time = schedule_config.get("auto_report", "")
        auto_report_days = schedule_config.get("auto_report_days", [])
        tz = schedule_config.get("timezone", "Asia/Seoul")

        _bot_scheduler = BotScheduler()
        if discovery_days:
            _bot_scheduler.add_weekdays(discovery_days, discovery_time, _pipeline.run_discovery, tz=tz)
        else:
            _bot_scheduler.add_daily(discovery_time, _pipeline.run_discovery, tz=tz)
        if auto_report_time:
            if auto_report_days:
                _bot_scheduler.add_weekdays(auto_report_days, auto_report_time, _pipeline.auto_report_top_idea, tz=tz)
            else:
                _bot_scheduler.add_daily(auto_report_time, _pipeline.auto_report_top_idea, tz=tz)
        _bot_scheduler.start()
        logger.info(
            f"Research pipeline scheduled: discovery={discovery_time} days={discovery_days or 'daily'}, "
            f"auto_report={auto_report_time or 'off'} days={auto_report_days or 'daily'} (tz={tz})"
        )

    logger.info(f"Memory: max {memory.max_messages} msgs/channel, session timeout {memory.session_timeout}s")

    handler = SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"])
    handler.start()
