"""
ReporterPipeline — token-efficient news briefing pipeline.
GATHER+CURATE (1 LLM call) → FORMAT (pure code) → PUBLISH (Slack upload)
"""

import json
import logging
import subprocess
from datetime import datetime, timezone, timedelta
from pathlib import Path

KST = timezone(timedelta(hours=9))

from pipelines.base import BasePipeline
from prompts.reporter import REPORTER_GATHER_PROMPT, REPORTER_GATHER_SCHEMA
from skills.types import LLMRunRequest
from tools.newspaper_html import generate_newspaper, generate_index

logger = logging.getLogger(__name__)


class ReporterPipeline(BasePipeline):
    """Orchestrates news collection and HTML newspaper publication."""

    def __init__(self, bot_config: dict, slack_client, api_url: str, api_key: str, bot_dir: Path):
        super().__init__(bot_config, slack_client, api_url, api_key, bot_dir)
        self.reporter_config = bot_config.get("reporter", {})
        self.publish_channel = self.reporter_config.get("publish_channel", "")
        self.status_channel = self.reporter_config.get("status_channel", "")
        self.search_queries = self.reporter_config.get("search_queries", [])
        self.digests_dir = bot_dir / "digests"
        self.digests_dir.mkdir(exist_ok=True)

    # ── public entry points ──────────────────────────────────────

    def run_full_pipeline(self):
        """Execute the full news briefing pipeline (1 LLM call)."""
        logger.info("=== Reporter Pipeline: Starting ===")
        try:
            self._post_status(":newspaper: 뉴스 수집을 시작합니다...")

            # Stage 1: GATHER + CURATE (single LLM call)
            digest = self._gather_and_curate()
            if not digest:
                self._post_status(":warning: 뉴스 수집 실패. 파이프라인 중단.")
                return

            article_count = sum(
                len(s.get("articles", [])) for s in digest.get("sections", [])
            )
            rumor_count = len(digest.get("rumors", []))
            self._post_status(
                f":newspaper: 뉴스 {article_count}건 + 루머 {rumor_count}건 수집 완료",
                agent="reporter",
            )

            # Archive JSON first (needed for index)
            self._archive_digest(digest)

            # Stage 2: FORMAT + ARCHIVE (pure code — no LLM)
            date_str = digest.get("date", datetime.now(KST).strftime("%Y-%m-%d"))
            filename = f"briefing_{date_str.replace('-', '')}.html"

            # Rebuild all HTML pages with correct prev/next links + index
            self._rebuild_archive()

            # Stage 3: PUBLISH
            html_path = self.digests_dir / filename
            if html_path.exists():
                html_content = html_path.read_text(encoding="utf-8")
                self._publish(html_content, filename, digest)
            else:
                logger.error(f"Generated HTML not found: {html_path}")
                self._post_status(":warning: HTML 생성 실패.")

            # Stage 4: PUSH to GitHub Pages
            self._push_to_github(date_str)

            logger.info("=== Reporter Pipeline: Completed ===")

        except Exception as e:
            logger.error(f"Reporter pipeline error: {e}", exc_info=True)
            self._post_status(f":x: 파이프라인 에러: {str(e)[:200]}")

    # ── internal stages ──────────────────────────────────────────

    def _gather_and_curate(self) -> dict | None:
        """Single LLM call: WebSearch + categorize + summarize → JSON."""
        queries_block = "\n".join(f"  - {q}" for q in self.search_queries)
        previous_titles = self._load_previous_titles()
        date = datetime.now(KST).strftime("%Y-%m-%d")

        prompt = REPORTER_GATHER_PROMPT.format(
            date=date,
            search_queries_block=queries_block,
            previous_titles=previous_titles or "(none — first run)",
        )

        _, parsed = self.runtime.run_json(
            LLMRunRequest(
                prompt=prompt,
                heartbeat_channel=self.status_channel or None,
                heartbeat_agent="reporter",
                heartbeat_label="뉴스 수집",
            ),
            task_name="news gathering + curation",
            expected_kind="object",
            schema_example=REPORTER_GATHER_SCHEMA,
        )

        if not isinstance(parsed, dict):
            return None

        if "date" not in parsed:
            parsed["date"] = date

        # Hard filter: remove articles older than 48 hours
        parsed = self._filter_by_freshness(parsed, hours=48)

        return parsed

    def _load_previous_titles(self) -> str:
        """Load article titles AND URLs from recent digests to prevent duplicates.

        Covers all digests from the last 7 days to ensure no repeats.
        """
        entries = []
        seen_urls: set[str] = set()
        cutoff = datetime.now(KST) - timedelta(days=7)

        json_files = sorted(self.digests_dir.glob("*.json"), reverse=True)
        for path in json_files:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                # Check date — skip digests older than 7 days
                date_str = data.get("date", "")
                if date_str:
                    try:
                        digest_date = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=KST)
                        if digest_date < cutoff:
                            continue
                    except ValueError:
                        pass

                digest = data.get("digest", data)
                for section in digest.get("sections", []):
                    for art in section.get("articles", []):
                        t = art.get("title", "")
                        url = (art.get("source_url", "") or "").strip().rstrip("/").lower()
                        if t:
                            entry = f"- {t}"
                            if url:
                                entry += f" [{url}]"
                                seen_urls.add(url)
                            entries.append(entry)

                # Also include rumors
                for r in digest.get("rumors", []):
                    snippet = r.get("snippet", "")
                    if snippet:
                        entries.append(f"- [rumor] {snippet[:80]}")

            except (json.JSONDecodeError, OSError):
                continue
        return "\n".join(entries) if entries else ""

    def _archive_digest(self, digest: dict) -> Path:
        """Save digest JSON for dedup and history."""
        date_str = digest.get("date", datetime.now(KST).strftime("%Y-%m-%d"))
        archive_path = self.digests_dir / f"{date_str.replace('-', '')}.json"
        archive_data = {
            "date": date_str,
            "digest": digest,
            "published_at": datetime.now(KST).isoformat(),
        }
        archive_path.write_text(
            json.dumps(archive_data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return archive_path

    def _rebuild_archive(self):
        """Rebuild all HTML pages with prev/next nav links and index page."""
        json_files = sorted(self.digests_dir.glob("*.json"))
        digests_with_filenames = []

        for path in json_files:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                digest = data.get("digest", data)
                date_str = data.get("date", digest.get("date", path.stem))
                html_filename = f"briefing_{date_str.replace('-', '')}.html"
                digests_with_filenames.append((digest, html_filename))
            except (json.JSONDecodeError, OSError):
                continue

        # Generate each HTML page with correct prev/next
        for i, (digest, filename) in enumerate(digests_with_filenames):
            prev_file = digests_with_filenames[i - 1][1] if i > 0 else None
            next_file = digests_with_filenames[i + 1][1] if i < len(digests_with_filenames) - 1 else None
            html_content = generate_newspaper(digest, prev_filename=prev_file, next_filename=next_file)
            (self.digests_dir / filename).write_text(html_content, encoding="utf-8")

        # Generate index page
        index_html = generate_index(self.digests_dir)
        (self.digests_dir / "index.html").write_text(index_html, encoding="utf-8")

        logger.info(f"Archive rebuilt: {len(digests_with_filenames)} issues + index")

    def _publish(self, html_content: str, filename: str, digest: dict):
        """Upload HTML newspaper to Slack."""
        if not self.publish_channel:
            logger.warning("No publish_channel configured — skipping publish")
            return

        date_str = digest.get("date", "")
        article_count = sum(
            len(s.get("articles", [])) for s in digest.get("sections", [])
        )

        comment = f":newspaper: *Daily Tech Brief — {date_str}*  ({article_count}건)"

        uploaded = self.slack_facade.upload_file(
            channel=self.publish_channel,
            content=html_content,
            filename=filename,
            title=f"Daily Tech Brief — {date_str}",
            initial_comment=comment,
        )

        if not uploaded:
            fallback = self._build_text_fallback(digest)
            self.post_to_slack(
                channel=self.publish_channel,
                text=fallback,
                agent_name="reporter",
            )

        self._post_status(":white_check_mark: 뉴스 브리핑이 발행되었습니다!", agent="reporter")

    def _build_text_fallback(self, digest: dict) -> str:
        """Build a plain-text fallback if HTML upload fails."""
        lines = [f":newspaper: *Daily Tech Brief — {digest.get('date', '')}*\n"]
        for section in digest.get("sections", []):
            cat = section.get("category", "").upper()
            lines.append(f"*[{cat}]*")
            for art in section.get("articles", []):
                title = art.get("title", "")
                url = art.get("source_url", "")
                summary = art.get("summary", "")
                link = f"<{url}|{title}>" if url else title
                lines.append(f"• {link}\n  {summary[:100]}")
            lines.append("")
        rumors = digest.get("rumors", [])
        if rumors:
            lines.append("*[RUMORS]*")
            for r in rumors:
                lines.append(f"• {r.get('snippet', '')}")
        return "\n".join(lines)[:3000]

    def _filter_by_freshness(self, digest: dict, hours: int = 48) -> dict:
        """Remove articles with published_date older than *hours* from now (KST).

        Articles missing a parseable published_date are kept (benefit of doubt),
        but a warning is logged so we can tighten later.
        """
        now = datetime.now(KST)
        cutoff = now - timedelta(hours=hours)

        for section in digest.get("sections", []):
            original = section.get("articles", [])
            kept = []
            for art in original:
                pub = art.get("published_date", "")
                if not pub:
                    logger.warning("Article missing published_date, keeping: %s", art.get("title", "?"))
                    kept.append(art)
                    continue
                try:
                    pub_dt = datetime.strptime(pub, "%Y-%m-%d").replace(tzinfo=KST)
                    if pub_dt >= cutoff:
                        kept.append(art)
                    else:
                        logger.info(
                            "Filtered out stale article (%s): %s",
                            pub, art.get("title", "?"),
                        )
                except ValueError:
                    logger.warning("Unparseable published_date '%s', keeping: %s", pub, art.get("title", "?"))
                    kept.append(art)
            section["articles"] = kept

        # Remove empty sections
        digest["sections"] = [s for s in digest.get("sections", []) if s.get("articles")]

        return digest

    def _push_to_github(self, date_str: str):
        """Commit and push new digests to GitHub for Pages deployment."""
        project_root = self.bot_dir.parent.parent
        try:
            # Check if this is a git repo with a remote
            result = subprocess.run(
                ["git", "remote", "get-url", "origin"],
                cwd=project_root, capture_output=True, text=True,
            )
            if result.returncode != 0:
                logger.debug("No git remote configured — skipping GitHub push")
                return

            subprocess.run(
                ["git", "add", "bots/reporter/digests/"],
                cwd=project_root, capture_output=True, text=True, check=True,
            )

            # Check if there are staged changes
            diff = subprocess.run(
                ["git", "diff", "--cached", "--quiet"],
                cwd=project_root, capture_output=True,
            )
            if diff.returncode == 0:
                logger.info("No new digest changes to push")
                return

            subprocess.run(
                ["git", "commit", "-m", f"briefing: {date_str}"],
                cwd=project_root, capture_output=True, text=True, check=True,
            )
            subprocess.run(
                ["git", "push"],
                cwd=project_root, capture_output=True, text=True, check=True,
            )
            logger.info("Pushed briefing %s to GitHub", date_str)
            self._post_status(":globe_with_meridians: GitHub Pages 배포 완료", agent="reporter")

        except subprocess.CalledProcessError as e:
            logger.warning("GitHub push failed: %s", e.stderr or e.stdout or str(e))
        except Exception as e:
            logger.warning("GitHub push error: %s", e)

    # ── helpers ───────────────────────────────────────────────────

    def _post_status(self, text: str, agent: str | None = None):
        if self.status_channel:
            self.post_to_slack(channel=self.status_channel, text=text, agent_name=agent)
