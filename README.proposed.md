# persona (personas_maker)

> **Proposed rewrite aligning the existing `README.md` with the 5-doc template.**
> Do NOT auto-apply. Review diff vs. `README.md` and merge manually.
> Key differences from current README:
> - Adds explicit **Status**, **Documentation**, and **License** sections.
> - License changed from "Private repository" → **MIT** (per owner confirmation 2026-04-22).
> - Links to the new `PRD.md` / `Agent.md` / `CHANGELOG.md` / `DECISIONS.md`.
> - Preserves the existing personas table, architecture tree, setup, and pipeline docs verbatim.

Config-driven multi-persona Slack bot framework powered by Claude.
Define a persona in `bots/<name>/config.json`, and the shared runtime handles the rest — scheduling, LLM calls, Slack integration.

## Status

Active development — personal tooling, single-operator deployment.

## Currently included personas

| Persona | Type | Description |
|---------|------|-------------|
| **seungbin** | `persona` | SecondMe persona bot. RAG-based persona chat over KakaoTalk-derived context. |
| **reporter** | `reporter` | AI/semiconductor news digest bot. Searches, curates, and publishes an HTML briefing on schedule. |
| **coder** | `coder` | Dev session bot. Receives tasks via Slack, orchestrates implementation and code review. |
| **research** | `research_pipeline` | Research team bot. Paper scan → report → reviewer loop with Discourse/Confluence knowledge. |

## Live Briefings

Reporter가 생성한 뉴스 브리핑은 GitHub Pages로 호스팅됩니다:

**https://seungbinshin.github.io/personas_maker/**

## Quick Start

```bash
# Python runtime
python3 -m venv .venv
source .venv/bin/activate
pip install slack-bolt python-dotenv requests schedule

# claude-code-api (shared Claude Pro HTTP wrapper)
cd claude-code-api && pnpm install && cd ..

# Configure root + per-bot env
cp .env.example .env                                  # fill ANTHROPIC_API_KEY
cp bots/reporter/config.example.json bots/reporter/config.json

# Run
./persona.sh start reporter        # one bot
./persona.sh start all             # everything
./persona.sh status                # check
```

Full setup, Slack app scopes, and per-bot config fields: see `Agent.md` and the original README sections below.

## Architecture

```
persona/
├── persona.sh              # Bot lifecycle manager (start/stop/restart/status)
├── src/                    # Unified runtime (bot.py routes by persona_type)
├── prompts/                # Canonical prompt assets (source of truth)
├── skills/                 # Reusable business logic
├── tools/                  # Runtime/API facade (Claude, Slack, HTML, PDF)
├── adapters/               # Integration adapters
├── claude-code-api/        # Node/TS HTTP server wrapping Claude CLI
└── bots/{seungbin,coder,reporter,research}/
                            # Per-bot config, env, data, artifacts
```

Layer boundaries (enforced): `src/adapters → skills → prompts/tools → external`. See `Agent.md` and `persona.spec.md` §3.1.

## Documentation

- [PRD.md](./PRD.md) — What this is and why it exists.
- [Agent.md](./Agent.md) — Working guide for AI agents and tools.
- [CHANGELOG.md](./CHANGELOG.md) — Version history.
- [DECISIONS.md](./DECISIONS.md) — Architecture decision records.
- [persona.spec.md](./persona.spec.md) — Full design spec.
- [CLAUDE.md](./CLAUDE.md) — Claude Code control file (skills, browse guidance).

## License

MIT © 2026 seungbinshin

<!-- TODO: if MIT is correct, add a root-level LICENSE file. The current README says "Private repository"; owner confirmed MIT on 2026-04-22. -->
