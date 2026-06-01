# Product Requirements: persona

> Last updated: 2026-04-22
> Owner: seungbinshin

## Problem

<!-- TODO: fill in — concrete pain point and before/after metric -->

Managing multiple autonomous Slack bots (news digest, research pipeline, dev-session orchestrator, personal RAG chatbot) as separate codebases duplicates scheduling, LLM-call plumbing, prompt management, and Slack integration. `persona` consolidates them into a **single config-driven multi-agent framework** where each bot is defined by a `bots/<name>/config.json` + `.env` pair and a `persona_type` field selects the pipeline.

<!-- TODO: fill in — quantify the "without this, I would..." cost -->

## Target Users

- **Primary**: seungbinshin — solo operator running personal Slack automation (news briefings, research report generation, persona chatbot, coder sessions) against a Claude Pro subscription.
- **Secondary**: <!-- TODO: fill in — anyone else expected to fork/adapt this? --> Currently none.

## Goals

- Single orchestrator (`persona.sh`) manages the lifecycle of every bot (start/stop/restart/status/list).
- Adding a new persona requires only a `bots/<name>/config.json` + `.env`, not new pipeline code, unless the `persona_type` itself is new.
- Reuse a single Claude-CLI-wrapping HTTP server (`claude-code-api`) so all bots share one Claude Pro quota.
- Research and reporter pipelines run autonomously on schedule with full Slack visibility (Socket Mode).
- Prompts are the source of truth in `prompts/`; `skills/` holds reusable logic; `tools/` wraps external APIs. Layer boundaries are enforced (see `persona.spec.md` §3.1).

## Non-Goals

- Not a SaaS product, not multi-tenant — single-operator deployment only.
- Not a general-purpose Slack bot framework — opinionated toward Claude + Socket Mode.
- Not a replacement for Claude Code itself — `claude-code-api` is a thin HTTP shim, not a reimplementation.

## Success Metrics

- All four bots (seungbin, coder, reporter, research) run concurrently under `./persona.sh start all` without port or token conflicts.
- Reporter publishes a daily briefing without manual intervention (see `briefing:` commit cadence in `git log`).
- Research pipeline can ingest Discourse/Confluence knowledge and emit a reviewed report end-to-end.
- <!-- TODO: fill in — uptime target, cost ceiling, etc. -->

## Requirements

### Functional

- Config-driven bot registration: drop `bots/<name>/{config.json,.env}` → `./persona.sh start <name>` works.
- Four `persona_type` values supported today: `persona`, `coder`, `reporter`, `research_pipeline`.
- Shared `claude-code-api` HTTP server for LLM inference against a Claude Pro subscription.
- Slack Socket Mode for all bots; each bot has its own Slack app + bot token + app token.
- Reporter pipeline: search → 48h freshness filter → 7-day dedup → HTML format → Slack publish + GitHub Pages archive.
- Research pipeline: scope management, paper cache, report store, Discourse/Confluence knowledge injection, reviewer loop, Q&A chat.
- Coder pipeline: Slack-triggered dev sessions with implementation + code review agents.

### Non-Functional

- **Performance**: each bot runs in its own process; shared Claude API server handles concurrency.
- **Security**: credentials live only in `bots/<name>/.env` and root `.env`; never committed. Bot API keys scoped per-bot via `api_keys` config field.
- **Platform**: macOS dev host (Python 3.12+ / 3.14 venv per `persona.spec.md`), Node.js 20+ / pnpm for `claude-code-api`.
- **Accessibility**: n/a — no user-facing UI beyond Slack and generated HTML briefings.

## Constraints

- Claude Pro subscription (not API billing) — inference must go through `claude-code-api`'s CLI wrapper.
- Slack Socket Mode (not public HTTP webhooks) — no inbound port required.
- Prompts live in `prompts/` only; `src/agents/*` is a compat shim (see §3.1 of `persona.spec.md`).
- Personal-use repo — not hardened for multi-user deployment.

## Out of Scope

- Web UI, mobile app, or non-Slack surfaces.
- Persona types beyond the four listed above (without explicit scope expansion).
- Fine-tuning or hosting custom models — Claude only, via `claude-code-api`.
- Multi-operator authentication / RBAC.

## Open Questions

- [ ] <!-- TODO: fill in — which persona types graduate to long-term support vs. stay experimental? -->
- [ ] <!-- TODO: fill in — licensing/visibility: README currently says "Private"; user intent is MIT. Reconcile. -->
- [ ] <!-- TODO: fill in — plan for migrating `src/agents` shim away after consumers move to `prompts/`? -->
