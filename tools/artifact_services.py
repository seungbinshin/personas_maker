"""Wrappers around artifact-related domain services."""

from __future__ import annotations

from pathlib import Path

from src.paper_cache import PaperCache
from src.report_store import ReportStore
from src.scope import ResearchScope


class ArtifactServices:
    """Container for artifact/state helpers used by shared skills."""

    def __init__(self, bot_dir: Path, scope_file: str | None = None):
        self.bot_dir = bot_dir
        self.store = ReportStore(bot_dir / "reports")
        self.paper_cache = PaperCache(bot_dir / "paper_cache")
        context_dir = bot_dir / "context"
        self.scope = (
            ResearchScope(bot_dir / scope_file, context_dir=context_dir)
            if scope_file
            else None
        )

