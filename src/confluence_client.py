"""Confluence Cloud REST API client for extracting wiki content."""

from __future__ import annotations

import base64
import logging
import re
import time
from dataclasses import dataclass, field

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)

FETCH_DELAY = 0.5  # seconds between API calls
CONNECT_TIMEOUT = 30   # seconds for TCP + SSL handshake
READ_TIMEOUT = 120     # seconds for response body


@dataclass
class ConfluencePage:
    id: str
    title: str
    space_key: str
    body: str  # storage format HTML
    created: str
    last_modified: str
    author: str
    labels: list[str] = field(default_factory=list)
    parent_id: str | None = None
    version: int = 1


class ConfluenceClient:
    """Fetches pages and spaces from a Confluence Cloud instance."""

    def __init__(self, base_url: str, email: str, api_token: str):
        self.base_url = base_url.rstrip("/")
        credentials = base64.b64encode(f"{email}:{api_token}".encode()).decode()
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Basic {credentials}",
            "Accept": "application/json",
        })
        retry = Retry(
            total=3,
            backoff_factor=2,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET"],
        )
        self.session.mount("https://", HTTPAdapter(max_retries=retry))

    def _get(self, path: str, params: dict | None = None) -> dict:
        url = f"{self.base_url}/rest/api{path}"
        resp = self.session.get(
            url, params=params, timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
        )
        resp.raise_for_status()
        return resp.json()

    def search(
        self, keywords: str, spaces: list[str] | None = None
    ) -> list[ConfluencePage]:
        """CQL search for pages matching keywords, optionally scoped to spaces."""
        cql_parts = ["type=page"]
        if spaces:
            safe_spaces = [s.replace('"', '\\"') for s in spaces]
            space_clauses = " OR ".join(f'space="{s}"' for s in safe_spaces)
            cql_parts.append(f"({space_clauses})")
        safe_keywords = keywords.replace('"', '\\"')
        cql_parts.append(f'text ~ "{safe_keywords}"')
        cql = " AND ".join(cql_parts)

        expand = "body.storage,version,metadata.labels,ancestors"
        pages: list[ConfluencePage] = []
        start = 0
        limit = 25

        seen_ids: set[str] = set()
        while True:
            try:
                data = self._get(
                    "/content/search",
                    params={"cql": cql, "expand": expand, "start": start, "limit": limit},
                )
            except requests.RequestException as exc:
                logger.warning(
                    "Search pagination failed at start=%d (returning %d pages so far): %s",
                    start, len(pages), exc,
                )
                break

            results = data.get("results", [])
            new_in_batch = 0
            for item in results:
                item_id = str(item.get("id", ""))
                if item_id in seen_ids:
                    continue
                seen_ids.add(item_id)
                new_in_batch += 1
                page = self._parse_page(item)
                if page:
                    pages.append(page)

            # Stop if: fewer results than limit, no new pages (API ignoring start), or no next link
            if len(results) < limit or new_in_batch == 0 or "_links" not in data or "next" not in data.get("_links", {}):
                break
            start += limit
            time.sleep(FETCH_DELAY)

        logger.info("Search '%s' returned %d pages", keywords, len(pages))
        return pages

    def fetch_page(self, page_id: str) -> ConfluencePage:
        """Fetch a single page with full body content."""
        expand = "body.storage,version,metadata.labels,ancestors"
        data = self._get(f"/content/{page_id}", params={"expand": expand})
        return self._parse_page(data)

    def fetch_children(self, page_id: str, _depth: int = 0) -> list[ConfluencePage]:
        """Recursively fetch ALL child pages under a given page."""
        if _depth > 5:
            logger.warning("Recursion depth limit reached for page %s", page_id)
            return []

        expand = "body.storage,version,metadata.labels,ancestors"
        children: list[ConfluencePage] = []
        start = 0
        limit = 25
        seen_ids: set[str] = set()

        while True:
            try:
                data = self._get(
                    f"/content/{page_id}/child/page",
                    params={"expand": expand, "start": start, "limit": limit},
                )
            except requests.RequestException as exc:
                logger.warning(
                    "Failed to fetch children of page %s: %s", page_id, exc
                )
                return children

            results = data.get("results", [])
            new_in_batch = 0
            for item in results:
                child = self._parse_page(item)
                if child and child.id not in seen_ids:
                    seen_ids.add(child.id)
                    new_in_batch += 1
                    children.append(child)
                    time.sleep(FETCH_DELAY)
                    grandchildren = self.fetch_children(child.id, _depth=_depth + 1)
                    children.extend(grandchildren)

            if len(results) < limit or new_in_batch == 0:
                break
            start += limit
            time.sleep(FETCH_DELAY)

        return children

    def _parse_page(self, data: dict) -> ConfluencePage | None:
        """Parse Confluence API JSON response into a ConfluencePage."""
        try:
            labels = [
                lbl["name"]
                for lbl in data.get("metadata", {})
                .get("labels", {})
                .get("results", [])
            ]

            ancestors = data.get("ancestors", [])
            parent_id = str(ancestors[-1]["id"]) if ancestors else None

            version_info = data.get("version", {})
            history = data.get("history", {})

            return ConfluencePage(
                id=str(data["id"]),
                title=data.get("title", ""),
                space_key=data.get("space", {}).get("key", ""),
                body=data.get("body", {}).get("storage", {}).get("value", ""),
                created=history.get("createdDate", ""),
                last_modified=version_info.get("when", ""),
                author=history.get("createdBy", {}).get("displayName", "")
                or version_info.get("by", {}).get("displayName", "unknown"),
                labels=labels,
                parent_id=parent_id,
                version=version_info.get("number", 1),
            )
        except (KeyError, IndexError) as exc:
            logger.warning("Failed to parse Confluence page data: %s", exc)
            return None


def _slugify(text: str) -> str:
    """Convert text to a filesystem-safe slug."""
    text = text.lower().strip()
    text = re.sub(r"[^a-z0-9가-힣\s-]", "", text)
    text = re.sub(r"\s+", "-", text)
    return text[:80] or "unnamed"
