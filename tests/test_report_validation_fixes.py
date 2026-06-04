"""Tests for report validation salvage + retry redesign + forensics.

Covers the root causes of the 4 historical Stage-4 "validation 통과 못함"
failures (033 5/11, 038 5/12, 052 6/2, 054 6/4): a complete report rejected
because of a single leading narration line, and a retry doomed by a 2-minute
timeout.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from skills.research.report_contracts import (
    looks_like_research_report_markdown,
    report_markdown_rejection_reason,
    salvage_research_report_markdown,
)
from skills.research.structured_artifact_authoring import StructuredArtifactAuthoring
from skills.research.artifact_critique_and_revision import ArtifactCritiqueAndRevision
from skills.types import LLMRunResult


_FILLER = (
    "LPU의 weight-stationary 데이터플로우와 LPDDR5X 540GB/s 대역폭 제약을 고려할 때, "
    "본 기법은 MPU 64x64 MAC array의 활용률을 유지하면서 가중치 트래픽을 직접 절감하는 "
    "방향으로 설계되어야 한다. VPU 64-lane 파이프라인과 SMA 스트리밍 경로의 상호작용, "
    "그리고 8MB L1 SRAM 예산 내에서의 버퍼링 전략이 핵심 검증 포인트다. "
)

VALID_BODY = f"""# 테스트 아이디어: LPU 적용 검토

## 1. 배경 및 동기
- 배경 설명: {_FILLER}

## 2. 핵심 기술 분석
- 기술 설명: {_FILLER}

## 3. HyperAccel 적용 방안

### 3-1. SW 관점 (ML/컴파일러 엔지니어용)
- SW 내용: {_FILLER}

### 3-2. HW 관점 (HW 아키텍트용)
- HW 내용: {_FILLER}

## 4. 장단점 및 오버헤드 분석
- 트레이드오프: {_FILLER}

## 5. 실현 가능성 평가
- 리스크: {_FILLER}

