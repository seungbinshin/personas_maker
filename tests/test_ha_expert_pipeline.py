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
