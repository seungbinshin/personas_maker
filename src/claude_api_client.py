"""Discover claude-code-api base URL from env override or registry file.

Priority:
1. CLAUDE_CODE_API_URL env var (explicit override)
2. ~/.config/claude-code-api/registry.json (written by running server)
3. Raise RuntimeError with actionable message
"""
import json
import os
from pathlib import Path

REGISTRY = Path(
    os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))
) / "claude-code-api" / "registry.json"


def base_url() -> str:
    if env := os.environ.get("CLAUDE_CODE_API_URL"):
        return env

    if not REGISTRY.exists():
        raise RuntimeError(
            "claude-code-api not running. Run `ccapi ensure` "
            "or set CLAUDE_CODE_API_URL."
        )

    try:
        data = json.loads(REGISTRY.read_text())
    except json.JSONDecodeError as e:
        raise RuntimeError(
            f"claude-code-api registry is corrupt at {REGISTRY}: {e}. "
            "Run `ccapi ensure` to refresh."
        ) from e

    port = data.get("port")
    if not isinstance(port, int):
        raise RuntimeError(
            f"claude-code-api registry missing 'port' at {REGISTRY}. "
            "Run `ccapi ensure` to refresh."
        )
    return f"http://127.0.0.1:{port}"
