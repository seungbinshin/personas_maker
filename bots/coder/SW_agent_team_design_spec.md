Software Agent Team Design Spec
Parallel Worktrees + Plan-First Execution + Tiered Memory + Explicit Handoffs
0. Purpose

This document defines the operating model for a software development agent team.

The goal is not to create one super-agent that does everything.
The goal is to create a persistent multi-agent development organization that can:

work across multiple git worktrees in parallel

start every complex task in plan mode

keep implementation contexts clean and isolated

prevent the same mistakes from recurring

convert repeated work into reusable skills

communicate through explicit structured handoffs

accumulate memory and experience across sessions

scale from simple bug fixes to multi-threaded long-horizon engineering work

This design incorporates the following principles:

Parallel execution across 3–5 git worktrees

Plan-first execution for every complex task

Continuous memory updates through CLAUDE.md and related files

Reusable project-agnostic skills committed to personal git

Slack-style agent-to-agent communication

Subagent offloading to keep the main context window clean

Optional analytics and data-driven evaluation

CORPGEN-inspired architecture for multi-horizon task environments

1. Core Design Philosophy
1.1 The team is a persistent organization, not a temporary prompt chain

Each agent is treated as a persistent digital employee with:

a stable role

a clear area of responsibility

explicit input/output contracts

memory of past mistakes and decisions

a communication protocol with other team members

The team must behave more like a real engineering org than a single interactive assistant.

1.2 Parallelism is useful only when context contamination is controlled

Running 3–5 worktrees in parallel is powerful, but only if each execution cell is isolated.

Each worktree should represent an independent execution context such as:

feature implementation

bug fix

technical spike

review and validation

refactoring or tech debt removal

No agent should dump its full internal session history into another agent’s context.
Agents should exchange only structured artifacts and summaries.

1.3 Planning is not optional overhead

Every complex task must start in plan mode.

The purpose of planning is to compress ambiguity early so that implementation can be as close to one-shot as possible.

The team should use a 3-layer planning model:

Strategic plan: overall goal, constraints, acceptance criteria

Tactical plan: decomposed sub-tasks, dependencies, ordering

Operational action: the immediate next step for one agent in one worktree

This directly reflects CORPGEN’s hierarchical planning approach for multi-horizon task alignment.

1.4 Memory must be layered

A single giant memory file is not enough.

The system should use three kinds of memory:

Working Memory

what is currently active

current issue

current logs

current files being touched

Structured Long-Term Memory

design decisions

task summaries

architecture constraints

known pitfalls

review conclusions

Semantic / Reusable Memory

reusable skills

successful past fix patterns

playbooks

examples and templates

similar prior incidents

This mirrors CORPGEN’s working memory, structured memory, and semantic memory split.

1.5 Repetition must turn into infrastructure

If the team does something more than once a day, it should become one of:

a skill

a slash command

a playbook

a lint/review rule

a reusable code template

an automation step

The objective is not just task completion.
The objective is to reduce future cognitive load.

2. What Problems This Team Is Designed to Solve

This architecture is designed for environments where agents must handle:

multiple simultaneous tasks

interleaved priorities

cross-task dependencies

repeated bug-fixing and review loops

architectural decisions that persist across sessions

context window pressure

recurring mistakes and duplicated work

These are exactly the kinds of issues CORPGEN identifies in multi-horizon task environments: context saturation, cross-task memory interference, dependency complexity, and the cost of repeated reprioritization.

3. Team Topology
3.1 Chief Orchestrator

The Orchestrator is the central coordination role.

Responsibilities:

receive incoming tasks

decide whether a task is simple or complex

force complex tasks into plan mode

build the strategic and tactical plan

create and maintain the dependency graph

assign work to worktree-specific agents

monitor blockers

trigger re-planning when needed

decide when to skip, escalate, or reprioritize

The Orchestrator should not carry heavy implementation detail unless necessary.
Its primary role is coordination and continuity.

3.2 Implementer Cells

Each implementer cell runs in its own git worktree and session.

Recommended cells:

implementer-feature

implementer-bugfix

implementer-refactor

implementer-data

implementer-experimental

Responsibilities:

execute one localized task

modify code only within the task scope

keep context local and minimal

return structured outputs only

Expected outputs:

patch summary

files changed

tests run

risks introduced

open questions

Implementers should not silently change architecture.
If architectural scope expands, they must escalate.

3.3 Reviewer Cell

Responsibilities:

inspect diffs

classify issues into blockers vs non-blocking suggestions

detect regression risk

compare implementation against plan and decision logs

request fixes through explicit messages

Inputs should be restricted to:

plan summary

diff

test result

decision log

issue context

The reviewer should not need the full raw implementation session.

3.4 Research / Spike Cell

Responsibilities:

