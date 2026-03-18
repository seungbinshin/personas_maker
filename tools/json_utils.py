"""JSON parsing helpers shared across tools and skills."""

from __future__ import annotations

import json
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)


def parse_json_response(text: str) -> dict[str, Any] | list[Any] | None:
    """Extract JSON from plain text or fenced markdown."""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            pass

    extracted = _extract_balanced_json_block(text)
    if extracted is not None:
        try:
            return json.loads(extracted)
        except json.JSONDecodeError:
            pass

    logger.warning("Failed to parse JSON from response: %s", text[:200])
    return None


def _extract_balanced_json_block(text: str) -> str | None:
    """Extract the first balanced JSON object/array from mixed text."""
    start_positions = [
        idx for idx in (text.find("{"), text.find("[")) if idx != -1
    ]
    if not start_positions:
        return None

    start = min(start_positions)
    opening = text[start]
    closing = "}" if opening == "{" else "]"
    depth = 0
    in_string = False
    escape = False

    for idx in range(start, len(text)):
        char = text[idx]

        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue

        if char == '"':
            in_string = True
        elif char == opening:
            depth += 1
        elif char == closing:
            depth -= 1
            if depth == 0:
                return text[start:idx + 1]

    return None

