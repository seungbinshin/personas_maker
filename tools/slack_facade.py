"""Slack helper facade for shared skills."""

from __future__ import annotations

import logging
from collections.abc import Iterable

from skills.types import MessagePayload

logger = logging.getLogger(__name__)


class SlackFacade:
    def __init__(self, slack_client, agents_config: dict[str, dict] | None = None):
        self.client = slack_client
        self.agents_config = agents_config or {}

    def post(self, payload: MessagePayload) -> None:
        kwargs: dict[str, object] = {"channel": payload.channel}
        if payload.text:
            kwargs["text"] = payload.text
        if payload.blocks:
            kwargs["blocks"] = payload.blocks
        if payload.thread_ts:
            kwargs["thread_ts"] = payload.thread_ts

        if payload.agent_name and payload.agent_name in self.agents_config:
            agent = self.agents_config[payload.agent_name]
            kwargs["username"] = agent.get("display_name", payload.agent_name)
            kwargs["icon_emoji"] = agent.get("emoji", ":robot_face:")

        try:
            self.client.chat_postMessage(**kwargs)
        except Exception as exc:
            logger.error("Slack post failed: %s", exc)

    def post_lines(
        self,
        channel: str,
        lines: Iterable[str],
        thread_ts: str | None = None,
    ) -> None:
        for line in lines:
            self.post(MessagePayload(channel=channel, text=line, thread_ts=thread_ts))

    def history(self, channel: str, limit: int = 15) -> list[dict]:
        try:
            result = self.client.conversations_history(channel=channel, limit=limit)
            return result.get("messages", [])
        except Exception as exc:
            logger.error("Slack history load failed: %s", exc)
            return []

    def upload_file(
        self,
        channel: str,
        content: str,
        filename: str,
        title: str,
        initial_comment: str = "",
    ) -> bool:
        """Upload a file to a Slack channel using files_upload_v2."""
        try:
            self.client.files_upload_v2(
                channel=channel,
                content=content.encode("utf-8"),
                filename=filename,
                title=title,
                initial_comment=initial_comment,
            )
            return True
        except Exception as exc:
            logger.error("Slack file upload failed: %s", exc)
            return False

    def user_name(self, user_id: str) -> str:
        try:
            user_info = self.client.users_info(user=user_id)
            profile = user_info["user"]["profile"]
            return profile.get("display_name") or user_info["user"]["real_name"]
        except Exception:
            return user_id

