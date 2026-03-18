"""Shared conversation/session orchestration skill."""

from __future__ import annotations

import re
import time
from collections import defaultdict
from dataclasses import dataclass
from typing import Callable

from skills.types import LLMRunRequest
from tools.claude_runtime import ClaudeRuntimeClient

RESEARCH_KEYWORDS = re.compile(
    r"(시세|시가|종가|주가|얼마|몇\s?달러|몇\s?원|환율|금리|금값"
    r"|나스닥|코스피|코스닥|다우|S&P|s&p|비트코인|이더리움|BTC|ETH|솔라나"
    r"|뉴스|소식|최근|최신|요즘|오늘|어제|이번\s?주|실적|발표"
    r"|날씨|기온|비\s?오|미세먼지"
    r"|찾아봐|검색해|알아봐|조사해|확인해|서치|search"
    r"|출시|업데이트|패치|언제\s?나|런칭|발매"
    r"|경기|스코어|순위|몇\s?대\s?몇|이겼|졌)",
    re.IGNORECASE,
)


@dataclass(slots=True)
class ConversationMessage:
    role: str
    sender: str
    text: str
    ts: float


class ChannelMemory:
    """Per-channel memory buffer with prompt cache."""

    def __init__(self, max_messages: int = 30, session_timeout: int = 1800):
        self.history: dict[str, list[ConversationMessage]] = defaultdict(list)
        self.cached_prompts: dict[str, dict[str, object]] = {}
        self.max_messages = max_messages
        self.session_timeout = session_timeout

    def add_message(
        self,
        channel_id: str,
        sender: str,
        text: str,
        role: str = "user",
    ) -> None:
        self.history[channel_id].append(
            ConversationMessage(role=role, sender=sender, text=text, ts=time.time())
        )
        if len(self.history[channel_id]) > self.max_messages:
            self.history[channel_id] = self.history[channel_id][-self.max_messages :]

    def get_conversation(self, channel_id: str) -> str:
        cutoff = time.time() - self.session_timeout
        recent = [m for m in self.history.get(channel_id, []) if m.ts > cutoff]
        return "\n".join(f"[{m.sender}] {m.text}" for m in recent)

    def get_cached_prompt(self, channel_id: str, ttl_sec: int = 300) -> str | None:
        cached = self.cached_prompts.get(channel_id)
        if cached and (time.time() - float(cached["ts"])) < ttl_sec:
            return str(cached["prompt"])
        return None

    def cache_prompt(self, channel_id: str, prompt: str) -> None:
        self.cached_prompts[channel_id] = {"prompt": prompt, "ts": time.time()}

    def clear_channel(self, channel_id: str) -> None:
        self.history.pop(channel_id, None)
        self.cached_prompts.pop(channel_id, None)


class ConversationSessionOrchestrator:
    """Reusable conversation orchestration skill."""

    def __init__(
        self,
        runtime: ClaudeRuntimeClient,
        display_name: str,
        memory: ChannelMemory | None = None,
    ):
        self.runtime = runtime
        self.display_name = display_name
        self.memory = memory or ChannelMemory()

    @staticmethod
    def classify_question(text: str) -> str:
        return "research" if RESEARCH_KEYWORDS.search(text) else "chat"

    @staticmethod
    def split_response(text: str) -> list[str]:
        text = text.replace("\\n", "\n")
        return [line.strip() for line in text.split("\n") if line.strip()]

    def build_system_prompt(
        self,
        channel_id: str,
        channel_tone: str,
        builder: Callable[[str, str], str],
    ) -> str:
        cached = self.memory.get_cached_prompt(channel_id)
        if cached:
            return cached
        conversation = self.memory.get_conversation(channel_id)
        prompt = builder(conversation, channel_tone)
        self.memory.cache_prompt(channel_id, prompt)
        return prompt

    def generate_response(
        self,
        *,
        channel_id: str,
        system_prompt: str,
        question_type: str,
        timeout_ms: int,
        research_instruction: str,
        chat_instruction: str,
        session_id: str | None = None,
    ) -> str:
        conversation = self.memory.get_conversation(channel_id)
        if not conversation:
            return ""

        instruction = research_instruction if question_type == "research" else chat_instruction
        prompt = f"{system_prompt}\n\n---\n\n{conversation}\n\n{instruction}"
        result = self.runtime.run(
            LLMRunRequest(prompt=prompt, timeout_ms=timeout_ms, session_id=session_id)
        )
        if not result.success:
            return ""
        return result.output if result.output != "[SKIP]" else ""

