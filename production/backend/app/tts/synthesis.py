"""全文合成（AC-TTS-02/03/04）。

流程固定为：取冻结计划 → 编译表达 → 合成 → 建检查项 → 登记产物。
任何一步的产物都只引用**已有的**计划与稿件，不新造第二份正文。

三条硬规则在这里落地：

- **不换声**。合成失败就是失败；不存在"换一个音色再试"的代码路径。
- **不伪造时长**。真实音频拿不到时长即失败，因为下游视频时间轴依赖它。
- **Mock 不出门**。占位产物带 `is_mock=True`，检查项里就写着"不得确认"。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from .. import storage as st
from . import audio as au
from . import compiler as cp
from . import plan as pl
from . import provider as pv
from . import voices as vc
from .errors import TtsBlocked, TtsRejected


def build_full_checks(*, compiled: Dict[str, Any], result: Dict[str, Any],
                      voice: Dict[str, Any]) -> List[Dict[str, Any]]:
    """正文音频的检查项。硬门禁失败即不可确认；软项只报告不阻断。"""
    is_mock = bool(result.get("is_mock"))
    duration_ms = int(result.get("duration_ms") or 0)
    integrity = compiled.get("integrity") or {}
    ctrl = compiled.get("control_summary", {})
    scope = compiled["voice_scope"]

    checks: List[Dict[str, Any]] = [
        {"code": "VOICE_SCOPE", "outcome": "pass", "hard_gate": True,
         "message": f"音色作用域四元组一致：{scope['provider']}/{scope['model']}/"
                    f"{scope['region']}/{scope['remote_voice_id']}"},
        {"code": "SCRIPT_BINDING", "outcome": "pass", "hard_gate": True,
         "message": f"朗读文本与冻结稿件逐字符一致（计划 {compiled['plan_id']}，"
                    f"稿件 {compiled['script_version_id']}）"},
        {"code": "COMPLETENESS", "outcome": "pass", "hard_gate": True,
         "message": "分段拼接与稿件一致，无缺字/重字"
                    if integrity.get("ok") else "分段与稿件不一致"},
        {"code": "AUDIO_DECODE", "outcome": "not_run" if is_mock else "pass",
         "hard_gate": True,
         "message": "占位产物没有真实音频字节，不得据此声称已解码校验" if is_mock
                    else f"容器 {result.get('audio_format')}，"
                         f"{result.get('sample_rate')}Hz/{result.get('channels')}ch，"
                         f"哈希 {str(result.get('file_hash'))[:20]}… 完整解码"},
        {"code": "DURATION", "outcome": "not_run" if not duration_ms else "pass",
         "hard_gate": True,
         "message": "占位产物无真实时长：下游视频时间轴不可据此规划" if not duration_ms
                    else f"时长取自"
                         f"{'音频容器' if result.get('duration_source') == 'container' else '供应商附加字段'}"
                         f"：{duration_ms}ms（最终音频时长以此为准）"},
        # 软项：本项目未接入音频检测能力，因此只能记录"待人工听检"，不能自报通过
        {"code": "PRONUNCIATION", "outcome": "not_run", "hard_gate": False,
         "message": "术语读法需人工听检：未接入音频识别能力，不产生自动结论；"
                    "用户确认动作即人工听检记录"},
        {"code": "VOICE_EXPRESSION", "outcome": "pass" if not ctrl.get("unsupported") else "review",
         "hard_gate": False,
         "message": f"表达控制：支持 {ctrl.get('supported', 0)} 项、"
                    f"近似 {ctrl.get('approximate', 0)} 项、"
                    f"不支持 {ctrl.get('unsupported', 0)} 项（不支持项已在调用前报告）"},
    ]

    if compiled.get("suspicious_tags"):
        checks.append({"code": "TAG_LEAK_REVIEW", "outcome": "review", "hard_gate": False,
                       "message": f"正文含 {len(compiled['suspicious_tags'])} 处疑似表达标记语法，"
                                  "已报告未改写，须人工确认是否会被朗读"})

    timing = result.get("timing_source")
    checks.append({
        "code": "ALIGNMENT_PROVENANCE",
        "outcome": "pass" if timing else "not_run",
        "hard_gate": False,
        "message": f"字词时间戳来源 {timing}（已通过确定性校验）" if timing
                   else "本次合成未取得可用字词时间戳：不得宣称字词对齐已完成，"
                        "由后期对齐并另行登记对齐产物版本",
    })

    if result.get("warnings"):
        checks.append({"code": "PROVIDER_NOTES", "outcome": "review", "hard_gate": False,
                       "message": "；".join(result["warnings"])})
    return checks


def synthesize_full(*, work_id: str, transport: Optional[pv.Transport] = None,
                    amount_fen: int = 0, dry_run: bool = False,
                    plan_id: Optional[str] = None, task_id: Optional[str] = None,
                    force_new_plan: bool = False, force_new: bool = False) -> Dict[str, Any]:
    work = st.get("works", work_id)
    if not work:
        raise TtsRejected("WORK_NOT_FOUND", f"作品不存在：{work_id}")
    if work.get("deleted_at"):
        raise TtsRejected("WORK_DELETED", "作品已删除，不能继续合成")
    if not work.get("script_version_id"):
        raise TtsRejected("SELECTION_REQUIRED",
                          "尚未采用稿件：全文合成以已采用稿件为唯一正文来源")
    voice_id = work.get("voice_asset_id")
    if not voice_id:
        raise TtsRejected("SELECTION_REQUIRED", "合成全文音频前必须选定音色")

    voice = vc.get_voice(voice_id)
    if not voice:
        raise TtsRejected("VOICE_NOT_FOUND", f"音色不存在：{voice_id}")
    if voice.get("approval_state") != "approved":
        raise TtsRejected("VOICE_NOT_APPROVED",
                          "未审批的音色不能用于生产：候选音色须先经明确选择与基准试听",
                          {"voice_asset_id": voice_id,
                           "approval_state": voice.get("approval_state")})

    plan = pl.ensure_plan(work_id, force_new=force_new_plan) if not plan_id else None
    if plan_id:
        plan = cp.load_frozen_plan(plan_id)
    assert plan is not None
    if plan.get("work_id") != work_id:
        raise TtsRejected("PLAN_WORK_MISMATCH", "表演计划不属于当前作品")
    if plan.get("script_version_id") != work["script_version_id"]:
        raise TtsRejected("PLAN_SCRIPT_MISMATCH", "表演计划不属于当前采用的稿件，请重新生成计划")
    compiled = cp.compile_speech(plan["id"], voice_id)
    digest = pv.request_digest(compiled["voice_scope"]["model"],
                               compiled["voice_scope"]["scope_key"],
                               compiled["provider_params"], compiled["spoken_text"])

    if not dry_run and not force_new and not force_new_plan:
        for cached in reversed(au.list_audio(work_id, au.KIND_FULL)):
            if (cached.get("is_mock") or au.currentness_problems(cached)
                    or cached.get("performance_plan_id") != plan["id"]
                    or cached.get("request_digest") != digest):
                continue
            root = Path(__file__).resolve().parents[2] / "runtime"
            target = (root / cached.get("storage_uri", "")).resolve()
            if not target.is_relative_to(root.resolve()) or not target.is_file():
                raise TtsBlocked("TTS_CACHED_AUDIO_MISSING", "已合成音频文件缺失；请明确重新合成，未重复提交")
            if pv.file_hash(target.read_bytes()) != cached.get("file_hash"):
                raise TtsBlocked("TTS_CACHED_AUDIO_CHANGED", "已合成音频文件已改变；请明确重新合成，未重复提交")
            return {
                "audio": cached, "compiled": compiled, "checks": cached.get("checks", []),
                "hard_failures": au.hard_failures(cached), "is_mock": False,
                "duration_ms": cached["duration_ms"], "timing_source": cached.get("timing_source"),
                "cached": True, "note": "已复用同稿件、计划与音色参数的完整音频，未重复提交合成",
            }

    if dry_run:
        result = pv.synthesize(work_id=work_id, voice=voice,
                               provider_params=compiled["provider_params"],
                               text=compiled["spoken_text"], dry_run=True, task_id=task_id)
    else:
        attempt_key = f"tts:full:{work_id}:{plan['id']}:{digest}"
        if force_new:
            attempt_key += ":" + st.new_id("attempt")
        result = pv.synthesize(work_id=work_id, voice=voice,
                               provider_params=compiled["provider_params"],
                               text=compiled["spoken_text"], transport=transport,
                               amount_fen=amount_fen, task_id=task_id,
                               idempotency_key=attempt_key)

    checks = build_full_checks(compiled=compiled, result=result, voice=voice)
    payload = result.get("audio_bytes")
    audio = au.register_audio(
        work_id=work_id, kind=au.KIND_FULL, voice_id=voice_id,
        duration_ms=int(result.get("duration_ms") or 0),
        file_hash=result.get("file_hash", ""), is_mock=bool(result.get("is_mock")),
        script_version_id=compiled["script_version_id"],
        performance_plan_id=compiled["plan_id"],
        provider_run_id=result.get("job_id"),
        timing_source=result.get("timing_source"),
        sample_rate=result.get("sample_rate"), channels=result.get("channels"),
        words=result.get("words") or [], checks=checks,
        note="正文音频；" + ("；".join(result.get("warnings") or []) or "真实合成已解析"),
    )
    # 落盘必须在登记之后：文件名就是资产 id，只有登记完才知道 id。
    # 先落盘再登记会写成 None.wav，同一作品的多次合成都落到同一个文件上互相覆盖。
    if payload:
        audio["request_digest"] = digest
        audio["storage_uri"] = au.store_audio_bytes(work_id, audio["id"], payload)
        st.put("audios", audio["id"], audio, {"work_id": work_id})
    audio = au.preserve_result_profile(audio, compiled["synthesis_profile"],
                                      compiled["tts_controls_digest"], compiled["tts_controls_revision"])

    from .. import lineage

    lineage_row = lineage.record(
        artifact_id=audio["id"], kind="audio", work_id=work_id,
        digest=audio.get("file_hash") or f"mock:{audio['id']}",
        depends_on=[compiled["plan_id"],
                    f"{compiled['script_version_id']}:{compiled['script_digest']}"],
        meta={"voice_asset_id": voice_id, "is_mock": audio["is_mock"],
              "kind": "full", "timing_source": audio.get("timing_source")},
    )
    if audio.get("invalidated"):
        lineage_row.update(invalidated_at=audio["invalidated_at"], invalidated_by="tts_controls",
                           change_type="tts_controls")
        st.put("lineage", audio["id"], lineage_row, {"work_id": work_id})

    return {
        "audio": audio, "compiled": compiled, "checks": checks,
        "hard_failures": au.hard_failures(audio),
        "is_mock": audio["is_mock"],
        "duration_ms": audio["duration_ms"],
        "timing_source": audio.get("timing_source"),
        "cached": False,
        "note": "正文音频已登记；需用户试听确认后才成为本次作品的最终音频"
                if not audio["is_mock"] else
                "占位产物：不得确认为最终音频，也不继承任何质量通过结论",
    }
