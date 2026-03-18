"""Base pipeline adapter backed by shared tools."""

import logging
from pathlib import Path

from skills.types import LLMRunRequest, MessagePayload
from tools.claude_runtime import ClaudeRuntimeClient, DEFAULT_TIMEOUT_MS
from tools.json_utils import parse_json_response
from tools.slack_facade import SlackFacade

logger = logging.getLogger(__name__)


class BasePipeline:
    """Base class for multi-agent pipelines."""

    def __init__(
        self,
        bot_config: dict,
        slack_client,
        api_url: str,
        api_key: str,
        bot_dir: Path,
    ):
        self.config = bot_config
        self.slack = slack_client
        self.api_url = api_url
        self.api_key = api_key
        self.bot_dir = bot_dir
        self.bot_name = bot_config["name"]
        self.agents_config = bot_config.get("agents", {})
        self.slack_facade = SlackFacade(slack_client, self.agents_config)
        self.runtime = ClaudeRuntimeClient(
            api_url=api_url,
            api_key=api_key,
            heartbeat_callback=self._post_heartbeat,
        )
        self._status_channel = bot_config.get(
            bot_config.get("persona_type", ""), {}
        ).get("status_channel", "")

    def call_llm(
        self,
        prompt: str,
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
        session_id: str | None = None,
        heartbeat_channel: str | None = None,
        heartbeat_agent: str | None = None,
        heartbeat_label: str | None = None,
    ) -> str:
        """Call the shared Claude runtime and return response text."""
        result = self.runtime.run(
            LLMRunRequest(
                prompt=prompt,
                timeout_ms=timeout_ms,
                session_id=session_id,
                heartbeat_channel=heartbeat_channel,
                heartbeat_agent=heartbeat_agent,
                heartbeat_label=heartbeat_label,
            )
        )
        if not result.success:
            logger.error("LLM call failed: %s", result.raw)
            return ""
        return result.output

    def post_to_slack(
        self,
        channel: str,
        text: str = "",
        blocks: list | None = None,
        agent_name: str | None = None,
    ):
        """Post a message to Slack, optionally with agent identity override."""
        self.slack_facade.post(
            MessagePayload(channel=channel, text=text, blocks=blocks, agent_name=agent_name)
        )

    def _post_heartbeat(self, channel: str, agent: str | None, text: str) -> None:
        self.post_to_slack(channel=channel, text=text, agent_name=agent)

    def save_artifact(self, report_id: str, filename: str, content: str):
        """Save an artifact to the bot's reports directory."""
        reports_dir = self.bot_dir / "reports" / report_id
        reports_dir.mkdir(parents=True, exist_ok=True)
        filepath = reports_dir / filename
        filepath.write_text(content, encoding="utf-8")
        logger.info(f"Saved artifact: {report_id}/{filename}")

    def load_artifact(self, report_id: str, filename: str) -> str | None:
        """Load an artifact from the bot's reports directory."""
        filepath = self.bot_dir / "reports" / report_id / filename
        if filepath.exists():
            return filepath.read_text(encoding="utf-8")
        return None

    def parse_json_response(self, text: str) -> dict | None:
        """Try to extract JSON from LLM response text."""
        parsed = parse_json_response(text)
        return parsed if isinstance(parsed, dict) else None
