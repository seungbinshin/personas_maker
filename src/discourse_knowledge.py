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
