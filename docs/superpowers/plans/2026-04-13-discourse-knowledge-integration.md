# Discourse Knowledge Integration — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extract HyperAccel Discourse forum content into an Obsidian-compatible knowledge vault and inject it into the research pipeline at Discovery and Report stages.

**Architecture:** New `DiscourseClient` fetches all topics/posts via Discourse REST API. `DiscourseSync` orchestrates LLM-summarization (via existing `ClaudeRuntimeClient`) into structured markdown vault. `DiscourseKnowledge` reads the vault and provides two-layer context (category summaries + keyword-matched topic notes) for prompt injection. Pipeline injection is done by augmenting `scope_text` in `run_discovery()` and `_write_single_report()`.

**Tech Stack:** Python 3.14, `requests` (already in project), existing `ClaudeRuntimeClient` for LLM calls, Discourse REST API, YAML-style frontmatter in markdown files.

**Spec:** `docs/superpowers/specs/2026-04-13-discourse-knowledge-integration-design.md`

---

## File Structure

| Action | File | Responsibility |
|--------|------|----------------|
| Create | `src/discourse_client.py` | Discourse REST API client — fetch categories, topics, posts |
| Create | `src/discourse_sync.py` | Sync orchestrator — extract → LLM-summarize → write vault |
| Create | `src/discourse_knowledge.py` | Read vault, keyword-search topics, build context string for prompt injection |
| Modify | `src/pipelines/research_pipeline.py:59-95` | Initialize `DiscourseKnowledge` in `__init__`, inject into discovery + report |
| Modify | `src/pipelines/research_pipeline.py:107-158` | Add discourse context to `run_discovery()` |
| Modify | `src/pipelines/research_pipeline.py:438-470` | Add discourse context to `_write_single_report()` |
| Modify | `src/bot.py:977+` | Add `!research sync-discourse` command handler |
| Modify | `bots/research/config.json` | Add `discourse` config section |
| Create | `bots/research/knowledge/` | Output vault directory (created at runtime by sync) |

---

### Task 1: Discourse API Client

**Files:**
- Create: `src/discourse_client.py`

- [ ] **Step 1: Create `discourse_client.py` with data classes and client**

