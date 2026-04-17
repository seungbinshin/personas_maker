# Discourse Engagement v2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend `DiscourseEngagement` to verify draft-introduced terms against the vault, auto-grow a glossary of internal terms, archive approved Q&A, and safety-edit its own published posts in response to corrections.

**Architecture:** Keep orchestration in `DiscourseEngagement`. Extract three new cohesive modules (`glossary.py`, `qa_archive.py`, `post_editor.py`) with clear single responsibilities. Add four prompts, rewrite one. Introduce a thin pytest suite for pure logic in the new modules.

**Tech Stack:** Python 3.14, `requests`, `pytest` (new dev dep), existing project pattern (script-style modules, no framework). Tests run with `.venv/bin/pytest`.

**Spec:** `docs/superpowers/specs/2026-04-17-discourse-engagement-v2-design.md`

---

### Task 0: Test infrastructure setup

**Files:**
- Create: `tests/__init__.py`
- Create: `tests/conftest.py`
- Create: `pytest.ini`

- [ ] **Step 1: Install pytest in the venv**

Run:
```bash
.venv/bin/pip install pytest pytest-mock
```
Expected: success messages. No network errors.

- [ ] **Step 2: Create pytest.ini at project root**

Create `/Users/shinseungbin/workspace/work/persona/pytest.ini`:

```ini
[pytest]
testpaths = tests
pythonpath = . src
addopts = -v --tb=short
```

- [ ] **Step 3: Create empty tests package**

Create `/Users/shinseungbin/workspace/work/persona/tests/__init__.py` (empty file).

- [ ] **Step 4: Create conftest with shared fixtures**

Create `/Users/shinseungbin/workspace/work/persona/tests/conftest.py`:

```python
"""Shared pytest fixtures."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "src"))


@pytest.fixture
def tmp_vault(tmp_path):
    """Minimal research bot vault layout for tests."""
    vault = tmp_path / "bot_dir"
    (vault / "knowledge").mkdir(parents=True)
    (vault / "knowledge" / "topics").mkdir()
    (vault / "reports").mkdir()
    (vault / "context").mkdir()
    return vault
```

- [ ] **Step 5: Smoke-check pytest runs**

Create a throwaway test `tests/test_smoke.py`:

```python
def test_pytest_works():
    assert 1 + 1 == 2
```

Run: `.venv/bin/pytest tests/test_smoke.py`
Expected: 1 passed.

Delete `tests/test_smoke.py` after.

- [ ] **Step 6: Commit**

```bash
git add pytest.ini tests/__init__.py tests/conftest.py
git commit -m "test: set up pytest with shared vault fixture"
```

---

### Task 1: Rewrite FACT_CHECK_PROMPT (Part C)

**Files:**
- Modify: `prompts/discourse_engagement.py` (FACT_CHECK_PROMPT)

- [ ] **Step 1: Replace FACT_CHECK_PROMPT**

Open `/Users/shinseungbin/workspace/work/persona/prompts/discourse_engagement.py` and replace the entire `FACT_CHECK_PROMPT = """..."""` block with:

```python
FACT_CHECK_PROMPT = """You are a fact-checker reviewing a draft response before it is posted publicly on an internal Discourse forum.

The response was drafted by an AI assistant to answer a comment on a research report.

Original comment by {comment_author}:
{comment_text}

Draft response:
{draft_response}

Internal knowledge (Confluence + Discourse):
{internal_context}

Verified internal-term glossary (HyperAccel-specific nouns known to be real):
{glossary}

Check the draft for:
1. **Factual accuracy**: Are the claims supported by the cited sources? Does anything contradict internal documents?
2. **Source quality**: Are the cited URLs real and relevant? Are any sources fabricated or questionable?
3. **Tone**: Is the response professional, helpful, and not dismissive of the commenter's point?
4. **Completeness**: Does it actually answer the question / address the correction?
5. **Overconfidence**: Does it claim certainty where uncertainty exists?

Guardrails when judging:
- Proper nouns that look like HyperAccel-specific internal components (HyperDex, LPU, SMA, MPU, VPU, LMU, ESL, BERTHA, HyperAccel, etc.) are likely REAL even if missing from internal_context. Do NOT declare them fabricated. If you still suspect misuse, use decision="revise" with guidance "cite or clarify this term" — never reject on this basis alone.
- External URLs that you cannot personally verify are "unverified", not "fabricated". If questionable, use decision="revise" with guidance "replace with a verifiable source (arXiv ID or DOI preferred) or remove".
- Claims of conference acceptance ("accepted at ICLR 2026" etc.) that you cannot confirm must be softened, not rejected outright. Use decision="revise" with guidance "describe as arXiv preprint unless acceptance is confirmed".
- Reserve decision="reject" for drafts that are unsafe to post even after revision — factually dangerous, clearly hostile, or off-topic. Everything else should be "approve" or "revise".

Return ONLY valid JSON:
{{
  "decision": "approve|revise|reject",
  "issues": [
    "Specific issue found (empty array if none)"
  ],
  "revision_guidance": "What to fix if decision is revise (empty string if approve)",
  "reason": "Overall assessment in Korean (1-2 lines)"
}}
"""
```

- [ ] **Step 2: Temporarily pass empty glossary**

Open `/Users/shinseungbin/workspace/work/persona/src/discourse_engagement.py`.

Find the `_fact_check` method (around line 288) and update its body to pass an empty `glossary` placeholder until Task 3 wires the real loader. Replace:

```python
    def _fact_check(
        self, comment_author: str, comment_text: str, draft: str, internal_context: str,
    ) -> dict | None:
        prompt = FACT_CHECK_PROMPT.format(
            comment_author=comment_author,
            comment_text=comment_text,
            draft_response=draft,
            internal_context=internal_context or "(내부 문서 없음)",
        )
```

with:

```python
    def _fact_check(
        self, comment_author: str, comment_text: str, draft: str, internal_context: str,
    ) -> dict | None:
        prompt = FACT_CHECK_PROMPT.format(
            comment_author=comment_author,
            comment_text=comment_text,
            draft_response=draft,
            internal_context=internal_context or "(내부 문서 없음)",
            glossary=self._load_glossary_text(),
        )
```

Add this helper method to the same class (place it in the `# ── Helpers ───────────────────────────────────────────────────` section):

```python
    def _load_glossary_text(self) -> str:
        """Return auto-block of the glossary, or an empty placeholder if absent.

        Wired in Task 3 (glossary module). Returning "" keeps fact-check
        functional before the glossary file exists.
        """
        return "(아직 수집된 내부 용어 glossary가 없음)"
```

- [ ] **Step 3: Commit**

```bash
git add prompts/discourse_engagement.py src/discourse_engagement.py
git commit -m "feat(discourse): rewrite FACT_CHECK_PROMPT with internal-term leniency"
```

---

### Task 2: DiscourseClient.edit_post

**Files:**
- Modify: `src/discourse_client.py`
- Create: `tests/test_discourse_client_edit.py`

- [ ] **Step 1: Write failing test**

Create `/Users/shinseungbin/workspace/work/persona/tests/test_discourse_client_edit.py`:

```python
"""Tests for DiscourseClient.edit_post."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

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
    try:
        client.edit_post(1304, "raw", "")
    except ValueError as e:
        assert "edit_reason" in str(e)
    else:
        raise AssertionError("expected ValueError")
```

- [ ] **Step 2: Run test, verify it fails**

Run: `.venv/bin/pytest tests/test_discourse_client_edit.py -v`
Expected: FAIL with `AttributeError: 'DiscourseClient' object has no attribute 'edit_post'`.

- [ ] **Step 3: Implement edit_post**

Open `/Users/shinseungbin/workspace/work/persona/src/discourse_client.py`. Locate the `_post` method (around line 74). Add a sibling `_put` method right below it:

```python
    def _put(self, path: str, data: dict) -> dict:
        url = f"{self.base_url}/{path.lstrip('/')}"
        resp = requests.put(url, headers=self.headers, json=data, timeout=30)
        resp.raise_for_status()
        return resp.json()
```

Then add `edit_post` below `create_reply` (around line 124):

```python
    def edit_post(self, post_id: int, raw: str, edit_reason: str) -> dict:
        """Edit an existing post. edit_reason is shown in Discourse's edit history."""
        if not edit_reason.strip():
            raise ValueError("edit_reason is required by Discourse and by the audit trail")
        result = self._put(
            f"/posts/{post_id}.json",
            {"post": {"raw": raw, "edit_reason": edit_reason}},
        )
        logger.info("Edited post: post_id=%s reason='%s'", post_id, edit_reason)
        return result
```

