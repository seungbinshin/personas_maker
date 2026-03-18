"""Slack-oriented adapter helpers built on shared skills/tools."""

from __future__ import annotations

from tools.claude_runtime import ClaudeRuntimeClient
from tools.slack_facade import SlackFacade


class SlackRuntimeAdapter:
    """Bundle Slack and runtime dependencies for adapter consumers."""

    def __init__(
        self,
        *,
        slack_client,
        agents_config: dict[str, dict],
        api_url: str,
        api_key: str,
    ):
        self.slack = SlackFacade(slack_client, agents_config)
        self.runtime = ClaudeRuntimeClient(api_url, api_key)

