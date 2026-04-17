# Discourse Engagement v2 — Design Spec

**Date**: 2026-04-17
**Author**: seungbinshin (via brainstorming)
**Status**: Approved, ready for implementation plan

## 1. Motivation

The current `DiscourseEngagement` pipeline (shipped 2026-04-15) auto-replies to comments on published research reports. After first real traffic, two structural issues surfaced:

- **False-positive rejects on internal terms.** On topic 291 (TurboQuant), fact-checker rejected a draft for referencing "HyperDex" — a real internal compiler name that appears 327 times across 75 files in the repo. The fact-checker only sees a pre-computed `internal_context` built from *comment* keywords, so any internal term introduced by the *draft* is unverifiable and gets flagged as hallucination.
- **Pipeline is write-only.** When a comment identifies a legitimate issue in the published report (e.g., LaTeX rendering bug on post #1 of topic 291), the bot can only reply — it cannot fix the underlying post. Users get an acknowledgment but the broken content stays.

This spec also introduces two knowledge-retention features so that each engagement strengthens the bot's future context: an auto-grown glossary of internal terms, and a Q&A archive of published answers.

## 2. Scope

In scope:
- **A.** Draft-aware internal context (re-gather after draft).
- **C.** FACT_CHECK_PROMPT rewrite for internal-term leniency.
- **Glossary.** Auto-maintained `knowledge/glossary.md` of vault-verified internal terms, injected into fact-check context.
- **Q&A archive.** On successful publish, save Q+A+sources to `knowledge/topics/qa/`.
- **Post editing (B).** Bot may edit its own published post when a comment identifies a format error or a clear factual error. Discourse `PUT /posts/:id`.

Out of scope:
- External-source caching (rejected — redundant with runtime WebSearch, staleness risk).
- Editing posts authored by humans (safety-gated against).
- Slack approval gate for edits on first rollout (can be added later if needed).
- Changes to publish flow (topic creation), discovery pipeline, or research pipeline itself.

## 3. Architecture

### 3.1 Flow overview

```
poll → fetch_posts → classify → gather(comment kw)
      → draft
      → extract_draft_terms → gather(draft kw) → merge_context
      → [if correction: classify_edit]
      → [if edit_needed: generate_edit → fact_check_edit → apply_edit(or fallback)]
      → reply_draft → fact_check_reply → publish_reply
      → [if approved: archive_qa]
      → refresh_glossary (background, once per poll)
```

### 3.2 New modules

| File | Responsibility |
|---|---|
| `src/glossary.py` | Grep vault for internal-term candidates; upsert to `knowledge/glossary.md`; expose top-N terms for prompt injection. |
| `src/qa_archive.py` | On approved publish, write Q+A markdown snapshot under `knowledge/topics/qa/`. |
| `src/post_editor.py` | Safety-gated edit of bot's own posts via `DiscourseClient.edit_post`. Owns backup + audit trail. |

### 3.3 Modified modules

- `src/discourse_engagement.py` — orchestrates the expanded flow, holds references to the three new modules.
- `src/discourse_client.py` — adds `edit_post(post_id, raw, edit_reason)`.
- `prompts/discourse_engagement.py` — adds `EXTRACT_DRAFT_TERMS_PROMPT`, `CLASSIFY_EDIT_PROMPT`, `GENERATE_EDIT_PROMPT`, `EDIT_FACT_CHECK_PROMPT`; rewrites `FACT_CHECK_PROMPT` with internal-term leniency and adds `{glossary}` slot.

## 4. Part A — Draft-aware context

**Problem.** `internal_context` is computed once from comment keywords, so draft-introduced internal terms (like "HyperDex") are unverifiable.

**Change.** After draft, run one extra LLM call to extract 8–12 verification-worthy terms from the draft (`EXTRACT_DRAFT_TERMS_PROMPT`). Re-run `_gather_internal_context(comment_kw + draft_terms)` and merge.

**Merge rules.** De-duplicate page matches by `path`. Enforce ≤30 KB total; if exceeded, keep highest-score pages first.

**Prompt.** Returns JSON array of strings. Instructed to prefer proper nouns, technical acronyms, and Korean compound technical terms; exclude generic English words.

## 5. Part C — FACT_CHECK_PROMPT rewrite

Add the following principles to the existing checklist:

- Internal proper nouns (HyperDex, LPU, SMA, MPU, VPU, LMU, ESL, BERTHA, etc.) that are absent from `internal_context` must NOT be declared fabricated. If suspicion remains, decision = `revise` with guidance "cite evidence for this term", never `reject`.
- External URLs are treated as *unverified* rather than fabricated. If questionable, `revise` with guidance "replace with verifiable source (arXiv ID preferred)".
- Conference-acceptance claims ("ICLR accepted") that cannot be confirmed are `revise` with guidance "soften to arXiv preprint language".

Adds a `{glossary}` slot to the prompt, filled with the top-N terms from `glossary.md` BEGIN-AUTO section (rough cap: top-50 or ~5 KB).

## 6. Glossary

### 6.1 File

`bots/research/knowledge/glossary.md`

```markdown
# HyperAccel 내부 용어 glossary

<!-- auto-maintained by discourse_engagement — do not edit between markers -->
<!-- BEGIN-AUTO -->
## HyperDex
- **Occurrences**: 327 (reports: 280, knowledge: 45, context: 2)
- **First seen**: 2026-03-01
- **Sample context**: "HyperDex 컴파일러의 정적 메모리 매핑 전략..." (reports/001)
<!-- END-AUTO -->

<!-- BEGIN-MANUAL -->
<!-- 사람이 수기로 유지 -->
<!-- END-MANUAL -->
```

### 6.2 Auto-upsert logic (`src/glossary.py`)

```
refresh_glossary(bot_dir, candidate_terms):
  for term in candidate_terms:
    count, breakdown, sample = grep_vault(bot_dir, term)
    if passes_filters(term, count):
      upsert_auto_block(glossary_path, term, count, breakdown, sample)
```

**Candidates.** `DiscourseEngagement.poll_and_respond` accumulates into a set across all posts processed in the cycle: each post contributes `extract_draft_terms` output plus classifier's `key_topic` tokens. At the end of the poll loop, this set is passed to `refresh_glossary`.

**Filters.**
- `count >= 3` (term must appear in vault at least 3 times).
- `count >= 10` if `len(term) <= 2` (short acronym noise control).
- Exclude stopword list: generic English articles/prepositions (`The`, `A`, `And`, etc.) and common markdown tokens. Domain-generic terms that are nonetheless meaningful (`LLM`, `GPU`, `API`) are allowed through — the occurrence threshold is enough to keep them useful when they do show up in context. Stopwords stored in `src/glossary.py` constant, tunable.
- Exclude terms inside the BEGIN-MANUAL block (manual entries take precedence; auto upsert must skip).

**Grep scope.** `bots/research/context/`, `bots/research/knowledge/`, `bots/research/reports/**/researcher/*.md`. Use `rg --count` or equivalent.

**Timing.** Once per poll, at the end of `poll_and_respond`, after all posts processed. Background-safe (a failure logs and returns, never breaks the reply flow).

**Seed script.** `python -m src.glossary seed` does a one-shot pass over the vault, extracts high-frequency capitalized/proper nouns, runs the same filters, populates BEGIN-AUTO. Run once at rollout.

### 6.3 Injection into fact-check

`DiscourseEngagement._fact_check` reads BEGIN-AUTO section (cap ~5 KB or top 50 entries), passes as `{glossary}` template variable.

## 7. Q&A archive

### 7.1 File layout

`bots/research/knowledge/topics/qa/YYYY-MM-DD-<topic-slug>-post<N>.md`

Example: `2026-04-17-turboquant-kv-cache-compression-post2.md`

### 7.2 Trigger

Inside `_respond_to_comment`, immediately after `client.create_reply` succeeds AND fact-check decision was `approve`. Reject / revise-exhausted / post failures are NOT archived.

### 7.3 Format

```markdown
---
source_topic_id: 291
source_topic_url: https://hyperaccel.discourse.group/t/.../291
source_post_number: 2
report_id: 010_turboquant-kv-cache-compression
commenter: jaewon_lim
comment_type: correction
published_at: 2026-04-17T12:41:42
---

# Q: {report_title에서 요약된 질문/지적}

## 원본 댓글
> {원문 인용}

## 답변 요약
{reply 본문 전체 — 최대 1500자, 초과 시 앞부분 보존}

## 참고 자료
- {fact-check 통과 URL들}

## 관련 리포트
- [{report_title}]({relative_path_to_report_final})
```

### 7.4 Downstream use

Existing `_gather_internal_context` already recurses `bots/research/knowledge/topics/`, so new QA files become searchable with no additional wiring. Future comments on similar topics will surface prior answers as part of `internal_context`.

## 8. Post editing (Part B)

### 8.1 Scope

Triggered only when `classify_comment` returns `correction`. Supports two edit types:

- `format` — LaTeX rendering, markdown syntax, broken links, typos. No semantic change.
- `factual` — numeric errors, wrong citations, bad dates. Only if fact-check is high-confidence. Interpretive/opinion disputes are NOT edits — those stay reply-only.

### 8.2 Sub-flow

```
# inside _respond_to_comment, only if comment_type == "correction":
edit_decision = _classify_edit(report_md, comment_text)
if edit_decision.edit_needed:
    new_raw = _generate_edit(report_md, edit_decision)
    fc = _fact_check_edit(diff=(report_md, new_raw), decision=edit_decision)
    if fc.decision == "approve":
        post_editor.apply_edit(
            post_id = metadata["discourse_post_id"],
            new_raw = new_raw,
            edit_reason = f"댓글 #{post.post_number} 지적 반영: {edit_decision.change_summary}",
        )
        edit_applied = True
    else:
        edit_applied = False  # fallback to reply-only
```

### 8.3 Reply coupling

Reply prompt gets a new slot `{edit_outcome}`:

- If edit applied: "본문을 수정했습니다 (변경: {change_summary}). 지적 감사합니다. 추가로..."
- If edit not applied / not needed: existing behavior.

### 8.4 `DiscourseClient.edit_post`

```python
def edit_post(self, post_id: int, raw: str, edit_reason: str) -> dict:
    # PUT /posts/{post_id}.json
    # body: {"post": {"raw": raw, "edit_reason": edit_reason}}
```

### 8.5 Safety gates (`post_editor.py` enforces all)

1. `post_id` must equal `publisher.get_report_for_post(post_id)["discourse_post_id"]`. Edit aborts if no owning report found.
2. Before apply: write backup to `bots/research/knowledge/edits/<post_id>-<ISO8601>.md` containing raw pre-edit markdown. If backup fails, edit aborts.
3. `edit_reason` is mandatory (Discourse requires it; also retains audit trail).
4. After apply: append entry to report's `state.json` under `metadata.edit_history`.
5. After apply: post Slack message to status channel with a collapsed diff and a "rollback" hint (manual — links to the backup file).

### 8.6 state.json addition

```json
"edit_history": [
  {
    "post_id": 1304,
    "edited_at": "2026-04-17T12:50:00",
    "edit_type": "format",
    "change_summary": "LaTeX 블록을 $$...$$ delimiter로 수정",
    "triggered_by_post": 2,
    "backup_path": "knowledge/edits/1304-20260417T125000.md"
  }
]
```

## 9. Error handling

- Any LLM call that returns `success=False` → skip that step, DO NOT advance `last_checked_post_number` (already fixed in 2026-04-17 hotfix). Slack-notify on consecutive failures.
- Glossary refresh failure → log warning, continue. Never breaks reply flow.
- QA archive write failure → log error, continue. The Discourse reply has already succeeded at that point.
- Edit apply failure (Discourse API error) → log, revert to reply-only flow for this comment, Slack-notify.
- Edit fact-check `reject` → fallback to reply-only. No retries (editing should be decisive).

## 10. Testing

- **Unit**: `glossary.py` filters (stopwords, length, count threshold, manual-block skip). `post_editor.py` safety gates (wrong post_id, missing backup).
- **Integration (dry-run)**: pipe a recorded comment through the whole engagement with a mock `DiscourseClient` that records API calls; assert edit/reply/archive are called with right args.
- **Live smoke**: after first deploy, trigger via `src/trigger_discourse_poll.py` on a staging topic (or re-poll topic 291 once post #3 is rolled back for retest). Verify Slack alerts and backup files appear.

## 11. Rollout

1. Seed glossary from existing vault (one-shot script).
2. Land v2 code; restart research bot.
3. Manually roll back topic 291 cursor (`last_checked_post_number` → 1) and re-run `trigger_discourse_poll.py`. Confirm post #3 now gets a reply and any format issue on post #1 is edited in place.
4. Monitor next 48h of natural Discourse traffic for false edits / glossary garbage / reject storms.
5. If clean: remove or repurpose `trigger_discourse_poll.py`. If not: tighten filters/prompts and repeat.
