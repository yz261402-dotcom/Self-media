"""只写本轮隔离验收库；复用真实本地成片验证播放器和下载，不证明上游生成/人工质量。"""
from pathlib import Path
import hashlib
import json
import os
import shutil
import sys

ROOT = Path(__file__).resolve().parents[3]
HERE = Path(__file__).resolve().parent
os.environ["AVATAR_STUDIO_DB"] = str(HERE / "验收运行库.db")
sys.path.insert(0, str(ROOT / "production/backend"))
from app import storage as st
from app.contracts import Work, WorkBrief, Task, ScriptVersion, ScriptBatch, QualityReport

st.init_db()
evidence = json.loads((HERE / "真实后期验收/验收证据.json").read_text(encoding="utf-8"))
wid, tid = "acceptance_real_preview", "acceptance_render_task"
runtime = ROOT / "production/backend/runtime"
outdir = runtime / "acceptance/20260925"
outdir.mkdir(parents=True, exist_ok=True)
files = []
for kind, name in [("master", "成片.mp4"), ("subtitles_ass", "字幕.ass"), ("subtitles_srt", "字幕.srt"), ("cover", "封面.jpg")]:
    source = HERE / "真实后期验收" / name
    dest = outdir / name
    shutil.copy2(source, dest)
    files.append({"kind": kind, "storage_uri": dest.relative_to(runtime).as_posix(),
                  "sha256": hashlib.sha256(dest.read_bytes()).hexdigest(), "bytes": dest.stat().st_size})
script = ScriptVersion(id="acceptance_existing_script", angle="已有真实素材验收", title="已有真人素材 · 后期与下载验收",
                       description="仅用于独立验收，不代表本轮新生成或发布", tags=["验收"], text=evidence["subtitle_text"],
                       duration_estimate_ms=23187, quality_report=QualityReport()).model_dump()
work = Work(id=wid, name="验收专用 · 已有真人样片", brief=WorkBrief(topic="验证真实播放器、字幕成片与下载，未调用新增生成服务"),
            identity_id="av_base_b_v1", voice_asset_id="voice_minimax_warm_bestie_v1", script_version_id=script["id"],
            final_audio_id="acceptance_existing_audio", video_asset_id="acceptance_existing_video", current_task_id=tid).model_dump()
task = Task(id=tid, work_id=wid, mode="integration", state="waiting_selection", stage="video_review",
            pending_selection_ids=["sel_final_video"]).model_dump()
st.put("works", wid, work)
st.put("tasks", tid, task, {"work_id": wid})
st.put("script_batches", wid, ScriptBatch(id="acceptance_batch", work_id=wid, candidates=[script],
                                        source_kind="existing_media_acceptance_fixture").model_dump())
# 只复用既有音频字节验证播放器，不继承其旧稿件审核结论。
# 此工程夹具故意保留硬门禁，不能确认或生成正式发布包。
audio = dict(st.get("audios", "audio_daefb8c42e08") or {})
audio.update(id="acceptance_existing_audio", work_id=wid, script_version_id=script["id"],
             storage_uri="production/backend/runtime/audio/wreal_2146ef1ce27d/audio_daefb8c42e08.wav",
             url=None, approved=False, confirmed=False, frozen_at=None, confirmed_at=None,
             checks=[{"code": "ENGINEERING_FIXTURE", "outcome": "review", "hard_gate": True,
                      "message": "仅验证播放器，未进行本作品的人工音频审核"}],
             note="工程夹具：已有真实音频的独立引用，不是本轮新合成，不证明人工审核")
st.put("audios", audio["id"], audio, {"work_id": wid})
record = {"id": "worker_acceptance_existing_real_render", "work_id": wid, "task_id": tid, "stage": "postprocess",
          "executor_kind": "worker", "state": "succeeded", "simulated": False, "mode": "integration",
          "processing_outcome": "rendered", "created_at": "2026-09-25T00:00:00Z", "input_digest": "acceptance-only",
          "input_assets": {"audio_id": work["final_audio_id"], "video_id": work["video_asset_id"],
                           "script_version_id": script["id"], "script_hash": hashlib.sha256(script["text"].encode()).hexdigest()},
          "output_files": files, "verify_log": evidence["verify"], "subtitle_checks": evidence["subtitle_checks"],
          "note": "工程验收夹具：成片为真实本地重渲染；未替用户通过上游人物与口型人工质量验收"}
st.put("media_jobs", record["id"], record, {"work_id": wid})
print(json.dumps({"isolated_db": str(HERE / "验收运行库.db"), "work_id": wid, "task_id": tid}, ensure_ascii=False))
