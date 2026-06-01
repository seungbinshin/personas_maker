# Agent Guide: persona

> **This file is the authoritative working manual for AI agents (Claude, Cursor, Copilot, Codex) and automated tools.** Read before making any changes.

## What This Is

`persona` is a **parent/container directory** holding a personal multi-agent Slack bot framework plus its embedded sub-projects. It is **not** a single codebase — the root is a Python project (the persona runtime) and `claude-code-api/` is an independent Node/TypeScript project with its own `package.json`, `LICENSE`, and `README`. The Python runtime orchestrates config-driven bots (news reporter, research pipeline, coder session, personal RAG persona) that share one `claude-code-api` HTTP server for Claude Pro inference and talk to Slack via Socket Mode. Authoritative design spec: `persona.spec.md`.

## Sub-Projects

| Path | Kind | Purpose |
|------|------|---------|
| `.` (root) | Python 3.12+ runtime | Shared bot/scheduler/pipelines code, entrypoint (`persona.sh`, `src/bot.py`) |
| `claude-code-api/` | Node/TS (pnpm) | HTTP server wrapping the Claude CLI so the Python bots can share a Claude Pro subscription |
| `bots/seungbin/` | Config + data | SecondMe persona bot (`persona_type: persona`, port 8080) |
| `bots/coder/` | Config + data + spec | Dev session bot (`persona_type: coder`, port 8081) |
| `bots/reporter/` | Config + digests | Daily news digest bot (`persona_type: reporter`, port 8082) |
| `bots/research/` | Config + knowledge + reports | Research pipeline bot (`persona_type: research_pipeline`, port 8083) |

Each `bots/<name>/` is an **instance**, not a full project — they share the single Python runtime at the root.

## Directory Map

```
.
├── persona.sh                 — Bot lifecycle orchestrator (start/stop/restart/status/list)
├── persona.spec.md            — Canonical design spec; read this first
├── agent-skills-spec.md       — Agent skill spec (reference)
├── config.json                — Parser config (KakaoTalk file → user-name mapping)
├── pytest.ini                 — pytest config (testpaths=tests, pythonpath=. src)
├── src/                       — Python runtime (bot.py, scheduler, pipelines, clients, stores)
│   ├── bot.py                 — Unified Slack bot, routes by persona_type
│   ├── scheduler.py           — Background scheduling engine
│   ├── pipelines/             — reporter_pipeline, research_pipeline, coder_pipeline, base
│   └── agents/                — Compat shim for legacy prompt imports (do not add new prompt text here)
├── prompts/                   — Canonical prompt assets (source of truth)
├── skills/                    — Reusable business logic (conversation, persona, research)
├── tools/                     — Runtime/API facade (claude_runtime, slack_facade, newspaper_html, pdf_extract, ...)
├── adapters/                  — Runtime-specific adapters (slack_adapter)
├── bots/                      — Per-bot config/env/data (seungbin, coder, reporter, research)
├── claude-code-api/           — Independent Node/TS sub-project; HTTP wrapper for Claude CLI
├── tests/                     — pytest suite
├── docs/                      — Skill and superpowers reference docs
├── plans/                     — Design plans (e.g. engagement v2)
├── output/, data/             — Parser/analyzer artifacts
└── .worktrees/                — Git worktree scratch (gitignored)
```

### Key Files

- `persona.sh` — the operator entrypoint. All lifecycle goes through it.
- `persona.spec.md` — authoritative spec for layers, persona_types, and layer boundaries.
- `src/bot.py` — unified Slack bot; `persona_type` in each bot's config selects behavior.
- `src/pipelines/base.py` — shared pipeline infrastructure.
- `bots/<name>/config.json` — per-bot config (schedule, channels, persona_type, model, `api_keys`).
- `bots/<name>/.env` — per-bot Slack tokens and API port.
- `.env` (root) — `ANTHROPIC_API_KEY`, Claude API URL/port.

## Tech Stack

- **Language**: Python 3.12+ (spec mentions 3.14 venv); TypeScript/Node 20+ inside `claude-code-api/`.
- **Framework**: slack-bolt (Socket Mode), `schedule` (cron-style), custom pipelines.
- **Key dependencies** (root): `slack-bolt`, `python-dotenv`, `requests`, `schedule`. Full list: `.venv/` — no `pyproject.toml` or `requirements.txt` committed at root. <!-- TODO: add a pinned requirements file -->
- **Key dependencies** (claude-code-api): see `claude-code-api/package.json`.
- **Package manager**: pip (root), pnpm (`claude-code-api/`).
- **Runtime**: local macOS host; bots run as background processes tracked via `.persona.<name>.pids`.

## Commands

