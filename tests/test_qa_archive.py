"""Tests for QA archive writer."""

from __future__ import annotations

from pathlib import Path

from qa_archive import QAArchiver


def _make_archiver(tmp_vault):
    return QAArchiver(tmp_vault)


def test_archive_creates_file_with_frontmatter(tmp_vault):
    a = _make_archiver(tmp_vault)
    path = a.archive(
        topic_info={
            "topic_id": 291,
            "topic_url": "https://example.com/t/x/291",
            "report_id": "010_turboquant-kv-cache-compression",
            "report_title": "TurboQuant: 근최적 벡터 양자화",
        },
        post_number=2,
        commenter="jaewon_lim",
        comment_type="correction",
        comment_text="LaTeX 렌더링이 깨졌습니다",
        reply_text="지적 감사합니다. 수정하였습니다.",
        sources=["https://arxiv.org/abs/2504.19874"],
        published_at_iso="2026-04-17T12:41:42",
    )

    assert path.exists()
    assert path.parent == tmp_vault / "knowledge" / "topics" / "qa"

    content = path.read_text("utf-8")
    assert content.startswith("---\n")
    assert "source_topic_id: 291" in content
    assert "source_post_number: 2" in content
    assert "commenter: jaewon_lim" in content
    assert "comment_type: correction" in content
    assert "LaTeX 렌더링이 깨졌습니다" in content
    assert "지적 감사합니다" in content
    assert "https://arxiv.org/abs/2504.19874" in content
    assert "TurboQuant" in content


def test_archive_filename_format(tmp_vault):
    a = _make_archiver(tmp_vault)
    path = a.archive(
        topic_info={
            "topic_id": 291,
            "topic_url": "",
            "report_id": "010_turboquant-kv-cache-compression",
            "report_title": "TurboQuant: 근최적 벡터 양자화",
        },
        post_number=7,
        commenter="user",
        comment_type="question",
        comment_text="q",
        reply_text="r",
        sources=[],
        published_at_iso="2026-04-17T12:41:42",
    )
    assert path.name.startswith("2026-04-17-")
    assert path.name.endswith("-post7.md")
    assert "turboquant" in path.name.lower()


def test_archive_overwrites_same_post(tmp_vault):
    a = _make_archiver(tmp_vault)
    info = {"topic_id": 1, "topic_url": "", "report_id": "r", "report_title": "title"}
    p1 = a.archive(info, 1, "u", "question", "q1", "r1", [], "2026-04-17T12:00:00")
    p2 = a.archive(info, 1, "u", "question", "q2", "r2", [], "2026-04-17T13:00:00")
    assert p1 == p2
    assert "r2" in p1.read_text("utf-8")


def test_archive_truncates_huge_reply(tmp_vault):
    a = _make_archiver(tmp_vault)
    huge = "x" * 5000
    p = a.archive(
        topic_info={"topic_id": 1, "topic_url": "", "report_id": "r", "report_title": "t"},
        post_number=1, commenter="u", comment_type="question",
        comment_text="q", reply_text=huge, sources=[],
        published_at_iso="2026-04-17T12:00:00",
    )
    body = p.read_text("utf-8")
    assert "..." in body