- [ ] **Step 4: Run test, verify pass**

Run: `.venv/bin/pytest tests/test_discourse_client_edit.py -v`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add src/discourse_client.py tests/test_discourse_client_edit.py
git commit -m "feat(discourse): add DiscourseClient.edit_post with audit-required reason"
```

---

### Task 3: Glossary module — core logic + file I/O

**Files:**
- Create: `src/glossary.py`
- Create: `tests/test_glossary.py`

- [ ] **Step 1: Write failing tests covering filters and upsert**

Create `/Users/shinseungbin/workspace/work/persona/tests/test_glossary.py`:

```python
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
    # Only one HyperDex heading
    assert content.count("## HyperDex") == 1
    assert "11" in content
    assert "10" not in content.split(AUTO_END)[0] or content.split(AUTO_END)[0].count("11") >= 1


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
    assert AUTO_BEGIN not in text  # markers stripped


def test_load_auto_respects_entry_cap(tmp_vault):
    gm = GlossaryManager(tmp_vault)
    for i in range(5):
        gm.upsert(f"Term{i}", count=10, breakdown={}, sample_context="s", first_seen="2026-04-17")

    text = gm.load_auto_text(max_entries=2)
    assert text.count("## ") == 2
```

- [ ] **Step 2: Run tests, verify they fail**

Run: `.venv/bin/pytest tests/test_glossary.py -v`
Expected: ImportError or all tests fail.

- [ ] **Step 3: Implement glossary module**

Create `/Users/shinseungbin/workspace/work/persona/src/glossary.py`:

```python
"""Auto-maintained glossary of HyperAccel-internal terms.

The glossary is a single markdown file with an auto-maintained block and
a manual block. Only the auto block is rewritten by this module; manual
entries are always preserved and take precedence (auto upsert skips any
term that already appears in the manual block).
"""

from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass
from datetime import date
from pathlib import Path

logger = logging.getLogger(__name__)

AUTO_BEGIN = "<!-- BEGIN-AUTO -->"
AUTO_END = "<!-- END-AUTO -->"
MANUAL_BEGIN = "<!-- BEGIN-MANUAL -->"
MANUAL_END = "<!-- END-MANUAL -->"

STOPWORDS = {
    "a", "an", "and", "the", "or", "but", "if", "of", "in", "on", "at",
    "to", "for", "by", "with", "from", "as", "is", "are", "was", "were",
    "be", "been", "being", "has", "have", "had", "do", "does", "did",
    "this", "that", "these", "those", "it", "its", "they", "them",
    "what", "which", "who", "how", "why", "when", "where",
    "not", "no", "yes", "also", "than", "then", "so", "such",
    "will", "would", "should", "could", "may", "might",
    # markdown / prose tokens
    "md", "html", "json", "eg", "ie", "etc",
}

SHORT_TERM_MIN_COUNT = 10
DEFAULT_MIN_COUNT = 3


def is_candidate_term(term: str, count: int) -> bool:
    t = term.strip()
    if not t:
        return False
    if t.lower() in STOPWORDS:
        return False
    if len(t) <= 2:
        return count >= SHORT_TERM_MIN_COUNT
    return count >= DEFAULT_MIN_COUNT


@dataclass
class GlossaryEntry:
    term: str
    count: int
    breakdown: dict[str, int]
    sample_context: str
    first_seen: str

    def render(self) -> str:
        breakdown_str = ", ".join(f"{k}: {v}" for k, v in sorted(self.breakdown.items()))
        safe_sample = (self.sample_context or "").strip().replace("\n", " ")
        if len(safe_sample) > 200:
            safe_sample = safe_sample[:200] + "..."
        return (
            f"## {self.term}\n"
            f"- **Occurrences**: {self.count}"
            + (f" ({breakdown_str})" if breakdown_str else "")
            + "\n"
            f"- **First seen**: {self.first_seen}\n"
            f"- **Sample context**: {safe_sample}\n"
        )


class GlossaryManager:
    """Reads and writes the auto block of `knowledge/glossary.md`."""

    def __init__(self, bot_dir: str | Path):
        self.bot_dir = Path(bot_dir)
        self.path = self.bot_dir / "knowledge" / "glossary.md"

    def upsert(
        self,
        term: str,
        count: int,
        breakdown: dict[str, int],
        sample_context: str,
        first_seen: str,
    ) -> None:
        """Insert or update a term in the auto block. Skips if term is in manual block."""
        auto_block, manual_block = self._read_blocks()

        if self._term_in_block(manual_block, term):
            logger.debug("Skipping '%s' — already present in manual block", term)
            return

        entries = self._parse_entries(auto_block)
        entries[term] = GlossaryEntry(
            term=term,
            count=count,
            breakdown=breakdown,
            sample_context=sample_context,
            first_seen=first_seen,
        )

        self._write(entries, manual_block)

    def load_auto_text(self, max_entries: int = 50) -> str:
        """Return the auto block content (sans markers), capped at max_entries."""
        if not self.path.exists():
            return ""
        auto_block, _ = self._read_blocks()
        entries = self._parse_entries(auto_block)
        if not entries:
            return ""
        # Sort by count desc, then term asc, take top N
        sorted_terms = sorted(
            entries.values(),
            key=lambda e: (-e.count, e.term.lower()),
        )[:max_entries]
        return "\n".join(e.render() for e in sorted_terms).strip()

    # ── Internal helpers ──

    def _read_blocks(self) -> tuple[str, str]:
        """Return (auto_block_content, manual_block_content). Creates file if missing."""
        if not self.path.exists():
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(self._scaffold(), encoding="utf-8")
        text = self.path.read_text("utf-8")
        auto = self._extract_between(text, AUTO_BEGIN, AUTO_END)
        manual = self._extract_between(text, MANUAL_BEGIN, MANUAL_END)
        return auto, manual

    @staticmethod
    def _extract_between(text: str, start: str, end: str) -> str:
        try:
            s = text.index(start) + len(start)
            e = text.index(end, s)
            return text[s:e].strip()
        except ValueError:
            return ""

    @staticmethod
    def _scaffold() -> str:
        return (
            "# HyperAccel 내부 용어 glossary\n\n"
            "<!-- auto-maintained by discourse_engagement — do not edit between markers -->\n"
            f"{AUTO_BEGIN}\n{AUTO_END}\n\n"
            f"{MANUAL_BEGIN}\n{MANUAL_END}\n"
        )

    @staticmethod
    def _term_in_block(block: str, term: str) -> bool:
        pattern = re.compile(rf"^##\s+{re.escape(term)}\s*$", re.MULTILINE)
        return bool(pattern.search(block))

    @staticmethod
    def _parse_entries(block: str) -> dict[str, GlossaryEntry]:
        """Parse existing auto block back into entries. Keeps unknown fields best-effort."""
        entries: dict[str, GlossaryEntry] = {}
        if not block.strip():
            return entries
        sections = re.split(r"^## ", block, flags=re.MULTILINE)
        for sect in sections:
            sect = sect.strip()
            if not sect:
                continue
            lines = sect.splitlines()
            term = lines[0].strip()
            count_match = re.search(r"\*\*Occurrences\*\*:\s*(\d+)", sect)
            first_match = re.search(r"\*\*First seen\*\*:\s*(\S+)", sect)
            sample_match = re.search(r"\*\*Sample context\*\*:\s*(.+)", sect)
            breakdown: dict[str, int] = {}
            if count_match:
                paren = re.search(r"\((.+)\)", count_match.group(0))
                if paren:
                    for part in paren.group(1).split(","):
                        if ":" in part:
                            k, v = part.split(":", 1)
                            try:
                                breakdown[k.strip()] = int(v.strip())
                            except ValueError:
                                pass
            entries[term] = GlossaryEntry(
                term=term,
                count=int(count_match.group(1)) if count_match else 0,
                breakdown=breakdown,
                sample_context=sample_match.group(1).strip() if sample_match else "",
                first_seen=first_match.group(1).strip() if first_match else str(date.today()),
            )
        return entries

    def _write(self, entries: dict[str, GlossaryEntry], manual_block: str) -> None:
        sorted_entries = sorted(
            entries.values(),
            key=lambda e: (-e.count, e.term.lower()),
        )
        auto_body = "\n".join(e.render() for e in sorted_entries).rstrip()

        text = (
            "# HyperAccel 내부 용어 glossary\n\n"
            "<!-- auto-maintained by discourse_engagement — do not edit between markers -->\n"
            f"{AUTO_BEGIN}\n"
            f"{auto_body}\n"
            f"{AUTO_END}\n\n"
            f"{MANUAL_BEGIN}\n"
            f"{manual_block.strip()}\n"
            f"{MANUAL_END}\n"
        )
        self.path.write_text(text, encoding="utf-8")