investigate unknowns

test libraries, APIs, frameworks, toolchains

isolate experimental or uncertain work

return a concise research brief

This cell is especially useful when uncertainty is high and the team wants to protect the main execution contexts from contamination.

3.5 Memory / Skill Librarian

Responsibilities:

update CLAUDE.md

update DECISIONS.md

update PLAYBOOKS.md

promote repeated patterns into reusable skills

log recurring pitfalls

record successful solution trajectories

maintain slash commands and automation routines

This role is critical.
Without it, the team will complete tasks but fail to improve.

3.6 Permission / Security Gate

Responsibilities:

evaluate commands that modify sensitive or dangerous areas

inspect production-impacting actions

approve or reject destructive operations

route security-sensitive permission checks to a stronger review layer

This role should sit outside normal implementation flow and act as a policy gate.

4. Worktree Model

Each worktree is an isolated execution cell.

Example mapping:

wt-feature-auth → new feature implementation

wt-bugfix-cache → bug reproduction and patching

wt-research-observability → logging/tooling investigation

wt-review-mainline → review and validation

wt-techdebt-cleanup → duplicate code removal

Rules:

One worktree = one main task focus

One worktree = one primary session context

Full conversation history does not move between worktrees

Only structured summaries and artifacts may move between cells

When a worktree finishes, its lessons must be promoted into memory

This gives parallelism without uncontrolled context expansion.

5. Planning System
5.1 Strategic Planning

The strategic plan must define:

problem statement

desired outcome

constraints

dependencies

success criteria

major risks

whether the task needs multiple worktrees

Example structure:

## Strategic Plan
- Goal:
- Why it matters:
- Constraints:
- Acceptance criteria:
- Risks:
- Dependencies:
- Suggested execution cells:
5.2 Tactical Planning

The tactical plan must decompose the task into smaller units.

Each unit should define:

task id

owner agent

worktree

dependencies

expected output

escalation trigger

Example:

## Tactical Plan
1. TASK-101: reproduce login timeout issue
2. TASK-102: inspect retry middleware
3. TASK-103: patch token refresh path
4. TASK-104: add regression tests
5. TASK-105: reviewer validation
6. TASK-106: update decision/memory docs
5.3 Operational Action

Each agent should always know the single next action it is responsible for.

Example:

## Current Next Action
Agent: implementer-bugfix
Worktree: wt-bugfix-auth
Action: reproduce retry timeout with integration test
Expected artifact: failing test and root-cause note

This minimizes indecision and keeps context focused.

6. Dependency Management

Tasks should not be represented as a flat checklist.

Use a dependency graph with fields such as:

depends_on

unblocks

waiting_for

priority

state

Example:

tasks:
  - id: TASK-101
    title: Reproduce login timeout
    owner: implementer-bugfix
    worktree: wt-bugfix-auth
    state: in_progress
    priority: high
    depends_on: []
    unblocks: [TASK-102, TASK-103]

  - id: TASK-102
    title: Inspect retry middleware
    owner: researcher-runtime
    worktree: wt-research-runtime
    state: pending
    priority: high
    depends_on: [TASK-101]
    unblocks: [TASK-103]

  - id: TASK-103
    title: Patch token refresh path
    owner: implementer-bugfix
    worktree: wt-bugfix-auth
    state: blocked
    priority: high
    depends_on: [TASK-101, TASK-102]
    unblocks: [TASK-104]

The Orchestrator is responsible for keeping this graph current.

7. Memory Architecture
7.1 Working Memory

Working Memory contains short-lived execution state.

Examples:

active issue description

current failing test

current logs

files currently being edited

open blockers

This memory should remain small and frequently refreshed.

7.2 Structured Long-Term Memory

This should live in durable markdown or yaml artifacts.

Recommended files:

CLAUDE.md

DECISIONS.md

TASK_LOG.md

KNOWN_ISSUES.md

CLAUDE.md

Use for team-wide rules and recurring lessons.

Contents may include:

coding rules

repo conventions

testing expectations

common mistakes

anti-patterns

workflow reminders

DECISIONS.md

Use for architecture and design decisions.

Each entry should include:

date

decision

reason

alternatives considered

consequences

reversal cost

TASK_LOG.md

Use for session summaries.

Each task entry should contain:

task id

what changed

what blocked

what was learned

next suggested action

7.3 Semantic / Reusable Memory

Recommended location:

SKILLS/

PLAYBOOKS/

TEMPLATES/

EXAMPLES/

This layer stores reusable patterns such as:

“how to debug CI failures”

“how to add regression tests”

“how to review auth changes”

“how to perform tech debt cleanup”

“how to build dbt models and validate them”

This is where repeated effort becomes leverage.

8. Communication Model

