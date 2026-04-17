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

        date_prefix = published_at_iso[:10]
        slug = self._slugify(
            topic_info.get("report_title", "") or str(topic_info.get("topic_id", "unknown"))
        )
        fname = f"{date_prefix}-{slug}-post{post_number}.md"
        path = self.qa_dir / fname

        truncated_reply = (
            reply_text
            if len(reply_text) <= REPLY_MAX_CHARS
            else reply_text[:REPLY_MAX_CHARS] + "..."
        )

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
        report_link = self._report_link(
            topic_info.get("report_id", ""), topic_info.get("report_title", "")
        )

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
