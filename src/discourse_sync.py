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
        progress_callback: callable | None = None,
    ):
        self.client = client
        self.runtime = runtime
        self.vault_path = Path(vault_path)
        self.progress_callback = progress_callback  # (message: str) -> None

    def _report_progress(self, message: str):
        logger.info(message)
        if self.progress_callback:
            self.progress_callback(message)

    def _load_last_sync(self) -> str | None:
        """Load last_sync timestamp from _index.json, or None if no previous sync."""
        index_path = self.vault_path / "_index.json"
        if not index_path.exists():
            return None
        try:
            data = json.loads(index_path.read_text(encoding="utf-8"))
            return data.get("last_sync")
        except (json.JSONDecodeError, OSError):
            return None

    def _is_topic_updated(self, topic_detail: DiscourseTopicDetail, since: str) -> bool:
        """Check if a topic has been updated since the given ISO timestamp."""
        last_posted = topic_detail.topic.last_posted_at
        if not last_posted or not since:
            return True
        return last_posted > since

    def run_full_sync(self) -> dict:
        """Run a complete snapshot sync. Returns sync stats."""
        return self._run_sync(incremental=False)

    def run_incremental_sync(self) -> dict:
        """Run an incremental sync — only re-summarize topics updated since last sync."""
        return self._run_sync(incremental=True)

    def _run_sync(self, incremental: bool = False) -> dict:
        start = time.time()
        last_sync = self._load_last_sync() if incremental else None
        mode = "incremental" if last_sync else "full"

        stats = {
            "mode": mode,
            "topic_count": 0,
            "skipped_topics": 0,
            "failed_summaries": 0,
            "category_count": 0,
        }

        # 1. Fetch everything from Discourse
        self._report_progress(f":satellite: Discourse {mode} 동기화: 토픽 목록 가져오는 중...")
        categories = self.client.fetch_categories()
        all_data = self.client.fetch_all(categories)

        total_topics = sum(len(d["topics"]) for d in all_data.values())
        self._report_progress(f":satellite: {total_topics}개 토픽 발견, 요약 시작...")

        # 2. Prepare vault directories
        topics_dir = self.vault_path / "topics"
        categories_dir = self.vault_path / "categories"
        tags_dir = self.vault_path / "tags"
        for d in [topics_dir, categories_dir, tags_dir]:
            d.mkdir(parents=True, exist_ok=True)

        # 3. Summarize and write topic notes
        all_topic_files: dict[int, str] = {}  # discourse_id -> relative vault path
        tag_index: dict[str, list[str]] = {}  # tag -> [topic paths]
        processed = 0
        categories_with_updates: set[str] = set()

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

                # Incremental: skip topics not updated since last sync
                filepath = cat_topic_dir / f"{filename}.md"
                if last_sync and not self._is_topic_updated(detail, last_sync) and filepath.exists():
                    stats["skipped_topics"] += 1
                    processed += 1
                    for tag in topic.tags:
                        tag_index.setdefault(tag, []).append(rel_path)
                    continue

                categories_with_updates.add(cat_slug)
                md_content = self._summarize_topic(detail, cat.name)
                if md_content is None:
                    md_content = self._raw_fallback(detail, cat.name)
                    stats["failed_summaries"] += 1

                filepath.write_text(md_content, encoding="utf-8")
                stats["topic_count"] += 1
                processed += 1

                for tag in topic.tags:
                    tag_index.setdefault(tag, []).append(rel_path)

                # Progress report every 10 topics
                if processed % 10 == 0:
                    elapsed = round(time.time() - start)
                    self._report_progress(
                        f":hourglass_flowing_sand: 진행: {processed}/{total_topics} "
                        f"({stats['topic_count']} 요약, {stats['skipped_topics']} 스킵, {elapsed}초 경과)"
                    )

        # 4. Post-process: add Related links via tag co-occurrence
        self._add_related_links(topics_dir, all_data, all_topic_files, tag_index)

        # 5. Write category summaries (incremental: only categories with updated topics)
        cats_to_summarize = all_data.items() if not last_sync else (
            (slug, data) for slug, data in all_data.items() if slug in categories_with_updates
        )
        for cat_slug, cat_data in cats_to_summarize:
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
            "Discourse sync complete (%s): %d summarized, %d skipped, %d categories, %d failed, %.1fs",
            mode, stats["topic_count"], stats["skipped_topics"],
            stats["category_count"], stats["failed_summaries"], stats["duration_seconds"],
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

        topic_summaries_text = []
        for detail in topic_details:
            rel_path = all_topic_files.get(detail.topic.id, "")
            filepath = self.vault_path / "topics" / f"{rel_path}.md"
            if filepath.exists():
                content = filepath.read_text(encoding="utf-8")
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

                related = set()
                for tag in topic.tags:
                    for other_path in tag_index.get(tag, []):
                        if other_path != rel_path:
                            related.add(other_path)

                cat_link = f"categories/{cat_slug}"

                filepath = topics_dir / f"{rel_path}.md"
                if not filepath.exists():
                    continue

                content = filepath.read_text(encoding="utf-8")

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
