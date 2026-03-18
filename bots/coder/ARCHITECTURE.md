# SW Agent Team — Implementation Architecture

Mapping `SW_agent_team_design_spec.md` to the existing persona infrastructure.

## System Constraints

| Resource | Current | Required |
|----------|---------|----------|
| `POOL_SIZE` (stateless workers) | 1 | **3-5** |
| `MAX_SESSIONS` (stateful) | 20 | 20 (OK) |
| Worker model | `claude-sonnet-4-6` | Sonnet for implementers, Opus for orchestrator/reviewer |
| `disallowedTools` | Write, Edit, NotebookEdit | **Remove for coder** — implementers need file write access |
| Git worktrees | Not used | `git worktree add` per task |

## Architecture Overview

```
User (Slack): "!team OAuth 로그인 추가해줘"
                    │
                    ▼
         ┌─── Orchestrator ───┐    (Python code + 1 LLM call for planning)
         │  1. Analyze request │
         │  2. Plan mode       │
         │  3. Split tasks     │
         │  4. Create worktrees│
         │  5. Assign agents   │
         └────────┬────────────┘
                  │ TASK_ASSIGN handoffs
        ┌─────────┼─────────┐
        ▼         ▼         ▼         ← parallel stateless LLM calls
   ┌─────────┐ ┌─────────┐ ┌─────────┐
   │ Cell A   │ │ Cell B   │ │ Cell C   │   Each in its own git worktree
   │ feature  │ │ feature  │ │ research │   Each gets scoped prompt
   │ backend  │ │ frontend │ │ spike    │   Each returns structured result
   └────┬─────┘ └────┬─────┘ └────┬─────┘
        │            │            │
        ▼            ▼            ▼
         └─────────┬─────────────┘
                   │ FIX_RESULT handoffs
                   ▼
         ┌─── Reviewer ──────┐    (1 LLM call — reads all diffs)
         │  Check integration │
         │  Flag blockers     │
         │  Approve / reject  │
         └────────┬───────────┘
                  │
           ┌──────┴──────┐
           ▼             ▼
     If APPROVED    If REJECTED
     Merge & PR     Re-assign cells
                   (retry ≤ 3)
                   │
                   ▼
         ┌─── Librarian ─────┐    (Python code + optional small LLM call)
         │  Update CLAUDE.md  │
         │  Update DECISIONS  │
         │  Promote skills    │
         └────────────────────┘
```

## Agent Roles → LLM Call Mapping

| Role | LLM? | Model | Tool Access | Runs In |
|------|------|-------|-------------|---------|
| **Orchestrator** | 1 call (planning) | Opus | Read, Glob, Grep, WebSearch | Main process |
| **Implementer** (×N) | 1 call each, parallel | Sonnet | **All tools** (Read, Write, Edit, Bash, Grep, Glob) | Git worktree |
| **Reviewer** | 1 call | Opus | Read, Grep, Glob, Bash (for `git diff`) | Main repo |
| **Librarian** | 0-1 call | Sonnet | Read, Write, Edit | Main repo |
| **Security Gate** | 0 calls | — | Python-level checks | Pre-execution filter |

**Total per task: 3-6 LLM calls** (1 plan + 2-3 implement + 1 review + 0-1 librarian)

## Worker Configuration

The current `worker.ts` has hardcoded `disallowedTools`. For the coder pipeline, implementer workers need full file access.

```
# Proposed: per-request tool permissions via request payload

RunRequest {
  prompt: string
  timeoutMs?: number
  sessionId?: string
  cwd?: string              ← NEW: worktree path
  allowFileWrite?: boolean   ← NEW: override disallowedTools
  model?: string             ← NEW: per-request model override
}
```

Alternatively, the Python orchestrator passes `cwd` and tool config, and worker.ts reads them:

```typescript
// worker.ts — per-request cwd support
const sessionOptions = {
  model: request.model || CLAUDE_MODEL,
  permissionMode: "bypassPermissions",
  disallowedTools: request.allowFileWrite ? [] : getDisallowedTools(),
  cwd: request.cwd || this.projectDir,
};
```

## Git Worktree Lifecycle

```python
# Orchestrator creates worktrees
def create_worktree(project_path: str, task_id: str, base_branch: str = "main") -> str:
    branch = f"agent/{task_id}"
    wt_path = f"{project_path}/.worktrees/{task_id}"
    subprocess.run(["git", "worktree", "add", "-b", branch, wt_path, base_branch],
                   cwd=project_path, check=True)
    return wt_path

# After implementer finishes
def collect_worktree_result(wt_path: str, task_id: str) -> dict:
    diff = subprocess.check_output(["git", "diff", "main...HEAD"], cwd=wt_path, text=True)
    files = subprocess.check_output(["git", "diff", "--name-only", "main...HEAD"],
                                     cwd=wt_path, text=True).strip().split("\n")
    return {
        "task_id": task_id,
        "diff": diff,
        "files_changed": files,
        "branch": f"agent/{task_id}",
    }

# After reviewer approves — merge into main
def merge_worktree(project_path: str, task_id: str):
    branch = f"agent/{task_id}"
    subprocess.run(["git", "merge", "--no-ff", branch], cwd=project_path, check=True)

# Cleanup
def cleanup_worktree(project_path: str, task_id: str):
    wt_path = f"{project_path}/.worktrees/{task_id}"
    subprocess.run(["git", "worktree", "remove", wt_path], cwd=project_path)
    subprocess.run(["git", "branch", "-d", f"agent/{task_id}"], cwd=project_path)
```

