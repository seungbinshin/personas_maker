"""Publish research reports to Discourse and track published topics."""

from __future__ import annotations

import json
import logging
import re
from html import unescape

from discourse_client import DiscourseClient
from report_store import ReportStore

logger = logging.getLogger(__name__)

REPORT_DISCLAIMER = """> 이 리포트는 **SeungbinShin이 구현한 리서치봇**이 자동 작성하였습니다.
> 지식 베이스는 **Confluence EVT1 관련 페이지**와 **Discourse에서 논의된 정보**에 의존합니다.
> 내용에 오류가 있을 수 있습니다. **틀린 부분에 대한 지적이나 질문을 댓글로 남겨주시면, 리서치봇이 추가 조사 후 답글을 달아드립니다.**

---

"""


class DiscoursePublisher:
    """Publishes research reports to Discourse topics."""

    def __init__(
        self,
        client: DiscourseClient,
        store: ReportStore,
        category_id: int,
        default_tags: list[str] | None = None,
    ):
        self.client = client
        self.store = store
        self.category_id = category_id
        self.default_tags = default_tags or ["research-bot"]

    def publish_report(self, report_id: str) -> dict | None:
        """Publish a report to Discourse. Returns created post data or None on failure.

        Skips if already published (topic_id exists in metadata).
        """
        state = self.store.get_report(report_id)
        if not state:
            logger.warning("Report %s not found", report_id)
            return None

        metadata = state.get("metadata", {})
        if metadata.get("discourse_topic_id"):
            logger.info(
                "Report %s already published (topic_id=%s)",
                report_id, metadata["discourse_topic_id"],
            )
            return None

        # Load the final report markdown
        report_md = self._load_latest_report(report_id)
        if not report_md:
            logger.warning("No report markdown found for %s", report_id)
            return None

        # Extract title from first H1
        title = self._extract_title(report_md, metadata)

        # Build post body: disclaimer + report
        body = REPORT_DISCLAIMER + report_md

        # Tags from idea keywords
        tags = list(self.default_tags)
        for kw in metadata.get("keywords", []):
            tag = re.sub(r"[^a-z0-9가-힣-]", "", kw.lower().replace(" ", "-"))
            if tag and tag not in tags:
                tags.append(tag)
        tags = tags[:5]  # Discourse default max 5 tags

        try:
            result = self.client.create_topic(
                title=title,
                raw=body,
                category_id=self.category_id,
                tags=tags,
            )
        except Exception as e:
            logger.error("Failed to publish report %s: %s", report_id, e)
            return None

        topic_id = result.get("topic_id")
        topic_slug = result.get("topic_slug", "")
        topic_url = f"{self.client.base_url}/t/{topic_slug}/{topic_id}"

        # Save topic info to report metadata
        self.store.update_state(report_id, state.get("status", "accepted"), metadata={
            "discourse_topic_id": topic_id,
            "discourse_topic_url": topic_url,
            "discourse_post_id": result.get("id"),
            "last_checked_post_number": 1,  # post 1 is the report itself
        })

        logger.info("Published report %s → %s", report_id, topic_url)
        return result

    def get_published_topics(self) -> list[dict]:
        """Get all reports that have been published to Discourse.

        Returns list of {"report_id", "topic_id", "topic_url", "last_checked_post_number"}.
        """
        published = []
        for state in self.store.list_reports():
            metadata = state.get("metadata", {})
            topic_id = metadata.get("discourse_topic_id")
            if topic_id:
                published.append({
                    "report_id": state.get("report_id"),
                    "topic_id": topic_id,
                    "topic_url": metadata.get("discourse_topic_url", ""),
                    "last_checked_post_number": metadata.get("last_checked_post_number", 1),
                })
        return published

    def update_last_checked(self, report_id: str, post_number: int):
        """Update the last checked post number for a published topic."""
        state = self.store.get_report(report_id)
        if state:
            self.store.update_state(
                report_id, state.get("status", "accepted"),
                metadata={"last_checked_post_number": post_number},
            )

    def _load_latest_report(self, report_id: str) -> str | None:
        """Load the latest report version (review_final.md > report_v*.md)."""
        # Prefer review_final.md
        report = self.store.load_artifact(report_id, "review_final.md")
        if report:
            return report
        # Fall back to latest report version
        for v in range(10, 0, -1):
            report = self.store.load_artifact(report_id, f"report_v{v}.md")
            if report:
                return report
        return None

    def _extract_title(self, md_text: str, metadata: dict) -> str:
        """Build Discourse topic title: [HA연구봇] {간결한 제목}."""
        raw_title = metadata.get("title", "")
        if not raw_title:
            match = re.search(r"^#\s+(.+)$", md_text, re.MULTILINE)
            if match:
                raw_title = match.group(1).strip()
                raw_title = re.sub(r"[*_`]", "", raw_title)
        raw_title = raw_title or "Research Report"
        # Truncate to keep total under 250
        return f"[HA연구봇] {raw_title[:230]}"