The team should use explicit Slack-style messages between agents.

No hidden assumptions.
No raw transcript forwarding.
No shared mental state.

8.1 Message Types

Recommended message types:

PLAN_REQUEST

TASK_ASSIGN

RESEARCH_REQUEST

FIX_REQUEST

FIX_RESULT

REVIEW_REQUEST

REVIEW_RESULT

BLOCKER_REPORT

DECISION_PROPOSAL

DECISION_RESULT

SKILL_PROMOTION

SESSION_SUMMARY

8.2 Example Message Formats
Fix Request
[FIX_REQUEST]
from: reviewer
to: implementer-bugfix-auth
task_id: TASK-103
issue: login timeout on retry path
context: integration test fails when retry occurs after token refresh
expected_behavior: request should retry successfully after token refresh
priority: high
references:
  - TASK-101
  - TASK-102
Fix Result
[FIX_RESULT]
from: implementer-bugfix-auth
to: reviewer
task_id: TASK-103
summary: patched retry backoff condition after token refresh
files_changed:
  - auth/retry.py
  - tests/test_retry_refresh.py
tests:
  - passed: 5
  - added: 1
risk: medium
notes: shared auth util touched; recommend focused regression review
Blocker Report
[BLOCKER_REPORT]
from: implementer-feature-payments
to: orchestrator
task_id: TASK-204
blocker: payment SDK behavior differs between dev and staging
attempts: 3
current_hypothesis: environment-specific retry policy mismatch
recommended_next_step: assign research cell to inspect SDK wrapper behavior
Skill Promotion
[SKILL_PROMOTION]
from: memory-librarian
to: team
candidate: review-ready
reason: repeated pre-merge checklist performed 4 times this week
proposed_scope:
  - verify tests
  - verify docs
  - verify migration safety
  - verify rollback path
destination: SKILLS/review-ready/
9. Subagent Policy

Subagents should be used to throw more compute at the problem without polluting the main context.

Use subagents when:

the task requires exploration

the task needs a temporary specialist

the task has a high chance of failure and retry

the task is computationally or cognitively expensive

the main agent must remain focused on orchestration

Examples:

library investigation

performance profiling

schema migration validation

test flakiness diagnosis

incident root-cause exploration

Subagent rules:

The parent agent defines a tight objective

The subagent works in isolated context

The subagent returns only structured output

The parent agent decides whether to integrate the result

The subagent does not rewrite team-wide memory directly

10. Retry, Skip, and Escalation Policy

Agents should not loop indefinitely.

Recommended policy:

retry up to 3 meaningful attempts

if still blocked, emit BLOCKER_REPORT

Orchestrator then chooses:

reassign

open a research task

lower priority temporarily

escalate to architecture/security gate

skip and continue with other unblocked tasks

This prevents the entire team from stalling on one stubborn problem.

11. CLAUDE.md and Related Files
11.1 CLAUDE.md

Purpose: shared operating rules and accumulated corrections

Suggested sections:

# CLAUDE.md

## Project-wide rules
## Coding conventions
## Testing requirements
## Common pitfalls
## Things we no longer do
## Review checklist reminders
## Escalation triggers
## Workflow rules

Examples of content:

always start complex tasks in plan mode

never silently change architecture in implementer cells

always add or update tests for production bug fixes unless explicitly impossible

summarize every session before closing

after every correction, update memory files

repeated manual work should be proposed as a skill

11.2 DECISIONS.md

Purpose: durable design memory

Suggested entry format:

## 2026-03-09 — Token refresh retry logic handled in shared auth util
- Context:
- Decision:
- Rationale:
- Alternatives considered:
- Consequences:
- Related tasks:
11.3 PLAYBOOKS.md

Purpose: procedural reuse

Suggested sections:

Bugfix playbook

Review playbook

Incident response playbook

CI failure playbook

Tech debt playbook

Refactor playbook

dbt/data engineering playbook

12. Skill System

All repeated work should be progressively promoted into reusable assets.

Recommended directories:

SKILLS/
  plan/
  techdebt/
  review-ready/
  incident-fix/
  regression-check/
  dbt-agent/
  migration-safety/
12.1 Promotion Rules

Promote to skill when one of these happens:

repeated more than once a day

repeated across multiple projects

repeated across multiple agents

repeated with minimal variation

repeated and error-prone

12.2 Example Skills
/plan

Converts a task into:

strategic goal

tactical breakdown

dependency graph

execution cell assignment

risk list

/techdebt

Runs at session end to detect:

duplicate code

stale files

dead helpers

fragile utilities

repeated workaround patterns

/review-ready

Checks:

tests updated

docs updated

migration safety addressed

rollback considered

risk notes written

/retro

Captures:

mistakes

fixes

lessons

skill candidates

memory updates needed