## Handoff Protocol

All inter-agent communication uses JSON files in `HANDOFF/`.

```
HANDOFF/
├── inbox/          ← pending handoffs
├── processed/      ← completed handoffs
└── archive/        ← historical record
```

### Message Types

```python
@dataclass
class Handoff:
    type: str           # TASK_ASSIGN | FIX_RESULT | REVIEW_REQUEST | REVIEW_RESULT | BLOCKER_REPORT
    from_agent: str     # orchestrator | implementer-A | reviewer
    to_agent: str
    task_id: str
    payload: dict       # type-specific content
    timestamp: str
```

### Example Flow

```json
// 1. Orchestrator → Implementer
{
  "type": "TASK_ASSIGN",
  "from_agent": "orchestrator",
  "to_agent": "implementer-backend",
  "task_id": "TASK-101",
  "payload": {
    "title": "Implement OAuth callback endpoint",
    "worktree": "/project/.worktrees/TASK-101",
    "scope": ["src/auth/", "src/routes/"],
    "interface_contract": "POST /auth/callback → {access_token, refresh_token}",
    "dependencies": [],
    "hints": "Use existing SessionStore for token persistence"
  }
}

// 2. Implementer → Reviewer
{
  "type": "FIX_RESULT",
  "from_agent": "implementer-backend",
  "to_agent": "reviewer",
  "task_id": "TASK-101",
  "payload": {
    "summary": "OAuth callback + token refresh implemented",
    "files_changed": ["src/auth/oauth.py", "src/routes/auth.py", "tests/test_oauth.py"],
    "diff_lines": 142,
    "tests": {"passed": 5, "added": 3},
    "risks": ["shared session store touched — needs integration test"],
    "branch": "agent/TASK-101"
  }
}

// 3. Reviewer → Orchestrator
{
  "type": "REVIEW_RESULT",
  "from_agent": "reviewer",
  "to_agent": "orchestrator",
  "task_id": "TASK-101",
  "payload": {
    "decision": "approved",
    "blockers": [],
    "suggestions": ["Consider rate limiting on callback endpoint"],
    "integration_risks": "none detected"
  }
}
```

## Orchestrator Planning Prompt

The orchestrator uses 1 LLM call with Opus to generate the tactical plan:

```python
ORCHESTRATOR_PLAN_PROMPT = """You are a software architect planning parallel work.

Project: {project_path}
Codebase summary: {codebase_summary}

Task: {user_request}

Analyze the task and produce a tactical plan.

Rules:
- Split into 2-4 independent subtasks that can run in parallel
- Define interface contracts between tasks so parallel work doesn't conflict
- Each task must have a clear scope (list of files/directories)
- Flag any tasks that MUST be sequential (dependency)

Return ONLY valid JSON:
{{
  "complexity": "simple/moderate/complex",
  "strategic_goal": "...",
  "tasks": [
    {{
      "task_id": "TASK-101",
      "title": "...",
      "agent": "implementer-backend",
      "scope": ["src/auth/"],
      "interface_contract": "...",
      "depends_on": [],
      "hints": "..."
    }}
  ],
  "sequential_constraints": ["TASK-103 must wait for TASK-101 and TASK-102"],
  "risks": ["..."]
}}
"""
```

## Task State Machine

```
PLANNED → ASSIGNED → IN_PROGRESS → COMPLETED → REVIEW → APPROVED → MERGED
                         │                         │
                         ▼                         ▼
                      BLOCKED                   REJECTED → RE_ASSIGNED (retry ≤ 3)
                         │
                         ▼
                   ESCALATED (after 3 retries)
```

State stored in `TASKS/active/{task_id}.json`.

## Memory Architecture

```
bots/coder/{project}/
├── CLAUDE.md           ← team-wide rules, coding conventions, pitfalls
├── DECISIONS.md        ← architecture decisions with rationale
├── TASK_LOG.md         ← completed task summaries
├── KNOWN_ISSUES.md     ← recurring problems
├── TASKS/
│   ├── backlog.json    ← queued tasks
│   ├── active/         ← in-progress task state files
│   └── done/           ← completed task records
├── HANDOFF/
│   ├── inbox/
│   ├── processed/
│   └── archive/
├── SKILLS/             ← reusable patterns (promoted from repeated work)
│   ├── review-ready/
│   ├── incident-fix/
│   └── regression-check/
└── METRICS/
    └── session_log.jsonl
```

