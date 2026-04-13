# Discourse Knowledge Integration Design

**Date:** 2026-04-13
**Status:** Approved
**Goal:** Integrate HyperAccel Discourse forum as a knowledge source for the Research Bot, structured as an Obsidian-compatible markdown vault.

---

## 1. Overview

The Research Bot currently draws context from `hyperaccel_lpu.md` (hardware specs), `scopes/hyperaccel.json` (conferences/keywords), and `paper_cache/` (previously analyzed papers). It has no awareness of what the team is actually working on, discussing, or has already evaluated.

This feature extracts all content from the HyperAccel Discourse forum (~196 topics, 12 categories), LLM-summarizes it into a structured Obsidian-compatible knowledge vault, and injects it into the research pipeline at Discovery and Report stages.

### Priority

- **Primary (A):** Internal context — the bot understands what teams are working on, their constraints, and current discussions. This improves paper prioritization and application analysis.
- **Secondary (B):** Technology radar — the bot avoids recommending technologies already tried/rejected and builds on existing team knowledge.

---

## 2. Vault Structure

```
bots/research/knowledge/
├── _index.json                    # sync metadata
├── categories/
│   ├── simulator.md               # category-level summary
│   ├── legato.md
│   ├── hardware.md
│   ├── runtime.md
│   ├── ml.md
│   ├── general.md
│   └── ...
├── topics/
│   ├── simulator/
│   │   ├── pim-device-addition.md
│   │   └── ...
│   ├── legato/
│   │   └── ...
│   ├── hardware/
│   │   └── ...
│   └── ...
└── tags/
    └── _tag_index.md              # tag -> topic mapping
```

### Topic Note Format

```markdown
---
discourse_id: 289
title: "[x-sim] pim 디바이스 추가"
category: simulator
tags: [simulator, hardware]
author: username
created: 2026-04-10
last_activity: 2026-04-12
post_count: 5
participants: [user1, user2, user3]
status: active
summarized: true
---

## 요약
One-paragraph synthesis of the full discussion thread.

## 핵심 결정사항
- Decision with rationale

## 제약 및 블로커
- Known limitations or blockers

## 미해결 질문
- Open questions still being debated

## 관련 기술/키워드
- Key technologies, tools, or concepts mentioned

## Related
- [[other-topic-name]]
- [[categories/category-name]]
```

### Category Summary Format

```markdown
---
category: simulator
discourse_id: 11
topic_count: 57
last_synced: 2026-04-13
---

## 개요
What this team/area focuses on (2-3 sentences).

## 진행 중인 프로젝트
- Active projects with current status and key participants

## 핵심 결정사항 및 제약
- Major decisions and technical constraints across all topics

## 최근 논의
- [[topic-1]] — one-line summary
- [[topic-2]] — one-line summary

## 기술 스택
- Frameworks, tools, and approaches this group uses
```

---

## 3. Discourse Extraction & Sync

### New module: `src/discourse_client.py`

```python
class DiscourseClient:
    def __init__(self, base_url: str, api_key: str, api_username: str)
    def fetch_categories(self) -> list[Category]
    def fetch_topics_by_category(self, category_id: int, page: int = 0) -> list[Topic]
    def fetch_topic_detail(self, topic_id: int) -> TopicDetail
    def fetch_all(self) -> DiscourseSnapshot
    def get_sync_diff(self, last_sync: datetime) -> list[TopicDetail]  # future incremental
```

### New module: `src/discourse_sync.py`

Orchestrates full extraction -> summarize -> write vault flow.

```python
class DiscourseSync:
    def __init__(self, client: DiscourseClient, runtime: ClaudeRuntimeClient, vault_path: str)

    def run_full_sync(self):
        # 1. Fetch all topics from Discourse API
        # 2. For each topic: summarize via ClaudeRuntimeClient -> write .md
        # 3. For each category: aggregate topic summaries -> write category .md
        # 4. Build tag index and wikilinks
        # 5. Update _index.json

    def summarize_topic(self, topic: TopicDetail) -> str
    def summarize_category(self, category, topic_summaries: list) -> str
```

### Trigger

- Manual: `!research sync-discourse` Slack command
- Later: addable to `config.json` schedule alongside `discovery_scan` and `auto_report`

### Rate Limiting

0.5s delay between topic detail fetches. ~196 topics = ~2 minutes for full extraction.

### Update Strategy

- **Phase 1 (now):** Snapshot-based — full extraction and rebuild on each sync
- **Phase 2 (later):** Incremental — track `last_seen_post_id` per topic, only fetch new/updated posts

---

## 4. Knowledge Retrieval & Pipeline Injection

### New module: `src/discourse_knowledge.py`

```python
class DiscourseKnowledge:
    def __init__(self, vault_path: str)

    def load_category_summaries(self) -> dict[str, str]
    def search_topics(self, keywords: list[str], top_k: int = 5) -> list[TopicNote]
    def build_context(self, keywords: list[str]) -> str
        # Layer 1: All category summaries (always included, ~3K tokens)
        # Layer 2: Top-k relevant topic notes (~2.5K tokens)
        # Total: ~5.5K tokens per injection
```

### Injection Points