```

- [ ] **Step 4: Run tests, verify pass**

Run: `.venv/bin/pytest tests/test_glossary.py -v`
Expected: all 7 tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/glossary.py tests/test_glossary.py
git commit -m "feat(glossary): auto-maintained internal-term glossary with manual-block safety"
```

---

### Task 4: Glossary — vault grep + candidate refresh

**Files:**
- Modify: `src/glossary.py`
- Modify: `tests/test_glossary.py`

- [ ] **Step 1: Add failing test for vault grep and refresh flow**

Append to `/Users/shinseungbin/workspace/work/persona/tests/test_glossary.py`:

```python
def test_grep_counts_occurrences_across_vault(tmp_vault):
    """Grep should count matches in knowledge/, context/, reports/."""
    (tmp_vault / "context" / "hw.md").write_text("HyperDex compiler. HyperDex scheduler.", "utf-8")
    (tmp_vault / "knowledge" / "topics" / "x.md").write_text("HyperDex notes", "utf-8")
    (tmp_vault / "reports").mkdir(exist_ok=True)
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
    assert "HyperDex" in content  # passed filters
    # stopwords & absent short acronyms filtered
    assert "## the" not in content
    assert "## xx" not in content
    # 'cat' lowercases: short but not stopword — check count filter
    # 'cat' count=20 and len=3, so passes DEFAULT_MIN_COUNT=3; expected present
    assert "## cat" in content
```

- [ ] **Step 2: Run, verify they fail**

Run: `.venv/bin/pytest tests/test_glossary.py -v`
Expected: 2 new FAIL (`AttributeError: ... 'grep_vault'`, `'refresh_candidates'`).

- [ ] **Step 3: Implement grep and refresh**

Add imports near top of `/Users/shinseungbin/workspace/work/persona/src/glossary.py` (import `shutil` for `rg` lookup):

```python
import shutil
```

Append to the `GlossaryManager` class (inside the class, below `load_auto_text`):

```python
    def grep_vault(self, term: str) -> tuple[int, dict[str, int], str]:
        """Return (total_count, per-area breakdown, sample context line) for term.

        Areas: context, knowledge, reports (first researcher/ subdir).
        """
        areas = {
            "context": self.bot_dir / "context",
            "knowledge": self.bot_dir / "knowledge",
            "reports": self.bot_dir / "reports",
        }
        total = 0
        breakdown: dict[str, int] = {}
        sample: str = ""
        for area, area_path in areas.items():
            if not area_path.exists():
                continue
            count, first_hit = self._count_in_dir(area_path, term)
            if count:
                breakdown[area] = count
                total += count
                if not sample and first_hit:
                    sample = first_hit
        return total, breakdown, sample

    @staticmethod
    def _count_in_dir(root: Path, term: str) -> tuple[int, str]:
        """Count occurrences of term inside .md files under root. Return (count, first_match_line)."""
        rg = shutil.which("rg")
        if rg:
            try:
                proc = subprocess.run(
                    [rg, "--no-messages", "-F", "-c", term, "--glob", "*.md", str(root)],
                    capture_output=True, text=True, timeout=30, check=False,
                )
                if proc.returncode not in (0, 1):
                    logger.warning("rg failed for %s: %s", term, proc.stderr[:200])
                    return 0, ""
                total = 0
                for line in proc.stdout.splitlines():
                    if ":" in line:
                        try:
                            total += int(line.rsplit(":", 1)[1])
                        except ValueError:
                            pass
                # Sample: first line
                sample = ""
                sample_proc = subprocess.run(
                    [rg, "--no-messages", "-F", "-N", "-m", "1", term, "--glob", "*.md", str(root)],
                    capture_output=True, text=True, timeout=30, check=False,
                )
                if sample_proc.returncode == 0:
                    for line in sample_proc.stdout.splitlines():
                        if ":" in line:
                            sample = line.split(":", 1)[1].strip()
                            break
                return total, sample
            except (subprocess.TimeoutExpired, FileNotFoundError) as e:
                logger.warning("rg invocation failed: %s", e)

        # Fallback: pure Python
        total = 0
        sample = ""
        for md in root.rglob("*.md"):
            try:
                text = md.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            hits = text.count(term)
            if hits:
                total += hits
                if not sample:
                    for line in text.splitlines():
                        if term in line:
                            sample = line.strip()
                            break
        return total, sample

    def refresh_candidates(self, candidates: set[str]) -> None:
        """Verify each candidate via grep and upsert those passing filters."""
        today = str(date.today())
        for term in candidates:
            try:
                count, breakdown, sample = self.grep_vault(term)
            except Exception as e:
                logger.warning("grep_vault failed for '%s': %s", term, e)
                continue
            if is_candidate_term(term, count):
                self.upsert(
                    term=term,
                    count=count,
                    breakdown=breakdown,
                    sample_context=sample,
                    first_seen=today,
                )
```

- [ ] **Step 4: Run, verify all glossary tests pass**

Run: `.venv/bin/pytest tests/test_glossary.py -v`
Expected: all 9 tests pass.

- [ ] **Step 5: Add seed CLI entry point**

Append at the bottom of `/Users/shinseungbin/workspace/work/persona/src/glossary.py`:

```python
def _seed_from_vault(bot_dir: Path) -> int:
    """One-shot pass over the vault: extract candidate proper nouns and upsert."""
    candidates: set[str] = set()
    areas = [bot_dir / "context", bot_dir / "knowledge", bot_dir / "reports"]
    # Match CamelCase words of length 3+, and ALL-CAPS acronyms of length 2+
    pattern = re.compile(r"\b([A-Z][A-Za-z]{2,}|[A-Z]{2,})\b")
    for area in areas:
        if not area.exists():
            continue
        for md in area.rglob("*.md"):
            try:
                text = md.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            for match in pattern.findall(text):
                candidates.add(match)
    logger.info("Seed: %d candidate terms found", len(candidates))

    gm = GlossaryManager(bot_dir)
    gm.refresh_candidates(candidates)
    return len(candidates)


if __name__ == "__main__":
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("command", choices=["seed"])
    ap.add_argument("--bot-dir", required=True, type=Path)
    args = ap.parse_args()
    if args.command == "seed":
        n = _seed_from_vault(args.bot_dir)
        print(f"Seeded glossary from {n} candidates → {args.bot_dir}/knowledge/glossary.md")
```

- [ ] **Step 6: Run seed against real vault (side-effect: creates glossary.md)**

Run:
```bash
.venv/bin/python -m src.glossary seed --bot-dir bots/research
```
Expected: `Seeded glossary from N candidates → bots/research/knowledge/glossary.md`.

Spot-check:
```bash
head -40 bots/research/knowledge/glossary.md
```
Expected: contains `HyperDex`, `LPU` near the top (highest counts).

- [ ] **Step 7: Wire glossary into fact-check**

Open `/Users/shinseungbin/workspace/work/persona/src/discourse_engagement.py`. Add at the top, with other imports:

```python
from glossary import GlossaryManager
```

In `__init__`, add a parameter and attribute. Locate the `__init__` signature (around line 41) and change:

```python
    def __init__(
        self,
        discourse_client: DiscourseClient,
        publisher: DiscoursePublisher,
        runtime: ClaudeRuntimeClient,
        confluence_knowledge: ConfluenceKnowledge,
        discourse_knowledge: DiscourseKnowledge,
        scope_text: str = "",
        slack_callback: callable | None = None,
    ):
```

to:

```python
    def __init__(
        self,
        discourse_client: DiscourseClient,
        publisher: DiscoursePublisher,
        runtime: ClaudeRuntimeClient,
        confluence_knowledge: ConfluenceKnowledge,
        discourse_knowledge: DiscourseKnowledge,
        glossary: GlossaryManager,
        scope_text: str = "",
        slack_callback: callable | None = None,
    ):
```

Inside the constructor body, add the assignment (place after the other self.X = X assignments):

```python
        self.glossary = glossary
```

Replace the placeholder `_load_glossary_text` added in Task 1:

```python
    def _load_glossary_text(self) -> str:
        text = self.glossary.load_auto_text(max_entries=50)
        return text if text else "(아직 수집된 내부 용어 glossary가 없음)"
```

- [ ] **Step 8: Update the pipeline wiring**

