"""Confluence sync — search by keyword, LLM-summarize pages, write Obsidian vault."""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime
from html import unescape
from pathlib import Path

from confluence_client import ConfluenceClient, ConfluencePage, _slugify
from skills.types import LLMRunRequest
from tools.claude_runtime import ClaudeRuntimeClient

logger = logging.getLogger(__name__)


PAGE_SUMMARY_PROMPT = """You are summarizing an internal Confluence wiki page from HyperAccel.

Space: {space_key}
Title: {title}
Author: {author}
Created: {created}
Last Modified: {last_modified}
Labels: {labels}

--- Page Content ---
{body_text}
---

Write a structured summary in Korean. Output ONLY the following markdown:

## 요약
One paragraph synthesizing the page content.

## 핵심 결정사항
- Bullet list of decisions documented (empty if none)

## 제약 및 블로커
- Known limitations, blockers, or constraints mentioned

## 미해결 질문
- Open questions or unresolved items (empty if all resolved)

## 관련 기술/키워드
- Key technologies, tools, or concepts mentioned

Rules:
- Be concise. Each section should be 1-5 bullet points.
- Preserve technical specifics (model names, numbers, configs).
- If information is sparse, note what is available and mark gaps."""


SPACE_SUMMARY_PROMPT = """You are building a space overview for HyperAccel's Confluence wiki.
Space key: {space_key}
Below are summaries of all {page_count} pages in this space.

{page_summaries}

Write a space-level overview in Korean. Output ONLY the following markdown:

## 개요
What this space covers (2-3 sentences).

## 주요 문서
- Key pages with one-line summary each

## 핵심 결정사항 및 제약
- Major decisions and technical constraints across all pages

## 기술 스택
- Frameworks, tools, and approaches documented in this space"""


