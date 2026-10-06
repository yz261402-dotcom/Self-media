"""任务 1：新契约与旧数据的兼容边界。"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.agents.contracts import QualityDecision
from app.content import contracts as cc
from app.contracts import ScriptBatch, ScriptVersion, Work


LEGACY_WORK = {"id": "work_x", "name": "旧作品", "brief": {"topic": "AI"},
               "script_version_id": None, "current_task_id": None, "version": 1}

LEGACY_CANDIDATE = {
    "id": "script_old", "angle": "角度", "title": "标题", "description": "说明",
    "tags": [], "text": "旧正文", "duration_estimate_ms": 1000,
    "quality_report": {"checks": [], "overall": "pass", "review_required": False},
    "claims": [],
}


def test_legacy_work_still_loads_with_safe_defaults():
    work = Work(**LEGACY_WORK)
    assert work.content_revision == 0
    assert work.reference_snapshot_id is None
    assert work.writing_prompt_id is None
    assert work.style_snapshot == {}
    assert work.current_content_job_id is None


def test_work_accepts_new_content_fields():
    work = Work(**LEGACY_WORK, content_revision=3, reference_snapshot_id="ref_1",
                writing_prompt_id="prm_1", style_snapshot={"persona": "x"},
                current_content_job_id="cjob_1")
    assert work.content_revision == 3


def test_legacy_batch_and_candidate_still_load():
    batch = ScriptBatch(id="b1", work_id="w1", candidates=[LEGACY_CANDIDATE])
    cand = batch.candidates[0]
    assert cand.pickable is None
    assert cand.review_state is None
    assert cand.unpickable_reasons == []
    assert batch.requested is None and batch.produced is None


def test_candidate_contract_is_still_strict():
    with pytest.raises(ValidationError):
        ScriptVersion(**LEGACY_CANDIDATE, surprise_field=1)


def test_legacy_quality_decision_is_not_an_independent_review():
    old = QualityDecision(id="qd_1", work_item_id="wi", subject_id="script_old",
                          subject_digest="sha256:a", recommendation="pass")
    assert old.independent is False
    assert old.review_kind == "rules"
    assert cc.is_independent_review(old.model_dump()) is False


def test_independent_review_requires_kind_and_binding():
    base = dict(id="qd_2", work_item_id="wi", subject_id="script_new",
                subject_digest="sha256:a", recommendation="pass")
    forged = QualityDecision(**base, independent=True)  # 只自称独立，没有绑定与审核者
    assert cc.is_independent_review(forged.model_dump()) is False
    real = QualityDecision(**base, independent=True, review_kind="semantic_single",
                           reviewer={"provider": "qwen", "model": "qwen3.7-plus"},
                           binding={"text_digest": "sha256:t"})
    assert cc.is_independent_review(real.model_dump()) is True


def test_reference_with_ready_transcript_must_have_text():
    with pytest.raises(ValidationError):
        cc.ReferenceSnapshot(
            id="ref_1", work_id="w", platform_work_id="p", source_url="https://x/y",
            fetched_at="2026-09-29T00:00:00Z", extraction_version="redfox-transcript-v1",
            transcript={"state": "ready", "text": ""})


def test_reference_metrics_default_to_unknown_not_zero():
    ref = cc.ReferenceSnapshot(
        id="ref_1", work_id="w", platform_work_id="p", source_url="https://x/y",
        fetched_at="2026-09-29T00:00:00Z", extraction_version="redfox-transcript-v1")
    assert ref.metrics.likes is None and ref.metrics.comments is None
    assert ref.transcript.state == "none"
    assert ref.evidence_kind == "inspiration"


def test_reference_digest_tracks_content_not_fetch_time():
    a = cc.ReferenceSnapshot(
        id="ref_a", work_id="w", platform_work_id="p", source_url="https://x/y", title="T",
        caption="C", fetched_at="2026-09-29T00:00:00Z", extraction_version="v1",
        transcript={"state": "ready", "text": "转写"})
    b = a.model_copy(update={"id": "ref_b", "fetched_at": "2026-09-30T00:00:00Z"})
    c = a.model_copy(update={"transcript": cc.Transcript(state="ready", text="改过的转写")})
    assert cc.reference_digest(a) == cc.reference_digest(b)
    assert cc.reference_digest(a) != cc.reference_digest(c)


def test_text_digest_is_stable_and_whitespace_sensitive_only_at_edges():
    assert cc.text_digest("你好  世界") == cc.text_digest("你好  世界")
    assert cc.text_digest("  你好  世界\n") == cc.text_digest("你好  世界")
    assert cc.text_digest("你好世界") != cc.text_digest("你好 世界")


def test_set_digest_is_order_independent_and_detects_replacement():
    a = [{"slot": 0, "candidate_id": "s0", "text_digest": "sha256:a"},
         {"slot": 1, "candidate_id": "s1", "text_digest": "sha256:b"}]
    assert cc.set_digest(a) == cc.set_digest(list(reversed(a)))
    replaced = [a[0], {"slot": 1, "candidate_id": "s1b", "text_digest": "sha256:c"}]
    assert cc.set_digest(a) != cc.set_digest(replaced)
