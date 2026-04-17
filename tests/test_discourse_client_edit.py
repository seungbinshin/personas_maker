"""Tests for DiscourseClient.edit_post."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from discourse_client import DiscourseClient


def test_edit_post_issues_put_with_reason():
    client = DiscourseClient("https://example.com", "k", "bot")
    fake_resp = MagicMock()
    fake_resp.json.return_value = {"id": 1304, "version": 2}
    fake_resp.raise_for_status = MagicMock()

    with patch("discourse_client.requests.put", return_value=fake_resp) as mocked:
        result = client.edit_post(1304, "new raw body", "댓글 반영")

    mocked.assert_called_once()
    args, kwargs = mocked.call_args
    assert args[0] == "https://example.com/posts/1304.json"
    assert kwargs["json"] == {
        "post": {"raw": "new raw body", "edit_reason": "댓글 반영"},
    }
    assert kwargs["headers"]["Api-Key"] == "k"
    assert result == {"id": 1304, "version": 2}


def test_edit_post_requires_edit_reason():
    client = DiscourseClient("https://example.com", "k", "bot")
    with pytest.raises(ValueError, match="edit_reason"):
        client.edit_post(1304, "raw", "")


def test_edit_post_rejects_whitespace_only_reason():
    client = DiscourseClient("https://example.com", "k", "bot")
    with pytest.raises(ValueError, match="edit_reason"):
        client.edit_post(1304, "raw", "   ")
