"""Confluence knowledge retrieval — reads the Obsidian vault and builds context for pipeline injection."""

from __future__ import annotations

import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)


class ConfluenceKnowledge:
    """Reads the Confluence knowledge vault and provides context for the research pipeline."""

    def __init__(self, vault_path: str | Path):
        self.vault_path = Path(vault_path) / "confluence"

    def is_available(self) -> bool:
        """Check if the vault exists and has content."""
        index = self.vault_path / "_index.json"
        return index.exists()

    def load_space_summaries(self) -> dict[str, str]:
        """Load all space summaries. Returns {space_key: markdown_content}."""
        spaces_dir = self.vault_path / "spaces"
        if not spaces_dir.exists():
            return {}

        summaries = {}
        for path in sorted(spaces_dir.glob("*.md")):
            content = path.read_text(encoding="utf-8")
            # Strip frontmatter for prompt injection
            content = re.sub(r"^---.*?---\s*", "", content, flags=re.DOTALL)
            summaries[path.stem] = content.strip()

        return summaries

    def search_pages(self, keywords: list[str], top_k: int = 5) -> list[dict]:
        """Find page notes most relevant to the given keywords.

        Returns list of {"path": str, "title": str, "content": str, "score": int}.
        """
        pages_dir = self.vault_path / "pages"
        if not pages_dir.exists():
            return []

        keywords_lower = [kw.lower() for kw in keywords if kw]
        if not keywords_lower:
            return []

        matches = []
        for path in pages_dir.rglob("*.md"):
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

        Layer 1: All space summaries (always included).
        Layer 2: Top-5 keyword-matched page notes.
        """
        if not self.is_available():
            return ""

        parts = []

        # Layer 1: Space summaries
        summaries = self.load_space_summaries()
        if summaries:
            parts.append("=== HyperAccel 내부 문서 (Confluence) ===\n")
            for space_key, content in summaries.items():
                parts.append(f"### [{space_key}]\n{content}\n")

        # Layer 2: Relevant page details
        pages = self.search_pages(keywords, top_k=5)
        if pages:
            parts.append("\n=== 관련 내부 문서 상세 ===\n")
            for p in pages:
                # Truncate long page content to ~500 chars
                body = p["content"]
                if len(body) > 500:
                    body = body[:500] + "..."
                parts.append(f"### {p['title']}\n{body}\n")

        return "\n".join(parts)
