"""HAExpertPipeline — business brief generation + chat sessions.

Two-agent pipeline:
  Investigator: WebSearch + RAG → investigation.json (raw findings + sources)
  Briefer: investigation.json + extra_context → brief.md (1-pager)

Shares Discourse/Confluence knowledge instances with ResearchPipeline.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from brief_store import BriefStore
from pipelines.base import BasePipeline
from skills.types import LLMRunRequest

logger = logging.getLogger(__name__)

MAX_CHAT_SESSIONS = 5
CHAT_SESSION_IDLE_SECONDS = 60 * 60  # 1 hour


@dataclass
class HAChatSession:
    """Tracks an active HA-Expert chat session bound to a Slack thread."""
    session_id: str
    brief_id: str
    channel: str
    thread_ts: str
    cwd: str = ""
    created_at: float = field(default_factory=time.time)
    last_activity: float = field(default_factory=time.time)
    message_count: int = 0


class HAExpertPipeline(BasePipeline):
    """Business brief generation + chat sessions for HA-Expert persona."""

    def __init__(
        self,
        bot_config: dict,
        slack_client: Any,
        api_url: str,
        api_key: str,
        bot_dir: Path,
        discourse_knowledge: Any = None,
        confluence_knowledge: Any = None,
        url_resolver: Callable[[], str] | None = None,
    ):
        super().__init__(bot_config, slack_client, api_url, api_key, bot_dir, url_resolver=url_resolver)
        self.ha_config = bot_config.get("ha_expert", {})
        self.publish_channel = self.ha_config.get("publish_channel", "")
        self.status_channel = (
            self.ha_config.get("status_channel")
            or bot_config.get("research", {}).get("status_channel", "")
        )
        self.discourse_knowledge = discourse_knowledge
        self.confluence_knowledge = confluence_knowledge

        self.store = BriefStore(bot_dir / "briefs")

        base_ctx_rel = self.ha_config.get("base_context_file", "context/ha_expert_base.md")
        base_ctx_path = bot_dir / base_ctx_rel
        if base_ctx_path.exists():
            self._base_context = base_ctx_path.read_text(encoding="utf-8")
        else:
            logger.warning("HA-Expert base context not found at %s", base_ctx_path)
            self._base_context = ""

        self._chat_sessions: dict[str, HAChatSession] = {}
        self._run_lock = threading.Lock()

    # ─── Brief Generation ────────────────────────────────────────

    def run_brief(self, target: str, extra_context: str, channel: str, source_ts: str, requester: str = "") -> str:
        """Run Investigator → Briefer. Returns brief_id. Posts progress + result to Slack."""
        brief_id = self.store.create_brief(
            target=target,
            extra_context=extra_context,
            requester=requester,
            channel=channel,
            source_ts=source_ts,
        )

        # Phase 1: Investigator
        self.post_to_slack(
            channel=channel,
            text=f":mag: Investigator 조사 중... (대상: *{target}*, 예상 3-7분)",
            agent_name="ha_expert",
        )
        investigation_text = self._run_investigator(target, extra_context)
        if not investigation_text:
            self.store.update_state(brief_id, "failed", {"error": "investigator returned empty"})
            self.post_to_slack(
                channel=channel,
                text=":x: 조사 단계에서 결과가 비었습니다. 다시 시도해 주세요.",
                agent_name="ha_expert",
            )
            return brief_id
        self.store.save_artifact(brief_id, "investigation.json", investigation_text)
        self.store.update_state(brief_id, "drafting")

        # Phase 2: Briefer
        self.post_to_slack(
            channel=channel,
            text=":memo: Briefer 1-pager 작성 중...",
            agent_name="ha_expert",
        )
        brief_md = self._run_briefer(target, extra_context, investigation_text)
        if not brief_md:
            self.store.update_state(brief_id, "failed", {"error": "briefer returned empty"})
            self.post_to_slack(
                channel=channel,
                text=":x: 1-pager 작성에 실패했습니다.",
                agent_name="ha_expert",
            )
            return brief_id
        self.store.save_artifact(brief_id, "brief.md", brief_md)
        self.store.update_state(brief_id, "drafted")

        # Post final brief
        seq = self.store.seq_of(brief_id)
        self.post_to_slack(
            channel=channel,
            text=brief_md,
            agent_name="ha_expert",
        )
        self.post_to_slack(
            channel=channel,
            text=(
                f":speech_balloon: 후속 대화: `!ha chat {seq}` "
                f"(전체 ID: `{brief_id}`)"
            ),
            agent_name="ha_expert",
        )
        return brief_id

    def _run_investigator(self, target: str, extra_context: str) -> str:
        from prompts.ha_expert_investigator import HA_EXPERT_INVESTIGATOR_PROMPT

        internal_context = self._gather_internal_context(target)
        prompt = (
            HA_EXPERT_INVESTIGATOR_PROMPT
            .replace("{base_context}", self._base_context)
            .replace("{internal_context}", internal_context)
            .replace("{target}", target)
            .replace("{extra_context}", extra_context or "(없음)")
        )
        return self.call_llm(
            prompt,
            heartbeat_channel=self.status_channel,
            heartbeat_agent="ha_expert",
            heartbeat_label="HA-Expert 조사",
        )

    def _run_briefer(self, target: str, extra_context: str, investigation_json: str) -> str:
        from prompts.ha_expert_briefer import HA_EXPERT_BRIEFER_PROMPT

        prompt = (
            HA_EXPERT_BRIEFER_PROMPT
            .replace("{base_context}", self._base_context)
            .replace("{target}", target)
            .replace("{extra_context}", extra_context or "(없음)")
            .replace("{investigation_json}", investigation_json)
        )
        return self.call_llm(
            prompt,
            heartbeat_channel=self.status_channel,
            heartbeat_agent="ha_expert",
            heartbeat_label="HA-Expert 작성",
        )

    def _gather_internal_context(self, target: str) -> str:
        """Pull matching snippets from Discourse + Confluence knowledge if available."""
        keywords = [w for w in target.split() if w]
        if not keywords:
            return "(자사 내부 문서 매칭 없음)"

        parts = []
        for label, kb in (("Discourse", self.discourse_knowledge), ("Confluence", self.confluence_knowledge)):
            if kb is None:
                continue
            try:
                snippets = kb.build_context(keywords)
                if snippets:
                    parts.append(f"=== Internal: {label} ===\n{snippets}")
            except Exception as e:
                logger.warning("Internal context fetch (%s) failed: %s", label, e)
        return "\n\n".join(parts) if parts else "(자사 내부 문서 매칭 없음)"

    def list_briefs(self, limit: int = 10) -> list[dict]:
        return self.store.list_briefs(limit=limit)

    # ─── Chat Session Lifecycle ──────────────────────────────────

    def _cleanup_expired_sessions(self):
        now = time.time()
        expired = [
            ts for ts, s in self._chat_sessions.items()
            if now - s.last_activity > CHAT_SESSION_IDLE_SECONDS
        ]
        for ts in expired:
            self.end_chat_session(
                ts,
                notify_reason=":hourglass: 1시간 이상 대화가 없어 세션을 자동 종료했습니다. `!ha chat <번호>`로 다시 시작할 수 있습니다.",
            )

    def start_chat_session(self, brief_id: str, channel: str, thread_ts: str) -> str | None:
        """Start a chat session about an existing brief. Returns the first response."""
        self._cleanup_expired_sessions()

        if len(self._chat_sessions) >= MAX_CHAT_SESSIONS:
            oldest_ts = min(self._chat_sessions, key=lambda k: self._chat_sessions[k].last_activity)
            self.end_chat_session(
                oldest_ts,
                notify_reason=(
                    f":wave: 동시 대화 세션 한도({MAX_CHAT_SESSIONS}개)를 초과해 가장 오래 비활성인 세션을 자동 종료했습니다."
                ),
            )

        state = self.store.get_brief(brief_id)
        if not state:
            return None
        actual_id = state["brief_id"]
        target = state.get("target", "")

        brief_md = self.store.load_artifact(actual_id, "brief.md") or "(brief.md 없음)"
        investigation = self.store.load_artifact(actual_id, "investigation.json") or "{}"
        request_raw = self.store.load_artifact(actual_id, "request.json") or "{}"
        try:
            request = json.loads(request_raw)
        except json.JSONDecodeError:
            request = {}
        extra_context = request.get("extra_context", "")

        brief_dir = self.store._brief_dir(actual_id)
        cwd = str(brief_dir.resolve()) if brief_dir else ""

        from prompts.ha_expert_chat import HA_EXPERT_CHAT_SYSTEM_PROMPT
        prompt = (
            HA_EXPERT_CHAT_SYSTEM_PROMPT
            .replace("{base_context}", self._base_context)
            .replace("{brief_md}", brief_md)
            .replace("{investigation_json}", investigation)
            .replace("{target}", target)
            .replace("{extra_context}", extra_context or "(없음)")
        )

        session_id = uuid.uuid4().hex
        result = self.runtime.run(
            LLMRunRequest(
                prompt=prompt,
                session_id=session_id,
                cwd=cwd or None,
                allow_file_write=True,
                heartbeat_channel=self.status_channel,
                heartbeat_agent="ha_expert",
                heartbeat_label="HA-Expert 대화 시작",
            )
        )
        if not result.success:
            logger.error("Failed to create HA-Expert chat session for brief %s", actual_id)
            return None

        session = HAChatSession(
            session_id=session_id,
            brief_id=actual_id,
            channel=channel,
            thread_ts=thread_ts,
            cwd=cwd,
            message_count=1,
        )
        self._chat_sessions[thread_ts] = session
        self.store.append_chat_log(actual_id, "ha_expert", result.output)
        logger.info("HA chat started: thread=%s brief=%s session=%s", thread_ts, actual_id, session_id)
        return result.output

    def continue_chat(self, thread_ts: str, user_message: str) -> str | None:
        self._cleanup_expired_sessions()
        session = self._chat_sessions.get(thread_ts)
        if not session:
            return None
        session.last_activity = time.time()
        session.message_count += 1

        result = self.runtime.run(
            LLMRunRequest(
                prompt=user_message,
                session_id=session.session_id,
                cwd=session.cwd or None,
                allow_file_write=True,
                heartbeat_channel=self.status_channel,
                heartbeat_agent="ha_expert",
                heartbeat_label="HA-Expert 대화",
            )
        )
        if not result.success:
            logger.error("HA chat continuation failed for thread %s", thread_ts)
            return None
        self.store.append_chat_log(session.brief_id, "user", user_message)
        self.store.append_chat_log(session.brief_id, "ha_expert", result.output)
        return result.output

    def end_chat_session(self, thread_ts: str, *, notify_reason: str | None = None) -> int:
        session = self._chat_sessions.pop(thread_ts, None)
        if not session:
            return 0

        if notify_reason and self.slack and session.channel and session.thread_ts:
            try:
                self.slack.chat_postMessage(
                    channel=session.channel,
                    text=notify_reason,
                    thread_ts=session.thread_ts,
                )
            except Exception as e:
                logger.warning("Failed to post HA chat-end notification: %s", e)

        try:
            import requests
            requests.delete(
                f"{self.api_url}/session",
                headers={"Content-Type": "application/json", "x-api-key": self.api_key},
                json={"sessionId": session.session_id},
                timeout=5,
            )
        except Exception as e:
            logger.warning("Failed to close API session %s: %s", session.session_id, e)

        logger.info("HA chat ended: thread=%s messages=%d", thread_ts, session.message_count)
        return session.message_count

    def has_chat_session(self, thread_ts: str) -> bool:
        self._cleanup_expired_sessions()
        return thread_ts in self._chat_sessions
