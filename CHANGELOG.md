# Changelog

All notable changes to **persona** will be documented here.

- Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
- Versioning: [Semantic Versioning](https://semver.org/spec/v2.0.0.html)

> Seeded from `git log` at `/Users/shinseungbin/workspace/work/persona` on 2026-04-22.
> Pre-standard history was unversioned; entries are grouped into a single `[0.1.0]` block. Future changes should land under `[Unreleased]` and graduate on release.

## [Unreleased]

### Added
- 5-doc standard: `PRD.md`, `Agent.md`, `CHANGELOG.md`, `DECISIONS.md` (this retrofit, 2026-04-22).
- `README.proposed.md` drafted to align existing README with the 5-doc template (status/docs/license sections); original `README.md` left untouched pending review.

### Changed
-

### Fixed
-

### Deprecated
-

### Removed
-

### Security
-

---

## [0.1.0] — 2026-04-22

Initial versioned snapshot. Everything below predates the 5-doc retrofit and is grouped here.

### Added
- **Discourse engagement v2** pipeline: post editor with safety-gated self-edit + backup/audit trail, Q&A archiver writing approved Q&A to `knowledge/topics/qa`, edit sub-flow prompts (classify/generate/fact-check), draft-aware internal context and glossary candidate accumulation (`feature/discourse-engagement-v2` merged).
- **Glossary** core module with filters/grep/refresh and seed CLI; wired `GlossaryManager` into engagement fact-check and pipeline.
- **Discourse** integration: REST API client with CQL sanitization + error handling, sync orchestrator with LLM summarization, incremental sync + progress reporting, `DiscourseClient.edit_post` with audit-required reason, `!research sync-discourse` Slack command, Discourse context injection into research discovery + report stages, two-layer Discourse knowledge retrieval.
- **Confluence** integration: REST API client with CQL search and recursive child fetch, sync orchestrator with LLM summarization and incremental sync, Confluence knowledge reader + context builder, `!research sync-confluence` Slack command, Confluence context injection into research pipeline.
- **Research pipeline** improvements: scope management, report store with `load_all_artifacts`, paper cache, researcher chat system prompt for report Q&A, `ChatSession` + chat methods on `ResearchPipeline`, `!research chat` command with thread-based routing, SW/HW report split with tightened dedup and 15-item threshold.
- **Reporter pipeline**: 48h freshness filter, 7-day dedup, HTML newspaper formatting, auto-push to GitHub Pages, initial reporter bot.
- **Infrastructure**: `.env.models` for consolidated model version pins, `.worktrees/` gitignored, pytest scaffold with shared vault fixture, `persona.sh` lifecycle orchestrator (start/stop/restart/status/list), unified `src/bot.py` routed by `persona_type`.
- **Docs**: implementation plans and design specs for Discourse engagement v2 and Discourse/Confluence knowledge integration, `persona.spec.md`, `agent-skills-spec.md`, README with setup guide.

### Fixed
- Empty-body guard, YAML quoting, and size limit in Confluence sync.
- `str.replace` for chat prompt to avoid JSON curly-brace crash.
- Pages deployment: enable Node 24, upgrade `upload-pages-artifact` to v4.

### Operational
- Daily `briefing:` commits from the reporter bot (2026-03-22 through 2026-04-22 at seed time) — these are data artifacts, not code changes; listed for completeness only.

<!-- Note: sub-projects under this directory (notably `claude-code-api/`) have their own git history and may carry their own changelogs. -->
