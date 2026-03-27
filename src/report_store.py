"""
ReportStore — manages report artifacts on disk.

Directory structure per report:
  reports/{seq}_{idea_id}/
  ├── state.json              ← report metadata and status
  ├── researcher/             ← researcher's artifacts
  │   ├── idea_brief.json
  │   ├── feedback_v{N}.json
  │   ├── report_v{N}.md
  │   └── report_final.md
  ├── intern/                 ← intern's artifacts
  │   └── deep_dive_v{N}.json
  └── reviewer/               ← reviewer's artifacts
      ├── review_v{N}.json
      └── batch_review.json
"""

import json
import logging
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

# Maps filename patterns to agent subdirectories
_AGENT_ROUTING = {
    "idea_brief": "researcher",
    "feedback_v": "researcher",
    "report_v": "researcher",
    "report_final": "researcher",
    "deep_dive_v": "intern",
    "review_v": "reviewer",
    "batch_review": "reviewer",
}


def _route_to_agent(filename: str) -> str | None:
    """Determine which agent subdirectory a file belongs to."""
    for prefix, agent in _AGENT_ROUTING.items():
        if filename.startswith(prefix):
            return agent
    return None


class ReportStore:
    """Create, update, and query research reports."""

    def __init__(self, reports_dir: str | Path):
        self.reports_dir = Path(reports_dir)
        self.reports_dir.mkdir(parents=True, exist_ok=True)

    def _report_dir(self, idea_id: str) -> Path | None:
        """Find report directory by idea_id (may have date prefix)."""
        # Try exact match first
        exact = self.reports_dir / idea_id
        if exact.exists():
            return exact
        # Search for date-prefixed directories
        for d in sorted(self.reports_dir.iterdir(), reverse=True):
            if d.is_dir() and idea_id in d.name:
                return d
        return None

    def _next_seq(self) -> int:
        """Return the next sequential number based on existing report directories."""
        max_seq = 0
        if self.reports_dir.exists():
            for d in self.reports_dir.iterdir():
                if d.is_dir():
                    parts = d.name.split("_", 1)
                    try:
                        max_seq = max(max_seq, int(parts[0]))
                    except (ValueError, IndexError):
                        pass
        return max_seq + 1

    def is_duplicate(self, idea_id: str, source_url: str = "", source_paper: str = "") -> str | None:
        """Check if an idea already exists by idea_id, source_url, or source_paper title.

        Returns the existing report_id if duplicate found, None otherwise.
        """
        source_url_clean = source_url.strip().rstrip("/").lower() if source_url else ""
        source_paper_clean = source_paper.strip().lower() if source_paper else ""

        for r in self.list_reports():
            existing_id = r.get("idea_id", "")
            rid = r.get("report_id", "")
            meta = r.get("metadata", {})

            # Check idea_id match
            if existing_id and existing_id == idea_id:
                return rid

            # Check source_url match
            if source_url_clean:
                existing_url = (meta.get("source_url", "") or "").strip().rstrip("/").lower()
                if existing_url and existing_url == source_url_clean:
                    return rid

            # Check source_paper title match (fuzzy — lowercase strip)
            if source_paper_clean and len(source_paper_clean) > 15:
                existing_paper = (meta.get("source_paper", "") or "").strip().lower()
                if existing_paper and existing_paper == source_paper_clean:
                    return rid

        return None

    def get_all_source_urls(self) -> set[str]:
        """Return all source URLs from existing reports."""
        urls = set()
        for r in self.list_reports():
            url = (r.get("metadata", {}).get("source_url", "") or "").strip().rstrip("/").lower()
            if url:
                urls.add(url)
        return urls

    def get_all_idea_ids(self) -> set[str]:
        """Return all idea_ids from existing reports."""
        return {r.get("idea_id", "") for r in self.list_reports() if r.get("idea_id")}

    def create_report(self, idea_id: str, metadata: dict | None = None) -> str:
        """Create a new report directory with agent subdirs and initial state.json.

        Returns the full report_id (seq_ideaId).
        """
        seq = self._next_seq()
        report_id = f"{seq:03d}_{idea_id}"
        report_dir = self.reports_dir / report_id
        report_dir.mkdir(parents=True, exist_ok=True)

        # Create agent subdirectories
        for agent in ("researcher", "intern", "reviewer"):
            (report_dir / agent).mkdir(exist_ok=True)

        state = {
            "report_id": report_id,
            "idea_id": idea_id,
            "status": "discovery",
            "created_at": datetime.now().isoformat(),
            "updated_at": datetime.now().isoformat(),
            "review_count": 0,
            "metadata": metadata or {},
        }
        self._write_json(report_dir / "state.json", state)
        logger.info(f"Created report: {report_id}")
        return report_id

    def update_state(self, report_id: str, status: str, metadata: dict | None = None):
        """Update a report's state.json."""
        report_dir = self._report_dir(report_id)
        if not report_dir:
            logger.error(f"Report not found: {report_id}")
            return

        state_path = report_dir / "state.json"
        state = self._read_json(state_path)
        previous_status = state.get("status")
        state["status"] = status
        state["updated_at"] = datetime.now().isoformat()
        if metadata:
            state["metadata"].update(metadata)
        if status == "review":
            state["review_count"] = state.get("review_count", 0) + 1
        elif status in {"accepted", "rejected", "revise"} and previous_status in {"report_draft", "revise"}:
            state["review_count"] = state.get("review_count", 0) + 1
        self._write_json(state_path, state)
        logger.info(f"Updated report {report_id}: status={status}")

    def save_artifact(self, report_id: str, filename: str, content: str):
        """Save an artifact file to the appropriate agent subdirectory."""
        report_dir = self._report_dir(report_id)
        if not report_dir:
            logger.error(f"Report not found: {report_id}")
            return

        agent = _route_to_agent(filename)
        if agent:
            agent_dir = report_dir / agent
            agent_dir.mkdir(exist_ok=True)
            filepath = agent_dir / filename
        else:
            filepath = report_dir / filename

        filepath.write_text(content, encoding="utf-8")
        logger.info(f"Saved artifact: {report_id}/{agent or '.'}/{filename}")

    def load_artifact(self, report_id: str, filename: str) -> str | None:
        """Load an artifact file, checking agent subdirectory first, then root (legacy)."""
        report_dir = self._report_dir(report_id)
        if not report_dir:
            return None

        # Try agent subdirectory first
        agent = _route_to_agent(filename)
        if agent:
            filepath = report_dir / agent / filename
            if filepath.exists():
                return filepath.read_text(encoding="utf-8")

        # Fallback to root (legacy flat structure)
        filepath = report_dir / filename
        if filepath.exists():
            return filepath.read_text(encoding="utf-8")

        return None

    def list_reports(self, status_filter: str | None = None) -> list[dict]:
        """List all reports, optionally filtered by status."""
        reports = []
        if not self.reports_dir.exists():
            return reports
        for d in sorted(self.reports_dir.iterdir(), reverse=True):
            if not d.is_dir():
                continue
            state_path = d / "state.json"
            if state_path.exists():
                state = self._read_json(state_path)
                if status_filter and state.get("status") != status_filter:
                    continue
                reports.append(state)
        return reports

    def get_report(self, report_id: str) -> dict | None:
        """Load full report state."""
        report_dir = self._report_dir(report_id)
        if not report_dir:
            return None
        state_path = report_dir / "state.json"
        if state_path.exists():
            return self._read_json(state_path)
        return None

    def load_all_artifacts(self, report_id: str) -> dict[str, str]:
        """Load all available artifacts for a report as a dict of {filename: content}.

        Searches agent subdirectories (researcher/, intern/, reviewer/) and root.
        """
        report_dir = self._report_dir(report_id)
        if not report_dir:
            return {}

        artifacts = {}
        # Search agent subdirectories
        for agent in ("researcher", "intern", "reviewer"):
            agent_dir = report_dir / agent
            if agent_dir.exists():
                for f in sorted(agent_dir.iterdir()):
                    if f.is_file() and f.name != ".DS_Store":
                        artifacts[f.name] = f.read_text(encoding="utf-8")
        # Also check root for state.json
        state_path = report_dir / "state.json"
        if state_path.exists():
            artifacts["state.json"] = state_path.read_text(encoding="utf-8")
        return artifacts

    def _read_json(self, path: Path) -> dict:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _write_json(self, path: Path, data: dict):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