```python
"""Discourse REST API client for extracting forum content."""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime

import requests

logger = logging.getLogger(__name__)

FETCH_DELAY = 0.5  # seconds between API calls


@dataclass
class DiscoursePost:
    id: int
    post_number: int
    username: str
    created_at: str
    cooked: str  # HTML content
    reply_count: int = 0
    score: float = 0.0


@dataclass
class DiscourseTopic:
    id: int
    title: str
    slug: str
    category_id: int
    created_at: str
    last_posted_at: str
    posts_count: int
    views: int
    like_count: int = 0
    tags: list[str] = field(default_factory=list)


@dataclass
class DiscourseTopicDetail:
    topic: DiscourseTopic
    posts: list[DiscoursePost]
    participants: list[str] = field(default_factory=list)


@dataclass
class DiscourseCategory:
    id: int
    name: str
    slug: str
    topic_count: int
    post_count: int


class DiscourseClient:
    """Fetches categories, topics, and posts from a Discourse instance."""

    def __init__(self, base_url: str, api_key: str, api_username: str):
        self.base_url = base_url.rstrip("/")
        self.headers = {
            "Api-Key": api_key,
            "Api-Username": api_username,
        }

    def _get(self, path: str, params: dict | None = None) -> dict:
        url = f"{self.base_url}/{path.lstrip('/')}"
        resp = requests.get(url, headers=self.headers, params=params, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def fetch_categories(self) -> list[DiscourseCategory]:
        data = self._get("/categories.json")
        cats = []
        for c in data.get("category_list", {}).get("categories", []):
            cats.append(DiscourseCategory(
                id=c["id"],
                name=c["name"],
                slug=c.get("slug", "") or _slugify(c["name"]),
                topic_count=c.get("topic_count", 0),
                post_count=c.get("post_count", 0),
            ))
        return cats

    def fetch_topics_by_category(self, category_id: int) -> list[DiscourseTopic]:
        """Fetch all topics in a category, paginating through all pages."""
        topics = []
        page = 0
        while True:
            data = self._get(f"/c/{category_id}.json", params={"page": page})
            topic_list = data.get("topic_list", {}).get("topics", [])
            if not topic_list:
                break
            for t in topic_list:
                topics.append(DiscourseTopic(
                    id=t["id"],
                    title=t["title"],
                    slug=t.get("slug", ""),
                    category_id=category_id,
                    created_at=t.get("created_at", ""),
                    last_posted_at=t.get("last_posted_at", ""),
                    posts_count=t.get("posts_count", 0),
                    views=t.get("views", 0),
                    like_count=t.get("like_count", 0),
                    tags=t.get("tags", []),
                ))
            if len(topic_list) < 30:  # less than a full page
                break
            page += 1
            time.sleep(FETCH_DELAY)
        return topics

    def fetch_topic_detail(self, topic_id: int) -> DiscourseTopicDetail | None:
        """Fetch a topic with all its posts."""
        try:
            data = self._get(f"/t/{topic_id}.json")
        except requests.HTTPError as e:
            logger.warning("Failed to fetch topic %d: %s", topic_id, e)
            return None

        topic_data = data
        topic = DiscourseTopic(
            id=topic_data["id"],
            title=topic_data["title"],
            slug=topic_data.get("slug", ""),
            category_id=topic_data.get("category_id", 0),
            created_at=topic_data.get("created_at", ""),
            last_posted_at=topic_data.get("last_posted_at", ""),
            posts_count=topic_data.get("posts_count", 0),
            views=topic_data.get("views", 0),
            like_count=topic_data.get("like_count", 0),
            tags=[t if isinstance(t, str) else t.get("name", "") for t in topic_data.get("tags", [])],
        )

        raw_posts = topic_data.get("post_stream", {}).get("posts", [])
        # Truncate to 30 most recent if very long
        if len(raw_posts) > 30:
            raw_posts = raw_posts[-30:]

        posts = []
        for p in raw_posts:
            if p.get("hidden") or p.get("deleted_at"):
                continue
            posts.append(DiscoursePost(
                id=p["id"],
                post_number=p.get("post_number", 0),
                username=p.get("username", "unknown"),
                created_at=p.get("created_at", ""),
                cooked=p.get("cooked", ""),
                reply_count=p.get("reply_count", 0),
                score=p.get("score", 0.0),
            ))

        participants = list({p.username for p in posts})

        return DiscourseTopicDetail(topic=topic, posts=posts, participants=participants)

    def fetch_all(self, categories: list[DiscourseCategory] | None = None) -> dict:
        """Fetch all topics with details, organized by category.

        Returns: {category_slug: {"category": DiscourseCategory, "topics": [DiscourseTopicDetail]}}
        """
        if categories is None:
            categories = self.fetch_categories()

        result = {}
        for cat in categories:
            if cat.topic_count == 0:
                continue
            logger.info("Fetching category: %s (%d topics)", cat.name, cat.topic_count)
            topics = self.fetch_topics_by_category(cat.id)
            details = []
            for t in topics:
                detail = self.fetch_topic_detail(t.id)
                if detail:
                    details.append(detail)
                time.sleep(FETCH_DELAY)
            result[cat.slug] = {"category": cat, "topics": details}
            logger.info("Fetched %d/%d topics for %s", len(details), len(topics), cat.name)

        return result


def _slugify(text: str) -> str:
    """Convert text to a filesystem-safe slug."""
    text = text.lower().strip()
    text = re.sub(r"[^a-z0-9가-힣\s-]", "", text)
    text = re.sub(r"\s+", "-", text)
    return text[:80] or "unnamed"
```

- [ ] **Step 2: Verify the client works against live Discourse**

Run from project root (with `.env` loaded):

```bash
cd /Users/shinseungbin/workspace/work/persona && python3 -c "
import sys; sys.path.insert(0, 'src')
from discourse_client import DiscourseClient
c = DiscourseClient('https://hyperaccel.discourse.group', '2b6fe5a79255eb6157f29a37d564957085aef35f2e07b00778fc04eddd4c1bcd', 'SeungBin')
cats = c.fetch_categories()
print(f'Categories: {len(cats)}')
for cat in cats:
    print(f'  {cat.slug}: {cat.topic_count} topics')
detail = c.fetch_topic_detail(289)
if detail:
    print(f'Topic: {detail.topic.title}, posts: {len(detail.posts)}')
"
```

Expected: Lists all categories with counts, fetches topic 289 with its posts.

- [ ] **Step 3: Commit**

```bash
git add src/discourse_client.py
git commit -m "feat: add Discourse API client for forum content extraction"
```

---

### Task 2: Discourse Sync Orchestrator

**Files:**
- Create: `src/discourse_sync.py`

**Depends on:** Task 1 (DiscourseClient)

- [ ] **Step 1: Create `discourse_sync.py` with prompts and vault writing**

