# personas_maker

Config-driven multi-persona Slack bot framework powered by Claude.
Define a persona in `bots/<name>/config.json`, and the shared runtime handles the rest — scheduling, LLM calls, Slack integration.

## Currently included personas

| Persona | Type | Description |
|---------|------|-------------|
| **reporter** | `reporter` | AI/semiconductor news digest bot. Searches, curates, and publishes an HTML briefing on schedule. |
| **coder** | `coder` | Dev session bot. Receives tasks via Slack, orchestrates implementation and code review. |

> Other persona types (`persona`, `research_pipeline`) are supported by the framework but their configs are not included in this repo.

## Live Briefings

Reporter가 생성한 뉴스 브리핑은 GitHub Pages로 호스팅됩니다:

**https://seungbinshin.github.io/personas_maker/**

## Architecture

```
personas_maker/
├── persona.sh              # Bot lifecycle manager (start/stop/restart/status)
├── src/
│   ├── bot.py              # Main Slack bot entry point (all persona types)
│   ├── scheduler.py        # Background task scheduling
│   └── pipelines/
│       ├── base.py          # Shared pipeline infrastructure
│       ├── reporter_pipeline.py   # News gather → format → publish
│       └── coder_pipeline.py      # Task orchestration for dev sessions
├── prompts/                # All prompt templates (source of truth)
├── skills/                 # Reusable business logic
├── tools/                  # Runtime wrappers (Claude API, Slack, HTML gen)
├── adapters/               # Integration adapters
├── claude-code-api/        # HTTP server wrapping Claude CLI for LLM inference
└── bots/
    ├── reporter/
    │   ├── config.example.json
    │   └── digests/         # Generated HTML briefings + JSON archives
    └── coder/
        ├── config.example.json
        ├── ARCHITECTURE.md
        └── SW_agent_team_design_spec.md
```

## Setup

### Prerequisites

- Python 3.12+
- Node.js 20+ / pnpm
- Slack app with Socket Mode enabled
- Anthropic API key (Claude Pro subscription for claude-code-api)

### 1. Clone and install

```bash
git clone https://github.com/seungbinshin/personas_maker.git
cd personas_maker

# Python
python3 -m venv .venv
source .venv/bin/activate
pip install slack-bolt python-dotenv requests schedule

# claude-code-api
cd claude-code-api
pnpm install
cd ..
```

### 2. Configure credentials

```bash
# Root .env (for claude-code-api)
cp .env.example .env
# Fill in ANTHROPIC_API_KEY

# Bot-specific config & credentials
cp bots/reporter/config.example.json bots/reporter/config.json
cp bots/reporter/config.example.json bots/reporter/.env  # Use .env.example format
```

Each bot needs:
- `config.json` — persona type, schedule, channels, search queries
- `.env` — Slack tokens (`SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`), API port, model

See `.env.example` and `config.example.json` for the full structure.

### 3. Slack app setup

Create a Slack app with:
- **Socket Mode** enabled
- **Bot Token Scopes**: `chat:write`, `files:write`, `app_mentions:read`, `channels:history`, `im:history`
- **Event Subscriptions**: `message.channels`, `message.im`, `app_mention`

### 4. Run

```bash
# Start a specific bot
./persona.sh start reporter

# Start all configured bots
./persona.sh start all

# Other commands
./persona.sh stop reporter
./persona.sh restart reporter
./persona.sh status
./persona.sh list
```

## Adding a new persona

1. Create `bots/<name>/config.json`:

```json
{
  "name": "my-bot",
  "display_name": "My Bot",
  "persona_type": "reporter",
  "default_model": "claude-sonnet-4-6",
  "api_keys": "my-bot:YOUR_API_KEY",
  "schedule": { ... },
  "reporter": { ... }
}
```

2. Create `bots/<name>/.env` with Slack tokens and API port.
3. Run `./persona.sh start my-bot`.

The `persona_type` field determines which pipeline handles the bot:
- `reporter` → `ReporterPipeline` (scheduled news digests)
- `coder` → `CoderPipeline` (interactive dev sessions)

## Reporter pipeline

The reporter bot runs on a configurable schedule and:

1. **Gathers** news via WebSearch (multiple keyword groups)
2. **Filters** articles to the last 48 hours only (hard cutoff in code)
3. **Deduplicates** against the last 7 days of published digests
4. **Formats** results as a styled HTML newspaper
5. **Publishes** the HTML to Slack and archives it locally

Generated briefings are saved to `bots/reporter/digests/` as both JSON (for dedup) and HTML (for viewing).

## License

Private repository. Contact the owner for access.
