"""复用 MW-001 现有真人素材和实测时间轴，仅验证本地后期；不使用业务 DB/网络。"""
from pathlib import Path
import hashlib
import json
import sys
import time
import traceback

PROJECT = Path(r"C:\Users\22346\Desktop\自媒体视频生成")
sys.path.insert(0, str(PROJECT / "production/backend"))
from app.video import ffmpeg, render, subtitles, timeline

OUTPUT = Path(__file__).parent
SOURCE = PROJECT / "experiments/M2-Worker链路/MW-001/后期证据.json"
source = json.loads(SOURCE.read_text(encoding="utf-8"))


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


report = {"status": "running", "scope": "existing_real_media_local_rerender",
          "new_asr_calls": 0, "new_gpu_calls": 0, "production_db_used": False,
          "human_visual_review": "not_run",
          "limits": ["复用 MW-001 已记录的真实时间轴，本次未重新识别或对齐。",
                     "编码、解码和响度检测不代表人物身份、口型、动作、表情经人工验收。",
                     "缩放、去噪、锐化为固定 FFmpeg 参数，不宣称真实超分。"],
          "source_evidence": {"path": str(SOURCE), "sha256": digest(SOURCE),
                              "asr_job": source["asr_job"], "align_job": source["align_job"]},
          "inputs": {name: {"path": source[name], "sha256": digest(source[name])}
                     for name in ("video", "audio")}}
started = time.monotonic()
try:
    master, ass, srt, cover = (OUTPUT / n for n in ("成片.mp4", "字幕.ass", "字幕.srt", "封面.jpg"))
    if any(p.exists() for p in (master, ass, srt, cover)):
        raise FileExistsError("拒绝覆盖已有验收产物；请为新尝试使用新的输出目录")
    info = ffmpeg.probe(source["audio"])
    duration_ms = round(float(info["format"]["duration"]) * 1000)
    cues = subtitles.map_cues(source["cues"], timeline.EditTimeline.single(duration_ms))
    # 取已记录的字幕正文，保留与冻结稿一致的标点；ASR transcript 的标点不同，单独保留。
    text = "".join(c.text for c in cues)
    report["source_transcript"] = source["transcript"]
    report["subtitle_text"] = text
    report["subtitle_checks"] = subtitles.check_cues(cues, final_duration_ms=duration_ms, script_text=text)
    ass.write_text(subtitles.build_ass(cues, title="真实口播链路验证"), encoding="utf-8")
    srt.write_text(subtitles.build_srt(cues), encoding="utf-8")
    report["render"] = render.render_master(video_path=source["video"], audio_path=source["audio"],
                                           out_path=str(master), ass_path=str(ass))
    report["verify"] = render.verify_master(str(master))
    final_duration_ms = round(report["verify"]["probe"]["duration"] * 1000)
    report["final_subtitle_checks"] = subtitles.check_cues(cues, final_duration_ms=final_duration_ms,
                                                         script_text=text)
    report["cover"] = render.render_cover(video_path=str(master), out_path=str(cover),
                                         title="真实口播链路验证")
    preview = OUTPUT / "关键帧.png"
    decoded = ffmpeg.run_ffmpeg(["-y", "-threads", "2", "-ss", "6", "-i", str(master),
                                "-frames:v", "1", "-threads:v", "2", str(preview)])
    if decoded.returncode:
        raise RuntimeError(decoded.stderr.decode("utf-8", "replace")[-2000:])
    report["outputs"] = [{"path": str(p), "sha256": digest(p), "bytes": p.stat().st_size}
                         for p in (master, ass, srt, cover, preview)]
    report["status"] = "succeeded"
except Exception as error:
    report["status"] = "failed"
    report["error"] = {"type": type(error).__name__, "message": str(error),
                       "detail": getattr(error, "detail", {}), "traceback": traceback.format_exc()}
finally:
    report["elapsed_seconds"] = round(time.monotonic() - started, 3)
    (OUTPUT / "验收证据.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps({"status": report["status"], "elapsed_seconds": report["elapsed_seconds"],
                  "verify": report.get("verify"), "error": report.get("error")}, ensure_ascii=False))
raise SystemExit(0 if report["status"] == "succeeded" else 1)
