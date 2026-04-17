"""One-shot trigger: poll Discourse comments and respond. Uses the running
claude-code-api instance. Run with BOT_DIR=bots/research."""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(PROJECT_ROOT))

BOT_DIR = Path(os.environ.get("BOT_DIR", Path(__file__).parent.parent / "bots" / "research"))
load_dotenv(BOT_DIR / ".env")
with open(BOT_DIR / "config.json", "r", encoding="utf-8") as f:
    BOT_CONFIG = json.load(f)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

from slack_sdk import WebClient  # noqa: E402

from pipelines.research_pipeline import ResearchPipeline  # noqa: E402

CLAUDE_API_URL = os.environ.get("CLAUDE_API_URL", "http://localhost:8083")
CLAUDE_API_KEY = os.environ.get("CLAUDE_API_KEY", "sk-research-key-12345")

slack = WebClient(token=os.environ["SLACK_BOT_TOKEN"])
pipeline = ResearchPipeline(BOT_CONFIG, slack, CLAUDE_API_URL, CLAUDE_API_KEY, BOT_DIR)

print(f"discourse_engagement initialized: {pipeline.discourse_engagement is not None}")
pipeline.poll_discourse_comments()
print("Poll complete.")