```python
"""Discourse sync — extract forum content, LLM-summarize, write Obsidian vault."""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime
from html import unescape
from pathlib import Path

from discourse_client import (
    DiscourseClient,
    DiscourseCategory,
    DiscourseTopicDetail,
    _slugify,
)
from skills.types import LLMRunRequest
from tools.claude_runtime import ClaudeRuntimeClient

logger = logging.getLogger(__name__)


TOPIC_SUMMARY_PROMPT = """You are summarizing an internal team discussion from HyperAccel's Discourse forum.

Category: {category_name}
Topic: {title}
Posts: {post_count} | Participants: {participants}
Date range: {created_at} ~ {last_activity}

--- Posts ---
{posts_text}
---

Write a structured summary in Korean. Output ONLY the following markdown:

## 요약
One paragraph synthesizing the full discussion.

## 핵심 결정사항
- Bullet list of decisions made (empty if none)

## 제약 및 블로커
- Known limitations, blockers, or constraints discussed

## 미해결 질문
- Open questions still being debated (empty if all resolved)

## 관련 기술/키워드
- Key technologies, tools, or concepts mentioned

Rules:
- Be concise. Each section should be 1-5 bullet points.
- Preserve technical specifics (model names, numbers, configs).
- If a decision was reversed or debated, note the final state.
- Mark status: active (ongoing discussion), resolved (concluded), archived (stale)."""


CATEGORY_SUMMARY_PROMPT = """You are building a category overview for HyperAccel's {category_name} team.
Below are summaries of all {topic_count} discussion topics in this category.

{topic_summaries}

Write a category-level overview in Korean. Output ONLY the following markdown:

## 개요
What this team/area focuses on (2-3 sentences).

## 진행 중인 프로젝트
- Active projects with current status and key participants

## 핵심 결정사항 및 제약
- Major decisions and technical constraints across all topics

## 최근 논의
- Top 5 most recent/active topics with one-line summary each

## 기술 스택
- Frameworks, tools, and approaches this group uses"""


class DiscourseSync:
    """Orchestrates Discourse extraction → LLM summarization → vault writing."""

    def __init__(
        self,
        client: DiscourseClient,
        runtime: ClaudeRuntimeClient,
        vault_path: str | Path,
    ):
        self.client = client
        self.runtime = runtime
        self.vault_path = Path(vault_path)

    def run_full_sync(self) -> dict:
        """Run a complete snapshot sync. Returns sync stats."""
        start = time.time()
        stats = {
            "topic_count": 0,
            "skipped_topics": 0,
            "failed_summaries": 0,
            "category_count": 0,
        }

        # 1. Fetch everything from Discourse
        logger.info("Fetching all Discourse content...")
        categories = self.client.fetch_categories()
        all_data = self.client.fetch_all(categories)

        # 2. Prepare vault directories
        topics_dir = self.vault_path / "topics"
        categories_dir = self.vault_path / "categories"
        tags_dir = self.vault_path / "tags"
        for d in [topics_dir, categories_dir, tags_dir]:
            d.mkdir(parents=True, exist_ok=True)

        # 3. Summarize and write topic notes
        # Collect all topic filenames for cross-referencing
        all_topic_files: dict[int, str] = {}  # discourse_id -> relative vault path
        tag_index: dict[str, list[str]] = {}  # tag -> [topic paths]

        for cat_slug, cat_data in all_data.items():
            cat: DiscourseCategory = cat_data["category"]
            topic_details: list[DiscourseTopicDetail] = cat_data["topics"]
            cat_topic_dir = topics_dir / cat_slug
            cat_topic_dir.mkdir(parents=True, exist_ok=True)

            for detail in topic_details:
                topic = detail.topic
                filename = _slugify(topic.slug or topic.title)
                rel_path = f"{cat_slug}/{filename}"
                all_topic_files[topic.id] = rel_path

                md_content = self._summarize_topic(detail, cat.name)
                if md_content is None:
                    # Fallback: write raw posts without summary
                    md_content = self._raw_fallback(detail, cat.name)
                    stats["failed_summaries"] += 1

                filepath = cat_topic_dir / f"{filename}.md"
                filepath.write_text(md_content, encoding="utf-8")
                stats["topic_count"] += 1

                # Track tags
                for tag in topic.tags:
                    tag_index.setdefault(tag, []).append(rel_path)

        # 4. Post-process: add Related links via tag co-occurrence
        self._add_related_links(topics_dir, all_data, all_topic_files, tag_index)

        # 5. Write category summaries
        for cat_slug, cat_data in all_data.items():
            cat = cat_data["category"]
            topic_details = cat_data["topics"]
            cat_summary = self._summarize_category(cat, topic_details, all_topic_files)
            if cat_summary:
                cat_file = categories_dir / f"{cat_slug}.md"
                cat_file.write_text(cat_summary, encoding="utf-8")
                stats["category_count"] += 1

        # 6. Write tag index
        self._write_tag_index(tags_dir, tag_index)

        # 7. Write sync metadata
        stats["duration_seconds"] = round(time.time() - start, 1)
        stats["last_sync"] = datetime.now().isoformat()
        index_path = self.vault_path / "_index.json"
        index_path.write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")

        logger.info(
            "Discourse sync complete: %d topics, %d categories, %d failed, %.1fs",
            stats["topic_count"], stats["category_count"],
            stats["failed_summaries"], stats["duration_seconds"],
        )
        return stats

    def _summarize_topic(self, detail: DiscourseTopicDetail, category_name: str) -> str | None:
        """Summarize a topic via LLM. Returns full markdown note or None on failure."""
        topic = detail.topic
        posts_text = self._format_posts(detail.posts)
        participants = ", ".join(detail.participants)

        prompt = TOPIC_SUMMARY_PROMPT.format(
            category_name=category_name,
            title=topic.title,
            post_count=topic.posts_count,
            participants=participants,
            created_at=topic.created_at[:10],
            last_activity=topic.last_posted_at[:10] if topic.last_posted_at else "unknown",
            posts_text=posts_text,
        )

        result = self.runtime.run(LLMRunRequest(prompt=prompt, timeout_ms=120_000))
        if not result.success:
            logger.warning("LLM summarization failed for topic %d: %s", topic.id, topic.title)
            return None

        summary_body = result.output.strip()

        # Build frontmatter
        frontmatter = self._build_topic_frontmatter(detail, category_name, summarized=True)
        return f"{frontmatter}\n\n{summary_body}"

    def _summarize_category(
        self,
        category: DiscourseCategory,
        topic_details: list[DiscourseTopicDetail],
        all_topic_files: dict[int, str],
    ) -> str | None:
        """Summarize a category from its topic summaries."""
        if not topic_details:
            return None

        # Read existing topic summaries from vault
        topic_summaries_text = []
        for detail in topic_details:
            rel_path = all_topic_files.get(detail.topic.id, "")
            filepath = self.vault_path / "topics" / f"{rel_path}.md"
            if filepath.exists():
                content = filepath.read_text(encoding="utf-8")
                # Strip frontmatter for the aggregation prompt
                content = re.sub(r"^---.*?---\s*", "", content, flags=re.DOTALL)
                topic_summaries_text.append(f"### {detail.topic.title}\n{content}")

        if not topic_summaries_text:
            return None

        prompt = CATEGORY_SUMMARY_PROMPT.format(
            category_name=category.name,
            topic_count=len(topic_summaries_text),
            topic_summaries="\n\n".join(topic_summaries_text),
        )

        result = self.runtime.run(LLMRunRequest(prompt=prompt, timeout_ms=180_000))
        if not result.success:
            logger.warning("Category summarization failed for %s", category.name)
            return None

        frontmatter = (
            f"---\ncategory: {category.slug}\n"
            f"discourse_id: {category.id}\n"
            f"topic_count: {category.topic_count}\n"
            f"last_synced: {datetime.now().strftime('%Y-%m-%d')}\n---"
        )
        return f"{frontmatter}\n\n{result.output.strip()}"

    def _build_topic_frontmatter(
        self, detail: DiscourseTopicDetail, category_name: str, summarized: bool
    ) -> str:
        topic = detail.topic
        cat_slug = _slugify(category_name)
        tags_str = "[" + ", ".join(topic.tags) + "]" if topic.tags else "[]"
        participants_str = "[" + ", ".join(detail.participants) + "]"
        truncated = len(detail.posts) < topic.posts_count

        lines = [
            "---",
            f"discourse_id: {topic.id}",
            f'title: "{topic.title}"',
            f"category: {cat_slug}",
            f"tags: {tags_str}",
            f"author: {detail.posts[0].username if detail.posts else 'unknown'}",
            f"created: {topic.created_at[:10]}",
            f"last_activity: {(topic.last_posted_at or topic.created_at)[:10]}",
            f"post_count: {topic.posts_count}",
            f"participants: {participants_str}",
            f"status: active",
            f"summarized: {str(summarized).lower()}",
        ]
        if truncated:
            lines.append(f"truncated: true")
            lines.append(f"total_posts: {topic.posts_count}")
        lines.append("---")
        return "\n".join(lines)

    def _format_posts(self, posts: list) -> str:
        """Format posts as plain text for the LLM prompt."""
        lines = []
        for p in posts:
            text = _strip_html(p.cooked)
            lines.append(f"[{p.username} | {p.created_at[:16]}]\n{text}\n")
        return "\n".join(lines)

    def _raw_fallback(self, detail: DiscourseTopicDetail, category_name: str) -> str:
        """Create a note from raw posts when LLM summarization fails."""
        frontmatter = self._build_topic_frontmatter(detail, category_name, summarized=False)
        posts_text = self._format_posts(detail.posts)
        return f"{frontmatter}\n\n## Raw Posts\n\n{posts_text}"

    def _add_related_links(
        self,
        topics_dir: Path,
        all_data: dict,
        all_topic_files: dict[int, str],
        tag_index: dict[str, list[str]],
    ):
        """Add ## Related section to topic notes based on tag co-occurrence."""
        for cat_slug, cat_data in all_data.items():
            for detail in cat_data["topics"]:
                topic = detail.topic
                rel_path = all_topic_files.get(topic.id, "")
                if not rel_path:
                    continue

                # Find related topics via shared tags (2+ shared tags)
                related = set()
                for tag in topic.tags:
                    for other_path in tag_index.get(tag, []):
                        if other_path != rel_path:
                            related.add(other_path)

                # Also add category backlink
                cat_link = f"categories/{cat_slug}"

                filepath = topics_dir / f"{rel_path}.md"
                if not filepath.exists():
                    continue

                content = filepath.read_text(encoding="utf-8")

                # Build Related section
                related_lines = [f"\n\n## Related", f"- [[{cat_link}]]"]
                for r in sorted(related):
                    related_lines.append(f"- [[{r}]]")

                content += "\n".join(related_lines)
                filepath.write_text(content, encoding="utf-8")

    def _write_tag_index(self, tags_dir: Path, tag_index: dict[str, list[str]]):
        """Write the tag index file."""
        lines = ["# Tag Index\n"]
        for tag in sorted(tag_index.keys()):
            lines.append(f"## {tag}")
            for path in sorted(tag_index[tag]):
                lines.append(f"- [[{path}]]")
            lines.append("")
        (tags_dir / "_tag_index.md").write_text("\n".join(lines), encoding="utf-8")


def _strip_html(html: str) -> str:
    """Strip HTML tags and decode entities."""
    text = re.sub(r"<[^>]+>", "", html)
    return unescape(text).strip()
```

