"""Discourse engagement pipeline — monitors published topics for new comments,
classifies them, researches answers, fact-checks, and replies.

Independent from research_pipeline. Shares infrastructure only.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from html import unescape

from confluence_knowledge import ConfluenceKnowledge
from discourse_client import DiscourseClient, DiscoursePost
from discourse_knowledge import DiscourseKnowledge
from discourse_publisher import DiscoursePublisher
from glossary import GlossaryManager
from post_editor import EditRefused, PostEditor
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
from qa_archive import QAArchiver
from skills.types import LLMRunRequest
from tools.claude_runtime import ClaudeRuntimeClient
from tools.json_utils import parse_json_response

logger = logging.getLogger(__name__)

MAX_REVISE_ROUNDS = 2


def _strip_html(html: str) -> str:
    text = re.sub(r"<[^>]+>", "", html)
    return unescape(text).strip()


class DiscourseEngagement:
    """Monitors published Discourse topics and responds to comments."""

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
        self.client = discourse_client
        self.publisher = publisher
        self.runtime = runtime
        self.confluence_knowledge = confluence_knowledge
        self.discourse_knowledge = discourse_knowledge
        self.glossary = glossary
        self.post_editor = post_editor
        self.qa_archive = qa_archive
        self.scope_text = scope_text
        self.slack_callback = slack_callback  # (message: str) -> None
        self._glossary_candidates: set[str] = set()
        self._current_report_id: str = ""
        self._current_topic_info: dict = {}

    def _notify(self, message: str):
        logger.info(message)
        if self.slack_callback:
            self.slack_callback(message)

    # ── Main polling entry point ──────────────────────────────────

    def poll_and_respond(self):
        """Check all published topics for new comments and respond if needed."""
        published = self.publisher.get_published_topics()
        if not published:
            return

        logger.info("Polling %d published topics for new comments", len(published))

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

    def _process_topic(self, topic_info: dict):
        """Process a single published topic — check for new comments and respond."""
        topic_id = topic_info["topic_id"]
        report_id = topic_info["report_id"]
        last_checked = topic_info.get("last_checked_post_number", 1)

        # Stash for helpers that need access during a comment's lifecycle
        self._current_report_id = report_id
        self._current_topic_info = topic_info

        new_posts = self.client.fetch_posts_since(topic_id, last_checked)
        if not new_posts:
            return

        logger.info(
            "Topic %s: %d new posts (after post_number %d)",
            topic_id, len(new_posts), last_checked,
        )

        # Load report excerpt for context
        report_md = self.publisher._load_latest_report(report_id)
        report_excerpt = (report_md or "")[:2000]
        report_title = self._get_report_title(topic_info, report_md)

        # Build thread context from recent posts
        all_posts = self.client.fetch_posts_since(topic_id, 0)
        thread_context = self._build_thread_context(all_posts, max_posts=10)

        max_post_number = last_checked
        for post in new_posts:
            classification = self._classify_comment(
                report_title, post, thread_context,
            )
            if not classification:
                # Leave cursor behind so the next poll retries this post.
                continue
            max_post_number = max(max_post_number, post.post_number)

            ctype = classification.get("classification", "skip")
            reason = classification.get("reason", "")
            logger.info(
                "Post #%d by %s → %s (%s)",
                post.post_number, post.username, ctype, reason,
            )

            if ctype in ("skip", "discussion"):
                continue

            # Respond to question or correction
            self._respond_to_comment(
                topic_id=topic_id,
                post=post,
                comment_type=ctype,
                report_title=report_title,
                report_excerpt=report_excerpt,
                classification=classification,
            )

        # Update last checked position
        self.publisher.update_last_checked(report_id, max_post_number)

    # ── Step 1: Classify ──────────────────────────────────────────

    def _classify_comment(
        self, report_title: str, post: DiscoursePost, thread_context: str,
    ) -> dict | None:
        prompt = COMMENT_CLASSIFY_PROMPT.format(
            report_title=report_title,
            comment_author=post.username,
            comment_html=post.cooked[:3000],
            thread_context=thread_context,
        )
        result = self.runtime.run(LLMRunRequest(prompt=prompt, timeout_ms=60_000))
        if not result.success:
            return None
        parsed = parse_json_response(result.output)
        return parsed if isinstance(parsed, dict) else None

    # ── Step 2-4: Search, Draft, FactCheck, Publish ──────────────

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

        # Edit sub-flow runs first for corrections, so the reply can mention it
        edit_outcome = {"applied": False, "change_summary": "", "edit_type": "none"}
        if comment_type == "correction":
            full_report_md = self.publisher._load_latest_report(self._current_report_id) or ""
            edit_outcome = self._attempt_post_edit(
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

        # Extract draft terms, re-gather context, merge
        draft_terms = self._extract_draft_terms(draft)
        if draft_terms - set(comment_keywords):
            merged_keywords = list({*comment_keywords, *draft_terms})
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

        # Publish reply
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
        if edit_outcome and edit_outcome.get("applied"):
            edit_outcome_str = (
                f"편집 적용됨 — type={edit_outcome.get('edit_type')}, "
                f"change_summary=\"{edit_outcome.get('change_summary', '')}\""
            )
        else:
            edit_outcome_str = "(편집 없음)"
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

    def _fact_check_loop(
        self,
        comment_author: str,
        comment_text: str,
        draft: str,
        internal_context: str,
    ) -> str | None:
        """Fact-check the draft. Returns approved response or None if rejected."""
        current_draft = draft

        for attempt in range(1, MAX_REVISE_ROUNDS + 2):
            fc_result = self._fact_check(
                comment_author, comment_text, current_draft, internal_context,
            )
            if not fc_result:
                logger.warning("Fact check call failed (attempt %d)", attempt)
                return current_draft  # fail-open: post the draft if checker fails

            decision = fc_result.get("decision", "reject")
            logger.info(
                "Fact check attempt %d: %s — %s",
                attempt, decision, fc_result.get("reason", ""),
            )

            if decision == "approve":
                return current_draft
            elif decision == "reject":
                return None
            elif decision == "revise" and attempt <= MAX_REVISE_ROUNDS:
                current_draft = self._revise_draft(
                    comment_author=comment_author,
                    comment_text=comment_text,
                    draft=current_draft,
                    fact_check_feedback=json.dumps(fc_result, ensure_ascii=False),
                    internal_context=internal_context,
                )
                if not current_draft:
                    return None
            else:
                return None  # max revisions exceeded

        return None

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
        result = self.runtime.run(LLMRunRequest(prompt=prompt, timeout_ms=120_000))
        if not result.success:
            return None
        parsed = parse_json_response(result.output)
        return parsed if isinstance(parsed, dict) else None

    def _revise_draft(
        self,
        comment_author: str,
        comment_text: str,
        draft: str,
        fact_check_feedback: str,
        internal_context: str,
    ) -> str | None:
        prompt = REVISE_DRAFT_PROMPT.format(
            comment_author=comment_author,
            comment_text=comment_text,
            draft_response=draft,
            fact_check_feedback=fact_check_feedback,
            internal_context=internal_context or "(내부 문서 없음)",
        )
        result = self.runtime.run(
            LLMRunRequest(prompt=prompt, timeout_ms=300_000)
        )
        if result.success and result.output.strip():
            return result.output.strip()
        return None

    # ── Edit sub-flow ─────────────────────────────────────────────

    def _classify_edit(
        self, report_excerpt: str, comment_author: str, comment_text: str,
    ) -> dict | None:
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

    def _generate_edit(
        self, report_md: str, change_summary: str, target_section: str,
    ) -> str | None:
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
        post: DiscoursePost,
        report_md: str,
        report_excerpt: str,
        fact_check_context: str,
    ) -> dict:
        """Try to edit the published post in response to a correction.

        Returns: {"applied": bool, "change_summary": str, "edit_type": str}.
        """
        if not report_md:
            return {"applied": False, "change_summary": "", "edit_type": "none"}

        decision = self._classify_edit(
            report_excerpt, post.username, _strip_html(post.cooked),
        )
        if not decision or not decision.get("edit_needed"):
            return {"applied": False, "change_summary": "", "edit_type": "none"}

        edit_type = decision.get("edit_type", "format")
        target_section = decision.get("target_section", "") or ""
        change_summary = decision.get("change_summary", "") or ""

        new_raw = self._generate_edit(report_md, change_summary, target_section)
        if not new_raw:
            self._notify(
                f":warning: 편집 초안 생성 실패 (post=#{post.post_number})"
            )
            return {"applied": False, "change_summary": change_summary, "edit_type": edit_type}

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
                f"(post=#{post.post_number}) reason: {reason}"
            )
            return {"applied": False, "change_summary": change_summary, "edit_type": edit_type}

        post_id = self._current_topic_info.get("discourse_post_id") or 0
        try:
            self.post_editor.apply_edit(
                post_id=post_id,
                new_raw=new_raw,
                edit_reason=f"댓글 #{post.post_number} 지적 반영: {change_summary}",
                current_raw=report_md,
                edit_type=edit_type,
                change_summary=change_summary,
                triggered_by_post=post.post_number,
            )
            self._notify(
                f":pencil2: 본문 편집 적용됨 "
                f"(post_id={post_id}, triggered_by=#{post.post_number}, type={edit_type})\n"
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

    # ── Helpers ───────────────────────────────────────────────────

    def _load_glossary_text(self) -> str:
        """Return auto-block of the glossary, capped at top 50 entries."""
        text = self.glossary.load_auto_text(max_entries=50)
        return text if text else "(아직 수집된 내부 용어 glossary가 없음)"

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
            return {
                str(t).strip()
                for t in parsed
                if isinstance(t, (str, int, float)) and str(t).strip()
            }
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
        seen_headings: set[str] = set()
        out_lines: list[str] = []
        current_heading: str | None = None
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

    def _gather_internal_context(self, keywords: list[str]) -> str:
        parts = []
        confluence_ctx = self.confluence_knowledge.build_context(keywords)
        if confluence_ctx:
            parts.append(confluence_ctx)
        discourse_ctx = self.discourse_knowledge.build_context(keywords)
        if discourse_ctx:
            parts.append(discourse_ctx)
        return "\n\n".join(parts)

    def _build_thread_context(
        self, posts: list[DiscoursePost], max_posts: int = 10,
    ) -> str:
        recent = posts[-max_posts:] if len(posts) > max_posts else posts
        lines = []
        for p in recent:
            text = _strip_html(p.cooked)[:300]
            lines.append(f"[Post #{p.post_number} by {p.username}]: {text}")
        return "\n".join(lines) if lines else "(no previous posts)"

    def _get_report_title(self, topic_info: dict, report_md: str | None) -> str:
        if report_md:
            match = re.search(r"^#\s+(.+)$", report_md, re.MULTILINE)
            if match:
                return match.group(1).strip()
        return f"Topic {topic_info.get('topic_id', '?')}"
