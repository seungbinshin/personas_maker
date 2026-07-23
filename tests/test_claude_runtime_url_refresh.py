"""Verify ClaudeRuntimeClient refreshes its base URL once on ConnectionError.

Background: long-running bots cache CLAUDE_API_URL at import time. If ccapi is
restarted on a different port (e.g. by launchd), the bot keeps hitting the
stale port until restarted. The url_resolver hook lets the runtime self-heal.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
import requests

from skills.types import LLMRunRequest
from tools.claude_runtime import ClaudeRuntimeClient


def _make_response(payload):
    class _R:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return payload
    return _R()


def test_connection_error_triggers_url_refresh_and_retry():
    new_url = "http://127.0.0.1:9999"
    client = ClaudeRuntimeClient(
        api_url="http://127.0.0.1:52661",
        api_key="k",
        url_resolver=lambda: new_url,
    )

    calls = []
    def fake_post(url, **_):
        calls.append(url)
        if "52661" in url:
            raise requests.exceptions.ConnectionError("refused")
        return _make_response({"success": True, "output": "ok", "durationMs": 10})

    with patch("tools.claude_runtime.requests.post", side_effect=fake_post):
        result = client.run(LLMRunRequest(prompt="hi"))

    assert result.success is True
    assert client.api_url == new_url
    assert len(calls) == 2
    assert "52661" in calls[0]
    assert "9999" in calls[1]


def test_no_resolver_does_not_retry():
    client = ClaudeRuntimeClient(api_url="http://127.0.0.1:52661", api_key="k")

    calls = []
    def fake_post(url, **_):
        calls.append(url)
        raise requests.exceptions.ConnectionError("refused")

    with patch("tools.claude_runtime.requests.post", side_effect=fake_post):
        result = client.run(LLMRunRequest(prompt="hi"))

    assert result.success is False
    assert len(calls) == 1


def test_resolver_returning_same_url_does_not_retry():
    client = ClaudeRuntimeClient(
        api_url="http://127.0.0.1:52661",
        api_key="k",
        url_resolver=lambda: "http://127.0.0.1:52661",
    )

    calls = []
    def fake_post(url, **_):
        calls.append(url)
        raise requests.exceptions.ConnectionError("refused")

    with patch("tools.claude_runtime.requests.post", side_effect=fake_post):
        result = client.run(LLMRunRequest(prompt="hi"))

    assert result.success is False
    assert len(calls) == 1


def test_gsapi_uses_responses_gateway_field_names():
    client = ClaudeRuntimeClient(
        api_url="http://127.0.0.1:8081",
        api_key="k",
        provider="gsapi",
    )
    captured = {}

    def fake_post(_url, **kwargs):
        captured.update(kwargs["json"])
        return _make_response({"success": True, "output": "ok", "durationMs": 10})

    with patch("tools.claude_runtime.requests.post", side_effect=fake_post):
        result = client.run(LLMRunRequest(
            prompt="hi", effort="medium", cwd="/private/project", allow_file_write=True,
        ))

    assert result.success is True
    assert captured["reasoningEffort"] == "medium"
    assert "effort" not in captured
    assert "cwd" not in captured
    assert "allowFileWrite" not in captured