- [ ] **Step 2: Smoke-test sync with a single category**

We can't easily test the full LLM summarization without a running `claude-code-api`, but we can test the extraction + vault-writing path. Create a quick test script:

```bash
cd /Users/shinseungbin/workspace/work/persona && python3 -c "
import sys; sys.path.insert(0, 'src')
from discourse_client import DiscourseClient
from discourse_sync import DiscourseSync, _strip_html

# Test HTML stripping
assert _strip_html('<p>Hello <b>world</b></p>') == 'Hello world'
assert _strip_html('a &amp; b') == 'a & b'
print('HTML stripping: OK')

# Test client can build a snapshot for one small category
c = DiscourseClient('https://hyperaccel.discourse.group', '2b6fe5a79255eb6157f29a37d564957085aef35f2e07b00778fc04eddd4c1bcd', 'SeungBin')
cats = c.fetch_categories()
# Find smallest category
smallest = min([cat for cat in cats if cat.topic_count > 0], key=lambda x: x.topic_count)
print(f'Testing with category: {smallest.name} ({smallest.topic_count} topics)')
data = c.fetch_all([smallest])
for slug, d in data.items():
    print(f'  {slug}: {len(d[\"topics\"])} topic details fetched')
    if d['topics']:
        t = d['topics'][0]
        print(f'    First topic: {t.topic.title} ({len(t.posts)} posts)')
print('Extraction: OK')
"
```

