import json

import pytest


def test_env_var_takes_precedence(tmp_path, monkeypatch):
    from claude_api_client import base_url
    monkeypatch.setenv("CLAUDE_CODE_API_URL", "http://example.com:1234")
    monkeypatch.setattr("claude_api_client.REGISTRY", tmp_path / "missing.json")
    assert base_url() == "http://example.com:1234"


def test_registry_file_used_when_env_unset(tmp_path, monkeypatch):
    from claude_api_client import base_url
    monkeypatch.delenv("CLAUDE_CODE_API_URL", raising=False)
    reg = tmp_path / "registry.json"
    reg.write_text(json.dumps({"port": 51234, "pid": 1, "started_at": "x", "version": "0.2.0"}))
    monkeypatch.setattr("claude_api_client.REGISTRY", reg)
    assert base_url() == "http://127.0.0.1:51234"


def test_raises_when_neither_env_nor_registry(tmp_path, monkeypatch):
    from claude_api_client import base_url
    monkeypatch.delenv("CLAUDE_CODE_API_URL", raising=False)
    monkeypatch.setattr("claude_api_client.REGISTRY", tmp_path / "missing.json")
    with pytest.raises(RuntimeError, match="not running"):
        base_url()


def test_raises_when_registry_corrupt(tmp_path, monkeypatch):
    from claude_api_client import base_url
    monkeypatch.delenv("CLAUDE_CODE_API_URL", raising=False)
    reg = tmp_path / "registry.json"
    reg.write_text("{not json")
    monkeypatch.setattr("claude_api_client.REGISTRY", reg)
    with pytest.raises(RuntimeError, match="corrupt"):
        base_url()
