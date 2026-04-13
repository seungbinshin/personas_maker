"""
ResearchPipeline — Human-in-the-loop research orchestration.

Flow:
  1. DISCOVERY: AI researcher proposes N ideas with investigation hints
  2. SELECTION: Human selects ideas + provides additional hints via Slack command
  3. DEEP_DIVE: AI intern deep-dives selected ideas (with self-review, no feedback loop)
  4. REPORT: AI researcher writes reports
  5. BATCH REVIEW: AI reviewer evaluates and ranks reports
  6. REVISION LOOP: For 'revise' decisions — researcher rewrites → reviewer re-evaluates
     - Max 2 rounds. If still not accepted → marked as infeasible.

Separate flow: !research dive <URL/PDF> — user-specified paper deep dive.

NOTE: claude-code-api processes one request at a time.
All LLM calls run sequentially. Heartbeat messages post every 2 min so Slack shows progress.
"""

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from pipelines.base import BasePipeline
from scope import ResearchScope
from report_store import ReportStore
from paper_cache import PaperCache
from skills.research.artifact_critique_and_revision import ArtifactCritiqueAndRevision
from skills.research.artifact_lifecycle_manager import ArtifactLifecycleManager
from skills.research.domain_fit_evaluator import DomainFitEvaluator
from skills.research.external_evidence_collector import ExternalEvidenceCollector
from skills.research.structured_artifact_authoring import StructuredArtifactAuthoring
from skills.types import LLMRunRequest
from tools.md_to_html import convert_report
from discourse_knowledge import DiscourseKnowledge

logger = logging.getLogger(__name__)

CHAT_IDLE_TIMEOUT = 1800  # 30 minutes
MAX_CHAT_SESSIONS = 3


@dataclass
class ChatSession:
    """Tracks an active report chat session bound to a Slack thread."""
    session_id: str
    report_id: str
    channel: str
    thread_ts: str
    created_at: float = field(default_factory=time.time)
    last_activity: float = field(default_factory=time.time)
    message_count: int = 0