Expected: HTML stripping passes, smallest category's topics are fetched with post details.

- [ ] **Step 3: Commit**

```bash
git add src/discourse_sync.py
git commit -m "feat: add Discourse sync orchestrator with LLM summarization"
```

---

### Task 3: Knowledge Retrieval Module

**Files:**
- Create: `src/discourse_knowledge.py`

**Depends on:** Task 2 (vault structure must be defined)

- [ ] **Step 1: Create `discourse_knowledge.py`**

```python
"""Discourse knowledge retrieval — reads the Obsidian vault and builds context for pipeline injection."""

from __future__ import annotations

import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)


class DiscourseKnowledge:
    """Reads the Discourse knowledge vault and provides context for the research pipeline."""

    def __init__(self, vault_path: str | Path):
        self.vault_path = Path(vault_path)

    def is_available(self) -> bool:
        """Check if the vault exists and has content."""
        index = self.vault_path / "_index.json"
        return index.exists()

    def load_category_summaries(self) -> dict[str, str]:
        """Load all category summaries. Returns {category_slug: markdown_content}."""
        categories_dir = self.vault_path / "categories"
        if not categories_dir.exists():
            return {}

        summaries = {}
        for path in sorted(categories_dir.glob("*.md")):
            content = path.read_text(encoding="utf-8")
            # Strip frontmatter for prompt injection
            content = re.sub(r"^---.*?---\s*", "", content, flags=re.DOTALL)
            summaries[path.stem] = content.strip()

        return summaries

    def search_topics(self, keywords: list[str], top_k: int = 5) -> list[dict]:
        """Find topic notes most relevant to the given keywords.

        Returns list of {"path": str, "title": str, "content": str, "score": int}.
        """
        topics_dir = self.vault_path / "topics"
        if not topics_dir.exists():
            return []

        keywords_lower = [kw.lower() for kw in keywords if kw]
        if not keywords_lower:
            return []

        matches = []
        for path in topics_dir.rglob("*.md"):
            content = path.read_text(encoding="utf-8")

            # Extract title from frontmatter
            title_match = re.search(r'^title:\s*"?(.+?)"?\s*$', content, re.MULTILINE)
            title = title_match.group(1) if title_match else path.stem

            # Strip frontmatter for scoring
            body = re.sub(r"^---.*?---\s*", "", content, flags=re.DOTALL)
            searchable = (title + " " + body).lower()

            score = sum(1 for kw in keywords_lower if kw in searchable)
            if score > 0:
                matches.append({
                    "path": str(path.relative_to(self.vault_path)),
                    "title": title,
                    "content": body.strip(),
                    "score": score,
                })

        matches.sort(key=lambda x: x["score"], reverse=True)
        return matches[:top_k]

    def build_context(self, keywords: list[str]) -> str:
        """Build a two-layer context string for prompt injection.

        Layer 1: All category summaries (always included).
        Layer 2: Top-5 keyword-matched topic notes.
        """
        if not self.is_available():
            return ""

        parts = []

        # Layer 1: Category summaries
        summaries = self.load_category_summaries()
        if summaries:
            parts.append("=== HyperAccel 팀 내부 논의 현황 (Discourse) ===\n")
            for slug, content in summaries.items():
                parts.append(f"### [{slug}]\n{content}\n")

        # Layer 2: Relevant topic details
        topics = self.search_topics(keywords, top_k=5)
        if topics:
            parts.append("\n=== 관련 내부 논의 상세 ===\n")
            for t in topics:
                # Truncate long topic content to ~500 chars
                body = t["content"]
                if len(body) > 500:
                    body = body[:500] + "..."
                parts.append(f"### {t['title']}\n{body}\n")

        return "\n".join(parts)
```

