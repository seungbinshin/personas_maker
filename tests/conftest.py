"""Shared pytest fixtures."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "src"))


@pytest.fixture
def tmp_vault(tmp_path):
    """Minimal research bot vault layout for tests."""
    vault = tmp_path / "bot_dir"
    (vault / "knowledge").mkdir(parents=True)
    (vault / "knowledge" / "topics").mkdir()
    (vault / "reports").mkdir()
    (vault / "context").mkdir()
    return vault
