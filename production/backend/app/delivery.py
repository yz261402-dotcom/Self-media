"""把已验收的后期文件冻结为可下载交付包，不调用模型或发布平台。"""
from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path
from urllib.parse import quote
import zipfile

from . import lineage, storage as st
from .contracts import PublishPackage, now_iso
from .digital_human import gates

RUNTIME_ROOT = Path(__file__).resolve().parent.parent / "runtime"
NAMES = {"master": "成片.mp4", "subtitles_ass": "字幕.ass", "subtitles_srt": "字幕.srt", "cover": "封面.jpg"}
_package_lock = threading.RLock()


class DeliveryBlocked(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


def digest_file(path: Path) -> str:
    with path.open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def local_path(uri: str) -> Path:
    root = RUNTIME_ROOT.resolve()
    path = (root / uri).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise DeliveryBlocked("MEDIA_FILE_MISSING", "交付文件缺失或路径超出运行目录，请重新生成成片")
    return path


def media_url(uri: str) -> str:
    return "/api/v1/media?uri=" + quote("production/backend/runtime/" + uri.replace("\\", "/"), safe="/")


def _script(work: dict) -> dict:
    return next((c for c in (st.get("script_batches", work["id"]) or {}).get("candidates", [])
                 if c.get("id") == work.get("script_version_id")), {})


def _matches(record: dict, work: dict) -> bool:
    inputs = record.get("input_assets") or {}
    if inputs.get("generation_mode") == "storyboard_native_audio":
        from .short_video.approval import matches_execution
        return matches_execution(record, work)
    if inputs.get("generation_mode") == "image_prompt_native_audio":
        from .digital_human import wan_native
        video = st.get("videos", work.get("video_asset_id") or "") or {}
        if not (video.get("confirmed") and video.get("approval_state") == "approved"
                and wan_native.matches_current(video, work) and gates.video_gate(video.get("id") or "").get("ok")):
            return False
        run = st.get("provider_runs", video.get("provider_run_id") or "") or {}
        return (inputs.get("video_id") == video.get("id") and inputs.get("video_hash") == video.get("file_hash")
                and inputs.get("provider_run_id") == video.get("provider_run_id")
                and inputs.get("source_asset_id") == video.get("source_asset_id")
                and inputs.get("source_asset_hash") == video.get("source_asset_hash")
                and inputs.get("request_digest") == run.get("request_digest") and inputs.get("sound_mode") == "native_audio")
    script = _script(work)
    audio = st.get("audios", work.get("final_audio_id") or "") or {}
    video = st.get("videos", work.get("video_asset_id") or "") or {}
    if (not audio.get("approved") or not audio.get("confirmed") or audio.get("is_mock")
            or not video.get("confirmed") or video.get("approval_state") != "approved" or video.get("is_mock")
            or not gates.video_gate(video.get("id") or "").get("ok")):
        return False
    for table, pointer, hash_key in (("audios", "final_audio_id", "audio_hash"), ("videos", "video_asset_id", "video_hash")):
        if inputs.get(hash_key):
            asset = st.get(table, work.get(pointer) or "") or {}
            if asset.get("file_hash") != inputs[hash_key]:
                return False
    return bool(script and inputs.get("audio_id") == work.get("final_audio_id")
                and inputs.get("video_id") == work.get("video_asset_id")
                and inputs.get("script_version_id") == work.get("script_version_id")
                and inputs.get("script_hash") == hashlib.sha256(script.get("text", "").encode()).hexdigest()
                and (not inputs.get("identity_id") or inputs["identity_id"] == work.get("identity_id"))
                and (not inputs.get("variant_id") or inputs["variant_id"] == work.get("variant_id")))


def latest_execution(work: dict) -> dict | None:
    rows = [r for r in st.all_rows("media_jobs", "work_id=?", [work["id"]])
            if r.get("stage") == "postprocess" and r.get("executor_kind") == "worker"
            and r.get("task_id") == work.get("current_task_id")]
    return max(rows, key=lambda r: r.get("created_at", ""), default=None)


def _active_task(work: dict, task_id: str | None = None) -> bool:
    current_id = work.get("current_task_id")
    task = st.get("tasks", current_id or "") or {}
    return bool(task and task.get("work_id") == work.get("id")
                and (not task_id or task_id == current_id)
                and task.get("state") not in ("cancelled", "failed", "blocked"))


def verified_outputs(record: dict) -> list[tuple[dict, Path]]:
    out = []
    for item in record.get("output_files", []):
        if item.get("kind") not in NAMES:
            continue
        path = local_path(item.get("storage_uri", ""))
        if path.stat().st_size <= 0 or path.stat().st_size != item.get("bytes") or digest_file(path) != item.get("sha256"):
            raise DeliveryBlocked("DELIVERY_HASH_MISMATCH", "成片文件与已审核版本不一致，请重新生成并预览")
        out.append((item, path))
    native = (record.get("input_assets") or {}).get("generation_mode") in ("image_prompt_native_audio", "storyboard_native_audio")
    required = {"master", "cover"} if native else {"master", "subtitles_ass", "subtitles_srt", "cover"}
    if (record.get("input_assets") or {}).get("subtitles_burned"):
        required |= {"subtitles_ass", "subtitles_srt"}
    if not required <= {item["kind"] for item, _ in out}:
        raise DeliveryBlocked("DELIVERY_FILES_INCOMPLETE", "成片、字幕或封面尚未全部生成")
    return out


def _require_render(work: dict) -> dict:
    if not _active_task(work):
        raise DeliveryBlocked("TASK_NOT_READY", "制作任务已取消、失败或不存在，请重新处理当前成片")
    record = latest_execution(work)
    if not record or record.get("state") != "succeeded" or record.get("simulated") or not record.get("verify_log"):
        raise DeliveryBlocked("RENDER_REQUIRED", "请先完成真实字幕和成片处理，再观看确认")
    if not _matches(record, work):
        raise DeliveryBlocked("DELIVERY_INPUT_CHANGED", "人物、稿件或音视频已变化，请重新生成成片")
    verified_outputs(record)
    return record


def status(work_id: str) -> dict:
    from . import engine
    work = st.get("works", work_id) or {"id": work_id}
    record = latest_execution(work)
    valid = bool(_active_task(work) and record and record.get("state") == "succeeded" and not record.get("simulated")
                 and record.get("verify_log") and _matches(record, work))
    error = None
    if valid:
        try:
            verified_outputs(record)
        except DeliveryBlocked as exc:
            valid = False
            error = {"code": exc.code, "message": exc.message}
    files = record.get("output_files", []) if valid else []
    task = st.get("tasks", work.get("current_task_id", ""))
    return {"task": engine.get_task(task["id"]) if task else None, "execution": record,
            "preview_url": next((media_url(f["storage_uri"]) for f in files if f["kind"] == "master"), None),
            "cover_url": next((media_url(f["storage_uri"]) for f in files if f["kind"] == "cover"), None),
            "files": files, "ready_for_review": valid, "error": error}


def confirm_review(work_id: str, *, actor_id: str, review_ack: bool, execution_id: str | None = None) -> dict:
    if not review_ack or not actor_id.startswith("user"):
        raise DeliveryBlocked("FINAL_REVIEW_REQUIRED", "请观看成片，确认字幕、画面与声音正常")
    work = st.get("works", work_id) or {"id": work_id}
    record = _require_render(work)
    if execution_id and execution_id != record["id"]:
        raise DeliveryBlocked("STALE_RENDER_REVIEW", "当前成片已更新，请观看最新版本再确认")
    review = {"id": st.new_id("review"), "work_id": work_id, "kind": "final_delivery_review",
              "execution_id": record["id"], "input_digest": record["input_digest"],
              "output_files": record["output_files"], "outcome": "pass", "actor_id": actor_id,
              "reviewed_at": now_iso()}
    st.put("quality_decisions", review["id"], review, {"work_id": work_id})
    return review


def _json_bytes(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8")


def build_delivery(task: dict, work: dict) -> dict:
    # 单用户单进程服务：重复点击/多标签同时确认只写一次归档，不共享正在写入的临时文件。
    with _package_lock:
        return _build_delivery(task, work)


def _build_delivery(task: dict, work: dict) -> dict:
    work = st.get("works", work["id"]) or work
    if not _active_task(work, task["id"]):
        raise DeliveryBlocked("TASK_NOT_READY", "制作任务已取消或已被新任务替代")
    record = _require_render(work)
    reviews = [r for r in st.all_rows("quality_decisions", "work_id=?", [work["id"]])
               if r.get("kind") == "final_delivery_review" and r.get("outcome") == "pass"
               and r.get("execution_id") == record["id"] and r.get("input_digest") == record["input_digest"]
               and r.get("output_files") == record["output_files"]]
    if not reviews:
        raise DeliveryBlocked("FINAL_REVIEW_REQUIRED", "成片已生成，请先观看并确认，再生成下载包")
    package_id = "pkg_" + hashlib.sha256(record["id"].encode()).hexdigest()[:24]
    existing = st.get("packages", package_id)
    if existing:
        download_path(package_id)
        return {"state": "succeeded", "processing_outcome": "packaged", "package_id": package_id,
                "output_files": existing["files"], "error": None}
    script = _script(work)
    files = verified_outputs(record)
    payloads = [(NAMES[item["kind"]], path) for item, path in files]
    info = {"title": script.get("title", work.get("name", "")), "description": script.get("description", ""),
            "tags": script.get("tags", []), "script": script.get("text", ""),
            "ai_label": "本片人物与声音由 AI 生成", "route": work.get("route"),
            "identity_id": work.get("identity_id"), "voice_asset_id": work.get("voice_asset_id")}
    native = (record.get("input_assets") or {}).get("generation_mode") == "image_prompt_native_audio"
    storyboard = (record.get("input_assets") or {}).get("generation_mode") == "storyboard_native_audio"
    if storyboard:
        from .short_video.approval import package_info
        info = package_info(record, work, info)
    if native:
        run = st.get("provider_runs", record["input_assets"].get("provider_run_id") or "") or {}
        params = run.get("provider_params") or {}
        info.update(title=work.get("name") or "Wan 原生声画", description=params.get("prompt") or "", tags=[], script="",
                    generation_mode="image_prompt_native_audio", sound_mode="native_audio", subtitles="not_generated",
                    voice_asset_id=None, provider_run_id=run.get("id"), prompt=params.get("prompt") or "",
                    note="成片保留供应商原片字节和原生声音；无自动字幕，AI 说明写入交付信息，未烧录到画面。")
    blobs = {"作品信息.json": _json_bytes(info),
             "质量报告.json": _json_bytes({"render": record["verify_log"], "subtitles": record.get("subtitle_checks"),
                                           "human_review": reviews[-1], "scope": "本地交付，未发布到平台"})}
    entries = [{"path": name, "sha256": digest_file(path), "bytes": path.stat().st_size} for name, path in payloads]
    entries += [{"path": name, "sha256": hashlib.sha256(blob).hexdigest(), "bytes": len(blob)} for name, blob in blobs.items()]
    manifest = {"package_id": package_id, "work_id": work["id"], "task_id": task["id"],
                "execution_id": record["id"], "input_assets": record["input_assets"],
                "files": entries, "created_at": now_iso()}
    manifest_blob = _json_bytes(manifest)
    out = RUNTIME_ROOT / "packages" / (package_id + ".zip")
    out.parent.mkdir(parents=True, exist_ok=True)
    temporary = out.with_suffix(".partial")
    with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as z:
        for name, path in payloads:
            z.write(path, name)
        for name, blob in blobs.items():
            z.writestr(name, blob)
        z.writestr("manifest.json", manifest_blob)
    # 压缩期间用户仍可改稿或取消。再次读取事实源，旧输出只保留为证据，不登记 ready。
    current_work = st.get("works", work["id"]) or {}
    current_task = st.get("tasks", task["id"]) or {}
    if (current_work.get("current_task_id") != task["id"] or current_task.get("state") == "cancelled"
            or not _matches(record, current_work)):
        raise DeliveryBlocked("DELIVERY_INPUT_CHANGED", "打包期间作品已修改或任务已取消，请确认当前版本后重试")
    temporary.replace(out)
    pkg = PublishPackage(id=package_id, work_id=work["id"], task_id=task["id"], mode=task["mode"], state="ready",
                         title=info["title"], description=info["description"], tags=info["tags"], ai_label=info["ai_label"],
                         files=entries, video_url=media_url(next(f["storage_uri"] for f, _ in files if f["kind"] == "master")),
                         cover_url=media_url(next(f["storage_uri"] for f, _ in files if f["kind"] == "cover")),
                         download_url=f"/api/v1/publish-packages/{package_id}/download",
                         manifest_digest=hashlib.sha256(manifest_blob).hexdigest()).model_dump()
    st.put("packages", package_id, {**pkg, "archive_uri": out.relative_to(RUNTIME_ROOT).as_posix(),
                                   "archive_hash": digest_file(out), "execution_id": record["id"],
                                   "input_assets": record["input_assets"]}, {"work_id": work["id"]})
    lineage.record(artifact_id=package_id, kind="native_package" if native else "package", work_id=work["id"], digest=pkg["manifest_digest"],
                   depends_on=([work["video_asset_id"], work["script_version_id"], record["input_assets"]["graph_id"]] if storyboard
                               else [work["video_asset_id"], record["input_assets"]["source_asset_id"]] if native
                               else [work["final_audio_id"], work["video_asset_id"]]),
                   meta={"generation_mode": "storyboard_native_audio" if storyboard else "image_prompt_native_audio"}
                   if native or storyboard else {})
    return {"state": "succeeded", "processing_outcome": "packaged", "package_id": package_id,
            "output_files": entries, "error": None}


def download_path(package_id: str) -> Path:
    pkg = st.get("packages", package_id)
    if not pkg or pkg.get("mode") == "mock" or pkg.get("state") != "ready":
        raise DeliveryBlocked("PACKAGE_NOT_READY", "真实交付包尚未生成")
    work = st.get("works", pkg["work_id"]) or {}
    lineage_row = lineage.get(package_id) or {}
    if not _active_task(work, pkg.get("task_id")) or not _matches(pkg, work) or lineage_row.get("invalidated_at"):
        raise DeliveryBlocked("DELIVERY_INPUT_CHANGED", "作品已修改，旧下载包已失效；请生成当前版本")
    path = local_path(pkg.get("archive_uri", ""))
    if digest_file(path) != pkg.get("archive_hash"):
        raise DeliveryBlocked("DELIVERY_HASH_MISMATCH", "下载包校验失败，请重新打包")
    return path


def public_package(pkg: dict) -> dict:
    return {k: v for k, v in pkg.items() if k in PublishPackage.model_fields}


def current_package(work_id: str) -> dict | None:
    for pkg in reversed(st.all_rows("packages", "work_id=?", [work_id])):
        try:
            download_path(pkg["id"])
            return public_package(pkg)
        except DeliveryBlocked:
            continue
    return None