- [ ] **Step 2: Verify module loads correctly**

```bash
cd /Users/shinseungbin/workspace/work/persona && python3 -c "
import sys; sys.path.insert(0, 'src')
from discourse_knowledge import DiscourseKnowledge

# Test with non-existent vault — should gracefully return empty
dk = DiscourseKnowledge('bots/research/knowledge')
print(f'Available: {dk.is_available()}')
print(f'Context: \"{dk.build_context([\"pim\", \"simulator\"])}\"')
print('Graceful degradation: OK')
"
```

Expected: `Available: False`, empty context string, no errors.

- [ ] **Step 3: Commit**

```bash
git add src/discourse_knowledge.py
git commit -m "feat: add Discourse knowledge retrieval with two-layer context"
```

---

### Task 4: Config & Pipeline Initialization

**Files:**
- Modify: `bots/research/config.json`
- Modify: `src/pipelines/research_pipeline.py:59-95`

**Depends on:** Task 3 (DiscourseKnowledge class)

- [ ] **Step 1: Add `discourse` section to config.json**

Add the following to `bots/research/config.json` after the `"research"` block:

```json
  "discourse": {
    "base_url": "https://hyperaccel.discourse.group",
    "api_username": "SeungBin",
    "vault_path": "knowledge"
  },
```

The `DISCOURSE_API_KEY` is already in `.env`. `vault_path` is relative to `bot_dir` (i.e., `bots/research/knowledge/`).

- [ ] **Step 2: Initialize DiscourseKnowledge in ResearchPipeline.__init__**

In `src/pipelines/research_pipeline.py`, add the import at the top (after the existing imports around line 29):

```python
from discourse_knowledge import DiscourseKnowledge
```

Then in `__init__` after the paper cache initialization (after line 94), add:

```python
        # Initialize Discourse knowledge
        discourse_config = bot_config.get("discourse", {})
        vault_rel = discourse_config.get("vault_path", "knowledge")
        self.discourse_knowledge = DiscourseKnowledge(bot_dir / vault_rel)
```

