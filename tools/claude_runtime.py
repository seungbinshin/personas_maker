"""Shared client for claude-code-api."""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable

import requests

from skills.types import LLMRunRequest, LLMRunResult
from tools.json_utils import parse_json_response

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_MS = 1_800_000


class ClaudeRuntimeClient:
    """Thin runtime wrapper shared by bot and pipeline adapters."""

    def __init__(
        self,
        api_url: str,
        api_key: str,
        heartbeat_callback: Callable[[str, str | None, str], None] | None = None,
    ):
        self.api_url = api_url.rstrip("/")
        self.api_key = api_key
        self.heartbeat_callback = heartbeat_callback
        self.last_session_id: str | None = None

    def run(self, request: LLMRunRequest) -> LLMRunResult:
        timeout_ms = request.timeout_ms or DEFAULT_TIMEOUT_MS
        http_timeout = (timeout_ms // 1000) + 30
        stop_event = threading.Event()
        heartbeat_thread = None

        if (
            self.heartbeat_callback
            and request.heartbeat_channel
            and request.heartbeat_label
        ):
            heartbeat_thread = threading.Thread(
                target=self._heartbeat_loop,
                args=(
                    stop_event,
                    request.heartbeat_channel,
                    request.heartbeat_agent,
                    request.heartbeat_label,
                ),
                daemon=True,
            )
            heartbeat_thread.start()

        try:
            body: dict[str, object] = {
                "prompt": request.prompt,
                "timeoutMs": timeout_ms,
            }
            if request.session_id:
                body["sessionId"] = request.session_id
            if request.cwd:
                body["cwd"] = request.cwd
            if request.model:
                body["model"] = request.model
            if request.allow_file_write:
                body["allowFileWrite"] = True

            resp = requests.post(
                f"{self.api_url}/run",
                headers={
                    "Content-Type": "application/json",
                    "x-api-key": self.api_key,
                },
                json=body,
                timeout=http_timeout,
            )
            resp.raise_for_status()
            data = resp.json()
            self.last_session_id = data.get("sessionId") or request.session_id

            return LLMRunResult(
                success=bool(data.get("success")),
                output=data.get("output", "").strip(),
                duration_ms=int(data.get("durationMs", 0) or 0),
                timed_out=bool(data.get("timedOut")),
                timeout_type=data.get("timeoutType"),
                session_id=self.last_session_id,
                raw=data,
            )
        except Exception as exc:
            logger.error("claude-code-api request failed: %s", exc)
            return LLMRunResult(success=False, output="", raw={"error": str(exc)})
        finally:
            stop_event.set()
            if heartbeat_thread:
                heartbeat_thread.join(timeout=5)

    def run_json(
        self,
        request: LLMRunRequest,
        *,
        task_name: str,
        expected_kind: str,
        schema_example: str,
        repair_timeout_ms: int = 90_000,
    ) -> tuple[LLMRunResult, Any | None]:
        """Run a request expected to return JSON, with auto-repair fallback."""
        result = self.run(request)
        if not result.success:
            return result, None

        parsed = parse_json_response(result.output)
        if self._matches_expected_kind(parsed, expected_kind):
            return result, parsed

        if result.output:
            logger.warning("%s returned malformed or unexpected JSON; attempting repair", task_name)
            repaired = self._repair_json_output(
                raw_output=result.output,
                schema_example=schema_example,
                expected_kind=expected_kind,
                timeout_ms=repair_timeout_ms,
                heartbeat_channel=request.heartbeat_channel,
                heartbeat_agent=request.heartbeat_agent,
                heartbeat_label=request.heartbeat_label,
                task_name=task_name,
            )
            if self._matches_expected_kind(repaired, expected_kind):
                return result, repaired

        return result, None

    def _heartbeat_loop(
        self,
        stop_event: threading.Event,
        channel: str,
        agent: str | None,
        label: str,
    ) -> None:
        start = time.time()
        interval = 120
        while not stop_event.wait(interval):
            if not self.heartbeat_callback:
                return
            mins = int(time.time() - start) // 60
            self.heartbeat_callback(
                channel,
                agent,
                f":hourglass_flowing_sand: _{label}_ 작업 중... ({mins}분 경과)",
            )

    @staticmethod
    def _matches_expected_kind(value: Any, expected_kind: str) -> bool:
        if expected_kind == "object":
            return isinstance(value, dict)
        if expected_kind == "array":
            return isinstance(value, list)
        raise ValueError(f"Unsupported expected_kind: {expected_kind}")

    def _repair_json_output(
        self,
        *,
        raw_output: str,
        schema_example: str,
        expected_kind: str,
        timeout_ms: int,
        heartbeat_channel: str | None,
        heartbeat_agent: str | None,
        heartbeat_label: str | None,
        task_name: str,
    ) -> Any | None:
        kind_label = "JSON object" if expected_kind == "object" else "JSON array"
        repair_prompt = f"""You are repairing a malformed structured response.

The previous model output did not follow the required JSON-only contract.
Convert it into a single valid {kind_label} that matches the schema below.

Rules:
- Do not perform any new research.
- Do not use external tools.
- Preserve the original meaning as much as possible.
- If some fields are missing, use empty arrays/objects or concise placeholder text instead of omitting required keys.
- Return ONLY valid JSON with no markdown fences or extra prose.

Target schema:
{schema_example}

Previous output:
{raw_output}
"""
        repaired_result = self.run(
            LLMRunRequest(
                prompt=repair_prompt,
                timeout_ms=timeout_ms,
                heartbeat_channel=heartbeat_channel,
                heartbeat_agent=heartbeat_agent,
                heartbeat_label=f"{heartbeat_label} JSON 복구" if heartbeat_label else f"{task_name} JSON 복구",
            )
        )
        if not repaired_result.success:
            return None
        return parse_json_response(repaired_result.output)

