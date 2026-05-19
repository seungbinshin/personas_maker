"""Tests for HAExpertPipeline — brief generation and chat session lifecycle."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from brief_store import BriefStore


@pytest.fixture
def bot_dir(tmp_path: Path) -> Path:
    (tmp_path / "context").mkdir()
    (tmp_path / "context" / "ha_expert_base.md").write_text("BASE CTX", encoding="utf-8")
    return tmp_path


@pytest.fixture
def bot_config() -> dict:
    return {
        "name": "research",
        "persona_type": "research_pipeline",
        "agents": {
            "ha_expert": {"display_name": "HA-Expert", "emoji": ":briefcase:"},
        },
        "ha_expert": {
            "base_context_file": "context/ha_expert_base.md",
        },
        "research": {"status_channel": "C_STATUS"},
    }


def test_run_brief_invokes_investigator_then_briefer(bot_dir, bot_config):
    from pipelines.ha_expert_pipeline import HAExpertPipeline

    slack = MagicMock()
    pipeline = HAExpertPipeline(
        bot_config=bot_config,
        slack_client=slack,
        api_url="http://api",
        api_key="key",
        bot_dir=bot_dir,
        discourse_knowledge=None,
        confluence_knowledge=None,
    )

    # Stub call_llm: first call (Investigator) returns JSON, second (Briefer) returns Markdown
    pipeline.call_llm = MagicMock(side_effect=[
        json.dumps({"target": "NTT Data", "all_sources": ["https://example.com"]}),
        "# Brief: NTT Data\n\nTL;DR ...",
    ])

    brief_id = pipeline.run_brief(
        target="NTT Data",
        extra_context="Tokyo meeting",
        channel="C123",
        source_ts="123.456",
    )

    assert brief_id is not None
    assert pipeline.call_llm.call_count == 2
    # Verify the two prompts are actually composed with the right substitutions
    first_call_prompt = pipeline.call_llm.call_args_list[0][0][0]
    second_call_prompt = pipeline.call_llm.call_args_list[1][0][0]
    assert "NTT Data" in first_call_prompt  # target substituted into investigator prompt
    assert "Tokyo meeting" in first_call_prompt  # extra_context substituted
    assert "NTT Data" in second_call_prompt  # target substituted into briefer prompt
    assert "https://example.com" in second_call_prompt  # investigation piped into briefer
    # Investigator output saved
    inv = pipeline.store.load_artifact(brief_id, "investigation.json")
    assert inv is not None and "NTT Data" in inv
    # Briefer output saved
    brief = pipeline.store.load_artifact(brief_id, "brief.md")
    assert brief is not None and "Brief: NTT Data" in brief
    # state advanced to drafted
    state = pipeline.store.get_brief(brief_id)
    assert state["status"] == "drafted"


def test_run_brief_fails_gracefully_on_empty_investigator(bot_dir, bot_config):
    from pipelines.ha_expert_pipeline import HAExpertPipeline

    pipeline = HAExpertPipeline(
        bot_config=bot_config,
        slack_client=MagicMock(),
        api_url="http://api",
        api_key="key",
        bot_dir=bot_dir,
        discourse_knowledge=None,
        confluence_knowledge=None,
    )
    pipeline.call_llm = MagicMock(return_value="")

    brief_id = pipeline.run_brief(target="X", extra_context="", channel="C", source_ts="1")
    assert brief_id is not None  # brief dir was still created
    state = pipeline.store.get_brief(brief_id)
    assert state["status"] == "failed"


def test_list_briefs_passthrough(bot_dir, bot_config):
    from pipelines.ha_expert_pipeline import HAExpertPipeline

    pipeline = HAExpertPipeline(
        bot_config=bot_config,
        slack_client=MagicMock(),
        api_url="http://api",
        api_key="key",
        bot_dir=bot_dir,
        discourse_knowledge=None,
        confluence_knowledge=None,
    )
    pipeline.store.create_brief("Alpha", "")
    pipeline.store.create_brief("Beta", "")
    out = pipeline.list_briefs()
    assert len(out) == 2


def test_gather_internal_context_calls_build_context_and_includes_snippets(bot_dir, bot_config):
    from pipelines.ha_expert_pipeline import HAExpertPipeline

    fake_kb = MagicMock()
    fake_kb.build_context.return_value = "INTERNAL_SNIPPET_TEXT"

    pipeline = HAExpertPipeline(
        bot_config=bot_config,
        slack_client=MagicMock(),
        api_url="http://api",
        api_key="key",
        bot_dir=bot_dir,
        discourse_knowledge=fake_kb,
        confluence_knowledge=None,
    )

    pipeline.call_llm = MagicMock(side_effect=['{"target":"NTT"}', "# Brief"])
    pipeline.run_brief(target="NTT Data", extra_context="ctx", channel="C", source_ts="1")

    fake_kb.build_context.assert_called_once_with(["NTT", "Data"])
    first_call_prompt = pipeline.call_llm.call_args_list[0][0][0]
    assert "INTERNAL_SNIPPET_TEXT" in first_call_prompt
    assert "=== Internal: Discourse ===" in first_call_prompt


def test_gather_internal_context_returns_placeholder_when_no_kb(bot_dir, bot_config):
    from pipelines.ha_expert_pipeline import HAExpertPipeline

    pipeline = HAExpertPipeline(
        bot_config=bot_config,
        slack_client=MagicMock(),
        api_url="http://api",
        api_key="key",
        bot_dir=bot_dir,
        discourse_knowledge=None,
        confluence_knowledge=None,
    )
    result = pipeline._gather_internal_context("Anything")
    assert result == "(자사 내부 문서 매칭 없음)"


def _make_pipeline_with_brief(bot_dir, bot_config):
    from pipelines.ha_expert_pipeline import HAExpertPipeline

    pipeline = HAExpertPipeline(
        bot_config=bot_config,
        slack_client=MagicMock(),
        api_url="http://api",
        api_key="key",
        bot_dir=bot_dir,
        discourse_knowledge=None,
        confluence_knowledge=None,
    )
    brief_id = pipeline.store.create_brief("Acme", "ctx")
    pipeline.store.save_artifact(brief_id, "brief.md", "# Brief: Acme\nTL;DR")
    pipeline.store.save_artifact(brief_id, "investigation.json", '{"target":"Acme"}')
    pipeline.store.update_state(brief_id, "drafted")
    return pipeline, brief_id


def test_start_chat_session_returns_response_and_registers_session(bot_dir, bot_config):
    pipeline, brief_id = _make_pipeline_with_brief(bot_dir, bot_config)

    fake_result = MagicMock(success=True, output="안녕하세요. Brief 요약은...")
    pipeline.runtime.run = MagicMock(return_value=fake_result)

    response = pipeline.start_chat_session(brief_id, channel="C123", thread_ts="ts.1")
    assert response == "안녕하세요. Brief 요약은..."
    assert pipeline.has_chat_session("ts.1") is True

    # chat_log should record the assistant's opening message
    import json as _json
    log_content = pipeline.store.load_artifact(brief_id, "chat_log.jsonl")
    assert log_content is not None
    lines = [_json.loads(l) for l in log_content.strip().splitlines()]
    assert lines[0]["role"] == "ha_expert"
    assert "안녕하세요" in lines[0]["message"]


def test_start_chat_returns_none_for_unknown_brief(bot_dir, bot_config):
    from pipelines.ha_expert_pipeline import HAExpertPipeline

    pipeline = HAExpertPipeline(
        bot_config=bot_config,
        slack_client=MagicMock(),
        api_url="http://api",
        api_key="key",
        bot_dir=bot_dir,
        discourse_knowledge=None,
        confluence_knowledge=None,
    )
    assert pipeline.start_chat_session("nonexistent", "C", "ts") is None


def test_continue_chat_routes_to_existing_session(bot_dir, bot_config):
    pipeline, brief_id = _make_pipeline_with_brief(bot_dir, bot_config)

    pipeline.runtime.run = MagicMock(side_effect=[
        MagicMock(success=True, output="첫 응답"),
        MagicMock(success=True, output="두번째 응답"),
    ])
    pipeline.start_chat_session(brief_id, channel="C", thread_ts="ts.1")
    out = pipeline.continue_chat("ts.1", "후속 질문")
    assert out == "두번째 응답"


def test_continue_chat_unknown_thread_returns_none(bot_dir, bot_config):
    from pipelines.ha_expert_pipeline import HAExpertPipeline

    pipeline = HAExpertPipeline(
        bot_config=bot_config,
        slack_client=MagicMock(),
        api_url="http://api",
        api_key="key",
        bot_dir=bot_dir,
        discourse_knowledge=None,
        confluence_knowledge=None,
    )
    assert pipeline.continue_chat("ts.unknown", "msg") is None


def test_end_chat_session_removes_mapping(bot_dir, bot_config):
    pipeline, brief_id = _make_pipeline_with_brief(bot_dir, bot_config)

    pipeline.runtime.run = MagicMock(return_value=MagicMock(success=True, output="첫"))
    pipeline.start_chat_session(brief_id, channel="C", thread_ts="ts.1")
    assert pipeline.has_chat_session("ts.1") is True

    with patch("requests.delete") as fake_delete:
        fake_delete.return_value = MagicMock(status_code=200)
        count = pipeline.end_chat_session("ts.1")
    assert count >= 1
    assert pipeline.has_chat_session("ts.1") is False


def test_start_chat_session_evicts_oldest_when_at_cap(bot_dir, bot_config):
    from pipelines.ha_expert_pipeline import HAExpertPipeline, MAX_CHAT_SESSIONS

    pipeline = HAExpertPipeline(
        bot_config=bot_config,
        slack_client=MagicMock(),
        api_url="http://api",
        api_key="key",
        bot_dir=bot_dir,
        discourse_knowledge=None,
        confluence_knowledge=None,
    )
    # Create MAX_CHAT_SESSIONS briefs and start chat sessions for each
    pipeline.runtime.run = MagicMock(return_value=MagicMock(success=True, output="hello"))
    brief_ids = []
    for i in range(MAX_CHAT_SESSIONS):
        bid = pipeline.store.create_brief(f"Brief{i}", "")
        pipeline.store.save_artifact(bid, "brief.md", f"# Brief: Brief{i}")
        pipeline.store.save_artifact(bid, "investigation.json", "{}")
        pipeline.store.update_state(bid, "drafted")
        brief_ids.append(bid)
        pipeline.start_chat_session(bid, channel="C", thread_ts=f"ts.{i}")

    assert len(pipeline._chat_sessions) == MAX_CHAT_SESSIONS

    # Make ts.0 the oldest by bumping the others' last_activity forward
    import time as _time
    now = _time.time()
    for i in range(1, MAX_CHAT_SESSIONS):
        pipeline._chat_sessions[f"ts.{i}"].last_activity = now + 1000

    # Create one more brief and start a new session — oldest (ts.0) should be evicted
    with patch("requests.delete"):
        new_bid = pipeline.store.create_brief("Overflow", "")
        pipeline.store.save_artifact(new_bid, "brief.md", "# Brief: Overflow")
        pipeline.store.save_artifact(new_bid, "investigation.json", "{}")
        pipeline.store.update_state(new_bid, "drafted")
        pipeline.start_chat_session(new_bid, channel="C", thread_ts="ts.new")

    assert len(pipeline._chat_sessions) == MAX_CHAT_SESSIONS
    assert "ts.0" not in pipeline._chat_sessions
    assert "ts.new" in pipeline._chat_sessions
