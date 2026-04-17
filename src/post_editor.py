"""Safety-gated editor for the bot's own Discourse posts.

All edits must:
- target a post the bot published (publisher confirms ownership),
- carry a non-empty edit_reason (audit trail in Discourse + Slack),
- be preceded by a backup of the pre-edit raw to knowledge/edits/,
- append an entry to the owning report's state.json edit_history.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)


class EditRefused(Exception):
    """Raised when a safety gate blocks an edit."""


class PostEditor:
    def __init__(self, bot_dir: str | Path, client, publisher):
        self.bot_dir = Path(bot_dir)
        self.client = client
        self.publisher = publisher
        self.edits_dir = self.bot_dir / "knowledge" / "edits"

    def apply_edit(
        self,
        post_id: int,
        new_raw: str,
        edit_reason: str,
        current_raw: str,
        edit_type: str = "format",
        change_summary: str = "",
        triggered_by_post: int = 0,
    ) -> dict:
        self._gate_ownership(post_id)
        self._gate_reason(edit_reason)
        self._gate_backup(current_raw)

        backup_path = self._write_backup(post_id, current_raw)

        try:
            result = self.client.edit_post(post_id, new_raw, edit_reason)
        except Exception as e:
            logger.error("edit_post failed: %s", e)
            raise

        self._append_edit_history(
            post_id=post_id,
            edit_type=edit_type,
            change_summary=change_summary,
            triggered_by_post=triggered_by_post,
            backup_path=backup_path,
        )

        logger.info("Applied edit to post %s (reason: %s)", post_id, edit_reason)
        return {"applied": True, "version": result.get("version"), "backup": str(backup_path)}

    # ── gates ──

    def _gate_ownership(self, post_id: int) -> None:
        info = self.publisher.get_report_for_post(post_id)
        if not info:
            raise EditRefused(f"post_id={post_id} is not a bot-owned post")

    @staticmethod
    def _gate_reason(edit_reason: str) -> None:
        if not (edit_reason or "").strip():
            raise EditRefused("edit_reason is required (empty string rejected)")

    @staticmethod
    def _gate_backup(current_raw: str) -> None:
        if not (current_raw or "").strip():
            raise EditRefused("cannot create backup — current_raw is empty")

    # ── side effects ──

    def _write_backup(self, post_id: int, current_raw: str) -> Path:
        self.edits_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%dT%H%M%S")
        path = self.edits_dir / f"{post_id}-{ts}.md"
        path.write_text(current_raw, encoding="utf-8")
        return path

    def _append_edit_history(
        self,
        post_id: int,
        edit_type: str,
        change_summary: str,
        triggered_by_post: int,
        backup_path: Path,
    ) -> None:
        info = self.publisher.get_report_for_post(post_id)
        if not info:
            return
        report_id = info["report_id"]
        state_path = self.bot_dir / "reports" / report_id / "state.json"
        if not state_path.exists():
            matches = list((self.bot_dir / "reports").glob(f"*{report_id}/state.json"))
            if matches:
                state_path = matches[0]
            else:
                logger.warning("state.json not found for %s", report_id)
                return

        state = json.loads(state_path.read_text("utf-8"))
        history = state["metadata"].get("edit_history", [])
        history.append({
            "post_id": post_id,
            "edited_at": datetime.now().isoformat(timespec="seconds"),
            "edit_type": edit_type,
            "change_summary": change_summary,
            "triggered_by_post": triggered_by_post,
            "backup_path": str(backup_path.relative_to(self.bot_dir)),
        })
        state["metadata"]["edit_history"] = history
        state["updated_at"] = datetime.now().isoformat()
        state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