- [ ] **Step 3: Commit**

```bash
git add bots/research/config.json src/pipelines/research_pipeline.py
git commit -m "feat: initialize DiscourseKnowledge in research pipeline config"
```

---

### Task 5: Inject Discourse Context into Discovery Stage

**Files:**
- Modify: `src/pipelines/research_pipeline.py:107-158` (`run_discovery` method)

**Depends on:** Task 4

- [ ] **Step 1: Build augmented scope text with Discourse context in run_discovery**

In `run_discovery()`, the current code at line 112 does:

```python
        scope_text = self.fit_evaluator.scope_text() or "General AI hardware research"
```

After that line, add:

```python
        # Inject Discourse team context
        discourse_ctx = self.discourse_knowledge.build_context(
            self.scope.keywords if self.scope else []
        )
        if discourse_ctx:
            scope_text = f"{scope_text}\n\n{discourse_ctx}"
```

This appends the two-layer Discourse context to the scope text that gets passed into `RESEARCHER_DISCOVERY_PROMPT` via `{scope}`. The researcher now sees internal team discussions alongside the LPU architecture and scope definition.

- [ ] **Step 2: Verify the code is syntactically correct**

```bash
cd /Users/shinseungbin/workspace/work/persona && python3 -c "
import sys; sys.path.insert(0, 'src')
import ast
with open('src/pipelines/research_pipeline.py') as f:
    ast.parse(f.read())
print('Syntax OK')
"
```

- [ ] **Step 3: Commit**

```bash
git add src/pipelines/research_pipeline.py
git commit -m "feat: inject Discourse context into research discovery stage"
```

---

### Task 6: Inject Discourse Context into Report Writing Stage

**Files:**
- Modify: `src/pipelines/research_pipeline.py:438-470` (`_write_single_report` method)

**Depends on:** Task 4

- [ ] **Step 1: Add Discourse context to report writing**

In `_write_single_report()`, the current code at line 457 does:

```python
        scope_text = self.fit_evaluator.scope_text()
```

After that line, add:

```python
        # Inject Discourse context relevant to this specific idea
        idea_keywords = [
            idea.get("idea_id", ""),
            idea.get("title", ""),
        ] + idea.get("keywords", [])
        discourse_ctx = self.discourse_knowledge.build_context(idea_keywords)
        if discourse_ctx:
            scope_text = f"{scope_text}\n\n{discourse_ctx}"
```

