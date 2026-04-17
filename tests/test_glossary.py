"""Tests for Glossary auto-upsert and filters."""

from __future__ import annotations

from pathlib import Path

import pytest

from glossary import (
    AUTO_BEGIN,
    AUTO_END,
    MANUAL_BEGIN,
    MANUAL_END,
    GlossaryManager,
    is_candidate_term,
)


def test_stopwords_rejected():
    assert not is_candidate_term("the", count=9999)
    assert not is_candidate_term("And", count=9999)
    assert not is_candidate_term("  ", count=5)


def test_short_terms_need_higher_count():
    assert not is_candidate_term("AI", count=5)
    assert is_candidate_term("AI", count=15)
    assert is_candidate_term("HyperDex", count=3)
    assert not is_candidate_term("HyperDex", count=2)


def test_upsert_creates_file_with_markers(tmp_vault):
    gm = GlossaryManager(tmp_vault)
    gm.upsert(
        "HyperDex",
        count=42,
        breakdown={"reports": 30, "knowledge": 12},
        sample_context="HyperDex 컴파일러의 정적 매핑...",
        first_seen="2026-04-17",
    )

    content = (tmp_vault / "knowledge" / "glossary.md").read_text("utf-8")
    assert AUTO_BEGIN in content and AUTO_END in content
    assert MANUAL_BEGIN in content and MANUAL_END in content
    assert "HyperDex" in content
    assert "42" in content


def test_upsert_updates_existing_term(tmp_vault):
    gm = GlossaryManager(tmp_vault)
    gm.upsert("HyperDex", count=10, breakdown={"reports": 10}, sample_context="x", first_seen="2026-04-17")
    gm.upsert("HyperDex", count=11, breakdown={"reports": 11}, sample_context="y", first_seen="2026-04-17")

    content = (tmp_vault / "knowledge" / "glossary.md").read_text("utf-8")
    assert content.count("## HyperDex") == 1
    auto_block = content.split(AUTO_END)[0]
    assert "11" in auto_block


def test_manual_block_preserved(tmp_vault):
    path = tmp_vault / "knowledge" / "glossary.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"""# glossary
{AUTO_BEGIN}
{AUTO_END}

{MANUAL_BEGIN}
## ManualTerm
Human-maintained description.
{MANUAL_END}
""",
        encoding="utf-8",
    )

    gm = GlossaryManager(tmp_vault)
    gm.upsert("HyperDex", count=5, breakdown={}, sample_context="s", first_seen="2026-04-17")

    content = path.read_text("utf-8")
    assert "Human-maintained description." in content
    assert "HyperDex" in content


def test_auto_never_overwrites_manual_term(tmp_vault):
    path = tmp_vault / "knowledge" / "glossary.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"""{AUTO_BEGIN}
{AUTO_END}

{MANUAL_BEGIN}
## HyperDex
Manually curated.
{MANUAL_END}
""",
        encoding="utf-8",
    )

    gm = GlossaryManager(tmp_vault)
    gm.upsert("HyperDex", count=5, breakdown={}, sample_context="s", first_seen="2026-04-17")

    auto_block = path.read_text("utf-8").split(AUTO_BEGIN)[1].split(AUTO_END)[0]
    assert "HyperDex" not in auto_block


def test_load_auto_block_returns_content(tmp_vault):
    gm = GlossaryManager(tmp_vault)
    gm.upsert("HyperDex", count=5, breakdown={"reports": 5}, sample_context="s", first_seen="2026-04-17")
    gm.upsert("LPU", count=8, breakdown={"reports": 8}, sample_context="s2", first_seen="2026-04-17")

    text = gm.load_auto_text(max_entries=10)
    assert "HyperDex" in text
    assert "LPU" in text
    assert AUTO_BEGIN not in text


def test_load_auto_respects_entry_cap(tmp_vault):
    gm = GlossaryManager(tmp_vault)
    for i in range(5):
        gm.upsert(f"Term{i}", count=10, breakdown={}, sample_context="s", first_seen="2026-04-17")

    text = gm.load_auto_text(max_entries=2)
    assert text.count("## ") == 2


def test_grep_counts_occurrences_across_vault(tmp_vault):
    """Grep should count matches in knowledge/, context/, reports/."""
    (tmp_vault / "context" / "hw.md").write_text("HyperDex compiler. HyperDex scheduler.", "utf-8")
    (tmp_vault / "knowledge" / "topics" / "x.md").write_text("HyperDex notes", "utf-8")
    r = tmp_vault / "reports" / "001_x" / "researcher"
    r.mkdir(parents=True)
    (r / "report_final.md").write_text("## HyperDex\n\nSomething.", "utf-8")

    gm = GlossaryManager(tmp_vault)
    count, breakdown, sample = gm.grep_vault("HyperDex")

    assert count >= 3
    assert breakdown.get("context", 0) >= 1
    assert breakdown.get("reports", 0) >= 1
    assert breakdown.get("knowledge", 0) >= 1
    assert "HyperDex" in sample


def test_refresh_upserts_qualifying_candidates_and_skips_noise(tmp_vault):
    (tmp_vault / "context" / "a.md").write_text("HyperDex " * 5, "utf-8")
    (tmp_vault / "context" / "b.md").write_text("cat " * 20, "utf-8")

    gm = GlossaryManager(tmp_vault)
    gm.refresh_candidates({"HyperDex", "cat", "the", "xx"})

    content = (tmp_vault / "knowledge" / "glossary.md").read_text("utf-8")
    assert "HyperDex" in content
    assert "## the" not in content
    assert "## xx" not in content
    assert "## cat" in content