Open `/Users/shinseungbin/workspace/work/persona/src/pipelines/research_pipeline.py`. Add import near the other imports:

```python
from glossary import GlossaryManager
```

Locate the `DiscourseEngagement(...)` construction (around line 137). Add the glossary argument. Before the call, add:

```python
            glossary_mgr = GlossaryManager(bot_dir)
```

Then in the `DiscourseEngagement(...)` call, add `glossary=glossary_mgr,` as an argument.

- [ ] **Step 9: Commit**

```bash
git add src/glossary.py tests/test_glossary.py src/discourse_engagement.py src/pipelines/research_pipeline.py bots/research/knowledge/glossary.md
git commit -m "feat(glossary): vault grep, candidate refresh, seed CLI, and fact-check wiring"
```

---

### Task 5: Q&A archive module

**Files:**
- Create: `src/qa_archive.py`
- Create: `tests/test_qa_archive.py`

- [ ] **Step 1: Write failing tests**

Create `/Users/shinseungbin/workspace/work/persona/tests/test_qa_archive.py`:

```python
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
    # Reply section body should be truncated
    assert "..." in body or len(body) < 3000
```

- [ ] **Step 2: Run, verify fails**

Run: `.venv/bin/pytest tests/test_qa_archive.py -v`
Expected: ImportError or all 4 fail.

- [ ] **Step 3: Implement qa_archive module**

Create `/Users/shinseungbin/workspace/work/persona/src/qa_archive.py`:

```python
"""Archive approved Discourse Q&A into the research knowledge base.

Writes one markdown file per successfully-answered comment to
`<bot_dir>/knowledge/topics/qa/YYYY-MM-DD-<slug>-post<N>.md`.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

REPLY_MAX_CHARS = 1500


class QAArchiver:
    """Writes Q&A markdown files. One file per (topic, post_number)."""

    def __init__(self, bot_dir: str | Path):
        self.bot_dir = Path(bot_dir)
        self.qa_dir = self.bot_dir / "knowledge" / "topics" / "qa"

    def archive(
        self,
        topic_info: dict,
        post_number: int,
        commenter: str,
        comment_type: str,
        comment_text: str,
        reply_text: str,
        sources: list[str],
        published_at_iso: str,
    ) -> Path:
        self.qa_dir.mkdir(parents=True, exist_ok=True)

        date_prefix = published_at_iso[:10]  # YYYY-MM-DD
        slug = self._slugify(topic_info.get("report_title", "") or str(topic_info.get("topic_id", "unknown")))
        fname = f"{date_prefix}-{slug}-post{post_number}.md"
        path = self.qa_dir / fname

        truncated_reply = reply_text if len(reply_text) <= REPLY_MAX_CHARS else reply_text[:REPLY_MAX_CHARS] + "..."

        frontmatter = "\n".join([
            "---",
            f"source_topic_id: {topic_info.get('topic_id', '')}",
            f"source_topic_url: {topic_info.get('topic_url', '')}",
            f"source_post_number: {post_number}",
            f"report_id: {topic_info.get('report_id', '')}",
            f"commenter: {commenter}",
            f"comment_type: {comment_type}",
            f"published_at: {published_at_iso}",
            "---",
            "",
        ])

        title_line = self._question_title(comment_text, topic_info.get("report_title", ""))
        sources_block = "\n".join(f"- {u}" for u in sources) if sources else "- (외부 출처 없음)"
        report_link = self._report_link(topic_info.get("report_id", ""), topic_info.get("report_title", ""))

        body = f"""# Q: {title_line}

## 원본 댓글
> {comment_text.strip()}

## 답변 요약
{truncated_reply.strip()}

## 참고 자료
{sources_block}

## 관련 리포트
{report_link}
"""

        path.write_text(frontmatter + body, encoding="utf-8")
        logger.info("Archived QA: %s", path)
        return path

    @staticmethod
    def _slugify(text: str) -> str:
        text = text.lower().strip()
        text = re.sub(r"[^a-z0-9가-힣\s-]", "", text)
        text = re.sub(r"\s+", "-", text)
        return (text[:60] or "unknown").strip("-")

    @staticmethod
    def _question_title(comment_text: str, report_title: str) -> str:
        snippet = comment_text.strip().splitlines()[0] if comment_text.strip() else ""
        snippet = re.sub(r"\s+", " ", snippet)
        if len(snippet) > 80:
            snippet = snippet[:80] + "..."
        return snippet or f"{report_title} 관련 질문"

    @staticmethod
    def _report_link(report_id: str, report_title: str) -> str:
        if not report_id:
            return "- (해당 없음)"
        return f"- [{report_title or report_id}](../../../reports/{report_id}/researcher/report_final.md)"
```

- [ ] **Step 4: Run, verify pass**

Run: `.venv/bin/pytest tests/test_qa_archive.py -v`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add src/qa_archive.py tests/test_qa_archive.py
git commit -m "feat(qa-archive): write approved Q&A as knowledge topics under knowledge/topics/qa"
```

---

### Task 6: Post editor module (safety gates)

**Files:**
- Create: `src/post_editor.py`
- Create: `tests/test_post_editor.py`

- [ ] **Step 1: Write failing tests**

Create `/Users/shinseungbin/workspace/work/persona/tests/test_post_editor.py`:

```python
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
        '{"report_id":"r1","status":"accepted","metadata":{"discourse_post_id":' + str(known_post_id) + '}}',
        encoding="utf-8",
    )
    # Dependencies
    client = MagicMock()
    publisher = MagicMock()
    publisher.get_report_for_post = MagicMock(side_effect=lambda pid: {
        "report_id": report_id, "discourse_post_id": known_post_id,
    } if pid == known_post_id else None)
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

    # Backup written
    backups = list((bot_dir / "knowledge" / "edits").glob("1304-*.md"))
    assert len(backups) == 1
    assert backups[0].read_text("utf-8") == "old body"

    # state.json has edit_history entry
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
    editor.apply_edit(post_id=1304, new_raw="v1", edit_reason="r1", current_raw="orig",
                      edit_type="format", change_summary="c1", triggered_by_post=2)
    editor.apply_edit(post_id=1304, new_raw="v2", edit_reason="r2", current_raw="v1",
                      edit_type="factual", change_summary="c2", triggered_by_post=3)

    import json
    state = json.loads((bot_dir / "reports" / "r1" / "state.json").read_text("utf-8"))
    assert len(state["metadata"]["edit_history"]) == 2
```

- [ ] **Step 2: Run, verify fails**

Run: `.venv/bin/pytest tests/test_post_editor.py -v`
Expected: ImportError; all 5 fail.

- [ ] **Step 3: Add `get_report_for_post` helper to DiscoursePublisher**

Open `/Users/shinseungbin/workspace/work/persona/src/discourse_publisher.py`. Add below `get_published_topics`:

```python
    def get_report_for_post(self, discourse_post_id: int) -> dict | None:
        """Return the report state owning the given post_id, or None."""
        for state in self.store.list_reports():
            metadata = state.get("metadata", {})
            if metadata.get("discourse_post_id") == discourse_post_id:
                return {
                    "report_id": state.get("report_id"),
                    "discourse_post_id": discourse_post_id,
                    "discourse_topic_id": metadata.get("discourse_topic_id"),
                }
        return None