This uses idea-specific keywords (the idea's ID, title, and any keywords from the brief) to select the most relevant Discourse topics for this particular report. The researcher will reference internal discussions when writing the 적용방안 section.

- [ ] **Step 2: Verify syntax**

```bash
cd /Users/shinseungbin/workspace/work/persona && python3 -c "
import sys; sys.path.insert(0, 'src')
import ast
with open('src/pipelines/research_pipeline.py') as f:
    ast.parse(f.read())
print('Syntax OK')
"
```

- [ ] **Step 3: Commit**

```bash
git add src/pipelines/research_pipeline.py
git commit -m "feat: inject Discourse context into research report writing stage"
```

---

### Task 7: Slack Command for Manual Sync

**Files:**
- Modify: `src/bot.py:977+` (`_handle_research_command`)
- Modify: `src/pipelines/research_pipeline.py` (add `sync_discourse` method)

**Depends on:** Tasks 1, 2, 4

- [ ] **Step 1: Add `sync_discourse` method to ResearchPipeline**

Add the following imports at the top of `src/pipelines/research_pipeline.py` (near the existing imports):

```python
import os
from discourse_client import DiscourseClient
from discourse_sync import DiscourseSync
```

Add this method to the `ResearchPipeline` class (after `__init__`, before `_llm`):

```python
    def sync_discourse(self) -> dict:
        """Run a full Discourse sync — extract, summarize, write vault."""
        discourse_config = self.config.get("discourse", {})
        base_url = discourse_config.get("base_url", "")
        api_username = discourse_config.get("api_username", "system")
        api_key = os.environ.get("DISCOURSE_API_KEY", "")

        if not base_url or not api_key:
            logger.error("Discourse config missing: base_url or DISCOURSE_API_KEY")
            return {"error": "missing config"}

        client = DiscourseClient(base_url, api_key, api_username)
        vault_rel = discourse_config.get("vault_path", "knowledge")
        sync = DiscourseSync(client, self.runtime, self.bot_dir / vault_rel)

        self._post_status(":books: Discourse 동기화를 시작합니다...", agent="researcher")
        stats = sync.run_full_sync()
        self._post_status(
            f":white_check_mark: Discourse 동기화 완료: "
            f"{stats['topic_count']}개 토픽, {stats['category_count']}개 카테고리, "
            f"{stats['failed_summaries']}건 실패, {stats['duration_seconds']}초",
            agent="researcher",
        )
        return stats
```

- [ ] **Step 2: Add `sync-discourse` command to bot.py**

In `src/bot.py`, find the `_handle_research_command` function. After the existing `elif` blocks (e.g., after `elif subcmd == "dive":` block ends), add a new command handler:

```python
    elif subcmd == "sync-discourse":
        client.chat_postMessage(
            channel=channel_id,
            text=":books: Discourse 지식 동기화를 시작합니다...",
            thread_ts=thread_ts,
        )

        def _run():
            _pipeline.sync_discourse()

        threading.Thread(target=_run, daemon=True).start()
```

- [ ] **Step 3: Verify syntax for both files**

```bash
cd /Users/shinseungbin/workspace/work/persona && python3 -c "
import sys; sys.path.insert(0, 'src')
import ast
for f in ['src/pipelines/research_pipeline.py', 'src/bot.py']:
    with open(f) as fh:
        ast.parse(fh.read())
    print(f'{f}: Syntax OK')
"
```

- [ ] **Step 4: Commit**

```bash
git add src/pipelines/research_pipeline.py src/bot.py
git commit -m "feat: add !research sync-discourse Slack command"
```

---

### Task 8: End-to-End Verification

**Files:** None (verification only)

**Depends on:** All previous tasks

- [ ] **Step 1: Verify all imports resolve**

```bash
cd /Users/shinseungbin/workspace/work/persona && python3 -c "
import sys; sys.path.insert(0, 'src')

# Verify all new modules import correctly
from discourse_client import DiscourseClient, DiscourseCategory, DiscourseTopic, DiscourseTopicDetail, DiscoursePost
from discourse_sync import DiscourseSync, _strip_html
from discourse_knowledge import DiscourseKnowledge

print('All imports: OK')

# Verify DiscourseKnowledge graceful degradation
dk = DiscourseKnowledge('bots/research/knowledge')
assert dk.build_context(['test']) == ''
print('Graceful degradation: OK')

# Verify DiscourseClient connects
c = DiscourseClient('https://hyperaccel.discourse.group', '2b6fe5a79255eb6157f29a37d564957085aef35f2e07b00778fc04eddd4c1bcd', 'SeungBin')
cats = c.fetch_categories()
assert len(cats) > 0
print(f'Discourse API: OK ({len(cats)} categories)')
"
```

- [ ] **Step 2: Verify pipeline file parses with all changes**

```bash
cd /Users/shinseungbin/workspace/work/persona && python3 -c "
import sys; sys.path.insert(0, 'src')
import ast
for path in ['src/discourse_client.py', 'src/discourse_sync.py', 'src/discourse_knowledge.py', 'src/pipelines/research_pipeline.py', 'src/bot.py']:
    with open(path) as f:
        ast.parse(f.read())
    print(f'{path}: OK')
print('All files parse successfully')
"
```

- [ ] **Step 3: Verify config.json is valid JSON**

```bash
cd /Users/shinseungbin/workspace/work/persona && python3 -c "
import json
with open('bots/research/config.json') as f:
    config = json.load(f)
assert 'discourse' in config
print(f'Config OK: discourse section present')
print(f'  base_url: {config[\"discourse\"][\"base_url\"]}')
print(f'  api_username: {config[\"discourse\"][\"api_username\"]}')
print(f'  vault_path: {config[\"discourse\"][\"vault_path\"]}')
"
```

- [ ] **Step 4: Run a real sync (requires running claude-code-api)**

If `claude-code-api` is running on the configured port:

```bash
cd /Users/shinseungbin/workspace/work/persona && python3 -c "
import sys, os; sys.path.insert(0, 'src')
from dotenv import load_dotenv
load_dotenv('bots/research/.env')
from discourse_client import DiscourseClient
from discourse_sync import DiscourseSync
from tools.claude_runtime import ClaudeRuntimeClient

client = DiscourseClient(
    'https://hyperaccel.discourse.group',
    os.environ['DISCOURSE_API_KEY'],
    'SeungBin',
)
runtime = ClaudeRuntimeClient(
    api_url=os.environ.get('CLAUDE_API_URL', 'http://localhost:8083'),
    api_key=os.environ.get('CLAUDE_API_KEY', ''),
)
sync = DiscourseSync(client, runtime, 'bots/research/knowledge')
stats = sync.run_full_sync()
print(f'Sync stats: {stats}')
"
```

This is the real test — if `claude-code-api` is not running, this step can be deferred to when the bot is deployed. All previous steps verify correctness without requiring the LLM server.
