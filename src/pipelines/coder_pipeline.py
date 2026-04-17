"""
CoderPipeline — multi-agent coding team with parallel worktree execution.
PLAN (Opus) → IMPLEMENT (parallel Sonnet) → REVIEW (Opus) → MERGE

All agent communications are posted to a Slack thread so the user can
observe the team working. The user only interacts with the orchestrator.
"""

import json
import logging
import os
import subprocess
import shutil
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

from pipelines.base import BasePipeline
from prompts.orchestrator import ORCHESTRATOR_PLAN_PROMPT, ORCHESTRATOR_PLAN_SCHEMA
from prompts.implementer import IMPLEMENTER_PROMPT
from prompts.code_reviewer import CODE_REVIEWER_PROMPT, CODE_REVIEWER_SCHEMA
from skills.types import LLMRunRequest
from tools.json_utils import parse_json_response

logger = logging.getLogger(__name__)

OPUS_MODEL = os.environ.get("CLAUDE_OPUS_MODEL", "claude-opus-4-7")
SONNET_MODEL = os.environ.get("CLAUDE_SONNET_MODEL", "claude-sonnet-4-6")


class TeamThread:
    """Tracks a Slack thread where agents communicate."""

    def __init__(self, channel: str, thread_ts: str | None = None):
        self.channel = channel
        self.thread_ts = thread_ts  # Set after first message creates the thread


class DualChannel:
    """Two communication channels: command (user↔orchestrator) and team (internal agent chat)."""

    def __init__(self, command: TeamThread, team: TeamThread):
        self.command = command  # User sees orchestrator updates here
        self.team = team        # All agents' internal discussion here