## Pipeline Class Design

```python
# src/pipelines/coder_pipeline.py

class CoderPipeline(BasePipeline):
    """Multi-agent coding team with parallel worktree execution."""

    def run_task(self, project_path: str, user_request: str):
        """Full pipeline: plan → implement (parallel) → review → merge."""

        # 1. PLAN (1 Opus call)
        plan = self._orchestrator_plan(project_path, user_request)

        if plan["complexity"] == "simple":
            # Single implementer, no worktree needed
            return self._run_simple(project_path, user_request)

        # 2. CREATE WORKTREES
        worktrees = self._setup_worktrees(project_path, plan["tasks"])

        # 3. IMPLEMENT (parallel stateless calls)
        results = self._run_implementers_parallel(worktrees, plan["tasks"])

        # 4. REVIEW (1 Opus call — all diffs)
        review = self._run_reviewer(project_path, results)

        # 5. MERGE or RETRY
        if review["decision"] == "approved":
            self._merge_all(project_path, results)
            self._run_librarian(project_path, plan, results, review)
        else:
            self._handle_rejection(project_path, plan, results, review)

    def _run_implementers_parallel(self, worktrees, tasks):
        """Fire N parallel stateless LLM calls, one per worktree."""
        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor(max_workers=len(tasks)) as pool:
            futures = {}
            for task in tasks:
                wt_path = worktrees[task["task_id"]]
                prompt = self._build_implementer_prompt(task, wt_path)
                future = pool.submit(
                    self.runtime.run,
                    LLMRunRequest(prompt=prompt, cwd=wt_path)
                )
                futures[task["task_id"]] = future

            results = {}
            for task_id, future in futures.items():
                results[task_id] = future.result()
            return results
```

## Implementation Status

### Phase 1 (MVP) — ✅ Complete

| Feature | File | Status |
|---------|------|--------|
| Per-request `cwd`, `model`, `allowFileWrite` | `worker.ts`, `types.ts` | ✅ |
| `POOL_SIZE=3` | `claude-code-api/.env` | ✅ |
| `CoderPipeline` | `src/pipelines/coder_pipeline.py` | ✅ |
| Orchestrator prompt (with governance) | `prompts/orchestrator.py` | ✅ |
| Implementer prompt (with governance) | `prompts/implementer.py` | ✅ |
| Reviewer prompt | `prompts/code_reviewer.py` | ✅ |
| `!team` Slack commands | `src/bot.py` | ✅ |
| Dual-channel Slack (command + team) | `coder_pipeline.py` | ✅ |
| Dependency-aware execution | `_run_implementers_with_deps()` | ✅ |
| Auto-retry on rejection (max 3) | `run_task()` retry loop | ✅ |
| Blocker-blocks-merge policy | `review_config` enforcement | ✅ |
| `max_tasks` cap from config | Passed to orchestrator prompt | ✅ |
| Session summary → TASK_LOG.md | `_write_session_summary()` | ✅ |
| Governance rules in prompts | Orchestrator + implementer | ✅ |

### Phase 2 (Memory + Skills) — Planned

| Feature | Description |
|---------|-------------|
| Librarian agent | Post-session CLAUDE.md, DECISIONS.md updates |
| Skill promotion | Detect repeated patterns → SKILLS/ |
| `/retro` command | Post-mortem analysis |
| KNOWN_ISSUES.md | Auto-track recurring problems |

### Phase 3 (Analytics + Advanced) — Planned

| Feature | Description |
|---------|-------------|
| Security gate | Pre-execution policy checks for dangerous ops |
| Metrics tracking | session_log.jsonl, reopen rate, skill reuse |
| Escalation policy | Auto-escalate to user after 3 retries |

## Slack Commands

```
!dev start <project>      — Set project context (required first)
!dev stop                 — End session
!dev status               — Session info
!team run <request>       — Full pipeline: plan → implement → review → merge
!team plan <request>      — Plan only (preview, no execution)
!team status              — Show active worktrees
```

## Communication Channels

```
#coder-command            — User ↔ Orchestrator (summaries only)
#coder-team               — All agents internal chat (full detail)
```

## Token Budget Estimate

| Stage | Model | Est. Input | Est. Output | Cost* |
|-------|-------|-----------|-------------|-------|
| Plan | Opus | ~5K | ~1K | $0.09 |
| Implement ×3 | Sonnet | ~8K each | ~3K each | $0.10 |
| Review | Opus | ~10K (diffs) | ~1K | $0.17 |
| Retry (if needed) | Sonnet+Opus | ~same | ~same | +$0.27 |
| **Total** | | | | **~$0.36–0.63/task** |

*Based on Opus $15/M input, Sonnet $3/M input pricing.
