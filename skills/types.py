"""Common types for shared skills and tools."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class LLMRunRequest:
    prompt: str
    timeout_ms: int = 0
    session_id: str | None = None
    heartbeat_channel: str | None = None
    heartbeat_agent: str | None = None
    heartbeat_label: str | None = None
    cwd: str | None = None
    model: str | None = None
    allow_file_write: bool = False


@dataclass(slots=True)
class LLMRunResult:
    success: bool
    output: str
    duration_ms: int = 0
    timed_out: bool = False
    timeout_type: str | None = None
    session_id: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class MessagePayload:
    channel: str
    text: str = ""
    blocks: list[dict[str, Any]] | None = None
    agent_name: str | None = None
    thread_ts: str | None = None


@dataclass(slots=True)
class SearchRequest:
    query: str
    today: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ArtifactRecord:
    artifact_id: str
    kind: str
    version: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