**1. Discovery stage** (`run_discovery`):
- Current: scope + hyperaccel_lpu.md + paper_cache
- Added: `DiscourseKnowledge.build_context(scope.keywords)`
- Effect: Researcher prioritizes papers relevant to active team projects

**2. Report writing stage** (`run_reports`):
- Current: idea_brief + deep_dive + scope + hyperaccel_lpu.md
- Added: `DiscourseKnowledge.build_context(idea-specific keywords)`
- Effect: Researcher references internal discussions in 적용방안 section, avoids already-tried approaches

---

## 5. Summarization Prompts

All LLM calls go through the existing `ClaudeRuntimeClient` (-> `claude-code-api` server).

### Topic Summarization Prompt

```
You are summarizing an internal team discussion from HyperAccel's Discourse forum.

Category: {category_name}
Topic: {title}
Posts: {post_count} | Participants: {participants}
Date range: {created_at} ~ {last_activity}

--- Posts ---
{all posts concatenated, with author and timestamp per post}
---

Write a structured summary in Korean. Output ONLY the following markdown:

## 요약
One paragraph synthesizing the full discussion.

## 핵심 결정사항
- Bullet list of decisions made (empty if none)

## 제약 및 블로커
- Known limitations, blockers, or constraints discussed

## 미해결 질문
- Open questions still being debated (empty if all resolved)

## 관련 기술/키워드
- Key technologies, tools, or concepts mentioned

Rules:
- Be concise. Each section should be 1-5 bullet points.
- Preserve technical specifics (model names, numbers, configs).
- If a decision was reversed or debated, note the final state.
- Mark status: active (ongoing discussion), resolved (concluded), archived (stale).
```

### Category Aggregation Prompt

```
You are building a category overview for HyperAccel's {category_name} team.
Below are summaries of all {topic_count} discussion topics in this category.

{all topic summaries concatenated}

Write a category-level overview in Korean. Output ONLY the following markdown:

## 개요
What this team/area focuses on (2-3 sentences).

## 진행 중인 프로젝트
- Active projects with current status and key participants

## 핵심 결정사항 및 제약
- Major decisions and technical constraints across all topics

## 최근 논의
- Top 5 most recent/active topics with one-line summary each

## 기술 스택
- Frameworks, tools, and approaches this group uses
```

---

## 6. Wikilinks & Cross-Referencing

### Link Generation

1. **Discourse internal links** — Posts linking to other topics (`/t/slug/123`) are converted to `[[category/topic-slug]]`.
2. **Tag co-occurrence** — Topics sharing 2+ tags get `[[related]]` links in a `## Related` section.
3. **Category backlinks** — Every topic links to its category: `[[categories/simulator]]`. Category summaries link to top topics.

### Filename Convention

- Topic files: `topics/{category_slug}/{discourse-topic-slug}.md`
- Category files: `categories/{category_slug}.md`
- Wikilinks: `[[simulator/pim-device-addition]]` (relative vault paths)

### Tag Index (`tags/_tag_index.md`)

```markdown
## legato
- [[legato/compiler-optimization-pass]]
- [[legato/kernel-fusion-strategy]]

## simulator
- [[simulator/pim-device-addition]]
- [[simulator/xsim-memory-model]]
```

---

## 7. Error Handling & Edge Cases

### Discourse API Failures
- Network/auth errors during fetch: log, skip topic, continue. Report skipped count.
- Entire API unreachable: abort sync, leave existing vault untouched.

### LLM Summarization Failures
- `ClaudeRuntimeClient.run()` returns `success=False` or timeout: save raw posts as fallback markdown. Mark `summarized: false` in frontmatter. Retry on next sync.

### Content Edge Cases
- Empty topics (0 posts after OP): create note with OP content only.
- Very long topics (20+ posts): truncate to most recent 30 posts. Mark `truncated: true, total_posts: N`.
- Deleted/hidden posts: skipped (API filters these).
- Non-Korean posts: summarize in original language.

### Vault Integrity
- Full sync overwrites all files (snapshot approach). No merge conflicts.
- `_index.json` tracks: `last_sync`, `topic_count`, `skipped_topics`, `failed_summaries`, `duration_seconds`.
- Future incremental: `_index.json` stores `last_seen_post_id` per topic.

### Pipeline Injection Failures
- Vault empty or missing: pipeline runs normally without Discourse context. Log warning.
- `build_context()` returns empty: same graceful degradation.

---

## 8. New Files Summary

| File | Purpose |
|------|---------|
| `src/discourse_client.py` | Discourse API client (fetch categories, topics, posts) |
| `src/discourse_sync.py` | Sync orchestrator (extract -> summarize -> write vault) |
| `src/discourse_knowledge.py` | Knowledge retrieval (load vault, search, build context) |
| `bots/research/knowledge/` | Obsidian-compatible vault (output directory) |

### Modified Files

| File | Change |
|------|--------|
| `src/pipelines/research_pipeline.py` | Inject Discourse context at Discovery and Report stages |
| `src/bot.py` | Add `!research sync-discourse` command handler |
| `bots/research/.env` | Already has `DISCOURSE_API_KEY` |
| `bots/research/config.json` | Add `discourse` config section (base_url, api_username, vault_path) |