class ResearchPipeline(BasePipeline):
    """Orchestrates sequential research with researcher-intern feedback loop."""

    def __init__(self, bot_config: dict, slack_client, api_url: str, api_key: str, bot_dir: Path):
        super().__init__(bot_config, slack_client, api_url, api_key, bot_dir)
        self.research_config = bot_config.get("research", {})
        self.publish_channel = self.research_config.get("publish_channel", "")
        self.status_channel = self.research_config.get("status_channel", "")
        self.max_intern_feedback_rounds = self.research_config.get("max_intern_feedback_rounds", 3)
        self.accept_threshold = self.research_config.get("reviewer_accept_threshold", 7)
        self.num_ideas = self.research_config.get("num_ideas", 5)
        self.max_revision_rounds = self.research_config.get("max_revision_rounds", 2)
        self._run_lock = threading.Lock()
        self._active_run_name: str | None = None
        self._chat_sessions: dict[str, ChatSession] = {}  # thread_ts -> ChatSession

        # Initialize scope with HyperAccel context
        scope_file = self.research_config.get("scope_file", "")
        context_dir = bot_dir / "context"
        if scope_file:
            self.scope = ResearchScope(bot_dir / scope_file, context_dir=context_dir)
        else:
            self.scope = None

        # Initialize report store
        self.store = ReportStore(bot_dir / "reports")

        # Initialize paper cache
        self.paper_cache = PaperCache(bot_dir / "paper_cache")
        self.fit_evaluator = DomainFitEvaluator(self.scope)
        self.evidence_collector = ExternalEvidenceCollector(self.runtime)
        self.authoring_skill = StructuredArtifactAuthoring(self.runtime)
        self.critique_skill = ArtifactCritiqueAndRevision(self.runtime)
        self.lifecycle_skill = ArtifactLifecycleManager(
            report_store=self.store,
            paper_cache=self.paper_cache,
            slack=self.slack_facade,
            status_channel=self.status_channel,
        )

        # Initialize Discourse knowledge
        discourse_config = bot_config.get("discourse", {})
        vault_rel = discourse_config.get("vault_path", "knowledge")
        self.discourse_knowledge = DiscourseKnowledge(bot_dir / vault_rel)

    def _llm(self, prompt: str, agent: str, label: str) -> str:
        """Call LLM with heartbeat. No explicit timeout — runs until done."""
        return self.call_llm(
            prompt,
            heartbeat_channel=self.status_channel,
            heartbeat_agent=agent,
            heartbeat_label=label,
        )

    # ─── Stage 1: Discovery ──────────────────────────────────────

    def run_discovery(self) -> list[dict]:
        """Researcher scans for ideas and provides investigation hints for each."""
        logger.info("=== Stage 1: Discovery ===")
        self._post_status(":microscope: 연구원이 최신 논문을 스캔합니다...", agent="researcher")

        scope_text = self.fit_evaluator.scope_text() or "General AI hardware research"
        existing_topics = self._collect_existing_topics()
        ideas = self.evidence_collector.discover_research_ideas(
            scope_text,
            self.num_ideas,
            existing_topics=existing_topics,
            heartbeat_channel=self.status_channel,
            heartbeat_agent="researcher",
            heartbeat_label="논문 스캔",
        )
        if not ideas:
            self._post_status(":warning: 아이디어 파싱 실패", agent="researcher")
            return []

        # ── Programmatic dedup: filter out ideas that already exist ──
        unique_ideas = []
        skipped = []
        for idea in ideas:
            idea_id = idea.get("idea_id", "unknown")
            source_url = idea.get("source_url", "")
            source_paper = idea.get("source_paper", "")

            existing_rid = self.store.is_duplicate(idea_id, source_url, source_paper)
            if existing_rid:
                skipped.append((idea_id, existing_rid))
                logger.info(f"Dedup: skipping '{idea_id}' — duplicate of {existing_rid}")
            else:
                unique_ideas.append(idea)

        if skipped:
            skip_msg = "\n".join(f"  • `{sid}` (기존: `{rid}`)" for sid, rid in skipped)
            self._post_status(
                f":recycle: *중복 필터링:* {len(skipped)}건 제거\n{skip_msg}",
                agent="researcher",
            )

        ideas = unique_ideas
        if not ideas:
            self._post_status(":warning: 모든 아이디어가 기존 리포트와 중복됩니다.", agent="researcher")
            return []

        # Create report entries with pending_selection status
        for idea in ideas:
            idea_id = idea.get("idea_id", "unknown")
            report_id = self.store.create_report(idea_id, metadata=idea)
            self.store.save_artifact(report_id, "idea_brief.json", json.dumps(idea, ensure_ascii=False, indent=2))
            self.store.update_state(report_id, "pending_selection")

        # Post summary with selection instructions
        lines = []
        for i, idea in enumerate(ideas, 1):
            priority = idea.get("priority", "?")
            emoji = ":star:" if priority == "high" else ":small_blue_diamond:" if priority == "medium" else ":white_small_square:"
            conf = idea.get("conference", "")
            lines.append(f"{emoji} *{i}. {idea.get('title', '')}* ({priority}) — {conf}")
            lines.append(f"    _{idea.get('summary', '')[:100]}_")

        self._post_status(
            f":microscope: *{len(ideas)}개 아이디어 발굴 완료*\n\n"
            + "\n".join(lines)
            + f"\n\n:point_right: `!research select 1,3,5 \"추가 조사 힌트\"` 로 아이디어를 선택하세요.",
            agent="researcher",
        )
        return ideas

    # ─── Stage 2: Sequential Deep Dive ───────────────────────────

    def run_deep_dives(self, report_ids: list[str]) -> dict[str, dict | None]:
        """Run intern deep dives sequentially with progress updates."""
        logger.info(f"=== Stage 2: Deep Dive ({len(report_ids)} ideas) ===")
        self._post_status(
            f":seedling: *{len(report_ids)}개 아이디어 딥다이브를 시작합니다*",
            agent="intern",
        )

        results = {}
        for i, rid in enumerate(report_ids, 1):
            self._post_status(f":seedling: 딥다이브 진행 중... ({i}/{len(report_ids)})", agent="intern")
            try:
                results[rid] = self._run_single_deep_dive(rid, i, len(report_ids))
            except Exception as e:
                logger.error(f"Deep dive failed for {rid}: {e}", exc_info=True)
                results[rid] = None

        succeeded = sum(1 for v in results.values() if v is not None)
        self._post_status(
            f":seedling: *딥다이브 완료: {succeeded}/{len(report_ids)}건 성공*",
            agent="intern",
        )
        return results

    def _run_single_deep_dive(self, report_id: str, idx: int, total: int) -> dict | None:
        """Single intern deep dive for one idea."""
        idea_brief_json = self.store.load_artifact(report_id, "idea_brief.json")
        if not idea_brief_json:
            return None

        idea = json.loads(idea_brief_json)
        idea_id = idea.get("idea_id", "unknown")
        title = idea.get("title", "")
        hints = idea.get("investigation_hints", {})
        hints_text = json.dumps(hints, ensure_ascii=False, indent=2) if hints else "No specific hints provided."

        self._post_status(f":seedling: 인턴이 *{title}* 딥다이브 시작 ({idx}/{total})", agent="intern")

        # Search paper cache for relevant previously-analyzed papers
        keywords = [idea.get("source_paper", ""), title] + hints.get("suggested_searches", [])[:3]
        keywords = [kw for kw in keywords if kw]
        cached_papers = self.lifecycle_skill.find_cached_papers(keywords, limit=5)
        cached_text = self.lifecycle_skill.format_cached_papers(cached_papers) if cached_papers else ""

        # Check for user-provided hints
        user_hints = idea.get("user_hints", "")

        scope_text = self.fit_evaluator.scope_text()
        deep_dive = self.authoring_skill.deep_dive_research(
            scope_text=scope_text,
            idea_brief_json=idea_brief_json,
            investigation_hints=hints_text,
            idea_id=idea_id,
            cached_papers=cached_text,
            user_hints=user_hints,
            heartbeat_channel=self.status_channel,
            heartbeat_agent="intern",
            heartbeat_label=f"{title} 딥다이브 ({idx}/{total})",
        )
        if not deep_dive:
            self._post_status(f":warning: *{title}* 딥다이브 실패", agent="intern")
            return None

        self.store.save_artifact(report_id, "deep_dive_v1.json", json.dumps(deep_dive, ensure_ascii=False, indent=2))
        self.store.update_state(report_id, "deep_dive")
        cached_count = self.lifecycle_skill.cache_papers_from_deep_dive(report_id, deep_dive, idea)
        self._post_status(
            f":white_check_mark: *{title}* 딥다이브 완료 ({idx}/{total}) — "
            f"관련 논문 {len(deep_dive.get('related_work', []))}건, "
            f"구현 사례 {len(deep_dive.get('implementations', []))}건"
            f"{f', 캐시 {cached_count}건' if cached_count else ''}",
            agent="intern",
        )
        return deep_dive

    # ─── Stage 3: Researcher-Intern Feedback Loop ────────────────

    def run_feedback_loop(self, report_ids: list[str]):
        """Researcher reviews each intern's work, intern revises."""
        # Filter to only report_ids that have deep dives
        active_ids = [
            rid for rid in report_ids
            if any(self.store.load_artifact(rid, f"deep_dive_v{v}.json") for v in range(10, 0, -1))
        ]
        if not active_ids:
            self._post_status(":warning: 딥다이브 결과가 없어 피드백 루프를 건너뜁니다.", agent="researcher")
            return

        logger.info(f"=== Stage 3: Feedback Loop ({len(active_ids)} ideas, max {self.max_intern_feedback_rounds} rounds) ===")

        for round_num in range(1, self.max_intern_feedback_rounds + 1):
            self._post_status(
                f":arrows_counterclockwise: *피드백 라운드 {round_num}/{self.max_intern_feedback_rounds}* ({len(active_ids)}건)",
                agent="researcher",
            )

            # Researcher reviews each sequentially
            feedback_results = {}
            for i, rid in enumerate(active_ids, 1):
                try:
                    feedback_results[rid] = self._researcher_review_intern(rid, round_num, i, len(active_ids))
                except Exception as e:
                    logger.error(f"Researcher feedback failed for {rid}: {e}")
                    feedback_results[rid] = None

            # Check which ideas need more work
            needs_revision = []
            for rid, feedback in feedback_results.items():
                if feedback is None:
                    needs_revision.append(rid)
                elif not feedback.get("ready_for_report", False):
                    needs_revision.append(rid)

            ready_count = len(active_ids) - len(needs_revision)
            self._post_status(
                f":microscope: *피드백 라운드 {round_num} 결과*: "
                f"리포트 준비 완료 {ready_count}건, 추가 조사 필요 {len(needs_revision)}건",
                agent="researcher",
            )

            if not needs_revision:
                logger.info("All ideas ready for report writing")
                break

            # Interns revise sequentially (only those needing revision)
            if round_num < self.max_intern_feedback_rounds:
                for i, rid in enumerate(needs_revision, 1):
                    try:
                        self._intern_revise(rid, round_num, feedback_results.get(rid, {}), i, len(needs_revision))
                    except Exception as e:
                        logger.error(f"Intern revision failed for {rid}: {e}")

    def _researcher_review_intern(self, report_id: str, round_num: int, idx: int, total: int) -> dict | None:
        """Researcher reviews a single intern's deep dive."""
        idea_brief_json = self.store.load_artifact(report_id, "idea_brief.json")
        if not idea_brief_json:
            return None

        idea = json.loads(idea_brief_json)
        title = idea.get("title", "")
        hints = idea.get("investigation_hints", {})
        hints_text = json.dumps(hints, ensure_ascii=False, indent=2)

        # Load latest deep dive version
        deep_dive_json = None
        for v in range(round_num + 5, 0, -1):
            deep_dive_json = self.store.load_artifact(report_id, f"deep_dive_v{v}.json")
            if deep_dive_json:
                break
        if not deep_dive_json:
            return None

        self._post_status(f":microscope: 연구원이 *{title}* 검토 중 ({idx}/{total})", agent="researcher")

        scope_text = self.fit_evaluator.scope_text()
        feedback = self.critique_skill.review_intern_deep_dive(
            scope_text=scope_text,
            idea_brief_json=idea_brief_json,
            investigation_hints=hints_text,
            deep_dive_json=deep_dive_json,
            heartbeat_channel=self.status_channel,
            heartbeat_agent="researcher",
            heartbeat_label=f"{title} 검토 ({idx}/{total})",
        )
        if feedback:
            self.store.save_artifact(
                report_id, f"feedback_v{round_num}.json",
                json.dumps(feedback, ensure_ascii=False, indent=2),
            )
            ready = feedback.get("ready_for_report", False)
            score = feedback.get("score", "?")
            emoji = ":white_check_mark:" if ready else ":arrows_counterclockwise:"
            self._post_status(
                f":microscope: *{title}* 검토 완료 ({score}점) {emoji}\n"
                f"_{feedback.get('researcher_notes', '')}_",
                agent="researcher",
            )
        return feedback

    def _intern_revise(self, report_id: str, round_num: int, feedback: dict | None, idx: int, total: int):
        """Intern revises deep dive based on researcher feedback."""
        idea_brief_json = self.store.load_artifact(report_id, "idea_brief.json")
        if not idea_brief_json:
            return

        idea = json.loads(idea_brief_json)
        idea_id = idea.get("idea_id", "unknown")
        title = idea.get("title", "")

        prev_deep_dive = None
        for v in range(round_num + 5, 0, -1):
            prev_deep_dive = self.store.load_artifact(report_id, f"deep_dive_v{v}.json")
            if prev_deep_dive:
                break
        if not prev_deep_dive:
            return

        feedback_text = json.dumps(feedback, ensure_ascii=False, indent=2) if feedback else "{}"

        self._post_status(
            f":seedling: 인턴이 *{title}* 피드백 반영 중 ({idx}/{total}, 라운드 {round_num + 1})",
            agent="intern",
        )

        scope_text = self.fit_evaluator.scope_text()
        revised = self.critique_skill.revise_deep_dive(
            scope_text=scope_text,
            idea_brief_json=idea_brief_json,
            previous_deep_dive=prev_deep_dive,
            researcher_feedback=feedback_text,
            idea_id=idea_id,
            heartbeat_channel=self.status_channel,
            heartbeat_agent="intern",
            heartbeat_label=f"{title} 재조사 ({idx}/{total})",
        )
        if not revised:
            self._post_status(f":warning: *{title}* 수정 실패", agent="intern")
            return
        new_version = round_num + 1
        self.store.save_artifact(
            report_id, f"deep_dive_v{new_version}.json",
            json.dumps(revised, ensure_ascii=False, indent=2),
        )
        self._post_status(f":white_check_mark: *{title}* 수정 완료 (v{new_version})", agent="intern")

    # ─── Stage 4: Report Writing ─────────────────────────────────

    def run_reports(self, report_ids: list[str]) -> dict[str, str | None]:
        """Researcher writes reports sequentially."""
        # Filter to only those with deep dives
        active_ids = [
            rid for rid in report_ids
            if any(self.store.load_artifact(rid, f"deep_dive_v{v}.json") for v in range(10, 0, -1))
        ]
        if not active_ids:
            self._post_status(":warning: 딥다이브 결과가 없어 리포트 작성을 건너뜁니다.", agent="researcher")
            return {}

        logger.info(f"=== Stage 4: Report Writing ({len(active_ids)} ideas) ===")
        self._post_status(
            f":microscope: *연구원이 {len(active_ids)}개 리포트를 작성합니다*",
            agent="researcher",
        )

        results = {}
        for i, rid in enumerate(active_ids, 1):
            try:
                results[rid] = self._write_single_report(rid, i, len(active_ids))
            except Exception as e:
                logger.error(f"Report writing failed for {rid}: {e}")
                results[rid] = None

        succeeded = sum(1 for v in results.values() if v is not None)
        self._post_status(
            f":microscope: *리포트 작성 완료: {succeeded}/{len(active_ids)}건*",
            agent="researcher",
        )
        return results

    def _write_single_report(self, report_id: str, idx: int, total: int) -> str | None:
        """Write a single report."""
        idea_brief_json = self.store.load_artifact(report_id, "idea_brief.json")
        if not idea_brief_json:
            return None

        idea = json.loads(idea_brief_json)
        title = idea.get("title", "")

        deep_dive_json = None
        for v in range(10, 0, -1):
            deep_dive_json = self.store.load_artifact(report_id, f"deep_dive_v{v}.json")
            if deep_dive_json:
                break
        if not deep_dive_json:
            return None

        self._post_status(f":microscope: *{title}* 리포트 작성 중 ({idx}/{total})", agent="researcher")

        scope_text = self.fit_evaluator.scope_text()
        report = self.authoring_skill.write_research_report(
            scope_text=scope_text,
            idea_brief_json=idea_brief_json,
            deep_dive_json=deep_dive_json,
            heartbeat_channel=self.status_channel,
            heartbeat_agent="researcher",
            heartbeat_label=f"{title} 리포트 작성 ({idx}/{total})",
        )
        if report:
            self.store.save_artifact(report_id, "report_v1.md", report)
            self.store.update_state(report_id, "report_draft")
            self._post_status(f":white_check_mark: *{title}* 리포트 작성 완료 ({idx}/{total})", agent="researcher")
        return report

    # ─── Stage 5: Batch Review ───────────────────────────────────

    def run_batch_review(self, report_ids: list[str]) -> dict | None:
        """Reviewer evaluates all reports at once and ranks them."""
        # Filter to only those with reports
        valid_ids = []
        reports_block_parts = []
        for rid in report_ids:
            report = self.store.load_artifact(rid, "report_v1.md")
            idea_brief_json = self.store.load_artifact(rid, "idea_brief.json")
            if not report or not idea_brief_json:
                continue
            idea = json.loads(idea_brief_json)
            idea_id = idea.get("idea_id", "unknown")
            valid_ids.append(rid)
            reports_block_parts.append(
                f"--- REPORT: {idea_id} ---\n"
                f"Title: {idea.get('title', '')}\n\n"
                f"{report}\n\n"
                f"--- END REPORT: {idea_id} ---"
            )

        if not reports_block_parts:
            self._post_status(":warning: 리뷰할 리포트가 없습니다.", agent="reviewer")
            return None

        logger.info(f"=== Stage 5: Batch Review ({len(valid_ids)} reports) ===")
        reports_block = "\n\n".join(reports_block_parts)

        self._post_status(
            f":judge: *리뷰어가 {len(valid_ids)}개 리포트를 일괄 평가합니다*",
            agent="reviewer",
        )

        scope_text = self.fit_evaluator.scope_text()
        review = self.critique_skill.batch_review_reports(
            scope_text=scope_text,
            num_reports=len(valid_ids),
            accept_threshold=self.accept_threshold,
            reports_block=reports_block,
            heartbeat_channel=self.status_channel,
            heartbeat_agent="reviewer",
            heartbeat_label=f"{len(valid_ids)}개 리포트 일괄 평가",
        )
        if not review:
            return None

        # Save full batch review to each report's reviewer/ folder
        batch_review_json = json.dumps(review, ensure_ascii=False, indent=2)
        for rid in valid_ids:
            self.store.save_artifact(rid, "batch_review.json", batch_review_json)

        # Save individual reviews per report
        reviews = review.get("reviews", [])
        for rev in reviews:
            idea_id = rev.get("idea_id", "")
            for rid in valid_ids:
                if idea_id in rid:
                    self.store.save_artifact(
                        rid, "review_v1.json",
                        json.dumps(rev, ensure_ascii=False, indent=2),
                    )
                    decision = rev.get("decision", "unknown")
                    if decision == "accept":
                        report = self.store.load_artifact(rid, "report_v1.md")
                        if report:
                            self.store.save_artifact(rid, "report_final.md", report)
                        self.store.update_state(rid, "accepted")
                    elif decision == "reject":
                        self.store.update_state(rid, "rejected")
                    else:
                        self.store.update_state(rid, "revise")
                    break

        self._post_batch_review_summary(review)
        return review

    def _post_batch_review_summary(self, review: dict):
        """Post formatted batch review results to status channel."""
        reviews = review.get("reviews", [])
        ranking = review.get("ranking", [])
        batch_summary = review.get("batch_summary", "")

        lines = [":judge: *리뷰어 일괄 평가 완료*\n"]

        for rev in reviews:
            decision = rev.get("decision", "?")
            idea_id = rev.get("idea_id", "?")
            scores = rev.get("scores", {})
            avg_score = 0
            count = 0
            for val in scores.values():
                if isinstance(val, dict) and "score" in val:
                    avg_score += val["score"]
                    count += 1
            avg_score = avg_score / count if count else 0

            emoji = ":white_check_mark:" if decision == "accept" else ":arrows_counterclockwise:" if decision == "revise" else ":no_entry:"
            lines.append(f"{emoji} `{idea_id}` — *{decision.upper()}* (평균 {avg_score:.1f}/10)")

        if ranking:
            lines.append("\n*순위:*")
            for r in ranking:
                lines.append(f"  {r.get('rank', '?')}위: `{r.get('idea_id', '?')}` — {r.get('reason', '')}")

        if batch_summary:
            lines.append(f"\n_총평: {batch_summary}_")

        self._post_status("\n".join(lines), agent="reviewer")

    # ─── Stage 6: Post-Review Revision Loop ─────────────────────

    def run_revision_loop(self, report_ids: list[str], batch_review: dict):
        """Researcher rewrites reports that got 'revise' decision. Max 2 rounds."""
        reviews_by_idea = {r.get("idea_id", ""): r for r in batch_review.get("reviews", [])}

        # Find report_ids with revise decision
        revise_ids = []
        for rid in report_ids:
            state = self.store.get_report(rid)
            if state and state.get("status") == "revise":
                revise_ids.append(rid)

        if not revise_ids:
            return

        logger.info(f"=== Stage 6: Revision Loop ({len(revise_ids)} reports, max {self.max_revision_rounds} rounds) ===")

        for round_num in range(1, self.max_revision_rounds + 1):
            self._post_status(
                f":arrows_counterclockwise: *리비전 라운드 {round_num}/{self.max_revision_rounds}* ({len(revise_ids)}건)",
                agent="researcher",
            )

            # Researcher rewrites each report
            revised_ids = []
            for i, rid in enumerate(revise_ids, 1):
                try:
                    report = self._researcher_revise_report(rid, round_num, i, len(revise_ids))
                    if report:
                        revised_ids.append(rid)
                except Exception as e:
                    logger.error(f"Revision failed for {rid}: {e}", exc_info=True)

            if not revised_ids:
                break

            # Reviewer re-evaluates revised reports
            re_review = self._review_revised_reports(revised_ids, round_num)
            if not re_review:
                break

            # Check results
            still_revise = []
            for rev in re_review.get("reviews", []):
                idea_id = rev.get("idea_id", "")
                decision = rev.get("decision", "unknown")
                for rid in revised_ids:
                    if idea_id in rid:
                        review_version = round_num + 1
                        self.store.save_artifact(
                            rid, f"review_v{review_version}.json",
                            json.dumps(rev, ensure_ascii=False, indent=2),
                        )
                        if decision == "accept":
                            report_version = round_num + 1
                            report = self.store.load_artifact(rid, f"report_v{report_version}.md")
                            if report:
                                self.store.save_artifact(rid, "report_final.md", report)
                            self.store.update_state(rid, "accepted")
                        elif decision == "reject":
                            self.store.update_state(rid, "rejected")
                        else:
                            still_revise.append(rid)
                        break

            accepted_count = len(revised_ids) - len(still_revise)
            self._post_status(
                f":judge: *리비전 라운드 {round_num} 결과*: "
                f"승인 {accepted_count}건, 추가 수정 필요 {len(still_revise)}건",
                agent="reviewer",
            )

            revise_ids = still_revise
            if not revise_ids:
                break

        # Mark remaining revise reports as infeasible after max rounds
        for rid in revise_ids:
            self.store.update_state(rid, "infeasible", metadata={"reason": f"{self.max_revision_rounds}회 리비전 후에도 승인 불가"})
            state = self.store.get_report(rid)
            idea_id = state.get("idea_id", "unknown") if state else "unknown"
            self._post_status(
                f":no_entry: `{idea_id}` — {self.max_revision_rounds}회 리비전 후 실현 불가능으로 판단, 종료",
                agent="reviewer",
            )

    def _researcher_revise_report(self, report_id: str, round_num: int, idx: int, total: int) -> str | None:
        """Researcher rewrites a report based on reviewer feedback."""
        idea_brief_json = self.store.load_artifact(report_id, "idea_brief.json")
        if not idea_brief_json:
            return None

        idea = json.loads(idea_brief_json)
        title = idea.get("title", "")

        # Load latest report version
        report_version = round_num  # v1 for round 1, v2 for round 2
        previous_report = self.store.load_artifact(report_id, f"report_v{report_version}.md")
        if not previous_report:
            return None

        # Load latest review
        review_json = self.store.load_artifact(report_id, f"review_v{round_num}.json")
        if not review_json:
            return None

        # Load latest deep dive
        deep_dive_json = None
        for v in range(10, 0, -1):
            deep_dive_json = self.store.load_artifact(report_id, f"deep_dive_v{v}.json")
            if deep_dive_json:
                break
        if not deep_dive_json:
            return None

        self._post_status(
            f":microscope: 연구원이 *{title}* 리포트 수정 중 ({idx}/{total}, 라운드 {round_num})",
            agent="researcher",
        )

        result = self.critique_skill.revise_report(
            scope_text=self.fit_evaluator.scope_text(),
            idea_brief_json=idea_brief_json,
            deep_dive_json=deep_dive_json,
            previous_report=previous_report,
            reviewer_feedback=review_json,
            report_version=report_version,
            heartbeat_channel=self.status_channel,
            heartbeat_agent="researcher",
            heartbeat_label=f"{title} 리포트 수정 ({idx}/{total})",
        )
        if not result:
            return None

        new_version = report_version + 1
        self.store.save_artifact(report_id, f"report_v{new_version}.md", result)
        self._post_status(f":white_check_mark: *{title}* 리포트 v{new_version} 작성 완료", agent="researcher")
        return result

    def _review_revised_reports(self, report_ids: list[str], round_num: int) -> dict | None:
        """Reviewer re-evaluates revised reports."""
        report_version = round_num + 1
        reports_block_parts = []
        valid_ids = []

        for rid in report_ids:
            report = self.store.load_artifact(rid, f"report_v{report_version}.md")
            idea_brief_json = self.store.load_artifact(rid, "idea_brief.json")
            if not report or not idea_brief_json:
                continue
            idea = json.loads(idea_brief_json)
            idea_id = idea.get("idea_id", "unknown")
            valid_ids.append(rid)
            reports_block_parts.append(
                f"--- REPORT (REVISED v{report_version}): {idea_id} ---\n"
                f"Title: {idea.get('title', '')}\n\n"
                f"{report}\n\n"
                f"--- END REPORT: {idea_id} ---"
            )

        if not reports_block_parts:
            return None

        self._post_status(
            f":judge: *리뷰어가 수정된 {len(valid_ids)}개 리포트를 재평가합니다* (라운드 {round_num + 1})",
            agent="reviewer",
        )

        reports_block = "\n\n".join(reports_block_parts)
        review = self.critique_skill.batch_review_reports(
            scope_text=self.fit_evaluator.scope_text(),
            num_reports=len(valid_ids),
            accept_threshold=self.accept_threshold,
            reports_block=reports_block,
            heartbeat_channel=self.status_channel,
            heartbeat_agent="reviewer",
            heartbeat_label=f"수정 리포트 {len(valid_ids)}건 재평가",
        )
        if review:
            batch_json = json.dumps(review, ensure_ascii=False, indent=2)
            for rid in valid_ids:
                self.store.save_artifact(rid, "batch_review.json", batch_json)

        return review

    # ─── Full Pipeline Orchestration ─────────────────────────────

    PENDING_THRESHOLD = 15

    def run_full_pipeline(self):
        """Run discovery — or auto-analyze top pending ideas if backlog >= PENDING_THRESHOLD."""
        # Check pending backlog before discovery
        all_reports = self.store.list_reports()
        pending = [r for r in all_reports if r.get("status") == "pending_selection"]

        if len(pending) >= self.PENDING_THRESHOLD:
            logger.info(
                f"Pending backlog ({len(pending)}) >= threshold ({self.PENDING_THRESHOLD}). "
                "Skipping discovery — auto-analyzing top ideas."
            )
            self._post_status(
                f":inbox_tray: *미처리 논문 {len(pending)}건 (>= {self.PENDING_THRESHOLD}건)* — "
                f"신규 아카이빙을 건너뛰고 기존 대기 논문 중 유망 후보를 자동 분석합니다.",
                agent="researcher",
            )
            self._auto_analyze_top_pending(pending)
            return

        if not self._begin_run("discovery"):
            return
        logger.info("=== Research Pipeline: Discovery (awaiting human selection) ===")
        try:
            self.run_discovery()
        except Exception as e:
            logger.error(f"Research pipeline error: {e}", exc_info=True)
            self._post_status(f":x: 파이프라인 에러: {str(e)[:200]}")
        finally:
            self._end_run()

    def _auto_analyze_top_pending(self, pending: list[dict]):
        """Auto-select top-priority pending ideas and run the full analysis pipeline."""
        if not self._begin_run("auto-analyze-pending"):
            return
        try:
            # Sort by priority: high > medium > low
            priority_order = {"high": 0, "medium": 1, "low": 2}
            pending.sort(key=lambda r: priority_order.get(
                r.get("metadata", {}).get("priority", "low"), 2
            ))

            # Select top ideas (up to num_ideas configured, default 5)
            count = min(self.num_ideas, len(pending))
            selected = pending[:count]
            report_ids = [r["report_id"] for r in selected]

            lines = []
            for i, r in enumerate(selected, 1):
                idea_id = r.get("idea_id", "?")
                title = r.get("metadata", {}).get("title", idea_id)
                priority = r.get("metadata", {}).get("priority", "?")
                lines.append(f"  {i}. *{title}* (`{idea_id}`, {priority})")

            self._post_status(
                f":rocket: *상위 {count}개 유망 논문 자동 분석 시작*\n\n"
                + "\n".join(lines),
                agent="researcher",
            )

            self._run_stages_after_selection(report_ids)

        except Exception as e:
            logger.error(f"Auto-analyze pending error: {e}", exc_info=True)
            self._post_status(f":x: 자동 분석 에러: {str(e)[:200]}")
        finally:
            self._end_run()

    def select_ideas(self, indices: list[int], extra_hints: str = ""):
        """Human selects ideas by index. Runs deep dive → report → review."""
        if not self._begin_run("selection"):
            return
        logger.info(f"=== Human Selection: indices={indices}, hints='{extra_hints}' ===")

        try:
            # Get pending_selection reports sorted by creation time
            all_reports = self.store.list_reports()
            pending = [r for r in all_reports if r.get("status") == "pending_selection"]
            # list_reports returns reverse sorted, so reverse to get chronological order
            pending.reverse()

            if not pending:
                self._post_status(":warning: 선택 대기 중인 아이디어가 없습니다.")
                return

            # Select by 1-based index
            selected_ids = []
            selected_titles = []
            for idx in indices:
                if 1 <= idx <= len(pending):
                    report = pending[idx - 1]
                    rid = report["report_id"]
                    selected_ids.append(rid)
                    title = report.get("metadata", {}).get("title", report.get("idea_id", "?"))
                    selected_titles.append(f"  {idx}. *{title}*")

                    # Add user hints to idea_brief if provided
                    if extra_hints:
                        idea_brief_json = self.store.load_artifact(rid, "idea_brief.json")
                        if idea_brief_json:
                            idea = json.loads(idea_brief_json)
                            idea["user_hints"] = extra_hints
                            self.store.save_artifact(rid, "idea_brief.json", json.dumps(idea, ensure_ascii=False, indent=2))

            # Mark unselected as skipped
            for i, report in enumerate(pending, 1):
                if i not in indices:
                    self.store.update_state(report["report_id"], "skipped")

            if not selected_ids:
                self._post_status(":warning: 유효한 아이디어 번호가 없습니다.")
                return

            self._post_status(
                f":rocket: *{len(selected_ids)}개 아이디어 선택 완료*\n\n"
                + "\n".join(selected_titles)
                + (f"\n\n:bulb: 추가 힌트: _{extra_hints}_" if extra_hints else ""),
                agent="researcher",
            )

            self._run_stages_after_selection(selected_ids)

        except Exception as e:
            logger.error(f"Selection pipeline error: {e}", exc_info=True)
            self._post_status(f":x: 파이프라인 에러: {str(e)[:200]}")
        finally:
            self._end_run()

    def dive_paper(self, url: str = "", pdf_text: str = "", user_hint: str = ""):
        """User-specified paper deep dive. Takes arXiv URL or extracted PDF text."""
        if not self._begin_run("paper-dive"):
            return
        logger.info(f"=== Paper Dive: url='{url}', hint='{user_hint}' ===")

        try:
            # Build paper info from URL or PDF text
            paper_info = ""
            if url:
                paper_info = f"URL: {url}\n"
                self._post_status(f":mag: 논문 정보를 가져옵니다: {url}", agent="researcher")
                fetched = self.evidence_collector.fetch_page_info(
                    url,
                    heartbeat_channel=self.status_channel,
                    heartbeat_agent="researcher",
                    heartbeat_label="논문 정보 수집",
                )
                if fetched:
                    paper_info += fetched
            elif pdf_text:
                paper_info = f"PDF content (first pages):\n{pdf_text[:3000]}"

            if not paper_info:
                self._post_status(":warning: 논문 정보를 가져올 수 없습니다.", agent="researcher")
                return

            # AI researcher generates idea brief
            self._post_status(":microscope: 연구원이 아이디어 브리프를 작성합니다...", agent="researcher")
            idea = self.evidence_collector.create_paper_brief(
                self.fit_evaluator.scope_text(),
                paper_info,
                user_hint,
                heartbeat_channel=self.status_channel,
                heartbeat_agent="researcher",
                heartbeat_label="논문 브리프 작성",
            )
            if not idea:
                self._post_status(":warning: 브리프 파싱 실패", agent="researcher")
                return

            # Add user hints
            if user_hint:
                idea["user_hints"] = user_hint

            # Create report and run pipeline
            idea_id = idea.get("idea_id", "unknown")
            report_id = self.store.create_report(idea_id, metadata=idea)
            self.store.save_artifact(report_id, "idea_brief.json", json.dumps(idea, ensure_ascii=False, indent=2))

            self._post_status(
                f":microscope: *논문 딥다이브 시작*: {idea.get('title', '')}\n"
                f"_{idea.get('summary', '')[:150]}_",
                agent="researcher",
            )

            self._run_stages_after_selection([report_id])

        except Exception as e:
            logger.error(f"Paper dive error: {e}", exc_info=True)
            self._post_status(f":x: 논문 딥다이브 에러: {str(e)[:200]}")
        finally:
            self._end_run()

    def research_topic(self, topic: str, user_hint: str = ""):
        """User-specified topic investigation. Searches for papers and creates brief."""
        if not self._begin_run("topic-research"):
            return
        logger.info(f"=== Topic Research: topic='{topic}', hint='{user_hint}' ===")

        try:
            self._post_status(f":mag: 주제 조사를 시작합니다: *{topic}*", agent="researcher")

            # Step 1: Search for relevant papers and create brief
            self._post_status(f":microscope: 연구원이 *{topic}* 관련 논문을 검색하고 브리프를 작성합니다...", agent="researcher")
            idea = self.evidence_collector.research_topic(
                self.fit_evaluator.scope_text(),
                topic,
                user_hint,
                heartbeat_channel=self.status_channel,
                heartbeat_agent="researcher",
                heartbeat_label=f"주제 조사: {topic}",
            )
            if not idea:
                self._post_status(":warning: 주제 브리프 생성 실패", agent="researcher")
                return

            if user_hint:
                idea["user_hints"] = user_hint

            # Step 2: Save brief and report
            idea_id = idea.get("idea_id", "unknown")
            report_id = self.store.create_report(idea_id, metadata=idea)
            self.store.save_artifact(report_id, "idea_brief.json", json.dumps(idea, ensure_ascii=False, indent=2))

            title = idea.get("title", topic)
            source = idea.get("source_paper", "")
            conf = idea.get("conference", "")
            priority = idea.get("priority", "?")
            self._post_status(
                f":white_check_mark: *브리프 작성 완료*: {title}\n"
                f"  :page_facing_up: 논문: _{source}_ ({conf})\n"
                f"  :dart: 우선순위: *{priority}*\n"
                f"  _{idea.get('summary', '')[:150]}_\n\n"
                f":rocket: 딥다이브 → 리포트 → 리뷰를 시작합니다...",
                agent="researcher",
            )

            self._run_stages_after_selection([report_id])

        except Exception as e:
            logger.error(f"Topic research error: {e}", exc_info=True)
            self._post_status(f":x: 주제 조사 에러: {str(e)[:200]}")
        finally:
            self._end_run()

    def run_from_existing(self, count: int = 5):
        """Run pipeline for existing pending_selection ideas."""
        if not self._begin_run("resume"):
            return
        logger.info(f"=== Research Pipeline: Resume from existing ({count} ideas) ===")

        try:
            all_reports = self.store.list_reports()
            pending = [r for r in all_reports if r.get("status") in ("pending_selection", "discovery")]

            if not pending:
                self._post_status(":warning: 대기 중인 아이디어가 없습니다.")
                return

            selected = pending[:count]
            report_ids = [r["report_id"] for r in selected]

            lines = []
            for r in selected:
                idea_id = r.get("idea_id", "?")
                title = r.get("metadata", {}).get("title", idea_id)
                lines.append(f"  • *{title}* (`{idea_id}`)")

            self._post_status(
                f":rocket: *기존 아이디어 {len(report_ids)}개로 파이프라인 재개*\n\n"
                + "\n".join(lines),
                agent="researcher",
            )

            self._run_stages_after_selection(report_ids)

        except Exception as e:
            logger.error(f"Research pipeline error: {e}", exc_info=True)
            self._post_status(f":x: 파이프라인 에러: {str(e)[:200]}")
        finally:
            self._end_run()

    def auto_report_top_idea(self):
        """Auto-select the highest-priority pending idea and run the full pipeline."""
        all_reports = self.store.list_reports()
        pending = [r for r in all_reports if r.get("status") == "pending_selection"]
        if not pending:
            self._post_status(":moon: 자동 리포트: 대기 중인 아이디어가 없습니다.")
            return

        # Sort by priority: high > medium > low
        priority_order = {"high": 0, "medium": 1, "low": 2}
        pending.sort(key=lambda r: priority_order.get(
            r.get("metadata", {}).get("priority", "low"), 2
        ))

        top = pending[0]
        rid = top["report_id"]
        title = top.get("metadata", {}).get("title", top.get("idea_id", "?"))
        priority = top.get("metadata", {}).get("priority", "?")

        self._post_status(
            f":crescent_moon: *자동 리포트 시작*: {title} (priority: {priority})",
            agent="researcher",
        )

        if not self._begin_run("auto-report"):
            return
        try:
            self._run_stages_after_selection([rid])
        except Exception as e:
            logger.error(f"Auto report error: {e}", exc_info=True)
            self._post_status(f":x: 자동 리포트 에러: {str(e)[:200]}")
        finally:
            self._end_run()

    def _run_stages_after_selection(self, report_ids: list[str]):
        """Run deep dive → report → review (no feedback loop)."""
        self.run_deep_dives(report_ids)
        self.run_reports(report_ids)

        batch_review = self.run_batch_review(report_ids)
        if batch_review:
            has_revise = any(
                rev.get("decision") == "revise"
                for rev in batch_review.get("reviews", [])
            )
            if has_revise:
                self.run_revision_loop(report_ids, batch_review)

            if self.publish_channel:
                self._publish_accepted_reports_final(report_ids)

        self._post_status(":checkered_flag: *연구 파이프라인 완료!*")
        logger.info("=== Research Pipeline: Complete ===")

    def _publish_accepted_reports(self, report_ids: list[str], batch_review: dict):
        """Publish accepted reports to the publish channel (from initial batch review)."""
        ranking = batch_review.get("ranking", [])
        reviews = {r.get("idea_id", ""): r for r in batch_review.get("reviews", [])}

        for rank_entry in ranking:
            idea_id = rank_entry.get("idea_id", "")
            rev = reviews.get(idea_id, {})
            if rev.get("decision") != "accept":
                continue

            for rid in report_ids:
                if idea_id not in rid:
                    continue
                report = self.store.load_artifact(rid, "report_final.md")
                idea_brief_json = self.store.load_artifact(rid, "idea_brief.json")
                if not report or not idea_brief_json:
                    continue
                idea = json.loads(idea_brief_json)

                rank = rank_entry.get("rank", "?")
                self.post_to_slack(
                    channel=self.publish_channel,
                    text=(
                        f":microscope: *연구 리포트 #{rank}: {idea.get('title', '')}*\n"
                        f"_{rank_entry.get('reason', '')}_\n\n"
                        f"{report[:3000]}"
                    ),
                    agent_name="researcher",
                )
                break

    def _publish_accepted_reports_final(self, report_ids: list[str]):
        """Publish all accepted reports as HTML files to Slack."""
        for rid in report_ids:
            state = self.store.get_report(rid)
            if not state or state.get("status") != "accepted":
                continue
            report = self.store.load_artifact(rid, "report_final.md")
            idea_brief_json = self.store.load_artifact(rid, "idea_brief.json")
            if not report or not idea_brief_json:
                continue
            idea = json.loads(idea_brief_json)
            title = idea.get("title", "Research Report")

            # Convert MD → HTML and save locally
            html = convert_report(report, title)
            self.store.save_artifact(rid, "report_final.html", html)

            # Upload HTML file to Slack
            idea_id = idea.get("idea_id", "report")
            uploaded = self.slack_facade.upload_file(
                channel=self.publish_channel,
                content=html,
                filename=f"{idea_id}_report.html",
                title=title,
                initial_comment=f":microscope: *연구 리포트: {title}*",
            )
            if not uploaded:
                self.post_to_slack(
                    channel=self.publish_channel,
                    text=f":microscope: *연구 리포트: {title}*\n\n{report[:3000]}",
                    agent_name="researcher",
                )

    # ─── Status & Utility ────────────────────────────────────────

    def get_status_summary(self) -> str:
        """Get a summary of all reports and their statuses."""
        reports = self.store.list_reports()
        if not reports:
            return "현재 진행 중인 연구가 없습니다."

        lines = ["*연구 파이프라인 현황*\n"]
        for r in reports:
            status = r.get("status", "unknown")
            idea_id = r.get("idea_id", "unknown")
            review_count = r.get("review_count", 0)
            emoji = {
                "pending_selection": ":ballot_box:",
                "skipped": ":fast_forward:",
                "discovery": ":mag:",
                "deep_dive": ":seedling:",
                "report_draft": ":memo:",
                "review": ":judge:",
                "accepted": ":white_check_mark:",
                "rejected": ":no_entry:",
                "revise": ":arrows_counterclockwise:",
                "infeasible": ":no_entry_sign:",
                "max_iterations": ":warning:",
            }.get(status, ":question:")
            lines.append(f"{emoji} `{idea_id}` — {status} (리뷰 {review_count}회)")

        # Paper cache stats
        cache_stats = self.paper_cache.get_cache_stats()
        if cache_stats["total_papers"] > 0:
            lines.append(f"\n:books: 논문 캐시: {cache_stats['total_papers']}편")

        return "\n".join(lines)

    def _collect_existing_topics(self) -> str:
        """Build a comprehensive dedup list from report store + paper cache.

        Includes idea_id, title, source_paper, AND source_url so the LLM
        can avoid proposing the same paper even if titles differ slightly.
        """
        lines = []
        seen_urls: set[str] = set()

        # From report store: all idea_ids, titles, source papers, and URLs
        for r in self.store.list_reports():
            idea_id = r.get("idea_id", "")
            meta = r.get("metadata", {})
            title = meta.get("title", "")
            source = meta.get("source_paper", "")
            url = meta.get("source_url", "")
            if idea_id:
                entry = f"- {idea_id}: {title}"
                if source:
                    entry += f" (paper: {source})"
                if url:
                    entry += f" [url: {url}]"
                    seen_urls.add(url.strip().rstrip("/").lower())
                lines.append(entry)

        # From paper cache: ALL cached papers (not just 30)
        cached = self.paper_cache.list_papers()
        for p in cached:
            p_title = p.get("title", p.get("paper_id", ""))
            if p_title and not any(p_title.lower() in line.lower() for line in lines):
                lines.append(f"- [cached] {p_title}")

        return "\n".join(lines) if lines else "(none)"

    def _post_status(self, text: str, agent: str | None = None):
        """Post a status update to the status channel."""
        if self.status_channel:
            self.post_to_slack(
                channel=self.status_channel,
                text=text,
                agent_name=agent,
            )

    def _begin_run(self, run_name: str) -> bool:
        if self._run_lock.acquire(blocking=False):
            self._active_run_name = run_name
            return True
        self._post_status(
            f":warning: 다른 연구 작업(`{self._active_run_name or 'running'}`)이 진행 중이라 새 요청을 건너뜁니다.",
            agent="researcher",
        )
        return False

    def _end_run(self) -> None:
        self._active_run_name = None
        if self._run_lock.locked():
            self._run_lock.release()

    # ─── Report Chat ─────────────────────────────────────────────

    def start_chat_session(self, report_id: str, channel: str, thread_ts: str) -> str | None:
        """Start a chat session about a report. Returns the first response or None on failure."""
        self._cleanup_expired_sessions()

        # Check max sessions
        if len(self._chat_sessions) >= MAX_CHAT_SESSIONS:
            oldest_ts = min(self._chat_sessions, key=lambda k: self._chat_sessions[k].last_activity)
            self.end_chat_session(oldest_ts)

        # Load report artifacts
        report = self.store.get_report(report_id)
        if not report:
            return None

        artifacts = self.store.load_all_artifacts(report_id)
        if not artifacts:
            return None

        # Build context prompt
        from prompts.researcher import RESEARCHER_CHAT_SYSTEM_PROMPT

        scope_text = self.fit_evaluator.scope_text() or "General AI hardware research"

        idea_brief = artifacts.get("idea_brief.json", "N/A")
        deep_dive = self._find_latest_artifact(artifacts, "deep_dive_v", ".json") or "N/A"
        report_text = (
            artifacts.get("report_final.md")
            or self._find_latest_artifact(artifacts, "report_v", ".md")
            or "N/A"
        )

        review = self._find_latest_artifact(artifacts, "review_v", ".json")
        review_section = f"=== Review Results ===\n{review}" if review else ""

        # Use replace() instead of .format() because artifact content
        # contains JSON with curly braces that would break str.format()
        prompt = (
            RESEARCHER_CHAT_SYSTEM_PROMPT
            .replace("{scope}", scope_text)
            .replace("{idea_brief}", idea_brief)
            .replace("{deep_dive}", deep_dive)
            .replace("{report}", report_text)
            .replace("{review_section}", review_section)
        )

        # Create session via LLM call
        result = self.runtime.run(
            LLMRunRequest(
                prompt=prompt,
                heartbeat_channel=self.status_channel,
                heartbeat_agent="researcher",
                heartbeat_label="리포트 대화 시작",
            )
        )
        if not result.success or not result.session_id:
            logger.error("Failed to create chat session for report %s", report_id)
            return None

        session = ChatSession(
            session_id=result.session_id,
            report_id=report_id,
            channel=channel,
            thread_ts=thread_ts,
            message_count=1,
        )
        self._chat_sessions[thread_ts] = session
        logger.info("Chat session started: thread=%s report=%s session=%s", thread_ts, report_id, result.session_id)
        return result.output

    def continue_chat(self, thread_ts: str, user_message: str) -> str | None:
        """Continue an existing chat session. Returns the response or None."""
        self._cleanup_expired_sessions()

        session = self._chat_sessions.get(thread_ts)
        if not session:
            return None

        session.last_activity = time.time()
        session.message_count += 1

        result = self.runtime.run(
            LLMRunRequest(
                prompt=user_message,
                session_id=session.session_id,
                heartbeat_channel=self.status_channel,
                heartbeat_agent="researcher",
                heartbeat_label="리포트 대화",
            )
        )
        if not result.success:
            logger.error("Chat continuation failed for thread %s", thread_ts)
            return None

        return result.output

    def end_chat_session(self, thread_ts: str) -> int:
        """End a chat session. Returns the message count."""
        session = self._chat_sessions.pop(thread_ts, None)
        if not session:
            return 0

        # Close the claude-code-api session
        try:
            import requests
            requests.delete(
                f"{self.api_url}/session",
                headers={"Content-Type": "application/json", "x-api-key": self.api_key},
                json={"sessionId": session.session_id},
                timeout=5,
            )
        except Exception as e:
            logger.warning("Failed to close API session %s: %s", session.session_id, e)

        logger.info("Chat session ended: thread=%s messages=%d", thread_ts, session.message_count)
        return session.message_count

    def has_chat_session(self, thread_ts: str) -> bool:
        """Check if a thread has an active (non-expired) chat session."""
        self._cleanup_expired_sessions()
        return thread_ts in self._chat_sessions

    def list_chattable_reports(self) -> list[dict]:
        """List reports that have enough artifacts for a chat session."""
        reports = self.store.list_reports()
        chattable = []
        for r in reports:
            rid = r.get("report_id", "")
            status = r.get("status", "")
            if status in ("report_draft", "review", "revise", "accepted", "rejected"):
                chattable.append(r)
            elif self.store.load_artifact(rid, "report_v1.md"):
                chattable.append(r)
        return chattable

    def _cleanup_expired_sessions(self):
        """Remove sessions that have exceeded the idle timeout."""
        now = time.time()
        expired = [
            ts for ts, s in self._chat_sessions.items()
            if now - s.last_activity > CHAT_IDLE_TIMEOUT
        ]
        for ts in expired:
            self.end_chat_session(ts)

    @staticmethod
    def _find_latest_artifact(artifacts: dict[str, str], prefix: str, suffix: str) -> str | None:
        """Find the latest versioned artifact (e.g., deep_dive_v2.json > deep_dive_v1.json)."""
        import re

        def _version_key(name: str) -> int:
            m = re.search(r"(\d+)", name[len(prefix):])
            return int(m[1]) if m else 0

        matching = [k for k in artifacts if k.startswith(prefix) and k.endswith(suffix)]
        if not matching:
            return None
        best = max(matching, key=_version_key)
        return artifacts[best]
