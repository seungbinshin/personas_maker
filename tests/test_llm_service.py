from __future__ import annotations

import pytest

from llm_service import resolve_llm_service


def test_ccapi_uses_explicit_url_without_registry(monkeypatch):
    monkeypatch.setenv("CLAUDE_API_URL", "http://127.0.0.1:9010")
    monkeypatch.setenv("CLAUDE_API_KEY", "claude-key")

    service = resolve_llm_service({"llm": {"provider": "ccapi"}})

    assert service.provider == "ccapi"
    assert service.url == "http://127.0.0.1:9010"
    assert service.api_key == "claude-key"
    assert service.url_resolver is not None


def test_gsapi_uses_bot_config_and_does_not_install_ccapi_resolver(monkeypatch):
    monkeypatch.setenv("GSAPI_API_KEY", "gpt-key")

    service = resolve_llm_service(
        {"llm": {"provider": "gsapi", "url": "http://127.0.0.1:8081/"}}
    )

    assert service.provider == "gsapi"
    assert service.url == "http://127.0.0.1:8081"
    assert service.api_key == "gpt-key"
    assert service.url_resolver is None


def test_gsapi_requires_a_caller_key(monkeypatch):
    for key in ("GSAPI_API_KEY", "GPT_SERVICE_API_KEY", "LLM_API_KEY", "BOT_API_KEY"):
        monkeypatch.delenv(key, raising=False)

    with pytest.raises(RuntimeError, match="GSAPI_API_KEY"):
        resolve_llm_service({"llm": {"provider": "gsapi"}})


def test_unknown_provider_is_rejected():
    with pytest.raises(ValueError, match="Unsupported"):
        resolve_llm_service({"llm": {"provider": "other"}})