## 참고 문헌
- Paper (arXiv)
"""

# The exact narration prefixes from the 4 real incidents.
REAL_NARRATION_PREFIXES = [
    "All 5 figures validated. Now I'll write the report.\n\n",
    (
        "All four figures validated (well-formed XML, present on disk). "
        "Verification surfaced and I fixed 1 real blocker per figure plus minors; "
        "Fig 4 passed clean. Now writing the report (Phase 2), with the corrected "
        'Q2 decomposition (precision = majority ~73%, not "half") baked into the prose.\n\n'
    ),
    "도면을 모두 생성했습니다. 보고서를 작성합니다.\n\n",
    "Figures are done. Now writing the report body.\n\n",
]


# ─── report_contracts: salvage ───────────────────────────────────


def test_clean_report_passes_validation():
    assert looks_like_research_report_markdown(VALID_BODY)
    assert salvage_research_report_markdown(VALID_BODY) == VALID_BODY.strip()


@pytest.mark.parametrize("prefix", REAL_NARRATION_PREFIXES)
def test_narration_prefix_is_salvaged(prefix):
    """A complete report behind a narration line must be recovered, not discarded."""
    salvaged = salvage_research_report_markdown(prefix + VALID_BODY)
    assert salvaged == VALID_BODY.strip()


def test_narration_stub_without_report_is_rejected():
    stub = "보고서를 작성 완료했습니다: `report_prefill_decode.md` 파일을 확인하세요."
    assert salvage_research_report_markdown(stub) is None


def test_heading_skeleton_without_content_is_rejected():
    """A truncated run can emit the title + all section headings with no body
    (a ~150-char skeleton). Accepting it would suppress the retry and push an
    empty report to the reviewer."""
    skeleton = (
        "All figures validated. Now I'll write the report.\n\n"
        "# 제목만 있는 스켈레톤\n\n"
        "## 1. 배경 및 동기\n"
        "## 2. 핵심 기술 분석\n"
        "## 3. 적용 방안\n"
        "## 4. 장단점 및 오버헤드 분석\n"
        "## 5. 실현 가능성 평가\n"
        "## 참고 문헌\n"
    )
    assert salvage_research_report_markdown(skeleton) is None
    assert "content" in report_markdown_rejection_reason(skeleton)


def test_empty_output_is_rejected():
    assert salvage_research_report_markdown("") is None
    assert salvage_research_report_markdown("   \n  ") is None


def test_filename_mention_inside_body_is_not_rejected():
    """A valid report that merely mentions a report_*.md filename must pass
    (the old ungated FILE_ACTION pattern would have rejected it)."""
    body = VALID_BODY.replace(
        "- Paper (arXiv)",
        "- Paper (arXiv)\n- 부록: 파이프라인이 report_v1.md로 저장함",
    )
    assert salvage_research_report_markdown(body) == body.strip()


def test_heading_variants_are_tolerated():
    """Minor heading drift (e.g. dropping '분석'/'평가' suffixes) must not reject
    an otherwise complete report."""
    body = VALID_BODY.replace(
        "## 4. 장단점 및 오버헤드 분석", "## 4. 장단점 및 오버헤드"
    ).replace("## 5. 실현 가능성 평가", "## 5. 실현 가능성")
    assert salvage_research_report_markdown(body) is not None


def test_missing_required_section_is_rejected():
    body = VALID_BODY.replace("## 4. 장단점 및 오버헤드 분석", "## 4. 기타")
    assert salvage_research_report_markdown(body) is None


def test_rejection_reason_explains_failure():
    assert "empty" in report_markdown_rejection_reason("")
    assert "title" in report_markdown_rejection_reason("그냥 내레이션 텍스트입니다.")
    missing = report_markdown_rejection_reason(
        VALID_BODY.replace("## 4. 장단점 및 오버헤드 분석", "## 4. 기타")
    )
    assert "missing" in missing


# ─── structured_artifact_authoring: salvage + retry + forensics ──


class FakeRuntime:
    def __init__(self, results: list[LLMRunResult]):
        self.results = list(results)
        self.requests = []

    def run(self, request):
        self.requests.append(request)
        return self.results.pop(0)


def _ok(output: str) -> LLMRunResult:
    return LLMRunResult(success=True, output=output, duration_ms=1000)


def test_write_report_salvages_narration_without_retry(tmp_path):
    """The 052/054 case: narration-prefixed complete report → salvaged on the
    first attempt, no retry call burned."""
    runtime = FakeRuntime([_ok(REAL_NARRATION_PREFIXES[0] + VALID_BODY)])
    skill = StructuredArtifactAuthoring(runtime)
    report = skill.write_research_report(
        scope_text="scope", idea_brief_json="{}", deep_dive_json="{}",
        cwd=str(tmp_path),
    )
    assert report == VALID_BODY.strip()
    assert len(runtime.requests) == 1


def test_write_report_retry_has_adequate_timeout_and_forbids_tools(tmp_path):
    """The retry must get a realistic budget (>= 600s, was 120s) and must tell
    the model to emit text only — no figure re-verification tool work."""
    runtime = FakeRuntime([_ok("리포트를 파일로 저장했습니다."), _ok(VALID_BODY)])
    skill = StructuredArtifactAuthoring(runtime)
    report = skill.write_research_report(
        scope_text="scope", idea_brief_json="{}", deep_dive_json="{}",
        cwd=str(tmp_path),
    )
    assert report == VALID_BODY.strip()
    assert len(runtime.requests) == 2
    retry_request = runtime.requests[1]
    assert retry_request.timeout_ms >= 600_000
    assert "Do NOT use any tools" in retry_request.prompt


def test_write_report_persists_invalid_outputs_for_forensics(tmp_path):
    """When both attempts fail validation, the raw outputs must be preserved
    on disk so the failure is diagnosable (was: silently discarded)."""
    runtime = FakeRuntime([_ok("쓰레기 출력 1"), _ok("쓰레기 출력 2")])
    skill = StructuredArtifactAuthoring(runtime)
    report = skill.write_research_report(
        scope_text="scope", idea_brief_json="{}", deep_dive_json="{}",
        cwd=str(tmp_path),
    )
    assert report == ""
    invalid_dir = tmp_path / "invalid"
    saved = sorted(p.name for p in invalid_dir.iterdir())
    assert len(saved) == 2
    contents = [(invalid_dir / n).read_text(encoding="utf-8") for n in saved]
    assert "쓰레기 출력 1" in contents[0]
    assert "쓰레기 출력 2" in contents[1]


def test_revise_report_salvages_narration_and_uses_adequate_retry(tmp_path):
    """The Stage-6 revision path had the identical bug class."""
    runtime = FakeRuntime([_ok(REAL_NARRATION_PREFIXES[2] + VALID_BODY)])
    skill = ArtifactCritiqueAndRevision(runtime)
    report = skill.revise_report(
        scope_text="scope", idea_brief_json="{}", deep_dive_json="{}",
        previous_report="prev", reviewer_feedback="{}", report_version=1,
        cwd=str(tmp_path),
    )
    assert report == VALID_BODY.strip()
    assert len(runtime.requests) == 1

    runtime2 = FakeRuntime([_ok("invalid"), _ok(VALID_BODY)])
    skill2 = ArtifactCritiqueAndRevision(runtime2)
    report2 = skill2.revise_report(
        scope_text="scope", idea_brief_json="{}", deep_dive_json="{}",
        previous_report="prev", reviewer_feedback="{}", report_version=1,
        cwd=str(tmp_path),
    )
    assert report2 == VALID_BODY.strip()
    assert runtime2.requests[1].timeout_ms >= 600_000


# ─── pipeline: revision-loop failure classification ──────────────


def _make_pipeline_stub(tmp_path):
    """Bare ResearchPipeline with only the attrs run_revision_loop touches."""
    import sys

    sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
    from pipelines.research_pipeline import ResearchPipeline
    from report_store import ReportStore

    pipeline = ResearchPipeline.__new__(ResearchPipeline)
    pipeline.store = ReportStore(tmp_path / "reports")
    pipeline.max_revision_rounds = 2
    pipeline._post_status = lambda *a, **k: None
    return pipeline


def test_authoring_failure_does_not_mark_report_infeasible(tmp_path):
    """If revision generation fails for system reasons (timeout/validation),
    the report must NOT be judged 'infeasible' — that label is a content
    verdict reserved for reviewer rejection."""
    pipeline = _make_pipeline_stub(tmp_path)
    rid = pipeline.store.create_report("test-idea", metadata={})
    pipeline.store.update_state(rid, "revise")

    pipeline._researcher_revise_report = lambda *a, **k: None  # authoring fails
    batch_review = {"reviews": [{"idea_id": "test-idea", "decision": "revise"}]}

    pipeline.run_revision_loop([rid], batch_review)

    state = pipeline.store.get_report(rid)
    assert state["status"] == "revise", (
        "authoring failure must keep status 'revise', not mark infeasible"
    )


def test_partial_authoring_failure_is_retried_and_kept_revise(tmp_path):
    """Multi-rid round where one revision fails and another succeeds: the
    failed rid must be retried next round and, if it keeps failing, stay
    'revise' — not silently vanish from the loop."""
    pipeline = _make_pipeline_stub(tmp_path)
    rid_a = pipeline.store.create_report("idea-a", metadata={})
    rid_b = pipeline.store.create_report("idea-b", metadata={})
    pipeline.store.update_state(rid_a, "revise")
    pipeline.store.update_state(rid_b, "revise")

    calls = []

    def fake_revise(rid, round_num, idx, total):
        calls.append((rid, round_num))
        return VALID_BODY if rid == rid_b else None  # A fails every round

    pipeline._researcher_revise_report = fake_revise
    pipeline._review_revised_reports = lambda ids, rn: {
        "reviews": [{"idea_id": "idea-b", "decision": "accept"}]
    }
    batch_review = {"reviews": [
        {"idea_id": "idea-a", "decision": "revise"},
        {"idea_id": "idea-b", "decision": "revise"},
    ]}

    pipeline.run_revision_loop([rid_a, rid_b], batch_review)

    assert (rid_a, 2) in calls, "failed rid must be retried in the next round"
    assert pipeline.store.get_report(rid_a)["status"] == "revise"
    assert pipeline.store.get_report(rid_b)["status"] == "accepted"


def test_stray_report_drafts_swept_into_batch_review(tmp_path):
    """Reports stuck at report_draft (e.g. salvaged after a validation-fail
    incident) must be included in the next run's batch review — otherwise they
    sit unreviewable forever."""
    pipeline = _make_pipeline_stub(tmp_path)
    pipeline.publish_channel = ""
    stray = pipeline.store.create_report("stray-idea", metadata={})
    pipeline.store.update_state(stray, "report_draft")
    current = pipeline.store.create_report("current-idea", metadata={})

    seen = {}
    pipeline.run_deep_dives = lambda ids: None
    pipeline.run_feedback_loop = lambda ids: None
    pipeline.run_reports = lambda ids: {}

    def fake_batch_review(ids):
        seen["ids"] = list(ids)
        return None

    pipeline.run_batch_review = fake_batch_review

    pipeline._run_stages_after_selection([current])

    assert current in seen["ids"]
    assert stray in seen["ids"], "report_draft strays must join the batch review"


def test_reviewer_rejection_after_max_rounds_still_marks_infeasible(tmp_path):
    """Content-based 'still revise after max rounds' must keep marking infeasible."""
    pipeline = _make_pipeline_stub(tmp_path)
    rid = pipeline.store.create_report("test-idea", metadata={})
    pipeline.store.update_state(rid, "revise")

    pipeline._researcher_revise_report = lambda *a, **k: VALID_BODY
    pipeline._review_revised_reports = lambda ids, rn: {
        "reviews": [{"idea_id": "test-idea", "decision": "revise"}]
    }
    batch_review = {"reviews": [{"idea_id": "test-idea", "decision": "revise"}]}

    pipeline.run_revision_loop([rid], batch_review)

    state = pipeline.store.get_report(rid)
    assert state["status"] == "infeasible"


# ─── pipeline: exact idea_id matching ────────────────────────────


def test_idea_id_matching_is_exact_not_substring(tmp_path):
    import sys

    sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
    from pipelines.research_pipeline import ResearchPipeline

    rid = "054_activation-sparsity-weight-skip"
    assert ResearchPipeline._rid_matches_idea(rid, "activation-sparsity-weight-skip")
    assert not ResearchPipeline._rid_matches_idea(rid, "sparsity")
    assert not ResearchPipeline._rid_matches_idea(rid, "weight-skip")