```

- [ ] **Step 4: Implement post_editor module**

Create `/Users/shinseungbin/workspace/work/persona/src/post_editor.py`:

```python
"""Safety-gated editor for the bot's own Discourse posts.

All edits must:
- target a post the bot published (publisher confirms ownership),
- carry a non-empty edit_reason (audit trail in Discourse + Slack),
- be preceded by a backup of the pre-edit raw to knowledge/edits/,
- append an entry to the owning report's state.json edit_history.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)


class EditRefused(Exception):
    """Raised when a safety gate blocks an edit."""


class PostEditor:
    def __init__(self, bot_dir: str | Path, client, publisher):
        self.bot_dir = Path(bot_dir)
        self.client = client
        self.publisher = publisher
        self.edits_dir = self.bot_dir / "knowledge" / "edits"

    def apply_edit(
        self,
        post_id: int,
        new_raw: str,
        edit_reason: str,
        current_raw: str,
        edit_type: str = "format",
        change_summary: str = "",
        triggered_by_post: int = 0,
    ) -> dict:
        self._gate_ownership(post_id)
        self._gate_reason(edit_reason)
        self._gate_backup(current_raw)

        backup_path = self._write_backup(post_id, current_raw)

        try:
            result = self.client.edit_post(post_id, new_raw, edit_reason)
        except Exception as e:
            logger.error("edit_post failed: %s", e)
            raise

        self._append_edit_history(
            post_id=post_id,
            edit_type=edit_type,
            change_summary=change_summary,
            triggered_by_post=triggered_by_post,
            backup_path=backup_path,
        )

        logger.info("Applied edit to post %s (reason: %s)", post_id, edit_reason)
        return {"applied": True, "version": result.get("version"), "backup": str(backup_path)}

    # ── gates ──

    def _gate_ownership(self, post_id: int) -> None:
        info = self.publisher.get_report_for_post(post_id)
        if not info:
            raise EditRefused(f"post_id={post_id} is not a bot-owned post")

    @staticmethod
    def _gate_reason(edit_reason: str) -> None:
        if not (edit_reason or "").strip():
            raise EditRefused("edit_reason is required (empty string rejected)")

    @staticmethod
    def _gate_backup(current_raw: str) -> None:
        if not (current_raw or "").strip():
            raise EditRefused("cannot create backup — current_raw is empty")

    # ── side effects ──

    def _write_backup(self, post_id: int, current_raw: str) -> Path:
        self.edits_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%dT%H%M%S")
        path = self.edits_dir / f"{post_id}-{ts}.md"
        path.write_text(current_raw, encoding="utf-8")
        return path

    def _append_edit_history(
        self,
        post_id: int,
        edit_type: str,
        change_summary: str,
        triggered_by_post: int,
        backup_path: Path,
    ) -> None:
        info = self.publisher.get_report_for_post(post_id)
        if not info:
            return
        report_id = info["report_id"]
        state_path = self.bot_dir / "reports" / report_id / "state.json"
        if not state_path.exists():
            # Some layouts prefix with a sequence number; scan
            matches = list((self.bot_dir / "reports").glob(f"*{report_id}/state.json"))
            if matches:
                state_path = matches[0]
            else:
                logger.warning("state.json not found for %s", report_id)
                return

        state = json.loads(state_path.read_text("utf-8"))
        history = state["metadata"].get("edit_history", [])
        history.append({
            "post_id": post_id,
            "edited_at": datetime.now().isoformat(timespec="seconds"),
            "edit_type": edit_type,
            "change_summary": change_summary,
            "triggered_by_post": triggered_by_post,
            "backup_path": str(backup_path.relative_to(self.bot_dir)),
        })
        state["metadata"]["edit_history"] = history
        state["updated_at"] = datetime.now().isoformat()
        state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
```

- [ ] **Step 5: Run, verify pass**

Run: `.venv/bin/pytest tests/test_post_editor.py -v`
Expected: 5 passed.

- [ ] **Step 6: Commit**

```bash
git add src/post_editor.py src/discourse_publisher.py tests/test_post_editor.py
git commit -m "feat(post-editor): safety-gated self-edit with backup and audit trail"
```

---

### Task 7: Draft-aware context (Part A)

**Files:**
- Modify: `prompts/discourse_engagement.py`
- Modify: `src/discourse_engagement.py`

- [ ] **Step 1: Add EXTRACT_DRAFT_TERMS_PROMPT**

Open `/Users/shinseungbin/workspace/work/persona/prompts/discourse_engagement.py`. Append at the bottom:

```python
EXTRACT_DRAFT_TERMS_PROMPT = """You are extracting verification-worthy technical terms from a draft response.

Draft:
{draft}

Return 8–12 terms that could benefit from cross-referencing against internal documents or the glossary. Prefer:
- Proper nouns (internal or external product/tool names, HW/SW component names, model family names)
- Technical acronyms (≥2 uppercase letters)
- Korean compound technical terms (복합 기술 용어)

