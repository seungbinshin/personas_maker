"""Tests for PostEditor safety gates and apply flow."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from post_editor import EditRefused, PostEditor


def _mk_editor(tmp_path, report_id="r1", known_post_id=1304):
    bot_dir = tmp_path / "bot"
    (bot_dir / "reports" / report_id).mkdir(parents=True)
    state = bot_dir / "reports" / report_id / "state.json"
    state.write_text(
        '{"report_id":"r1","status":"accepted","metadata":{"discourse_post_id":'
        + str(known_post_id)
        + '}}',
        encoding="utf-8",
    )
    client = MagicMock()
    publisher = MagicMock()
    publisher.get_report_for_post = MagicMock(
        side_effect=lambda pid: (
            {"report_id": report_id, "discourse_post_id": known_post_id}
            if pid == known_post_id
            else None
        )
    )
    return PostEditor(bot_dir, client, publisher), client, publisher, bot_dir


def test_refuses_unknown_post_id(tmp_path):
    editor, client, publisher, _ = _mk_editor(tmp_path)
    with pytest.raises(EditRefused, match="not a bot-owned"):
        editor.apply_edit(post_id=999, new_raw="x", edit_reason="r", current_raw="old")
    client.edit_post.assert_not_called()


def test_refuses_empty_edit_reason(tmp_path):
    editor, client, _, _ = _mk_editor(tmp_path)
    with pytest.raises(EditRefused, match="edit_reason"):
        editor.apply_edit(post_id=1304, new_raw="x", edit_reason="   ", current_raw="old")
    client.edit_post.assert_not_called()


def test_refuses_when_current_raw_missing(tmp_path):
    editor, client, _, _ = _mk_editor(tmp_path)
    with pytest.raises(EditRefused, match="backup"):
        editor.apply_edit(post_id=1304, new_raw="x", edit_reason="r", current_raw="")
    client.edit_post.assert_not_called()


def test_applies_edit_writes_backup_and_updates_state(tmp_path):
    editor, client, _, bot_dir = _mk_editor(tmp_path)
    client.edit_post.return_value = {"id": 1304, "version": 2}

    result = editor.apply_edit(
        post_id=1304,
        new_raw="fixed body",
        edit_reason="댓글 #2 반영",
        current_raw="old body",
        edit_type="format",
        change_summary="LaTeX fence 수정",
        triggered_by_post=2,
    )

    assert result["applied"] is True
    assert result["version"] == 2
    client.edit_post.assert_called_once_with(1304, "fixed body", "댓글 #2 반영")

    backups = list((bot_dir / "knowledge" / "edits").glob("1304-*.md"))
    assert len(backups) == 1
    assert backups[0].read_text("utf-8") == "old body"

    import json
    state = json.loads((bot_dir / "reports" / "r1" / "state.json").read_text("utf-8"))
    history = state["metadata"].get("edit_history", [])
    assert len(history) == 1
    assert history[0]["post_id"] == 1304
    assert history[0]["edit_type"] == "format"
    assert history[0]["change_summary"] == "LaTeX fence 수정"
    assert history[0]["triggered_by_post"] == 2


def test_edit_history_appends_not_overwrites(tmp_path):
    editor, client, _, bot_dir = _mk_editor(tmp_path)
    client.edit_post.return_value = {"id": 1304, "version": 2}
    editor.apply_edit(
        post_id=1304, new_raw="v1", edit_reason="r1", current_raw="orig",
        edit_type="format", change_summary="c1", triggered_by_post=2,
    )
    editor.apply_edit(
        post_id=1304, new_raw="v2", edit_reason="r2", current_raw="v1",
        edit_type="factual", change_summary="c2", triggered_by_post=3,
    )

    import json
    state = json.loads((bot_dir / "reports" / "r1" / "state.json").read_text("utf-8"))
    assert len(state["metadata"]["edit_history"]) == 2
