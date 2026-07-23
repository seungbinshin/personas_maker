"""Resolve the LLM gateway configured for an individual bot.

``ccapi`` discovers the launchd-managed Claude gateway as before.  ``gsapi``
uses the stable URL supplied in configuration/environment and is intended for
the sibling gpt-service-api process.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Callable

from claude_api_client import base_url as ccapi_base_url


@dataclass(frozen=True)
class LLMService:
    provider: str
    url: str
    api_key: str
    url_resolver: Callable[[], str] | None = None


def resolve_llm_service(bot_config: dict) -> LLMService:
    """Return the configured gateway without silently crossing providers.

    Config values take precedence over environment values so two bots can use
    different services in the same process environment.  ``LLM_PROVIDER`` is
    useful for local overrides; omission preserves the existing ccapi setup.
    """
    gateway = bot_config.get("llm", {})
    if not isinstance(gateway, dict):
        raise ValueError("'llm' must be a JSON object when provided")

    provider = str(
        gateway.get("provider") or os.environ.get("LLM_PROVIDER") or "ccapi"
    ).lower()
    if provider == "ccapi":
        url = str(
            gateway.get("url")
            or os.environ.get("CLAUDE_API_URL")
            or ccapi_base_url()
        )
        api_key = str(
            gateway.get("api_key")
            or os.environ.get("CLAUDE_API_KEY")
            or os.environ.get("LLM_API_KEY")
            or "sk-secondme-key-12345"
        )
        return LLMService("ccapi", url.rstrip("/"), api_key, ccapi_base_url)

    if provider == "gsapi":
        url = str(
            gateway.get("url")
            or os.environ.get("GSAPI_URL")
            or os.environ.get("GPT_SERVICE_API_URL")
            or "http://127.0.0.1:8081"
        )
        api_key = str(
            gateway.get("api_key")
            or os.environ.get("GSAPI_API_KEY")
            or os.environ.get("GPT_SERVICE_API_KEY")
            or os.environ.get("LLM_API_KEY")
            or os.environ.get("BOT_API_KEY")
            or ""
        )
        if not api_key:
            raise RuntimeError(
                "gsapi requires GSAPI_API_KEY (or LLM_API_KEY/BOT_API_KEY) in the bot .env"
            )
        return LLMService("gsapi", url.rstrip("/"), api_key)

    raise ValueError("Unsupported llm.provider %r; use 'ccapi' or 'gsapi'" % provider)