class CoderPipeline(BasePipeline):
    """Orchestrates parallel coding agents with git worktree isolation."""

    def __init__(self, bot_config: dict, slack_client, api_url: str, api_key: str, bot_dir: Path):
        super().__init__(bot_config, slack_client, api_url, api_key, bot_dir)
        self.coder_config = bot_config.get("coder", {})
        self.command_channel = self.coder_config.get("command_channel", "")
        self.team_channel = self.coder_config.get("team_channel", "")
        self.max_parallel = self.coder_config.get("max_parallel", 3)
        self.planning_config = self.coder_config.get("planning", {})
        self.retry_config = self.coder_config.get("retry_policy", {})
        self.review_config = self.coder_config.get("review", {})
        self.governance = self.coder_config.get("governance", {})
        self.task_log_dir = bot_dir / "task_log"
        self.task_log_dir.mkdir(exist_ok=True)

    # ── public entry points ──────────────────────────────────────

    def _build_channels(self, channel_id: str, thread_ts: str | None = None) -> DualChannel:
        """Build dual-channel communication.
        - command: where the user typed !team run (user ↔ orchestrator)
        - team: separate channel for internal agent chat (if configured)
        """
        cmd_ch = channel_id or self.command_channel
        command = TeamThread(cmd_ch, thread_ts)

        # If team_channel is configured, agents chat there; otherwise same thread
        if self.team_channel and self.team_channel != cmd_ch:
            team = TeamThread(self.team_channel)  # new top-level thread in team channel
        else:
            team = command  # fallback: everything in one thread

        return DualChannel(command=command, team=team)

    def run_task(
        self,
        project_path: str,
        user_request: str,
        channel_id: str = "",
        thread_ts: str | None = None,
    ) -> dict:
        """Full pipeline: plan → implement (parallel) → review → merge.
        Orchestrator talks to user in command channel.
        All agents' internal chat goes to team channel."""
        logger.info(f"=== Coder Pipeline: Starting task in {project_path} ===")
        dc = self._build_channels(channel_id, thread_ts)

        try:
            # ── 1. PLAN ──────────────────────────────────────────
            self._say(dc.command, "orchestrator",
                      f":brain: *아키텍트가 작업을 분석합니다...*\n\n"
                      f"> {user_request}")

            plan = self._orchestrator_plan(project_path, user_request, dc.team)
            if not plan:
                self._say(dc.command, "orchestrator", ":warning: 계획 수립 실패. 요청을 더 구체적으로 작성해 주세요.")
                return {"success": False, "error": "planning_failed"}

            tasks = plan.get("tasks", [])
            complexity = plan.get("complexity", "simple")
            goal = plan.get("strategic_goal", "")
            risks = plan.get("risks", [])

            # Orchestrator announces the plan — to BOTH channels
            plan_msg = (
                f":clipboard: *계획 완료* — `{complexity}`\n"
                f"*목표:* {goal}\n\n"
                f"*작업 분배:*\n"
            )
            for t in tasks:
                deps = f" (depends: {', '.join(t['depends_on'])})" if t.get("depends_on") else ""
                plan_msg += f"  • `{t['task_id']}` — {t['title']}{deps}\n"
                plan_msg += f"    scope: `{', '.join(t.get('scope', []))}`\n"
            if risks:
                plan_msg += f"\n*리스크:*\n" + "\n".join(f"  :warning: {r}" for r in risks)

            self._say(dc.command, "orchestrator", plan_msg)
            self._say(dc.team, "orchestrator", plan_msg)

            # Simple task — no worktrees
            if complexity == "simple" or len(tasks) <= 1:
                return self._run_simple(project_path, tasks[0] if tasks else plan, user_request, dc)

            # ── 2. IMPLEMENT (dependency-aware parallel) ─────────
            self._say(dc.command, "orchestrator",
                      f":hammer_and_wrench: *구현 시작* — {len(tasks)}개 워크트리에서 병렬 실행")
            self._say(dc.team, "orchestrator",
                      f":hammer_and_wrench: *구현 시작* — {len(tasks)}개 워크트리에서 병렬 실행합니다.\n"
                      f"각 개발자가 독립된 브랜치에서 작업합니다.")

            worktrees = self._setup_worktrees(project_path, tasks)

            # Announce worktree creation — team channel only
            for task_id, wt_path in worktrees.items():
                title = next((t["title"] for t in tasks if t["task_id"] == task_id), task_id)
                self._say(dc.team, "implementer",
                          f":seedling: `{task_id}` 워크트리 준비 완료 — _{title}_\n"
                          f"branch: `agent/{task_id.lower()}`")

            results = self._run_implementers_with_deps(project_path, worktrees, tasks, dc)

            # Orchestrator summarizes — command channel (user sees summary)
            completed = [r for r in results.values() if r.get("status") == "completed"]
            blocked = [r for r in results.values() if r.get("status") == "blocked"]
            self._say(dc.command, "orchestrator",
                      f":bar_chart: *구현 현황*: 완료 {len(completed)}건 / 블록 {len(blocked)}건 — 리뷰 진행 중")

            # ── 3. REVIEW ────────────────────────────────────────
            self._say(dc.team, "reviewer",
                      ":mag: *코드 리뷰를 시작합니다.*\n"
                      "모든 워크트리의 diff를 검사합니다...")

            review = self._run_reviewer(project_path, worktrees, results, plan, dc.team)

            if not review:
                self._cleanup_worktrees(project_path, worktrees)
                self._say(dc.command, "orchestrator", ":warning: 리뷰 실행 실패.")
                return {"success": False, "error": "review_failed"}

            decision = review.get("decision", "reject")

            # Check blocker_blocks_merge policy
            if self.review_config.get("blocker_blocks_merge", True):
                has_blocker = any(
                    tr.get("blockers") for tr in review.get("task_reviews", [])
                )
                if has_blocker and decision == "approve":
                    decision = "reject"
                    review["decision"] = "reject"
                    review["reason"] = (review.get("reason", "") +
                                        " [auto-rejected: blocker found despite approve]")

            # Reviewer posts detailed review — team channel
            review_msg = f":mag: *리뷰 결과: `{decision.upper()}`*\n"
            review_msg += f"_{review.get('reason', '')}_\n\n"

            for tr in review.get("task_reviews", []):
                verdict_emoji = ":white_check_mark:" if tr.get("verdict") == "pass" else ":x:"
                review_msg += f"{verdict_emoji} `{tr['task_id']}` — {tr.get('verdict', '?').upper()}\n"
                for b in tr.get("blockers", []):
                    review_msg += f"  :rotating_light: *blocker:* {b}\n"
                for s in tr.get("suggestions", []):
                    review_msg += f"  :bulb: suggestion: {s}\n"
                if tr.get("integration_notes"):
                    review_msg += f"  :link: integration: {tr['integration_notes']}\n"
                review_msg += "\n"

            if review.get("integration_risks"):
                review_msg += f":warning: *통합 리스크:* {review['integration_risks']}\n"

            self._say(dc.team, "reviewer", review_msg)

            # ── 4. MERGE or RETRY ────────────────────────────────
            if decision == "approve":
                merge_order = review.get("merge_order", list(worktrees.keys()))
                self._say(dc.team, "orchestrator",
                          f":arrows_counterclockwise: *머지 진행* — 순서: {' → '.join(merge_order)}")

                self._merge_worktrees(project_path, worktrees, merge_order, dc.team)

                # Session summary — log what was done
                self._write_session_summary(project_path, plan, results, review)

                # Generate run guide — tell the user how to test/run the result
                run_guide = self._generate_run_guide(project_path, plan, results)

                # Final result — command channel (user sees)
                completion_msg = (
                    f":tada: *작업 완료!*\n"
                    f"*목표:* {goal}\n"
                    f"*작업:* {len(tasks)}건 머지 완료\n"
                )
                if run_guide:
                    completion_msg += f"\n---\n{run_guide}"

                self._say(dc.command, "orchestrator", completion_msg)
                self._say(dc.team, "orchestrator",
                          f":tada: *작업 완료!* 브랜치 정리 완료.")

                self._log_task(plan, results, review, "completed")
                return {"success": True, "plan": plan, "review": review}
            else:
                # Retry loop: up to max_retries, passing review feedback to implementers
                max_retries = self.retry_config.get("max_retries", 3)

                for retry_num in range(1, max_retries + 1):
                    self._say(dc.command, "orchestrator",
                              f":repeat: *리뷰 거절 — 자동 재시도 ({retry_num}/{max_retries})*\n"
                              f"사유: {review.get('reason', '')[:200]}")

                    # Collect review feedback per task for targeted fixes
                    failed_ids = set()
                    review_feedback: dict[str, list[str]] = {}
                    for tr in review.get("task_reviews", []):
                        if tr.get("verdict") == "fail":
                            tid = tr["task_id"]
                            failed_ids.add(tid)
                            review_feedback[tid] = tr.get("blockers", [])

                    if not failed_ids:
                        # Reviewer rejected overall but no individual failures — retry all
                        failed_ids = set(worktrees.keys())
                        for tid in failed_ids:
                            review_feedback[tid] = [review.get("reason", "")]

                    self._say(dc.team, "orchestrator",
                              f":repeat: 거절된 태스크 재실행 ({retry_num}/{max_retries})\n"
                              f"피드백을 전달합니다: {', '.join(failed_ids)}")

                    failed_tasks = [t for t in tasks if t["task_id"] in failed_ids]
                    failed_wts = {k: v for k, v in worktrees.items() if k in failed_ids}

                    # Pass review feedback to implementers
                    retry_results = self._run_implementers_with_feedback(
                        project_path, failed_wts, failed_tasks, review_feedback, dc)
                    results.update(retry_results)

                    # Re-review ALL worktrees (check integration)
                    retry_review = self._run_reviewer(
                        project_path, worktrees, results, plan, dc.team)

                    if retry_review and retry_review.get("decision") == "approve":
                        # Check blocker_blocks_merge on retry review too
                        has_blocker = any(
                            tr.get("blockers") for tr in retry_review.get("task_reviews", [])
                        )
                        if self.review_config.get("blocker_blocks_merge", True) and has_blocker:
                            review = retry_review
                            review["decision"] = "reject"
                            self._say(dc.team, "orchestrator",
                                      f":warning: 재시도 {retry_num}: 승인됐지만 블로커 발견 — 재거절")
                            continue

                        merge_order = retry_review.get("merge_order", list(worktrees.keys()))
                        self._merge_worktrees(project_path, worktrees, merge_order, dc.team)
                        self._write_session_summary(project_path, plan, results, retry_review)
                        run_guide = self._generate_run_guide(project_path, plan, results)
                        retry_msg = (
                            f":tada: *재시도 후 작업 완료!*\n*목표:* {goal}\n"
                        )
                        if run_guide:
                            retry_msg += f"\n---\n{run_guide}"
                        self._say(dc.command, "orchestrator", retry_msg)
                        self._log_task(plan, results, retry_review, "completed_after_retry")
                        return {"success": True, "plan": plan, "review": retry_review}
                    elif retry_review:
                        review = retry_review  # Update review for next iteration
                        self._say(dc.team, "orchestrator",
                                  f":no_entry: 재시도 {retry_num}/{max_retries} 실패: "
                                  f"{retry_review.get('reason', '')[:200]}")
                    else:
                        self._say(dc.team, "orchestrator",
                                  f":no_entry: 재시도 {retry_num}/{max_retries}: 리뷰 실행 실패")

                # All retries exhausted
                if self.retry_config.get("escalate_on_failure", True):
                    self._say(dc.command, "orchestrator",
                              f":sos: *리뷰 거절 (재시도 {max_retries}회 소진)*\n"
                              f"사유: {review.get('reason', '')[:200]}\n"
                              f"수동 개입이 필요합니다. `!team run`으로 재시도하세요.")
                else:
                    self._say(dc.command, "orchestrator",
                              f":no_entry: *리뷰 거절됨*\n"
                              f"사유: {review.get('reason', '')[:200]}\n"
                              f"`!team run`으로 수정 후 재시도하세요.")

                self._say(dc.team, "orchestrator",
                          ":no_entry: *리뷰 거절* — 워크트리 정리 중...")
                self._cleanup_worktrees(project_path, worktrees)
                self._log_task(plan, results, review, "rejected")
                return {"success": False, "error": "review_rejected", "review": review}

        except Exception as e:
            logger.error(f"Coder pipeline error: {e}", exc_info=True)
            self._say(dc.command, "orchestrator", f":x: *파이프라인 에러:*\n```\n{str(e)[:300]}\n```")
            return {"success": False, "error": str(e)}

    def run_plan_only(
        self,
        project_path: str,
        user_request: str,
        channel_id: str = "",
        thread_ts: str | None = None,
    ) -> dict | None:
        """Plan without executing — for preview."""
        dc = self._build_channels(channel_id, thread_ts)
        return self._orchestrator_plan(project_path, user_request, dc.team)

    # ── Slack communication ──────────────────────────────────────

    def _say(self, thread: TeamThread, agent: str, text: str):
        """Post a message to the team thread. Creates the thread on first message."""
        if not thread.channel:
            return

        try:
            resp = self.slack.chat_postMessage(
                channel=thread.channel,
                text=text,
                thread_ts=thread.thread_ts,
                username=self._agent_display_name(agent),
                icon_emoji=self._agent_emoji(agent),
            )
            # Capture thread_ts from the first message
            if not thread.thread_ts and resp.get("ok"):
                thread.thread_ts = resp["ts"]
        except Exception as e:
            logger.warning(f"Slack post failed: {e}")
            # Fallback to SlackFacade
            self.post_to_slack(
                channel=thread.channel, text=text,
                agent_name=agent,
            )

    def _agent_display_name(self, agent: str) -> str:
        agent_cfg = self.agents_config.get(agent, {})
        return agent_cfg.get("display_name", agent.title())

    def _agent_emoji(self, agent: str) -> str:
        agent_cfg = self.agents_config.get(agent, {})
        return agent_cfg.get("emoji", ":robot_face:")

    # ── planning ─────────────────────────────────────────────────

    def _orchestrator_plan(self, project_path: str, user_request: str, thread: TeamThread) -> dict | None:
        """Use Opus to analyze the codebase and create a tactical plan."""
        self._say(thread, "orchestrator", ":file_folder: 코드베이스 스캔 중...")
        codebase_summary = self._scan_codebase(project_path)

        if not codebase_summary or "scan failed" in codebase_summary:
            self._say(thread, "orchestrator",
                      f":warning: 코드베이스 스캔 실패: `{codebase_summary}`\n"
                      f"프로젝트 경로를 확인하세요: `{project_path}`")
            return None

        self._say(thread, "orchestrator",
                  f":brain: LLM에 계획 요청 중... (model: {OPUS_MODEL})")

        max_tasks = self.planning_config.get("max_tasks", 5)
        prompt = ORCHESTRATOR_PLAN_PROMPT.format(
            project_path=project_path,
            codebase_summary=codebase_summary,
            user_request=user_request,
            max_tasks=max_tasks,
        )

        raw_result, parsed = self.runtime.run_json(
            LLMRunRequest(
                prompt=prompt,
                model=OPUS_MODEL,
                cwd=project_path,
                heartbeat_channel=thread.channel or None,
                heartbeat_agent="orchestrator",
                heartbeat_label="작업 분석",
            ),
            task_name="task planning",
            expected_kind="object",
            schema_example=ORCHESTRATOR_PLAN_SCHEMA,
        )

        if not isinstance(parsed, dict):
            error_detail = ""
            if raw_result and hasattr(raw_result, 'timed_out') and raw_result.timed_out:
                error_detail = f"타임아웃 ({raw_result.duration_ms // 1000}초)"
            elif raw_result and hasattr(raw_result, 'output'):
                error_detail = f"파싱 실패 — raw: {str(raw_result.output)[:200]}"
            else:
                error_detail = "LLM 응답 없음"
            self._say(thread, "orchestrator",
                      f":x: 계획 생성 실패: {error_detail}")
            return None

        return parsed

    def _scan_codebase(self, project_path: str) -> str:
        """Quick codebase scan — directory tree."""
        try:
            result = subprocess.run(
                ["find", ".", "-type", "f", "-not", "-path", "./.git/*",
                 "-not", "-path", "./node_modules/*", "-not", "-path", "./.venv/*",
                 "-not", "-path", "./.worktrees/*",
                 "-not", "-path", "./__pycache__/*", "-not", "-name", "*.pyc"],
                cwd=project_path, capture_output=True, text=True, timeout=10,
            )
            files = result.stdout.strip().split("\n")
            if len(files) > 100:
                return f"({len(files)} files total — showing first 100)\n" + "\n".join(files[:100])
            return "\n".join(files)
        except Exception as e:
            return f"(scan failed: {e})"

    # ── simple task (no worktree) ────────────────────────────────

    def _run_simple(self, project_path: str, task: dict, user_request: str, dc: DualChannel) -> dict:
        """Run a simple single-task directly without worktrees."""
        self._say(dc.team, "implementer",
                  f":hammer_and_wrench: *단순 작업* — 워크트리 없이 직접 실행합니다.\n"
                  f"task: `{task.get('task_id', 'TASK-001')}` — {task.get('title', user_request)}")

        prompt = IMPLEMENTER_PROMPT.format(
            project_path=project_path,
            task_title=task.get("title", user_request),
            task_id=task.get("task_id", "TASK-001"),
            scope="\n".join(task.get("scope", ["."])),
            interface_contract=task.get("interface_contract", "N/A"),
            hints=task.get("hints", user_request),
        )

        result = self.runtime.run(
            LLMRunRequest(
                prompt=prompt,
                model=SONNET_MODEL,
                cwd=project_path,
                allow_file_write=True,
                heartbeat_channel=dc.team.channel or None,
                heartbeat_agent="implementer",
                heartbeat_label="구현",
            )
        )

        if result.success:
            output_preview = result.output[:800]
            self._say(dc.team, "implementer", f":white_check_mark: *구현 완료*\n\n{output_preview}")

            # Generate run guide for simple tasks too
            simple_results = {task.get("task_id", "TASK-001"): {"files_changed": [], "files_added": []}}
            run_guide = self._generate_run_guide(project_path, {"strategic_goal": user_request}, simple_results)
            completion_msg = ":tada: *작업 완료!*"
            if run_guide:
                completion_msg += f"\n\n---\n{run_guide}"
            self._say(dc.command, "orchestrator", completion_msg)
            return {"success": True, "output": result.output}
        else:
            self._say(dc.team, "implementer", ":warning: 구현 실패.")
            self._say(dc.command, "orchestrator", ":warning: 구현 실패.")
            return {"success": False, "error": "implementation_failed"}

    # ── worktree management ──────────────────────────────────────

    def _setup_worktrees(self, project_path: str, tasks: list[dict]) -> dict[str, str]:
        """Create git worktrees for each task."""
        worktrees = {}
        base_branch = self._get_current_branch(project_path)

        # Verify base branch has commits
        check = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=project_path, capture_output=True, text=True,
        )
        if check.returncode != 0:
            logger.error(f"No commits in {project_path} — cannot create worktrees")
            return worktrees

        for task in tasks:
            task_id = task["task_id"]
            branch = f"agent/{task_id.lower()}"
            wt_path = f"{project_path}/.worktrees/{task_id}"

            if Path(wt_path).exists():
                subprocess.run(["git", "worktree", "remove", "--force", wt_path],
                               cwd=project_path, capture_output=True)
            subprocess.run(["git", "branch", "-D", branch],
                           cwd=project_path, capture_output=True)

            try:
                subprocess.run(
                    ["git", "worktree", "add", "-b", branch, wt_path, base_branch],
                    cwd=project_path, check=True, capture_output=True, text=True,
                )
                worktrees[task_id] = wt_path
            except subprocess.CalledProcessError as e:
                logger.error(f"Failed to create worktree for {task_id}: {e.stderr}")

        return worktrees

    def _get_current_branch(self, project_path: str) -> str:
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=project_path, capture_output=True, text=True, check=True,
            )
            return result.stdout.strip()
        except Exception:
            return "main"

    def _merge_worktrees(self, project_path: str, worktrees: dict[str, str],
                         merge_order: list[str], thread: TeamThread):
        """Merge worktree branches in order, then clean up."""
        for task_id in merge_order:
            if task_id not in worktrees:
                continue
            branch = f"agent/{task_id.lower()}"
            try:
                subprocess.run(
                    ["git", "merge", "--no-ff", "-m", f"Merge {task_id}", branch],
                    cwd=project_path, check=True, capture_output=True, text=True,
                )
                self._say(thread, "orchestrator", f":arrows_counterclockwise: `{task_id}` 머지 완료 (branch: `{branch}`)")
            except subprocess.CalledProcessError as e:
                self._say(thread, "orchestrator", f":x: `{task_id}` 머지 실패: {e.stderr[:200]}")
                logger.error(f"Merge failed for {branch}: {e.stderr}")

        self._cleanup_worktrees(project_path, worktrees)

    def _cleanup_worktrees(self, project_path: str, worktrees: dict[str, str]):
        """Remove worktrees and branches."""
        for task_id, wt_path in worktrees.items():
            branch = f"agent/{task_id.lower()}"
            try:
                subprocess.run(["git", "worktree", "remove", "--force", wt_path],
                               cwd=project_path, capture_output=True)
            except Exception:
                shutil.rmtree(wt_path, ignore_errors=True)
            subprocess.run(["git", "branch", "-D", branch],
                           cwd=project_path, capture_output=True)

    # ── dependency-aware execution ───────────────────────────────

    def _run_implementers_with_deps(
        self,
        project_path: str,
        worktrees: dict[str, str],
        tasks: list[dict],
        dc: DualChannel,
    ) -> dict[str, dict]:
        """Execute tasks respecting depends_on ordering.
        Tasks with no dependencies run in parallel first, then dependents."""
        results: dict[str, dict] = {}
        completed_ids: set[str] = set()
        remaining = list(tasks)

        while remaining:
            # Find tasks whose dependencies are all completed
            ready = [t for t in remaining
                     if all(d in completed_ids for d in t.get("depends_on", []))]

            if not ready:
                # Circular dependency or missing dep — run all remaining
                self._say(dc.team, "orchestrator",
                          ":warning: 의존성 해결 불가 — 남은 태스크 병렬 실행")
                ready = remaining

            if len(ready) > 1:
                self._say(dc.team, "orchestrator",
                          f":arrows_counterclockwise: 병렬 실행: {', '.join(t['task_id'] for t in ready)}")

            batch_results = self._run_implementers_parallel(
                project_path, worktrees, ready, dc)
            results.update(batch_results)

            for t in ready:
                completed_ids.add(t["task_id"])
                remaining.remove(t)

        return results

    # ── parallel implementation ──────────────────────────────────

    def _run_implementers_parallel(
        self,
        project_path: str,
        worktrees: dict[str, str],
        tasks: list[dict],
        dc: DualChannel,
    ) -> dict[str, dict]:
        """Fire N parallel LLM calls, one per worktree.
        Progress goes to team channel; orchestrator summaries go to command channel."""
        results: dict[str, dict] = {}

        with ThreadPoolExecutor(max_workers=min(len(tasks), self.max_parallel)) as pool:
            futures = {}
            for task in tasks:
                task_id = task["task_id"]
                wt_path = worktrees.get(task_id)
                if not wt_path:
                    results[task_id] = {"task_id": task_id, "status": "blocked",
                                       "blocked_reason": "worktree creation failed"}
                    self._say(dc.team, "implementer",
                              f":x: `{task_id}` 워크트리 생성 실패 — 블록됨")
                    continue

                self._say(dc.team, "implementer",
                          f":construction: `{task_id}` 작업 시작 — _{task.get('title', '')}_")
                future = pool.submit(self._run_single_implementer, wt_path, task, dc.team)
                futures[future] = task_id

            for future in as_completed(futures):
                task_id = futures[future]
                try:
                    result = future.result()
                    results[task_id] = result
                    status = result.get("status", "unknown")
                    summary = result.get("summary", "")[:200]
                    files = result.get("files_changed", [])

                    if status == "completed":
                        files_str = ", ".join(f"`{f}`" for f in files[:5])
                        extra = f" +{len(files)-5} more" if len(files) > 5 else ""
                        self._say(dc.team, "implementer",
                                  f":white_check_mark: `{task_id}` *완료*\n"
                                  f"{summary}\n"
                                  f"변경: {files_str}{extra}")
                    else:
                        reason = result.get("blocked_reason", "unknown")
                        self._say(dc.team, "implementer",
                                  f":warning: `{task_id}` *블록됨*\n사유: {reason}")

                except Exception as e:
                    logger.error(f"Implementer {task_id} failed: {e}")
                    results[task_id] = {"task_id": task_id, "status": "blocked",
                                       "blocked_reason": str(e)}
                    self._say(dc.team, "implementer",
                              f":x: `{task_id}` 에러: {str(e)[:200]}")

        return results

    def _run_implementers_with_feedback(
        self,
        project_path: str,
        worktrees: dict[str, str],
        tasks: list[dict],
        review_feedback: dict[str, list[str]],
        dc: DualChannel,
    ) -> dict[str, dict]:
        """Re-run implementers with review feedback injected into prompts."""
        # Enrich task hints with reviewer blockers
        enriched_tasks = []
        for task in tasks:
            task_copy = dict(task)
            task_id = task_copy["task_id"]
            blockers = review_feedback.get(task_id, [])
            if blockers:
                feedback_text = "\n".join(f"- {b}" for b in blockers)
                existing_hints = task_copy.get("hints", "")
                task_copy["hints"] = (
                    f"{existing_hints}\n\n"
                    f"REVIEWER FEEDBACK (must fix):\n{feedback_text}\n\n"
                    f"The previous implementation was rejected. Fix the above issues. "
                    f"Make sure your interfaces match what other tasks expect."
                )
            enriched_tasks.append(task_copy)

        return self._run_implementers_parallel(project_path, worktrees, enriched_tasks, dc)

    def _run_single_implementer(self, wt_path: str, task: dict, team_thread: TeamThread) -> dict:
        """Run one implementer in its worktree."""
        task_id = task["task_id"]

        self._say(team_thread, "implementer",
                  f":gear: `{task_id}` LLM 호출 중... (model: {SONNET_MODEL}, cwd: `{wt_path}`)")

        prompt = IMPLEMENTER_PROMPT.format(
            project_path=wt_path,
            task_title=task.get("title", ""),
            task_id=task_id,
            scope="\n".join(task.get("scope", ["."])),
            interface_contract=task.get("interface_contract", "N/A"),
            hints=task.get("hints", ""),
        )

        result = self.runtime.run(
            LLMRunRequest(
                prompt=prompt,
                model=SONNET_MODEL,
                cwd=wt_path,
                allow_file_write=True,
                timeout_ms=600_000,
                heartbeat_channel=team_thread.channel or None,
                heartbeat_agent="implementer",
                heartbeat_label=f"{task_id} 구현",
            )
        )

        if not result.success:
            # Report specific failure reason
            if result.timed_out:
                reason = f"타임아웃 ({result.duration_ms // 1000}초)"
            elif result.raw and "error" in str(result.raw):
                reason = f"API 에러: {str(result.raw)[:200]}"
            else:
                reason = f"LLM 응답 없음 (duration: {result.duration_ms}ms)"

            self._say(team_thread, "implementer",
                      f":x: `{task_id}` 실패 — {reason}")
            return {"task_id": task_id, "status": "blocked", "blocked_reason": reason}

        parsed = parse_json_response(result.output)
        if isinstance(parsed, dict) and "task_id" in parsed:
            return parsed

        return self._extract_result_from_worktree(wt_path, task_id, result.output)

    def _extract_result_from_worktree(self, wt_path: str, task_id: str, raw_output: str) -> dict:
        """Extract result by inspecting git state in the worktree."""
        try:
            diff_result = subprocess.run(
                ["git", "diff", "--name-only", "HEAD~1"],
                cwd=wt_path, capture_output=True, text=True,
            )
            files = [f for f in diff_result.stdout.strip().split("\n") if f]
        except Exception:
            files = []

        return {
            "task_id": task_id,
            "status": "completed",
            "summary": raw_output[:500],
            "files_changed": files,
        }

    # ── review ───────────────────────────────────────────────────

    def _run_reviewer(
        self,
        project_path: str,
        worktrees: dict[str, str],
        results: dict[str, dict],
        plan: dict,
        thread: TeamThread,
    ) -> dict | None:
        """Review all diffs with Opus."""
        diffs_block = self._collect_diffs(project_path, worktrees)
        if not diffs_block.strip():
            return {"decision": "approve", "reason": "No changes to review",
                    "task_reviews": [], "merge_order": list(worktrees.keys())}

        prompt = CODE_REVIEWER_PROMPT.format(
            project_path=project_path,
            strategic_goal=plan.get("strategic_goal", ""),
            diffs_block=diffs_block,
        )

        _, parsed = self.runtime.run_json(
            LLMRunRequest(
                prompt=prompt,
                model=OPUS_MODEL,
                cwd=project_path,
                heartbeat_channel=thread.channel or None,
                heartbeat_agent="reviewer",
                heartbeat_label="코드 리뷰",
            ),
            task_name="code review",
            expected_kind="object",
            schema_example=CODE_REVIEWER_SCHEMA,
        )
        return parsed if isinstance(parsed, dict) else None

    def _collect_diffs(self, project_path: str, worktrees: dict[str, str]) -> str:
        """Collect diffs from all worktrees."""
        blocks = []
        base_branch = self._get_current_branch(project_path)

        for task_id, wt_path in worktrees.items():
            try:
                result = subprocess.run(
                    ["git", "diff", f"{base_branch}...HEAD"],
                    cwd=wt_path, capture_output=True, text=True, timeout=30,
                )
                diff = result.stdout.strip()
                if diff:
                    if len(diff) > 10000:
                        diff = diff[:10000] + f"\n... (truncated, {len(diff)} total chars)"
                    blocks.append(f"=== {task_id} ===\n{diff}\n")
            except Exception as e:
                blocks.append(f"=== {task_id} ===\n(diff failed: {e})\n")

        return "\n".join(blocks)

    # ── run guide generation ────────────────────────────────────

    def _generate_run_guide(self, project_path: str, plan: dict, results: dict) -> str:
        """Generate a 'how to run/test' guide based on the completed work."""
        # Collect info about what was built
        all_files = []
        for r in results.values():
            all_files.extend(r.get("files_changed", []))
            all_files.extend(r.get("files_added", []))
        all_files = list(set(f for f in all_files if f))

        # Check for common run patterns
        readme_path = Path(project_path) / "README.md"
        readme_snippet = ""
        if readme_path.exists():
            try:
                content = readme_path.read_text(encoding="utf-8")
                # Extract setup/run sections
                for section in ["## Setup", "## Run", "## Usage", "## Getting Started",
                                "## Installation", "## 실행", "## 사용법"]:
                    if section.lower() in content.lower():
                        readme_snippet = f"(README.md에 '{section}' 섹션이 있습니다)"
                        break
            except Exception:
                pass

        # Detect project type from files
        project_indicators = self._detect_project_type(project_path)

        # Build guide with LLM
        guide_prompt = (
            f"프로젝트: {project_path}\n"
            f"완료된 작업: {plan.get('strategic_goal', '')}\n"
            f"변경된 파일: {', '.join(all_files[:20])}\n"
            f"프로젝트 특성: {project_indicators}\n"
            f"{readme_snippet}\n\n"
            f"위 작업이 완료되었습니다. 사용자가 이 결과물을 실행/테스트하려면 어떻게 해야 하는지 "
            f"간결하게 안내해주세요.\n\n"
            f"규칙:\n"
            f"- 한국어로 작성\n"
            f"- 5줄 이내로 간결하게\n"
            f"- 실행 명령어가 있으면 코드블록으로\n"
            f"- 의존성 설치가 필요하면 포함\n"
            f"- 확실하지 않은 내용은 추측하지 말고 '프로젝트 README 확인' 안내\n"
        )

        try:
            result = self.runtime.run(
                LLMRunRequest(
                    prompt=guide_prompt,
                    model=SONNET_MODEL,
                    cwd=project_path,
                    timeout_ms=60_000,
                )
            )
            if result.success and result.output.strip():
                guide = result.output.strip()
                # Cap length for Slack readability
                if len(guide) > 1000:
                    guide = guide[:1000] + "..."
                return f":rocket: *실행 가이드*\n{guide}"
        except Exception as e:
            logger.warning(f"Run guide generation failed: {e}")

        # Fallback: basic guide from project type detection
        if project_indicators:
            return f":rocket: *실행 가이드*\n프로젝트 경로: `{project_path}`\n{project_indicators}"
        return ""

    def _detect_project_type(self, project_path: str) -> str:
        """Quick detection of project type for run guide hints."""
        p = Path(project_path)
        indicators = []

        if (p / "package.json").exists():
            indicators.append("Node.js 프로젝트 (`npm install && npm start`)")
        if (p / "requirements.txt").exists():
            indicators.append("Python 프로젝트 (`pip install -r requirements.txt`)")
        if (p / "pyproject.toml").exists():
            indicators.append("Python 프로젝트 (`pip install -e .`)")
        if (p / "setup.py").exists():
            indicators.append("Python 패키지 (`python setup.py install`)")
        if (p / "Makefile").exists():
            indicators.append("`make` 빌드 가능")
        if (p / "Dockerfile").exists():
            indicators.append("Docker 지원 (`docker build .`)")
        if (p / "Cargo.toml").exists():
            indicators.append("Rust 프로젝트 (`cargo run`)")
        if (p / "go.mod").exists():
            indicators.append("Go 프로젝트 (`go run .`)")
        if (p / "app.py").exists() or (p / "main.py").exists():
            entry = "app.py" if (p / "app.py").exists() else "main.py"
            indicators.append(f"엔트리포인트: `python {entry}`")

        return " / ".join(indicators) if indicators else ""

    # ── logging & memory ────────────────────────────────────────

    def _log_task(self, plan: dict, results: dict, review: dict, outcome: str):
        """Log completed task for history."""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        log_entry = {
            "timestamp": timestamp,
            "outcome": outcome,
            "strategic_goal": plan.get("strategic_goal", ""),
            "tasks": len(plan.get("tasks", [])),
            "task_details": [
                {
                    "task_id": t.get("task_id"),
                    "title": t.get("title"),
                    "status": results.get(t.get("task_id", ""), {}).get("status", "unknown"),
                }
                for t in plan.get("tasks", [])
            ],
            "review_decision": review.get("decision", ""),
            "review_reason": review.get("reason", ""),
            "retry_count": plan.get("_retry_count", 0),
        }
        log_path = self.task_log_dir / f"{timestamp}.json"
        log_path.write_text(json.dumps(log_entry, ensure_ascii=False, indent=2), encoding="utf-8")

    def _write_session_summary(self, project_path: str, plan: dict,
                               results: dict, review: dict):
        """Append a session summary to TASK_LOG.md in the project (spec §7.2, §13.3)."""
        task_log_path = Path(project_path) / "TASK_LOG.md"
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        goal = plan.get("strategic_goal", "")
        task_count = len(plan.get("tasks", []))
        review_decision = review.get("decision", "")

        task_lines = []
        for t in plan.get("tasks", []):
            tid = t.get("task_id", "?")
            title = t.get("title", "")
            status = results.get(tid, {}).get("status", "unknown")
            files = results.get(tid, {}).get("files_changed", [])
            files_str = ", ".join(files[:5]) if files else "none"
            task_lines.append(f"  - `{tid}`: {title} — {status} (files: {files_str})")

        entry = (
            f"\n## {timestamp} — {goal}\n"
            f"- Tasks: {task_count}, Review: {review_decision}\n"
            + "\n".join(task_lines)
            + "\n"
        )

        try:
            existing = task_log_path.read_text(encoding="utf-8") if task_log_path.exists() else "# Task Log\n"
            task_log_path.write_text(existing + entry, encoding="utf-8")
        except Exception as e:
            logger.warning(f"Failed to write session summary: {e}")