| Intent | Command |
|--------|---------|
| Create/activate venv | `python3 -m venv .venv && source .venv/bin/activate` |
| Install Python deps | `pip install slack-bolt python-dotenv requests schedule` <!-- TODO: replace with `pip install -r requirements.txt` once pinned --> |
| Install claude-code-api deps | `cd claude-code-api && pnpm install` |
| List configured bots | `./persona.sh list` |
| Start one bot | `./persona.sh start <name>` (e.g. `reporter`) |
| Start all bots | `./persona.sh start all` |
| Stop / restart / status | `./persona.sh stop <name>` · `./persona.sh restart <name>` · `./persona.sh status` |
| Run tests | `pytest` (config in `pytest.ini`; `testpaths=tests`) |
| Lint | <!-- TODO: add lint command if/when adopted --> |
| Typecheck | <!-- TODO: add typecheck command if/when adopted --> |
| Build claude-code-api | `cd claude-code-api && pnpm build` (see its own README) |

## Conventions

- **Layer boundaries (from `persona.spec.md` §3.1)** — allowed direction: `src/adapters → skills → prompts/tools → external services`. Forbidden: `prompts/* → skills/*`, `prompts/* → tools/*`, `skills/* → src/*`, `tools/* → skills/*`, and new prompt text defined directly in `src/agents/*`.
- **Placement rule of thumb**: prompt text → `prompts/`; reusable domain logic → `skills/`; runtime wrapper / external integration → `tools/`; entrypoint/bootstrap glue → `src/` or `adapters/`.
- **Per-bot config pattern**: every bot needs `bots/<name>/config.json` (persona_type, schedule, channels, `api_keys`, `default_model`) and `bots/<name>/.env` (Slack tokens + `API_PORT`).
- **Shared runtime**: all four bot types are served by the same `src/bot.py`; the `persona_type` field dispatches to the right pipeline.
- **Reporter freshness**: hard 48-hour cutoff in code + 7-day dedup against `bots/reporter/digests/`.
- **Secrets never in config.json** — only `.env` files; tokens are gitignored.
- **PIDs tracked** in `.persona.<name>.pids` at the repo root.

## Forbidden Patterns

- **Adding prompt text in `src/agents/*`** — Why: that module is a compatibility shim for legacy imports; `prompts/` is the source of truth (`persona.spec.md` §3.1).
- **Importing `skills/*` from `prompts/*` or `tools/*`** — Why: violates the enforced dependency direction; causes circular/layer-break regressions.
- **Committing `bots/<name>/config.json` with real tokens** — Why: tokens belong in `.env`; committed JSON is checked into history forever.
- **Calling the Claude API directly from pipelines** — Why: all LLM calls go through `tools/claude_runtime.py` → `claude-code-api` so the Claude Pro quota is shared.
- <!-- TODO: add as they're discovered -->

## Common Pitfalls

- Forgetting that `bots/<name>/.env` `API_PORT` must match the port `claude-code-api` is listening on, or the bot will silently fail LLM calls.
- Editing prompts inside `src/agents/*` instead of `prompts/*` — the change will be shadowed or lost.
- Running `./persona.sh start all` without first starting `claude-code-api` — bots will error on first LLM call.
- Large `.log` files at the repo root (`.reporter-bot.log` > 100 MB observed) — rotate or gitignore; don't grep naively.
- <!-- TODO: add as they're discovered -->

## Testing Policy

- **When required**: any change to `src/pipelines/*`, `skills/*`, `tools/*`, or shared `src/*` modules (parsers, stores, clients).
- **Where tests live**: `tests/` (per `pytest.ini`).
- **Before claiming done**: run `pytest` and confirm pass.
- <!-- TODO: coverage target, integration vs unit split -->

## Safety Rails

- **Never commit** `.env`, `bots/*/.env`, or `bots/*/config.json` with real tokens.
- **Never force-push** `main` — reporter auto-publishes briefing commits on a schedule; rewriting history nukes digests.
- **Never call `./persona.sh stop all` on a running research pipeline mid-report** — artifacts in `bots/research/reports/` may be left in a partial state.
- **Never delete `.persona.<name>.pids`** while a bot is running; the orchestrator relies on it for stop/restart.
- **Do not modify** sub-project internals (e.g. `claude-code-api/`) as part of a root-level change without flagging it — it has its own README, LICENSE, and lifecycle.

## Related Docs

- [README.md](./README.md) — Human entry point and setup guide.
- [CLAUDE.md](./CLAUDE.md) — Existing Claude Code control file for this scope (gstack/browse guidance, available skills). Respected as-is; not overwritten by this doc.
- [PRD.md](./PRD.md) — Why this exists, who it's for.
- [DECISIONS.md](./DECISIONS.md) — Why the architecture looks like this.
- [CHANGELOG.md](./CHANGELOG.md) — What changed when.
- [persona.spec.md](./persona.spec.md) — Full design spec (layers, persona types, pipelines).
- [agent-skills-spec.md](./agent-skills-spec.md) — Skill spec reference.
- `claude-code-api/README.md` — sub-project docs (independent lifecycle).