Exclude:
- Generic English words (article, preposition, verb, pronoun)
- Common, obviously-global terms (API, GPU, CPU are OK to include only if they are the draft's subject)
- Words longer than 30 characters

Return ONLY a JSON array of strings, no explanation:
["term1", "term2", ...]
"""
```

- [ ] **Step 2: Add extraction + re-gather logic**

Open `/Users/shinseungbin/workspace/work/persona/src/discourse_engagement.py`. Update imports at the top to include the new prompt:

```python
from prompts.discourse_engagement import (
    COMMENT_CLASSIFY_PROMPT,
    EXTRACT_DRAFT_TERMS_PROMPT,
    FACT_CHECK_PROMPT,
    REVISE_DRAFT_PROMPT,
    SEARCH_AND_DRAFT_PROMPT,
)
```

Find `_respond_to_comment` (around line 159) and rework its body to re-gather context after the draft. Replace the method:

```python
    def _respond_to_comment(
        self,
        topic_id: int,
        post: DiscoursePost,
        comment_type: str,
        report_title: str,
        report_excerpt: str,
        classification: dict,
    ):
        comment_text = _strip_html(post.cooked)
        key_topic = classification.get("key_topic", "")

        # Initial context from comment keywords
        comment_keywords = [w for w in re.split(r"[\s,]+", key_topic) if len(w) > 1][:10]
        initial_context = self._gather_internal_context(comment_keywords)

        # Step 2+3: Search external sources + draft response (using initial context)
        draft = self._search_and_draft(
            report_title=report_title,
            report_excerpt=report_excerpt,
            comment_author=post.username,
            comment_text=comment_text,
            comment_type=comment_type,
            internal_context=initial_context,
        )
        if not draft:
            self._notify(
                f":warning: Discourse 답변 초안 생성 실패 (topic={topic_id}, post={post.post_number})"
            )
            return

        # Step 3.5: Extract draft terms, re-gather context, merge
        draft_terms = self._extract_draft_terms(draft)
        merged_keywords = list({*comment_keywords, *draft_terms})
        # Only re-gather if we found new terms; keep initial_context otherwise
        if set(draft_terms) - set(comment_keywords):
            draft_context = self._gather_internal_context(merged_keywords)
            fact_check_context = self._merge_contexts(initial_context, draft_context)
        else:
            fact_check_context = initial_context

        # Remember candidates for end-of-poll glossary refresh
        self._accumulate_glossary_candidates(draft_terms | set(comment_keywords))

        # Step 4: Fact check (+ revise loop)
        final_response = self._fact_check_loop(
            comment_author=post.username,
            comment_text=comment_text,
            draft=draft,
            internal_context=fact_check_context,
        )
        if not final_response:
            self._notify(
                f":no_entry: Discourse 답변 reject됨 — 수동 확인 필요 "
                f"(topic={topic_id}, post=#{post.post_number} by {post.username})\n"
                f"댓글: _{comment_text[:100]}_"
            )
            return

        # Step 5: Publish reply
        try:
            self.client.create_reply(
                topic_id=topic_id,
                raw=final_response,
                reply_to_post_number=post.post_number,
            )
            self._notify(
                f":speech_balloon: Discourse 답변 게시 완료 "
                f"(topic={topic_id}, post=#{post.post_number} by {post.username})"
            )
        except Exception as e:
            logger.error("Failed to post reply: %s", e)
            self._notify(f":x: Discourse 답변 게시 실패: {e}")
```

- [ ] **Step 3: Implement helper methods on DiscourseEngagement**

Add these methods to the same class (in the `# ── Helpers ───` section):

```python
    def _extract_draft_terms(self, draft: str) -> set[str]:
        """Ask the LLM for technical terms worth verifying against the vault."""
        try:
            result = self.runtime.run(
                LLMRunRequest(
                    prompt=EXTRACT_DRAFT_TERMS_PROMPT.format(draft=draft[:4000]),
                    timeout_ms=60_000,
                )
            )
            if not result.success:
                return set()
            parsed = parse_json_response(result.output)
            if not isinstance(parsed, list):
                return set()
            return {str(t).strip() for t in parsed if isinstance(t, (str, int, float)) and str(t).strip()}
        except Exception as e:
            logger.warning("extract_draft_terms failed: %s", e)
            return set()

    def _merge_contexts(self, a: str, b: str) -> str:
        """Merge two context strings, dedup by '### <title>' headings; cap to 30KB."""
        if not a:
            return b
        if not b:
            return a
        merged = a + "\n\n" + b
        # Dedupe by heading — keep first occurrence
        seen_headings = set()
        out_lines = []
        current_heading = None
        for line in merged.splitlines():
            m = re.match(r"^###\s+(.+)$", line)
            if m:
                heading = m.group(1).strip()
                if heading in seen_headings:
                    current_heading = "__SKIP__"
                    continue
                seen_headings.add(heading)
                current_heading = heading
                out_lines.append(line)
                continue
            if current_heading == "__SKIP__":
                continue
            out_lines.append(line)
        result = "\n".join(out_lines)
        if len(result) > 30_000:
            result = result[:30_000] + "\n\n...(truncated)"
        return result

    def _accumulate_glossary_candidates(self, terms: set[str]) -> None:
        self._glossary_candidates.update(terms)
```

- [ ] **Step 4: Initialize the candidate set in `__init__`**

At the end of `DiscourseEngagement.__init__`, add:

```python
        self._glossary_candidates: set[str] = set()
```

- [ ] **Step 5: Call glossary refresh at end of poll_and_respond**

Find `poll_and_respond` (around line 66). Replace:

```python
        for topic_info in published:
            try:
                self._process_topic(topic_info)
            except Exception as e:
                logger.error(
                    "Engagement error for topic %s: %s",
                    topic_info.get("topic_id"), e, exc_info=True,
                )
```

with:

```python
        for topic_info in published:
            try:
                self._process_topic(topic_info)
            except Exception as e:
                logger.error(
                    "Engagement error for topic %s: %s",
                    topic_info.get("topic_id"), e, exc_info=True,
                )

        # Refresh glossary once per poll cycle (non-fatal if it fails)
        if self._glossary_candidates:
            try:
                self.glossary.refresh_candidates(self._glossary_candidates)
            except Exception as e:
                logger.warning("Glossary refresh failed: %s", e)
            finally:
                self._glossary_candidates.clear()
```

- [ ] **Step 6: Smoke-run unit tests (no new tests; existing must still pass)**

Run: `.venv/bin/pytest tests/ -v`
Expected: all prior tests still pass (no new tests added this task; covered by live smoke at end).

- [ ] **Step 7: Commit**

```bash
git add prompts/discourse_engagement.py src/discourse_engagement.py
git commit -m "feat(engagement): draft-aware internal context and glossary candidate accumulation"
```

---

### Task 8: Post-edit sub-flow prompts

**Files:**
- Modify: `prompts/discourse_engagement.py`

- [ ] **Step 1: Append three new prompts**

Open `/Users/shinseungbin/workspace/work/persona/prompts/discourse_engagement.py` and append:

```python
CLASSIFY_EDIT_PROMPT = """You are deciding whether a comment on a published research report warrants EDITING the report itself (not just replying).

Published report (markdown, first 3000 chars):
{report_excerpt}

Comment by {comment_author}:
{comment_text}

Decide:
- edit_needed=true when the comment identifies (a) a rendering/formatting/typo problem in the report, or (b) a clear factual error that has a high-confidence fix (wrong number, wrong citation, wrong date, broken link). Interpretive disagreements, alternate opinions, or stylistic suggestions are NOT edits — those get reply-only.
- edit_needed=false otherwise.

Return ONLY valid JSON:
{{
  "edit_needed": true | false,
  "edit_type": "format" | "factual" | "none",
  "target_section": "short anchor text or regex fragment identifying the passage to modify (empty if none)",
  "change_summary": "One-line Korean summary of the intended change (empty if none)"
}}
"""

GENERATE_EDIT_PROMPT = """You are producing an edited version of a published report.

Original report (markdown):
{report_md}

Reviewer-identified issue:
{change_summary}

Target section (approximate):
{target_section}

Instructions:
1. Modify ONLY the passage identified by `target_section`. Leave everything else byte-identical.
2. Do NOT add new sections or delete existing ones.
3. Return the FULL edited report markdown — not a diff.
4. Make no semantic changes beyond what `change_summary` requires.
"""

EDIT_FACT_CHECK_PROMPT = """You are fact-checking a proposed edit to a published report.

Original report (relevant passage):
{target_section}

Proposed new passage (extracted from the edited report):
{new_section}

Change summary:
{change_summary}

Internal knowledge:
{internal_context}

Verified internal-term glossary:
{glossary}

Guardrails:
- Approve if the change is limited to the stated intent, factually sound given the context, and does not introduce hallucinated terms or sources.
- Reject if the change introduces new claims not supported by context, rewrites unrelated content, or replaces factual text with less accurate text.
- There is NO "revise" option for edits — editing should be decisive.

Return ONLY valid JSON:
{{
  "decision": "approve" | "reject",
  "reason": "Short Korean explanation"
}}
"""
```

- [ ] **Step 2: Commit**

```bash
git add prompts/discourse_engagement.py
git commit -m "feat(engagement): add edit sub-flow prompts (classify, generate, fact-check)"
```

---

### Task 9: Wire edit sub-flow into DiscourseEngagement

**Files:**
- Modify: `src/discourse_engagement.py`

- [ ] **Step 1: Update imports and constructor**

Open `/Users/shinseungbin/workspace/work/persona/src/discourse_engagement.py`. Extend the prompts import:

```python
from prompts.discourse_engagement import (
    CLASSIFY_EDIT_PROMPT,
    COMMENT_CLASSIFY_PROMPT,
    EDIT_FACT_CHECK_PROMPT,
    EXTRACT_DRAFT_TERMS_PROMPT,
    FACT_CHECK_PROMPT,
    GENERATE_EDIT_PROMPT,
    REVISE_DRAFT_PROMPT,
    SEARCH_AND_DRAFT_PROMPT,
)
```

Add at top:

```python
from post_editor import EditRefused, PostEditor
from qa_archive import QAArchiver
```

Extend `__init__` to accept the new collaborators. Update signature:

```python
    def __init__(
        self,
        discourse_client: DiscourseClient,
        publisher: DiscoursePublisher,
        runtime: ClaudeRuntimeClient,
        confluence_knowledge: ConfluenceKnowledge,
        discourse_knowledge: DiscourseKnowledge,
        glossary: GlossaryManager,
        post_editor: PostEditor,
        qa_archive: QAArchiver,
        scope_text: str = "",
        slack_callback: callable | None = None,
    ):
```

and inside the constructor body add:

```python
        self.post_editor = post_editor
        self.qa_archive = qa_archive
```

- [ ] **Step 2: Add edit helper methods**

Inside the class, add these methods:

```python
    # ── Edit sub-flow ──

    def _classify_edit(self, report_excerpt: str, comment_author: str, comment_text: str) -> dict | None:
        prompt = CLASSIFY_EDIT_PROMPT.format(
            report_excerpt=report_excerpt,
            comment_author=comment_author,
            comment_text=comment_text,
        )
        result = self.runtime.run(LLMRunRequest(prompt=prompt, timeout_ms=60_000))
        if not result.success:
            return None
        parsed = parse_json_response(result.output)
        return parsed if isinstance(parsed, dict) else None

    def _generate_edit(self, report_md: str, change_summary: str, target_section: str) -> str | None:
        prompt = GENERATE_EDIT_PROMPT.format(
            report_md=report_md,
            change_summary=change_summary,
            target_section=target_section,
        )
        result = self.runtime.run(LLMRunRequest(prompt=prompt, timeout_ms=300_000))
        if result.success and result.output.strip():
            return result.output.strip()
        return None

    def _fact_check_edit(
        self,
        target_section: str,
        new_section: str,
        change_summary: str,
        internal_context: str,
    ) -> dict | None:
        prompt = EDIT_FACT_CHECK_PROMPT.format(
            target_section=target_section,
            new_section=new_section,
            change_summary=change_summary,
            internal_context=internal_context or "(내부 문서 없음)",
            glossary=self._load_glossary_text(),
        )
        result = self.runtime.run(LLMRunRequest(prompt=prompt, timeout_ms=120_000))
        if not result.success:
            return None
        parsed = parse_json_response(result.output)
        return parsed if isinstance(parsed, dict) else None

    def _attempt_post_edit(
        self,
        topic_info: dict,
        post,  # DiscoursePost
        report_md: str,
        report_excerpt: str,
        fact_check_context: str,
    ) -> dict:
        """Try to edit the published post in response to a correction.

        Returns: {"applied": bool, "change_summary": str, "edit_type": str}.
        """
        decision = self._classify_edit(report_excerpt, post.username, _strip_html(post.cooked))
        if not decision or not decision.get("edit_needed"):
            return {"applied": False, "change_summary": "", "edit_type": "none"}

        edit_type = decision.get("edit_type", "format")
        target_section = decision.get("target_section", "")
        change_summary = decision.get("change_summary", "")

        new_raw = self._generate_edit(report_md, change_summary, target_section)
        if not new_raw:
            self._notify(f":warning: 편집 초안 생성 실패 (post={post.post_number})")
            return {"applied": False, "change_summary": change_summary, "edit_type": edit_type}

        # Fact-check the edit — compare full new_raw vs old around the target
        fc = self._fact_check_edit(
            target_section=target_section or report_excerpt[:1500],
            new_section=new_raw[:4000],
            change_summary=change_summary,
            internal_context=fact_check_context,
        )
        if not fc or fc.get("decision") != "approve":
            reason = (fc or {}).get("reason", "fact-check 실패")
            self._notify(
                f":warning: 편집 적용 중단 — fact-check reject "
                f"(post={post.post_number}) reason: {reason}"
            )
            return {"applied": False, "change_summary": change_summary, "edit_type": edit_type}

        try:
            result = self.post_editor.apply_edit(
                post_id=topic_info.get("discourse_post_id") or 0,
                new_raw=new_raw,
                edit_reason=f"댓글 #{post.post_number} 지적 반영: {change_summary}",
                current_raw=report_md,
                edit_type=edit_type,
                change_summary=change_summary,
                triggered_by_post=post.post_number,
            )
            self._notify(
                f":pencil2: 본문 편집 적용됨 "
                f"(post_id={topic_info.get('discourse_post_id')}, "
                f"triggered_by=#{post.post_number}, type={edit_type})\n"
                f"변경 요약: {change_summary}"
            )
            return {"applied": True, "change_summary": change_summary, "edit_type": edit_type}
        except EditRefused as e:
            self._notify(f":warning: 편집 safety gate 차단: {e}")
            return {"applied": False, "change_summary": change_summary, "edit_type": edit_type}
        except Exception as e:
            logger.error("apply_edit failed: %s", e, exc_info=True)
            self._notify(f":x: 편집 적용 실패: {e}")
            return {"applied": False, "change_summary": change_summary, "edit_type": edit_type}
```

- [ ] **Step 3: Invoke edit flow from `_respond_to_comment`**

Locate `_respond_to_comment` (edited in Task 7). Inside the method, AFTER `fact_check_context` is computed and BEFORE the `_fact_check_loop` call, insert:

```python
        # Edit sub-flow (correction only, before we draft a reply)
        edit_outcome = {"applied": False, "change_summary": "", "edit_type": "none"}
        if comment_type == "correction":
            # Need the full report md and the publisher's topic_info (for post_id)
            full_report_md = self.publisher._load_latest_report(self._current_report_id) or ""
            edit_outcome = self._attempt_post_edit(
                topic_info=self._current_topic_info,
                post=post,
                report_md=full_report_md,
                report_excerpt=report_excerpt,
                fact_check_context=fact_check_context,
            )
```

This needs `_current_report_id` and `_current_topic_info` to be set by `_process_topic`. Update `_process_topic` (around line 83) to stash them:

At the top of `_process_topic`, just after `report_id = topic_info["report_id"]`, add:

```python
        self._current_report_id = report_id
        self._current_topic_info = topic_info
```

- [ ] **Step 4: Let the reply draft know about the edit outcome**

Replace the `_search_and_draft` call in `_respond_to_comment` so it can mention the edit. Add an `edit_outcome` parameter to `_search_and_draft` and wire it into the prompt. First update the prompt.

Open `prompts/discourse_engagement.py`. Modify `SEARCH_AND_DRAFT_PROMPT`: append before the final instructions block:

```
Edit outcome for this comment (if any):
{edit_outcome}
```

Specifically: replace the existing `Instructions:` section opening with:

```
Edit outcome for this comment:
{edit_outcome}

Instructions:
```

(Do NOT change the rest of the prompt; keep instruction numbering intact. Also add a new instruction item at the top of the list: `0. If an edit was applied, acknowledge it explicitly: "본문을 수정했습니다 (변경: {change_summary})."`)

The final `SEARCH_AND_DRAFT_PROMPT` should read:

```python
SEARCH_AND_DRAFT_PROMPT = """You are responding to a comment on an internal technical research report.
Your role is to provide a well-researched, factual answer with references.

{scope}

Report title: {report_title}
Report summary (first 2000 chars):
{report_excerpt}

Comment by {comment_author}:
{comment_text}

Comment type: {comment_type} (question or correction)

Internal knowledge (Confluence + Discourse):
{internal_context}

Edit outcome for this comment:
{edit_outcome}

Instructions:
0. If an edit was applied, acknowledge it explicitly at the top of the response: "본문을 수정했습니다 (변경: ...)." Then continue with a direct answer or explanation.
1. Use WebSearch to find 2-3 relevant external sources (papers, docs, benchmarks) related to the comment's topic.
2. Cross-reference the comment against the internal knowledge provided above.
3. Write a response in Korean that:
   - Directly addresses the commenter's point
   - Cites specific sources (with URLs) for factual claims
   - If the commenter found an error, acknowledge it honestly
   - If the commenter asked a question, answer it concisely with evidence
   - Keep the response under 500 words
4. End with a "참고 자료" section listing the sources you used.

Return ONLY the response text in markdown format. Do not wrap in JSON or code blocks.
"""
```

Now update `_search_and_draft` in `src/discourse_engagement.py`:

Change its signature to accept `edit_outcome`:

```python
    def _search_and_draft(
        self,
        report_title: str,
        report_excerpt: str,
        comment_author: str,
        comment_text: str,
        comment_type: str,
        internal_context: str,
        edit_outcome: dict | None = None,
    ) -> str | None:
        edit_outcome_str = "(편집 없음)"
        if edit_outcome and edit_outcome.get("applied"):
            edit_outcome_str = (
                f"편집 적용됨 — type={edit_outcome.get('edit_type')}, "
                f"change_summary=\"{edit_outcome.get('change_summary', '')}\""
            )
        prompt = SEARCH_AND_DRAFT_PROMPT.format(
            scope=self.scope_text,
            report_title=report_title,
            report_excerpt=report_excerpt,
            comment_author=comment_author,
            comment_text=comment_text,
            comment_type=comment_type,
            internal_context=internal_context or "(내부 문서 없음)",
            edit_outcome=edit_outcome_str,
        )
        result = self.runtime.run(
            LLMRunRequest(prompt=prompt, timeout_ms=300_000)
        )
        if result.success and result.output.strip():
            return result.output.strip()
        return None
```

Now update the call in `_respond_to_comment`. Replace the first `self._search_and_draft(...)` call (the initial draft, before `_extract_draft_terms`) to pass `edit_outcome=None` (it runs before the edit), and add a **second** draft call AFTER the edit sub-flow that overwrites the draft if an edit was applied.

Simpler approach: run the edit sub-flow BEFORE drafting the reply (since correction-type comments may trigger edits). Restructure `_respond_to_comment`:

Replace the entire method body one more time with this final version:

```python
    def _respond_to_comment(
        self,
        topic_id: int,
        post: DiscoursePost,
        comment_type: str,
        report_title: str,
        report_excerpt: str,
        classification: dict,
    ):
        comment_text = _strip_html(post.cooked)
        key_topic = classification.get("key_topic", "")

        # Context from comment keywords
        comment_keywords = [w for w in re.split(r"[\s,]+", key_topic) if len(w) > 1][:10]
        initial_context = self._gather_internal_context(comment_keywords)

        # Edit sub-flow runs first for corrections, so the reply can mention it
        edit_outcome = {"applied": False, "change_summary": "", "edit_type": "none"}
        if comment_type == "correction":
            full_report_md = self.publisher._load_latest_report(self._current_report_id) or ""
            edit_outcome = self._attempt_post_edit(
                topic_info=self._current_topic_info,
                post=post,
                report_md=full_report_md,
                report_excerpt=report_excerpt,
                fact_check_context=initial_context,
            )

        # Draft reply (knows about the edit outcome)
        draft = self._search_and_draft(
            report_title=report_title,
            report_excerpt=report_excerpt,
            comment_author=post.username,
            comment_text=comment_text,
            comment_type=comment_type,
            internal_context=initial_context,
            edit_outcome=edit_outcome,
        )
        if not draft:
            self._notify(
                f":warning: Discourse 답변 초안 생성 실패 (topic={topic_id}, post={post.post_number})"
            )
            return

        # Extract draft terms, re-gather
        draft_terms = self._extract_draft_terms(draft)
        merged_keywords = list({*comment_keywords, *draft_terms})
        if set(draft_terms) - set(comment_keywords):
            draft_context = self._gather_internal_context(merged_keywords)
            fact_check_context = self._merge_contexts(initial_context, draft_context)
        else:
            fact_check_context = initial_context

        self._accumulate_glossary_candidates(draft_terms | set(comment_keywords))

        # Fact check + revise loop
        final_response = self._fact_check_loop(
            comment_author=post.username,
            comment_text=comment_text,
            draft=draft,
            internal_context=fact_check_context,
        )
        if not final_response:
            self._notify(
                f":no_entry: Discourse 답변 reject됨 — 수동 확인 필요 "
                f"(topic={topic_id}, post=#{post.post_number} by {post.username})\n"
                f"댓글: _{comment_text[:100]}_"
            )
            return

        # Publish
        try:
            reply_result = self.client.create_reply(
                topic_id=topic_id,
                raw=final_response,
                reply_to_post_number=post.post_number,
            )
            self._notify(
                f":speech_balloon: Discourse 답변 게시 완료 "
                f"(topic={topic_id}, post=#{post.post_number} by {post.username})"
            )
            self._archive_qa_if_possible(
                topic_id=topic_id,
                post=post,
                report_title=report_title,
                comment_text=comment_text,
                comment_type=comment_type,
                reply_text=final_response,
                published_at=reply_result.get("created_at", ""),
            )
        except Exception as e:
            logger.error("Failed to post reply: %s", e)
            self._notify(f":x: Discourse 답변 게시 실패: {e}")
```

- [ ] **Step 5: Add `_archive_qa_if_possible` helper**

Add to the class:

```python
    def _archive_qa_if_possible(
        self,
        topic_id: int,
        post: DiscoursePost,
        report_title: str,
        comment_text: str,
        comment_type: str,
        reply_text: str,
        published_at: str,
    ) -> None:
        try:
            sources = self._extract_urls(reply_text)
            self.qa_archive.archive(
                topic_info={
                    "topic_id": topic_id,
                    "topic_url": self._current_topic_info.get("topic_url", ""),
                    "report_id": self._current_report_id,
                    "report_title": report_title or self._current_report_id,
                },
                post_number=post.post_number,
                commenter=post.username,
                comment_type=comment_type,
                comment_text=comment_text,
                reply_text=reply_text,
                sources=sources,
                published_at_iso=(published_at or datetime.now().isoformat(timespec="seconds")),
            )
        except Exception as e:
            logger.warning("QA archive failed (non-fatal): %s", e)

    @staticmethod
    def _extract_urls(text: str) -> list[str]:
        return re.findall(r"https?://[^\s)]+", text)
```

Add the required imports at the top of the file if missing:

```python
from datetime import datetime
```

- [ ] **Step 6: Run test suite, verify no regressions**

Run: `.venv/bin/pytest tests/ -v`
Expected: all green (no engagement-level tests were added — covered by smoke).

- [ ] **Step 7: Commit**

```bash
git add prompts/discourse_engagement.py src/discourse_engagement.py
git commit -m "feat(engagement): wire post edit sub-flow and Q&A archive into engagement pipeline"
```

---

### Task 10: Research pipeline wiring

**Files:**
- Modify: `src/pipelines/research_pipeline.py`

- [ ] **Step 1: Update the DiscourseEngagement construction**

Open `/Users/shinseungbin/workspace/work/persona/src/pipelines/research_pipeline.py`. Add imports near the other imports:

```python
from post_editor import PostEditor
from qa_archive import QAArchiver
```

Locate the `DiscourseEngagement(...)` construction (around line 137, in the block already edited in Task 4 Step 8). Replace the construction with:

```python
        if self.discourse_publisher:
            glossary_mgr = GlossaryManager(bot_dir)
            post_editor = PostEditor(bot_dir, self._discourse_client, self.discourse_publisher)
            qa_archiver = QAArchiver(bot_dir)
            self.discourse_engagement = DiscourseEngagement(
                discourse_client=self._discourse_client,
                publisher=self.discourse_publisher,
                runtime=self.runtime,
                confluence_knowledge=self.confluence_knowledge,
                discourse_knowledge=self.discourse_knowledge,
                glossary=glossary_mgr,
                post_editor=post_editor,
                qa_archive=qa_archiver,
                scope_text=self.fit_evaluator.scope_text() if self.fit_evaluator else "",
                slack_callback=lambda msg: self._post_status(msg, agent="discourse-bot"),
            )
```

- [ ] **Step 2: Sanity-run trigger script's imports**

Do NOT run the full poll — just verify imports resolve:

```bash
BOT_DIR=$(pwd)/bots/research .venv/bin/python -c "
import sys
sys.path.insert(0, 'src')
sys.path.insert(0, '.')
from pipelines.research_pipeline import ResearchPipeline
print('imports OK')
"
```

Expected: `imports OK`.

- [ ] **Step 3: Commit**

```bash
git add src/pipelines/research_pipeline.py
git commit -m "feat(engagement): wire glossary, post editor, and QA archive into research pipeline"
```

---

### Task 11: Live smoke test

**Files:**
- Modify: `bots/research/reports/010_turboquant-kv-cache-compression/state.json` (cursor rollback)

- [ ] **Step 1: Roll back topic 291 cursor**

Edit `bots/research/reports/010_turboquant-kv-cache-compression/state.json`: change `"last_checked_post_number": 3` back to `"last_checked_post_number": 1`.

- [ ] **Step 2: Restart research bot (picks up all new code paths)**

```bash
./persona.sh restart research
```

Expected output (summarized):
```
[OK] [research] claude-code-api healthy ...
[OK] [research] bot started ...
```

Tail `.research-api.log` and `.research-bot.log` for ~10s. Confirm workers come up (no "Claude Code native binary not found" errors).

- [ ] **Step 3: Trigger a poll immediately**

```bash
BOT_DIR=$(pwd)/bots/research CLAUDE_API_URL=http://localhost:8083 CLAUDE_API_KEY=sk-research-key-12345 \
  .venv/bin/python src/trigger_discourse_poll.py 2>&1 | tee /tmp/engagement-smoke.log
```

Wait for `Poll complete.` (up to ~10 minutes for 2 posts).

- [ ] **Step 4: Assert outcomes from log**

Verify the smoke log (`/tmp/engagement-smoke.log`) contains:

- `Polling 1 published topics`
- `Topic 291: 2 new posts`
- `Post #2 by jaewon_lim → correction` and either a `:pencil2: 본문 편집 적용됨` (if edit path fired) or skip edit then reply
- `Post #3 by jaewon_lim → question` and `Fact check attempt` lines followed by `approve` (not all revise cycles ending in reject this time — if it still rejects, inspect the glossary and internal_context together and iterate on the prompt)
- `:speech_balloon: Discourse 답변 게시 완료` for at least one post (ideally both)

- [ ] **Step 5: Verify filesystem side-effects**

Check:
```bash
ls bots/research/knowledge/topics/qa/
ls bots/research/knowledge/edits/ 2>/dev/null || echo "(no edits — either edit path didn't fire or was not approved)"
grep -c "^## " bots/research/knowledge/glossary.md
```

Expected:
- One or two new `.md` files in `knowledge/topics/qa/` (one per successful reply).
- If a format edit fired: a backup `.md` under `knowledge/edits/`.
- Glossary has ≥ dozens of entries (seed populated it; new entries may also have been added during the poll).

- [ ] **Step 6: Verify in Discourse UI (manual)**

Open https://hyperaccel.discourse.group/t/.../291 in a browser. Confirm:
- A reply to post #2 and/or #3 appears, signed by the bot account.
- If the edit sub-flow fired: the original topic post shows an "edited" badge with the reason text.

- [ ] **Step 7: Clean up and commit**

Remove the now-obsolete trigger script if preferred, or keep for future ops:

```bash
# Keep trigger_discourse_poll.py as a documented ops tool
git add bots/research/reports/010_turboquant-kv-cache-compression/state.json
git commit -m "chore(engagement): roll back topic 291 cursor after v2 smoke test"
```

---

## Summary checklist

After all tasks are complete, confirm spec coverage:

- [ ] Part A (draft-aware context) — Task 7
- [ ] Part C (FACT_CHECK_PROMPT rewrite) — Task 1
- [ ] Glossary auto-upsert — Tasks 3, 4
- [ ] Glossary seed CLI — Task 4
- [ ] Glossary injection into fact-check — Tasks 1, 4
- [ ] Q&A archive module — Task 5
- [ ] Q&A archive wiring — Task 9
- [ ] Post editing (B): format + factual with safety gates — Tasks 2, 6, 8, 9
- [ ] `edit_history` in state.json — Task 6
- [ ] Research pipeline wiring — Tasks 4, 10
- [ ] Error handling (never advance cursor on failure) — inherited from 2026-04-17 hotfix (no change needed)
- [ ] Live smoke — Task 11
