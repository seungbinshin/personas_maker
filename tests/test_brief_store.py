"""Tests for BriefStore — disk-backed brief artifact management."""

from __future__ import annotations

from pathlib import Path

import pytest

from brief_store import BriefStore


@pytest.fixture
def store(tmp_path: Path) -> BriefStore:
    return BriefStore(tmp_path / "briefs")


def test_create_brief_assigns_seq_and_dir(store: BriefStore):
    brief_id = store.create_brief(target="NTT Data", extra_context="Tokyo meeting")
    assert brief_id.endswith("_ntt-data") or brief_id.endswith("_ntt_data")
    # numeric prefix is zero-padded 3 digits, preceded by YYYYMMDD_
    parts = brief_id.split("_")
    assert len(parts) >= 3
    assert len(parts[0]) == 8 and parts[0].isdigit()  # date
    assert parts[1].isdigit() and len(parts[1]) == 3  # seq


def test_seq_increments(store: BriefStore):
    a = store.create_brief("Company A", "ctx")
    b = store.create_brief("Company B", "ctx")
    seq_a = int(a.split("_")[1])
    seq_b = int(b.split("_")[1])
    assert seq_b == seq_a + 1


def test_numeric_tag_lookup(store: BriefStore):
    brief_id = store.create_brief("NTT Data", "")
    seq = int(brief_id.split("_")[1])
    found = store.get_brief(str(seq))
    assert found is not None
    assert found["brief_id"] == brief_id


def test_numeric_tag_returns_none_when_not_found(store: BriefStore):
    store.create_brief("X", "")
    assert store.get_brief("999") is None


def test_substring_lookup_falls_back_for_non_numeric(store: BriefStore):
    brief_id = store.create_brief("NTT Data", "")
    # full id works
    assert store.get_brief(brief_id) is not None
    # partial slug suffix should work via substring fallback
    assert store.get_brief("ntt") is not None or store.get_brief("ntt-data") is not None


def test_save_and_load_artifact(store: BriefStore):
    brief_id = store.create_brief("Acme", "")
    store.save_artifact(brief_id, "investigation.json", '{"a": 1}')
    store.save_artifact(brief_id, "brief.md", "# Brief\n")
    assert store.load_artifact(brief_id, "investigation.json") == '{"a": 1}'
    assert store.load_artifact(brief_id, "brief.md") == "# Brief\n"


def test_update_state(store: BriefStore):
    brief_id = store.create_brief("Acme", "")
    store.update_state(brief_id, "drafted")
    state = store.get_brief(brief_id)
    assert state["status"] == "drafted"


def test_list_briefs_newest_first(store: BriefStore):
    a = store.create_brief("A", "")
    b = store.create_brief("B", "")
    listed = store.list_briefs()
    ids = [b["brief_id"] for b in listed]
    assert ids[0] == b
    assert ids[1] == a


def test_list_briefs_limit(store: BriefStore):
    for i in range(5):
        store.create_brief(f"X{i}", "")
    listed = store.list_briefs(limit=3)
    assert len(listed) == 3