/promote-to-skill

Turns a repeated workflow into:

command spec

playbook

template

reusable skill package

13. Session Lifecycle
13.1 Start of Session

Receive the task

Determine whether the task is simple or complex

If complex, force plan mode

Build strategic plan

Build tactical breakdown

Update dependency graph

Assign worktrees and owners

Open any required research/review cells

13.2 Mid-Session Execution

Implementer cells work independently

Research cells investigate unknowns

Reviewer inspects submitted artifacts

Orchestrator monitors dependency state

Blockers trigger explicit reports

Replanning occurs only when needed

13.3 End of Session

Review all completed tasks

Run review-ready checks

Run tech debt scan

Update CLAUDE.md

Update DECISIONS.md

Update playbooks and skills

Store a concise session summary

Define the next suggested action for next session

14. Suggested Repository Layout
agent-team/
├─ CLAUDE.md
├─ DECISIONS.md
├─ PLAYBOOKS.md
├─ TASK_LOG.md
├─ KNOWN_ISSUES.md
├─ TASKS/
│  ├─ backlog.yaml
│  ├─ active/
│  ├─ blocked/
│  └─ done/
├─ SKILLS/
│  ├─ plan/
│  ├─ techdebt/
│  ├─ review-ready/
│  ├─ incident-fix/
│  └─ dbt-agent/
├─ HANDOFF/
│  ├─ inbox/
│  ├─ processed/
│  └─ archive/
├─ METRICS/
│  ├─ session_log.jsonl
│  ├─ reopen_rate.json
│  ├─ skill_reuse.json
│  └─ blockage_stats.json
└─ WORKTREES/
   ├─ wt-feature-auth/
   ├─ wt-bugfix-cache/
   ├─ wt-research-runtime/
   ├─ wt-review-mainline/
   └─ wt-techdebt-cleanup/
15. Metrics and Analytics

Analytics are optional in theory, but highly recommended in practice.

Track:

completion rate

reopen rate

number of retries before success

blocker frequency

worktree context size

handoff failure rate

skill reuse count

duplicated work incidents

tech debt findings per session

mean time to recover from bug reports

The purpose of analytics is not vanity.
It is to understand whether the team is getting better over time.

16. Governance Rules
Hard Rules

Every complex task starts in plan mode

No uncontrolled raw transcript sharing across agents

Implementers do not silently change architecture

Every important correction must update long-term memory

Repeated work must be proposed for skill promotion

Repeated failure must trigger escalation

Reviews must separate blockers from suggestions

High-risk operations must pass through permission/security gate

Soft Rules

Prefer isolated experimentation over polluting the main path

Prefer concise structured handoffs over conversational handoffs

Prefer reusable patterns over heroic improvisation

Prefer durable memory over repeating the same discovery

17. Operating Principles Inspired by CORPGEN

This team design intentionally adopts the following ideas:

Hierarchical Planning

Use multi-level planning so the team is not forced to recompute everything at every step.

Sub-Agent Isolation

Offload tasks to isolated execution contexts to prevent cross-task contamination.

Tiered Memory

Separate immediate execution state from durable structured memory and reusable semantic memory.

Adaptive Summarization

Keep only what matters:

what changed

what failed

what was decided

what should happen next

Persistent Roles

Treat agents as stable organizational actors with continuity across sessions.

Experience Reuse

The team should accumulate successful trajectories and reuse them in future work.

18. Final Design Summary

This system should be understood as:

A plan-first, multi-worktree, persistent software agent organization with explicit handoffs, layered memory, reusable skills, isolated subagents, and continuous learning.

In simpler terms:

parallelize aggressively

isolate contexts strictly

plan before implementing

remember corrections permanently

convert repetition into reusable skills

communicate through explicit message schemas

escalate instead of looping forever

treat the team as a long-lived engineering organization

This is the intended final model.

19. Immediate Implementation Checklist
- [ ] Create 3–5 worktree operating model
- [ ] Define orchestrator role
- [ ] Define implementer/reviewer/research/memory-librarian roles
- [ ] Add plan-first policy to CLAUDE.md
- [ ] Create DECISIONS.md
- [ ] Create PLAYBOOKS.md
- [ ] Create SKILLS/ directory
- [ ] Define handoff message schema
- [ ] Define retry/escalation rules
- [ ] Create dependency graph format
- [ ] Add /techdebt command
- [ ] Add /plan command
- [ ] Add /retro command
- [ ] Add review-ready checklist
- [ ] Start tracking skill reuse and blocker frequency
20. One-Sentence Directive

Build the agent team as a persistent engineering organization, not a prompt chain: plan first, execute in isolated parallel worktrees, communicate through structured handoffs, store durable lessons, and turn repetition into reusable skill infrastructure.