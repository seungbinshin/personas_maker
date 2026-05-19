"""
BriefStore — manages HA-Expert brief artifacts on disk.

Directory structure per brief:
  briefs/{YYYYMMDD}_{seq:03d}_{slug}/
  ├── state.json              ← brief metadata + status
  ├── request.json            ← original user input
  ├── investigation.json      ← Investigator output (raw findings + sources)
  ├── brief.md                ← Briefer output (1-pager)
  └── chat_log.jsonl          ← optional chat session log
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)


def _slugify(text: str) -> str:
    """Lowercase, non-alphanumeric → hyphen, collapse repeats, trim."""
    s = text.lower().strip()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = s.strip("-")
    return s or "untitled"


class BriefStore:
    """Create, update, and query HA-Expert briefs."""

    def __init__(self, briefs_dir: str | Path):
        self.briefs_dir = Path(briefs_dir)
        self.briefs_dir.mkdir(parents=True, exist_ok=True)

    def _brief_dir(self, brief_id: str) -> Path | None:
        """Resolve a brief directory by:
          1. Exact directory name.
          2. Numeric input → match seq segment exactly (e.g. "12" → *_012_*).
          3. Substring fallback for partial slugs (newest first).
        """
        if not self.briefs_dir.exists():
            return None

        exact = self.briefs_dir / brief_id
        if exact.exists() and exact.is_dir():
            return exact

        dirs = [d for d in self.briefs_dir.iterdir() if d.is_dir()]

        if brief_id.isdigit():
            target_seq = int(brief_id)
            for d in dirs:
                parts = d.name.split("_")
                if len(parts) >= 2 and parts[1].isdigit() and int(parts[1]) == target_seq:
                    return d
            return None  # numeric input but no seq match — do NOT fall through

        for d in sorted(dirs, reverse=True):
            if brief_id in d.name:
                return d
        return None

    def _next_seq(self) -> int:
        max_seq = 0
        if self.briefs_dir.exists():
            for d in self.briefs_dir.iterdir():
                if not d.is_dir():
                    continue
                parts = d.name.split("_")
                if len(parts) >= 2 and parts[1].isdigit():
                    max_seq = max(max_seq, int(parts[1]))
        return max_seq + 1

    def create_brief(self, target: str, extra_context: str, requester: str = "", channel: str = "", source_ts: str = "") -> str:
        """Create a new brief directory + initial state.json + request.json. Returns full brief_id."""
        seq = self._next_seq()
        now = datetime.now()
        date = now.strftime("%Y%m%d")
        slug = _slugify(target)
        brief_id = f"{date}_{seq:03d}_{slug}"
        brief_dir = self.briefs_dir / brief_id
        brief_dir.mkdir(parents=True, exist_ok=True)

        state = {
            "brief_id": brief_id,
            "target": target,
            "status": "investigating",
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
        }
        self._write_json(brief_dir / "state.json", state)

        request = {
            "target": target,
            "extra_context": extra_context,
            "requester": requester,
            "channel": channel,
            "source_ts": source_ts,
        }
        self._write_json(brief_dir / "request.json", request)

        logger.info(f"Created brief: {brief_id}")
        return brief_id

    def update_state(self, brief_id: str, status: str, metadata: dict | None = None):
        brief_dir = self._brief_dir(brief_id)
        if not brief_dir:
            logger.error(f"Brief not found: {brief_id}")
            return
        state_path = brief_dir / "state.json"
        if not state_path.exists():
            logger.error(f"state.json missing for brief {brief_id}")
            return
        state = self._read_json(state_path)
        state["status"] = status
        state["updated_at"] = datetime.now().isoformat()
        if metadata is not None:
            state.setdefault("metadata", {}).update(metadata)
        self._write_json(state_path, state)
        logger.info(f"Updated brief {brief_id}: status={status}")

    def save_artifact(self, brief_id: str, filename: str, content: str):
        brief_dir = self._brief_dir(brief_id)
        if not brief_dir:
            logger.error(f"Brief not found: {brief_id}")
            return
        (brief_dir / filename).write_text(content, encoding="utf-8")
        logger.info(f"Saved artifact: {brief_id}/{filename}")

    def load_artifact(self, brief_id: str, filename: str) -> str | None:
        brief_dir = self._brief_dir(brief_id)
        if not brief_dir:
            return None
        path = brief_dir / filename
        if path.exists():
            return path.read_text(encoding="utf-8")
        return None

    def append_chat_log(self, brief_id: str, role: str, message: str):
        brief_dir = self._brief_dir(brief_id)
        if not brief_dir:
            return
        path = brief_dir / "chat_log.jsonl"
        entry = {"ts": datetime.now().isoformat(), "role": role, "message": message}
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def get_brief(self, brief_id: str) -> dict | None:
        brief_dir = self._brief_dir(brief_id)
        if not brief_dir:
            return None
        state_path = brief_dir / "state.json"
        if state_path.exists():
            return self._read_json(state_path)
        return None

    def list_briefs(self, limit: int | None = None) -> list[dict]:
        briefs = []
        if not self.briefs_dir.exists():
            return briefs
        for d in sorted(self.briefs_dir.iterdir(), reverse=True):
            if not d.is_dir():
                continue
            state_path = d / "state.json"
            if state_path.exists():
                briefs.append(self._read_json(state_path))
            if limit is not None and len(briefs) >= limit:
                break
        return briefs

    @staticmethod
    def seq_of(brief_id: str) -> int:
        """Extract the numeric sequence from a brief_id like '20260519_012_acme'."""
        parts = brief_id.split("_")
        if len(parts) < 2 or not parts[1].isdigit():
            raise ValueError(f"Invalid brief_id format: {brief_id!r}")
        return int(parts[1])

    def _read_json(self, path: Path) -> dict:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _write_json(self, path: Path, data: dict):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
