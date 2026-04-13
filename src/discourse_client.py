"""Discourse REST API client for extracting forum content."""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field

import requests

logger = logging.getLogger(__name__)

FETCH_DELAY = 0.5  # seconds between API calls


@dataclass
class DiscoursePost:
    id: int
    post_number: int
    username: str
    created_at: str
    cooked: str  # HTML content
    reply_count: int = 0
    score: float = 0.0


@dataclass
class DiscourseTopic:
    id: int
    title: str
    slug: str
    category_id: int
    created_at: str
    last_posted_at: str
    posts_count: int
    views: int
    like_count: int = 0
    tags: list[str] = field(default_factory=list)


@dataclass
class DiscourseCategory:
    id: int
    name: str
    slug: str
    topic_count: int
    post_count: int


@dataclass
class DiscourseTopicDetail:
    topic: DiscourseTopic
    posts: list[DiscoursePost]
    participants: list[str] = field(default_factory=list)


class DiscourseClient:
    """Fetches categories, topics, and posts from a Discourse instance."""

    def __init__(self, base_url: str, api_key: str, api_username: str):
        self.base_url = base_url.rstrip("/")
        self.headers = {
            "Api-Key": api_key,
            "Api-Username": api_username,
        }

    def _get(self, path: str, params: dict | None = None) -> dict:
        url = f"{self.base_url}/{path.lstrip('/')}"
        resp = requests.get(url, headers=self.headers, params=params, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def fetch_categories(self) -> list[DiscourseCategory]:
        data = self._get("/categories.json")
        cats = []
        for c in data.get("category_list", {}).get("categories", []):
            cats.append(DiscourseCategory(
                id=c["id"],
                name=c["name"],
                slug=c.get("slug", "") or _slugify(c["name"]),
                topic_count=c.get("topic_count", 0),
                post_count=c.get("post_count", 0),
            ))
        return cats

    def fetch_topics_by_category(self, category_id: int) -> list[DiscourseTopic]:
        """Fetch all topics in a category, paginating through all pages."""
        topics = []
        page = 0
        while True:
            data = self._get(f"/c/{category_id}.json", params={"page": page})
            topic_list = data.get("topic_list", {}).get("topics", [])
            if not topic_list:
                break
            for t in topic_list:
                topics.append(DiscourseTopic(
                    id=t["id"],
                    title=t["title"],
                    slug=t.get("slug", ""),
                    category_id=category_id,
                    created_at=t.get("created_at", ""),
                    last_posted_at=t.get("last_posted_at", ""),
                    posts_count=t.get("posts_count", 0),
                    views=t.get("views", 0),
                    like_count=t.get("like_count", 0),
                    tags=t.get("tags", []),
                ))
            if len(topic_list) < 30:  # less than a full page
                break
            page += 1
            time.sleep(FETCH_DELAY)
        return topics

    def fetch_topic_detail(self, topic_id: int) -> DiscourseTopicDetail | None:
        """Fetch a topic with all its posts."""
        try:
            data = self._get(f"/t/{topic_id}.json")
        except requests.HTTPError as e:
            logger.warning("Failed to fetch topic %d: %s", topic_id, e)
            return None

        topic_data = data
        topic = DiscourseTopic(
            id=topic_data["id"],
            title=topic_data["title"],
            slug=topic_data.get("slug", ""),
            category_id=topic_data.get("category_id", 0),
            created_at=topic_data.get("created_at", ""),
            last_posted_at=topic_data.get("last_posted_at", ""),
            posts_count=topic_data.get("posts_count", 0),
            views=topic_data.get("views", 0),
            like_count=topic_data.get("like_count", 0),
            tags=[t if isinstance(t, str) else t.get("name", "") for t in topic_data.get("tags", [])],
        )

        raw_posts = topic_data.get("post_stream", {}).get("posts", [])
        # Truncate to 30 most recent if very long
        if len(raw_posts) > 30:
            raw_posts = raw_posts[-30:]

        posts = []
        for p in raw_posts:
            if p.get("hidden") or p.get("deleted_at"):
                continue
            posts.append(DiscoursePost(
                id=p["id"],
                post_number=p.get("post_number", 0),
                username=p.get("username", "unknown"),
                created_at=p.get("created_at", ""),
                cooked=p.get("cooked", ""),
                reply_count=p.get("reply_count", 0),
                score=p.get("score", 0.0),
            ))

        participants = list({p.username for p in posts})

        return DiscourseTopicDetail(topic=topic, posts=posts, participants=participants)

    def fetch_all(self, categories: list[DiscourseCategory] | None = None) -> dict:
        """Fetch all topics with details, organized by category.

        Returns: {category_slug: {"category": DiscourseCategory, "topics": [DiscourseTopicDetail]}}
        """
        if categories is None:
            categories = self.fetch_categories()

        result = {}
        for cat in categories:
            if cat.topic_count == 0:
                continue
            logger.info("Fetching category: %s (%d topics)", cat.name, cat.topic_count)
            topics = self.fetch_topics_by_category(cat.id)
            details = []
            for t in topics:
                detail = self.fetch_topic_detail(t.id)
                if detail:
                    details.append(detail)
                time.sleep(FETCH_DELAY)
            result[cat.slug] = {"category": cat, "topics": details}
            logger.info("Fetched %d/%d topics for %s", len(details), len(topics), cat.name)

        return result


def _slugify(text: str) -> str:
    """Convert text to a filesystem-safe slug."""
    text = text.lower().strip()
    text = re.sub(r"[^a-z0-9가-힣\s-]", "", text)
    text = re.sub(r"\s+", "-", text)
    return text[:80] or "unnamed"