class ConfluenceSync:
    """Orchestrates Confluence search -> LLM summarization -> vault writing."""

    def __init__(
        self,
        client: ConfluenceClient,
        runtime: ClaudeRuntimeClient,
        vault_path: str | Path,
        progress_callback: callable | None = None,
    ):
        self.client = client
        self.runtime = runtime
        self.vault_path = Path(vault_path) / "confluence"
        self.progress_callback = progress_callback  # (message: str) -> None

    def _report_progress(self, message: str):
        logger.info(message)
        if self.progress_callback:
            self.progress_callback(message)

    def _load_index(self) -> dict:
        """Load sync index from _index.json, or empty dict if none exists."""
        index_path = self.vault_path / "_index.json"
        if not index_path.exists():
            return {}
        try:
            return json.loads(index_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}

    def _save_index(self, data: dict):
        """Write sync index to _index.json."""
        self.vault_path.mkdir(parents=True, exist_ok=True)
        index_path = self.vault_path / "_index.json"
        index_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def run_sync(
        self,
        keywords: str,
        spaces: list[str] | None = None,
        full: bool = False,
    ) -> dict:
        """Main sync entry point.

        Args:
            keywords: Search keywords for Confluence CQL.
            spaces: Optional list of space keys to scope the search.
            full: Force full sync (ignore last_sync timestamps).

        Returns:
            Stats dict with sync results.
        """
        start = time.time()
        index = self._load_index()
        last_sync = None if full else index.get("last_sync")
        mode = "full" if not last_sync else "incremental"
        synced_pages: dict[str, str] = index.get("synced_pages", {})

        stats = {
            "mode": mode,
            "keywords": keywords,
            "spaces": spaces or [],
            "page_count": 0,
            "skipped_pages": 0,
            "child_pages": 0,
            "failed_summaries": 0,
            "space_count": 0,
            "duration_seconds": 0,
        }

        # 1. Search for matching pages
        self._report_progress(
            f":mag: Confluence {mode} 동기화: '{keywords}' 검색 중..."
        )
        search_results = self.client.search(keywords, spaces)
        self._report_progress(
            f":mag: {len(search_results)}개 페이지 발견, 하위 페이지 수집 중..."
        )

        # 2. Collect child pages for each search result
        all_pages: dict[str, ConfluencePage] = {}
        for page in search_results:
            all_pages[page.id] = page
            children = self.client.fetch_children(page.id)
            for child in children:
                if child.id not in all_pages:
                    all_pages[child.id] = child
                    stats["child_pages"] += 1

        total_pages = len(all_pages)
        self._report_progress(
            f":page_facing_up: 총 {total_pages}개 페이지 (하위 {stats['child_pages']}개 포함), 요약 시작..."
        )

        # 3. Prepare vault directories
        pages_dir = self.vault_path / "pages"
        spaces_dir = self.vault_path / "spaces"
        pages_dir.mkdir(parents=True, exist_ok=True)
        spaces_dir.mkdir(parents=True, exist_ok=True)

        # Group pages by space for space summaries later
        pages_by_space: dict[str, list[ConfluencePage]] = {}
        processed = 0

        # 4. Summarize and write page notes
        for page_id, page in all_pages.items():
            space_key = page.space_key or "unknown"
            pages_by_space.setdefault(space_key, []).append(page)

            # Create space subdirectory under pages/
            space_page_dir = pages_dir / space_key
            space_page_dir.mkdir(parents=True, exist_ok=True)

            filename = _slugify(page.title)
            filepath = space_page_dir / f"{filename}.md"

            # Incremental: skip pages not updated since last sync
            if (
                mode == "incremental"
                and page_id in synced_pages
                and page.last_modified
                and page.last_modified <= synced_pages[page_id]
            ):
                stats["skipped_pages"] += 1
                processed += 1
                if processed % 5 == 0:
                    elapsed = round(time.time() - start)
                    self._report_progress(
                        f":hourglass_flowing_sand: 진행: {processed}/{total_pages} "
                        f"({stats['page_count']} 요약, {stats['skipped_pages']} 스킵, {elapsed}초 경과)"
                    )
                continue

            # Summarize or fallback
            md_content = self._summarize_page(page)
            if md_content is None:
                md_content = self._raw_fallback(page)
                stats["failed_summaries"] += 1

            filepath.write_text(md_content, encoding="utf-8")
            synced_pages[page_id] = page.last_modified
            stats["page_count"] += 1
            processed += 1

            # Progress report every 5 pages
            if processed % 5 == 0:
                elapsed = round(time.time() - start)
                self._report_progress(
                    f":hourglass_flowing_sand: 진행: {processed}/{total_pages} "
                    f"({stats['page_count']} 요약, {stats['skipped_pages']} 스킵, {elapsed}초 경과)"
                )

        # 5. Write space summaries
        for space_key, space_pages in pages_by_space.items():
            space_summary = self._summarize_space(space_key, space_pages)
            if space_summary:
                space_file = spaces_dir / f"{space_key}.md"
                space_file.write_text(space_summary, encoding="utf-8")
                stats["space_count"] += 1

        # 6. Save index
        keyword_history: list[str] = index.get("keyword_history", [])
        if keywords not in keyword_history:
            keyword_history.append(keywords)

        self._save_index(
            {
                "last_sync": datetime.now().isoformat(),
                "synced_pages": synced_pages,
                "keyword_history": keyword_history,
            }
        )

        stats["duration_seconds"] = round(time.time() - start, 1)
        self._report_progress(
            f":white_check_mark: Confluence 동기화 완료: {stats['page_count']}개 요약, "
            f"{stats['skipped_pages']}개 스킵, {stats['space_count']}개 스페이스, "
            f"{stats['failed_summaries']}개 실패, {stats['duration_seconds']}초 소요"
        )

        logger.info(
            "Confluence sync complete (%s): %d summarized, %d skipped, %d spaces, %d failed, %.1fs",
            mode,
            stats["page_count"],
            stats["skipped_pages"],
            stats["space_count"],
            stats["failed_summaries"],
            stats["duration_seconds"],
        )
        return stats

    def _summarize_page(self, page: ConfluencePage) -> str | None:
        """Summarize a single page via LLM. Returns markdown string or None on failure."""
        body_text = _strip_html(page.body)
        body_text = body_text[:15000]

        labels = ", ".join(page.labels) if page.labels else "none"

        prompt = PAGE_SUMMARY_PROMPT.format(
            space_key=page.space_key,
            title=page.title,
            author=page.author,
            created=page.created[:10] if page.created else "unknown",
            last_modified=page.last_modified[:10] if page.last_modified else "unknown",
            labels=labels,
            body_text=body_text,
        )

        result = self.runtime.run(LLMRunRequest(prompt=prompt, timeout_ms=120_000))
        if not result.success:
            logger.warning(
                "LLM summarization failed for page %s: %s", page.id, page.title
            )
            return None

        summary_body = result.output.strip()
        frontmatter = self._build_frontmatter(page, summarized=True)
        return f"{frontmatter}\n\n{summary_body}"

    def _summarize_space(
        self, space_key: str, pages: list[ConfluencePage]
    ) -> str | None:
        """Summarize a space from its page summaries. Returns markdown string or None."""
        if not pages:
            return None

        page_summaries_parts = []
        for page in pages:
            filename = _slugify(page.title)
            filepath = self.vault_path / "pages" / space_key / f"{filename}.md"
            if filepath.exists():
                content = filepath.read_text(encoding="utf-8")
                # Strip frontmatter
                content = re.sub(r"^---.*?---\s*", "", content, flags=re.DOTALL)
                page_summaries_parts.append(f"### {page.title}\n{content}")

        if not page_summaries_parts:
            return None

        prompt = SPACE_SUMMARY_PROMPT.format(
            space_key=space_key,
            page_count=len(page_summaries_parts),
            page_summaries="\n\n".join(page_summaries_parts),
        )

        result = self.runtime.run(LLMRunRequest(prompt=prompt, timeout_ms=180_000))
        if not result.success:
            logger.warning("Space summarization failed for %s", space_key)
            return None

        frontmatter = (
            f"---\nspace_key: {space_key}\n"
            f"page_count: {len(pages)}\n"
            f"last_synced: {datetime.now().strftime('%Y-%m-%d')}\n---"
        )
        return f"{frontmatter}\n\n{result.output.strip()}"

    def _build_frontmatter(self, page: ConfluencePage, summarized: bool) -> str:
        """Build YAML frontmatter for a page note."""
        labels_str = (
            "[" + ", ".join(page.labels) + "]" if page.labels else "[]"
        )

        lines = [
            "---",
            f"confluence_id: {page.id}",
            f'title: "{page.title}"',
            f"space: {page.space_key}",
            f"author: {page.author}",
            f"created: {page.created[:10] if page.created else 'unknown'}",
            f"last_modified: {page.last_modified[:10] if page.last_modified else 'unknown'}",
            f"labels: {labels_str}",
            f"version: {page.version}",
            f"summarized: {str(summarized).lower()}",
        ]
        if page.parent_id:
            lines.append(f"parent_id: {page.parent_id}")
        lines.append("---")
        return "\n".join(lines)

    def _raw_fallback(self, page: ConfluencePage) -> str:
        """Fallback when LLM fails: frontmatter + raw stripped HTML content."""
        frontmatter = self._build_frontmatter(page, summarized=False)
        body_text = _strip_html(page.body)[:15000]
        return f"{frontmatter}\n\n## Raw Content\n\n{body_text}"


def _strip_html(html: str) -> str:
    """Strip HTML tags and decode entities."""
    text = re.sub(r"<[^>]+>", "", html)
    return unescape(text).strip()
