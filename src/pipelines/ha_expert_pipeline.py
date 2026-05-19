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
        seq = brief_id.split("_")[1]
        self.post_to_slack(
            channel=channel,
            text=brief_md,
            agent_name="ha_expert",
        )
        self.post_to_slack(
            channel=channel,
            text=(
                f":speech_balloon: 후속 대화: `!ha chat {int(seq)}` "
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
        parts = []
        for label, kb in (("Discourse", self.discourse_knowledge), ("Confluence", self.confluence_knowledge)):
            if kb is None:
                continue
            try:
                # Try a few common search method names; skip if not available
                snippets = None
                for method_name in ("search", "query", "lookup"):
                    if hasattr(kb, method_name):
                        snippets = getattr(kb, method_name)(target)
                        break
                if snippets:
                    parts.append(f"=== Internal: {label} ===\n{snippets}")
            except Exception as e:
                logger.warning("Internal context fetch (%s) failed: %s", label, e)
        return "\n\n".join(parts) if parts else "(자사 내부 문서 매칭 없음)"

    def list_briefs(self, limit: int = 10) -> list[dict]:
        return self.store.list_briefs(limit=limit)
