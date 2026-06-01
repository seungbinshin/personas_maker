# Architecture Decisions: persona

> Lightweight ADRs. Append-only. To supersede a decision, write a new ADR
> with `Supersedes: ADR-NNN` instead of editing the old one.

---

## ADR-001: Adopt 5-doc standard

**Status**: Accepted — 2026-04-22

**Context**
Projects drift from their original intent when the only documentation is a README. AI agents onboarding a codebase lack a consistent place to find working rules (test commands, forbidden patterns, conventions). Solo development across many projects compounds this — context-switching cost grows without a canonical index per project.

`persona` is a parent/container directory with multiple sub-projects (`claude-code-api/`, per-bot instances under `bots/`) and enforced layer boundaries (`prompts/` → `skills/` → `tools/`, per `persona.spec.md` §3.1). Its existing docs (`README.md`, `persona.spec.md`, `CLAUDE.md`) cover pieces of the picture but don't give AI agents a single authoritative entry into product intent, working manual, time-ordered history, and decision rationale.

**Decision**
This project will maintain five documents at the root:
- `README.md` — human entry point
- `PRD.md` — product intent, users, success metrics
- `Agent.md` — AI/tool working manual
- `CHANGELOG.md` — time-ordered version history
- `DECISIONS.md` — this file

`CLAUDE.md` is a Claude Code control file and is left in place, unmodified; `Agent.md` references it under "Related Docs".

New major decisions are recorded here as append-only ADRs.

**Consequences**
- (+) Any agent can orient from `Agent.md` in under a minute.
- (+) Intent is persisted, not held only in the author's head.
- (+) Decisions and their reasoning survive codebase rewrites.
- (−) ~20–30 min upfront per project to populate meaningfully.
- (−) Requires discipline to keep `Agent.md` in sync as code evolves — especially across the root Python runtime and `claude-code-api/` sub-project.

---

<!--
Template for future ADRs:

## ADR-NNN: Title

**Status**: Proposed | Accepted | Superseded by ADR-XXX — YYYY-MM-DD

**Context**
What problem or choice triggered this? What constraints apply?

**Decision**
What we decided, in concrete terms.

**Consequences**
What becomes easier, harder, or locked in because of this.
-->
