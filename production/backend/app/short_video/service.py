"""Persistent Wan storyboard graph; no GET side effects or automatic paid retries."""
from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import quote

from .. import config, jobs, storage as st
from ..avatar import person_image, registry
from ..content import contracts as cc, selection
from ..contracts import Task, now_iso
from ..digital_human import cost, wan_provider
from ..digital_human.errors import TalkingHeadBlocked, TalkingHeadRejected
from ..digital_human.wan_media import prepare_native_media, validate_image
from ..digital_human.plate_source import resolve_plate_source
from ..digital_human.wan_inputs import local_image_path

KIND = "wan_storyboard_graph"
MAX_SHOTS = 30
IMAGE_LEASE_SECONDS = person_image.DEFAULT_TIMEOUT_S * 2 + 120
_NO_IMAGE_JOB_CHECK = object()
SHOT_FIELDS = ("shot_index", "visual", "dialogue", "sound_effects", "duration_seconds", "image_prompt", "wan_prompt")
PLAN_FIELDS = ("work_id", "script_version_id", "script_digest", "task_id", "content_revision", "resolution",
               "image_model", "image_size", "image_ceiling_fen_per_shot", "estimate")


class StoryboardError(Exception):
    def __init__(self, code: str, message: str, http_status: int = 409):
        super().__init__(message)
        self.code, self.message, self.http_status = code, message, http_status


def _storyboard_budget_unlimited() -> bool:
    return os.environ.get("AVATAR_STUDIO_WAN_STORYBOARD_BUDGET_UNLIMITED", "").strip() == "1"


def get_graph(graph_id: str, work_id: str | None = None) -> dict:
    graph = st.get("media_jobs", graph_id)
    if not graph or graph.get("kind") != KIND or (work_id and graph["work_id"] != work_id):
        raise StoryboardError("STORYBOARD_NOT_FOUND", "分镜任务图不存在", 404)
    return graph


def list_graphs(work_id: str) -> list[dict]:
    return [project(g) for g in st.all_rows("media_jobs", "work_id=?", [work_id]) if g.get("kind") == KIND]


def _mutate(graph_id: str, callback) -> dict:
    with st.transaction() as conn:
        graph = get_graph(graph_id)
        callback(graph)
        graph["updated_at"] = now_iso()
        st.insert_row(conn, "media_jobs", graph_id, graph, {"work_id": graph["work_id"]})
    return graph


def _update_shot(graph_id: str, index: int, *, expected_image_job_id=_NO_IMAGE_JOB_CHECK,
                 expected_video_job_id=_NO_IMAGE_JOB_CHECK, expected_video_job_state=_NO_IMAGE_JOB_CHECK, **fields) -> dict:
    def apply(graph):
        shot = next(s for s in graph["shots"] if s["shot_index"] == index)
        if ((expected_image_job_id is _NO_IMAGE_JOB_CHECK or shot.get("image_job_id") == expected_image_job_id)
                and (expected_video_job_id is _NO_IMAGE_JOB_CHECK or shot.get("video_job_id") == expected_video_job_id)
                and (expected_video_job_state is _NO_IMAGE_JOB_CHECK
                     or (jobs.get_job(shot.get("video_job_id") or "") or {}).get("state") == expected_video_job_state)):
            shot.update(fields)
    return _mutate(graph_id, apply)


@contextmanager
def _paid_claim(graph_id: str):
    graph = get_graph(graph_id)
    key, owner = "storyboard_submit:" + graph["work_id"], uuid.uuid4().hex
    with st.transaction() as conn:
        work = st.get("works", graph["work_id"]) or {}
        if work.get("deleted_at"):
            raise StoryboardError("WORK_DELETED", "作品已删除，不能提交分镜制作", 404)
        lock = st.get("resource_locks", key) or {}
        if lock.get("expires_at", 0) > time.time():
            raise StoryboardError("STORYBOARD_SUBMISSION_BUSY", "该作品有正在执行的提交流程，请等待原请求返回")
        st.insert_row(conn, "resource_locks", key, {"id": key, "owner": owner, "graph_id": graph_id,
                      "expires_at": time.time() + IMAGE_LEASE_SECONDS})
    try:
        yield key, owner
    finally:
        with st.transaction() as conn:
            lock = st.get("resource_locks", key) or {}
            if lock.get("owner") == owner:
                conn.execute("DELETE FROM resource_locks WHERE id=?", [key])


def _renew_claim(claim: tuple[str, str]) -> None:
    key, owner = claim
    with st.transaction() as conn:
        lock = st.get("resource_locks", key) or {}
        if lock.get("owner") != owner or lock.get("expires_at", 0) <= time.time():
            raise StoryboardError("STORYBOARD_SUBMISSION_BUSY", "付费提交租约已失效，请核对原作业")
        lock["expires_at"] = time.time() + IMAGE_LEASE_SECONDS
        st.insert_row(conn, "resource_locks", key, lock)


def _selected(work_id: str) -> tuple[dict, dict]:
    work = st.get("works", work_id) or {}
    if work.get("deleted_at"):
        raise StoryboardError("WORK_DELETED", "作品已删除，不能继续制作", 404)
    version = st.get("script_versions", work.get("script_version_id") or "") or {}
    if (not work or not version or version.get("work_id") != work_id
            or version.get("content_route") != "short_video_storyboard"
            or not selection.current_selection_valid(work_id)):
        raise StoryboardError("STORYBOARD_SELECTION_REQUIRED", "请先采用当前审核通过的分镜剧本")
    batch = selection.project_batch(work_id) or {}
    current = next((c for c in batch.get("candidates", []) if c.get("id") == version["id"]), None)
    if (not batch.get("ready_to_present") or len(batch.get("candidates", [])) != 3
            or {c.get("slot") for c in batch["candidates"]} != {1, 2, 3}
            or current is None or cc.candidate_digest(current) != cc.candidate_digest(version)):
        raise StoryboardError("STORYBOARD_SELECTION_REQUIRED", "当前三篇候选必须全部通过审核，且选稿与已审核分镜完全一致")
    return work, version


def assert_current(graph: dict, *, paid: bool = False) -> None:
    if (graph.get("schema_version") not in (2, 3)
            or (graph.get("schema_version") == 3 and "identity_reference" not in graph)
            or _plan_digest(graph) != graph.get("plan_digest")):
        raise StoryboardError("STORYBOARD_GRAPH_CHANGED", "任务图提示词、时长或费用快照已变化，原确认不能继续使用")
    try:
        work, version = _selected(graph["work_id"])
    except StoryboardError as error:
        raise StoryboardError("STORYBOARD_INPUT_CHANGED", "选稿或内容审核已变化，原任务仅保留为历史记录") from error
    if (work.get("script_version_id") != graph["script_version_id"]
            or int(work.get("content_revision") or 0) != graph["content_revision"]
            or cc.candidate_digest(version) != graph["script_digest"]):
        raise StoryboardError("STORYBOARD_INPUT_CHANGED", "分镜或创作输入已变化，请重新准备任务图")
    task = st.get("tasks", graph.get("task_id") or "") or {}
    if (work.get("current_task_id") != graph.get("task_id") or task.get("work_id") != graph["work_id"]
            or task.get("state") in (None, "cancelled", "failed", "blocked") or work.get("route") != "WAN_3_0"):
        raise StoryboardError("STORYBOARD_INPUT_CHANGED", "制作任务或路线已变化，旧任务图仅保留为历史记录")
    if paid and (graph.get("is_mock") or config.resolve_mode() == "mock"):
        raise StoryboardError("STORYBOARD_REAL_MODE_REQUIRED", "占位稿件或 mock 模式不能提交付费制作")
    if "identity_reference" in graph:
        reference, _ = _identity_reference(work)
        if reference != graph["identity_reference"]:
            raise StoryboardError("STORYBOARD_IDENTITY_CHANGED", "人物形象或参考图已变化，请按当前人物重新准备任务图并确认费用")
    elif paid and work.get("identity_id"):
        raise StoryboardError("STORYBOARD_IDENTITY_REFERENCE_REQUIRED", "旧任务图未使用选定人物，请按当前人物重新准备任务图并确认费用")


def _identity_reference(work: dict) -> tuple[dict | None, bytes | None]:
    """Resolve the selected approved person, preserving exact source bytes and provenance."""
    if not work.get("identity_id"):
        return None, None
    try:
        source = resolve_plate_source(work)
        path = local_image_path(source)
        validate_image(path)
        data = path.read_bytes()
        if "sha256:" + hashlib.sha256(data).hexdigest() != source["asset_hash"]:
            raise ValueError("reference changed during read")
        ident = registry.get_identity(source["identity_id"]) or {}
        reference = dict(source, identity_version=ident.get("identity_version", 1),
                         identity_digest=ident.get("identity_digest"))
    except (TalkingHeadBlocked, TalkingHeadRejected, OSError, ValueError) as error:
        raise StoryboardError("STORYBOARD_IDENTITY_REFERENCE_INVALID",
                              "选定人物参考图不可用：" + str(error)) from error
    return reference, data


def _shots(version: dict, resolution: str, image_size: str, image_model: str,
           identity_reference: dict | None = None) -> list[dict]:
    raw = version.get("storyboard")
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_SHOTS:
        raise StoryboardError("STORYBOARD_SHOT_INVALID", f"分镜数量须为 1 至 {MAX_SHOTS}", 422)
    out = []
    for index, shot in enumerate(raw, 1):
        try:
            if not isinstance(shot, dict) or shot.get("shot_index") != index:
                raise ValueError()
            visual, dialogue, sounds = shot["visual"], shot.get("dialogue", ""), shot.get("sound_effects", "")
            if not all(isinstance(s, str) for s in (visual, dialogue, sounds)) or not visual.strip():
                raise ValueError()
            prompt = (str(shot.get("prompt") or visual).strip() + "\n画面：" + visual.strip()
                      + "\n台词/旁白：" + dialogue + "\n声音/音效：" + sounds)
            if identity_reference:
                prompt = "保持起始图中的人物身份与外貌，不换脸、不换人。\n" + prompt
            params = wan_provider.assert_native_input(duration_seconds=shot["duration_seconds"],
                                                      prompt=prompt, resolution=resolution)
            image_prompt = "短片首帧，竖构图，无文字水印。\n" + visual.strip()
            if identity_reference:
                image_prompt = ("画面中出现主角时，使用参考图中的同一人物，优先保持面部特征、发型、年龄感、"
                                "身材比例与整体形象一致；画面描述中的泛称或外貌冲突不应改变参考人物身份。"
                                "只按镜头调整动作、表情、场景和构图。空镜、道具和手部特写不额外加入人脸。\n"
                                + image_prompt)
            person_image.build_request(prompt=image_prompt, size=image_size, model=image_model)
        except (KeyError, TypeError, ValueError, TalkingHeadRejected, person_image.PersonImageRejected) as error:
            raise StoryboardError("STORYBOARD_SHOT_INVALID", "每镜头需连续编号、有效画面提示及 2–30 秒整数时长", 422) from error
        out.append({"shot_index": index, "visual": visual, "dialogue": dialogue, "sound_effects": sounds,
                    "duration_seconds": params["duration_seconds"], "image_prompt": image_prompt,
                    "wan_prompt": params["prompt"], "image_state": "pending", "video_state": "pending",
                    "image": None, "clip": None, "remote_task_id": None})
    return out


def _graph_task(work: dict) -> tuple[dict, bool]:
    task = st.get("tasks", work.get("current_task_id") or "") or {}
    if (task.get("work_id") == work["id"] and task.get("mode") != "mock" and task.get("route") == "WAN_3_0"
            and task.get("state") not in (None, "cancelled", "failed", "blocked")):
        return task, False
    task = Task(id=st.new_id("task"), work_id=work["id"], mode=config.resolve_mode(),
                route="WAN_3_0", stage="content_review", state="waiting_review",
                stage_rows=st.stage_rows("content_review", "waiting_review")).model_dump()
    return task, True


def _plan_digest(graph: dict) -> str:
    plan = {**{k: graph.get(k) for k in PLAN_FIELDS},
            "shots": [{k: shot.get(k) for k in SHOT_FIELDS} for shot in graph["shots"]]}
    # Historical schema 2 hashes must keep their original meaning.
    if "identity_reference" in graph:
        plan["identity_reference"] = graph["identity_reference"]
    return jobs.args_digest(plan)


def create_graph(work_id: str, *, resolution: str = "720p", image_size: str = person_image.DEFAULT_SIZE,
                 image_ceiling_fen_per_shot: int = 0) -> dict:
    work, version = _selected(work_id)
    if isinstance(image_ceiling_fen_per_shot, bool) or not isinstance(image_ceiling_fen_per_shot, int) or image_ceiling_fen_per_shot < 0:
        raise StoryboardError("STORYBOARD_IMAGE_CEILING_INVALID", "图片预算上限须为非负整数分", 422)
    resolution = wan_provider.normalize_resolution(resolution)
    model = os.environ.get(person_image.DEFAULT_MODEL_ENV, "").strip() or person_image.DEFAULT_MODEL
    reference, _ = _identity_reference(work)
    shots = _shots(version, resolution, image_size, model, reference)
    task, new_task = _graph_task(work)
    estimates = [cost.estimate("WAN_3_0", duration_seconds=s["duration_seconds"], resolution=resolution) for s in shots]
    video_total = sum(e["ceiling_fen"] for e in estimates)
    image_total = image_ceiling_fen_per_shot * len(shots)
    quote = {"currency": "CNY", "video_total_fen": video_total,
             "image_ceiling_total_fen": image_total, "total_ceiling_fen": video_total + image_total,
             "budget_unlimited": _storyboard_budget_unlimited(),
             "image_price_known": False, "image_calls": len(shots), "video_calls": len(shots),
             "shot_prices": [{"shot_index": s["shot_index"], "ceiling_fen": e["ceiling_fen"],
                               "price_snapshot_id": e["price_snapshot_id"]} for s, e in zip(shots, estimates)],
             "note": "Wan 按当前官方价格估算；图片上限是预算预留，非已核验供应商单价。失败可能计费，不自动重试。"}
    plan = {"work_id": work_id, "script_version_id": version["id"], "script_digest": cc.candidate_digest(version),
            "task_id": task["id"],
            "content_revision": int(work.get("content_revision") or 0), "resolution": resolution,
            "identity_reference": reference,
            "image_model": model, "image_size": image_size, "image_ceiling_fen_per_shot": image_ceiling_fen_per_shot,
            "estimate": quote,
            "shots": [{k: s[k] for k in ("shot_index", "visual", "dialogue", "sound_effects", "duration_seconds",
                                          "image_prompt", "wan_prompt")} for s in shots]}
    digest = _plan_digest(plan)
    with st.transaction() as conn:
        for prior in list_graphs(work_id):
            if prior["plan_digest"] == digest:
                return prior
        current, selected = _selected(work_id)
        if (current.get("script_version_id") != version["id"] or cc.candidate_digest(selected) != plan["script_digest"]
                or current.get("content_revision", 0) != plan["content_revision"]
                or _identity_reference(current)[0] != reference):
            raise StoryboardError("STORYBOARD_INPUT_CHANGED", "准备任务图期间内容已变化")
        if new_task:
            current.update(current_task_id=task["id"], route="WAN_3_0")
            st.insert_row(conn, "works", work_id, current)
            st.insert_row(conn, "tasks", task["id"], task, {"work_id": work_id})
        graph = dict(plan, id=st.new_id("storyboard"), kind=KIND, schema_version=3, plan_digest=digest,
                     state="waiting_image_approval", shots=shots, is_mock=selection.is_mock_batch(st.get("script_batches", work_id)),
                     image_approval=None, video_approval=None, clip_approval=None, created_at=now_iso(), updated_at=now_iso())
        st.insert_row(conn, "media_jobs", graph["id"], graph, {"work_id": work_id})
    return project(graph)


def _task_digest(graph: dict) -> str:
    return jobs.args_digest({"plan_digest": graph["plan_digest"], "images": [
        {"shot_index": s["shot_index"], "image": s.get("image")} for s in graph["shots"]]})


def _clip_digest(graph: dict) -> str:
    return jobs.args_digest({"task_graph_digest": _task_digest(graph), "clips": [
        {"shot_index": s["shot_index"], "remote_task_id": s.get("remote_task_id"), "clip": s.get("clip")}
        for s in graph["shots"]]})


def project(graph: dict) -> dict:
    result = json.loads(json.dumps(graph))
    # Runtime policy is surfaced on both new and historical graphs without mutating
    # the persisted plan digest. This lets old pending graphs use the same unlimited
    # Wan storyboard controls after a restart.
    result.setdefault("estimate", {})["budget_unlimited"] = _storyboard_budget_unlimited()
    from .image_recovery import project_shot
    from .video_regeneration import project_shot as project_video
    for shot in result["shots"]:
        project_shot(shot)
        project_video(shot)
    states = [s["image_state"] for s in result["shots"]] + [s["video_state"] for s in result["shots"]]
    if result.get("state") not in ("stitch_queued", "stitching", "succeeded", "stitch_failed"):
        if "unknown" in states:
            result["state"] = "unknown"
        elif "failed" in states or "blocked" in states:
            result["state"] = "blocked"
        elif all(s["video_state"] == "succeeded" for s in result["shots"]):
            result["state"] = "ready_to_stitch" if result.get("clip_approval") else "waiting_clip_review"
        elif any(s["video_state"] != "pending" for s in result["shots"]):
            result["state"] = "waiting_provider"
        elif all(s["image_state"] == "succeeded" for s in result["shots"]):
            result["state"] = "images_ready"
        elif any(s["image_state"] != "pending" for s in result["shots"]):
            result["state"] = "preparing_images"
    result["task_graph_digest"], result["clip_digest"] = _task_digest(result), _clip_digest(result)
    from .subtitle_jobs import available as subtitles_available
    result["subtitle_generation_available"] = subtitles_available(result)
    for item in (result.get("output") or {}).get("subtitle_files", []):
        item["preview_url"] = (f"/api/v1/works/{quote(result['work_id'], safe='')}/wan/storyboards/"
                               f"{quote(result['id'], safe='')}/subtitles/files/{item['kind']}")
    if result.get("identity_reference"):
        result["identity_reference"]["preview_url"] = "/api/v1/media?uri=" + quote(result["identity_reference"]["storage_uri"], safe="/")
    from .image_regeneration import available
    from .video_regeneration import available as video_available
    for shot in result["shots"]:
        shot["image_regeneration_available"] = available(result, shot)
        shot["video_regeneration_available"] = video_available(result, shot)
        for name in ("image", "clip"):
            asset = shot.get(name)
            if asset:
                asset["preview_url"] = "/api/v1/media?uri=" + quote(asset["storage_uri"], safe="/")
    if result.get("state") == "succeeded" and result.get("video_id"):
        from .. import delivery

        try:
            package = delivery.current_package(result["work_id"])
        except OSError:
            package = None
            result["package_error"] = {"code": "STORYBOARD_PACKAGE_UNAVAILABLE",
                                       "message": "发布包暂不可访问，请检查本地文件后刷新"}
        record = st.get("packages", package["id"]) if package else None
        if (record and (record.get("input_assets") or {}).get("graph_id") == result["id"]
                and (record.get("input_assets") or {}).get("video_id") == result["video_id"]):
            result["package_id"] = package["id"]
    return result


def approve_images(graph_id: str, *, actor_id: str, plan_digest: str, paid_ack: bool, total_ceiling_fen: int) -> dict:
    graph = get_graph(graph_id)
    assert_current(graph)
    if (not str(actor_id).startswith("user") or paid_ack is not True or plan_digest != graph["plan_digest"]
            or (graph["estimate"]["image_ceiling_total_fen"] <= 0 and not _storyboard_budget_unlimited())
            or total_ceiling_fen != graph["estimate"]["image_ceiling_total_fen"]):
        raise StoryboardError("STORYBOARD_IMAGE_APPROVAL_INVALID", "请确认当前任务图、图片调用次数及图片预算上限")
    return project(_mutate(graph_id, lambda g: g.update(image_approval={"actor_id": actor_id,
        "plan_digest": plan_digest, "total_ceiling_fen": total_ceiling_fen, "approved_at": now_iso()})))


def _image_file(graph: dict, shot: dict) -> Path:
    root = Path(person_image.workspace_root()).resolve()
    folder = root / "production/backend/runtime/wan_inputs/storyboards" / graph["id"]
    if not folder.resolve().is_relative_to(root) or any(p.is_symlink() for p in [folder, *folder.parents] if p.is_relative_to(root)):
        raise StoryboardError("STORYBOARD_ASSET_PATH_INVALID", "分镜产物目录越界")
    folder.mkdir(parents=True, exist_ok=True)
    attempt = shot.get("image_attempt", 0)
    if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 0:
        raise StoryboardError("STORYBOARD_ASSET_PATH_INVALID", "图片尝试编号无效")
    suffix = f"_attempt_{attempt:03d}" if attempt else ""
    return folder / f"shot_{shot['shot_index']:03d}{suffix}.png"


def _store(path: Path, data: bytes) -> str:
    if path.is_symlink() or path.resolve().parent != path.parent.resolve():
        raise StoryboardError("STORYBOARD_ASSET_PATH_INVALID", "分镜产物路径越界")
    if path.exists():
        if path.read_bytes() != data:
            raise StoryboardError("STORYBOARD_ARTIFACT_CONFLICT", "原始产物字节冲突，停止覆盖")
    else:
        with path.open("xb") as stream:
            stream.write(data)
    return "sha256:" + hashlib.sha256(data).hexdigest()


def generate_images(graph_id: str, *, transport=None) -> dict:
    with _paid_claim(graph_id) as claim:
        return _generate_images(graph_id, transport=transport, claim=claim)


def recover_image(graph_id: str, shot_index: int) -> dict:
    from .image_recovery import recover
    return recover(graph_id, shot_index)


def regenerate_image(graph_id: str, shot_index: int, **kwargs) -> dict:
    from .image_regeneration import regenerate
    return regenerate(graph_id, shot_index, **kwargs)


def regenerate_video(graph_id: str, shot_index: int, **kwargs) -> dict:
    from .video_regeneration import regenerate
    return regenerate(graph_id, shot_index, **kwargs)


def queue_subtitles(graph_id: str, **kwargs) -> dict:
    from .subtitle_jobs import queue
    return queue(graph_id, **kwargs)


def _image_args(graph: dict, shot: dict) -> dict:
    args = {"graph_id": graph["id"], "shot_index": shot["shot_index"], "plan_digest": graph["plan_digest"],
            "prompt": shot["image_prompt"], "size": graph["image_size"], "model": graph["image_model"]}
    if graph.get("identity_reference"):
        args["identity_reference"] = graph["identity_reference"]
    if shot.get("image_attempt"):
        args["image_attempt"] = shot["image_attempt"]
    return args


def _save_image(graph: dict, shot: dict, produced: dict) -> dict:
    path = _image_file(graph, shot)
    digest = _store(path, produced["image_bytes"])
    validate_image(path)
    image = {"asset_id": st.new_id("shot_image"), "work_id": graph["work_id"], "kind": "wan_image",
             "asset_hash": digest, "storage_uri": path.relative_to(Path(person_image.workspace_root()).resolve()).as_posix(),
             "width": produced["width"], "height": produced["height"], "provider": person_image.PROVIDER,
             "model": graph["image_model"], "provider_response_id": produced.get("response_id"),
             "request_digest": produced["request_digest"]}
    if graph.get("identity_reference"):
        image["identity_reference"] = graph["identity_reference"]
    return image


def _restore_completed_image(graph: dict, shot: dict, job: dict | None) -> bool:
    if not job or job.get("state") != "succeeded":
        return False
    image = (job.get("result") or {}).get("image") or {}
    path = _image_file(graph, shot)
    expected_uri = path.relative_to(Path(person_image.workspace_root()).resolve()).as_posix()
    try:
        if (image.get("work_id") != graph["work_id"] or image.get("storage_uri") != expected_uri
                or path.is_symlink() or not path.is_file()):
            raise ValueError("image result mismatch")
        with path.open("rb") as stream:
            digest = "sha256:" + hashlib.file_digest(stream, "sha256").hexdigest()
        if digest != image.get("asset_hash"):
            raise ValueError("image file mismatch")
        validate_image(path)
    except (OSError, ValueError, TalkingHeadRejected):
        _update_shot(graph["id"], shot["shot_index"], image_state="unknown", image=None, image_job_id=job["id"],
                     expected_image_job_id=job["id"],
                     error={"code": "STORYBOARD_IMAGE_ARTIFACT_CHANGED", "message": "已完成图片文件缺失或变化，请核对原作业，不自动重发"})
        return False
    _update_shot(graph["id"], shot["shot_index"], expected_image_job_id=job["id"],
                 image_state="succeeded", image=image, image_job_id=job["id"], error=None)
    return True


def _generate_images(graph_id: str, *, transport=None, claim) -> dict:
    graph = get_graph(graph_id)
    assert_current(graph, paid=True)
    approval = graph.get("image_approval") or {}
    if approval.get("plan_digest") != graph["plan_digest"]:
        raise StoryboardError("STORYBOARD_IMAGE_APPROVAL_REQUIRED", "请先确认整批图片生成费用")
    _batch_budget(graph, stage="image")
    for shot in graph["shots"]:
        if shot["image_state"] == "succeeded":
            continue
        if shot["image_state"] != "pending":
            if _restore_completed_image(graph, shot, jobs.get_job(shot.get("image_job_id") or "")):
                continue
            return project(get_graph(graph_id))
        _renew_claim(claim)
        assert_current(get_graph(graph_id), paid=True)
        reference, reference_bytes = _identity_reference(st.get("works", graph["work_id"]) or {})
        if reference != graph.get("identity_reference"):
            raise StoryboardError("STORYBOARD_IDENTITY_CHANGED", "人物参考图已变化，请重新准备任务图")
        args = _image_args(graph, shot)
        job = jobs.submit(work_id=graph["work_id"], capability="storyboard_image", provider=person_image.PROVIDER,
                          model=graph["image_model"], args=args, idempotency_key=f"{graph_id}:image:{shot['shot_index']}",
                          billable=True, amount_fen=graph["image_ceiling_fen_per_shot"], namespace="wan-storyboard")
        if job.get("replayed"):
            if _restore_completed_image(graph, shot, job):
                continue
            return project(_update_shot(graph_id, shot["shot_index"], image_state="unknown", image_job_id=job["id"]))
        if job["state"] == "blocked":
            return project(_update_shot(graph_id, shot["shot_index"], image_state="blocked", image_job_id=job["id"],
                                       error={"code": job["block_code"], "message": job["block_detail"]}))
        result = _generate_image_job(graph, shot, job, reference_bytes, transport=transport)
        returned = next(s for s in result["shots"] if s["shot_index"] == shot["shot_index"])
        if returned["image_state"] != "succeeded" or returned.get("image_job_id") != job["id"]:
            return result
    return project(get_graph(graph_id))


def _generate_image_job(graph: dict, shot: dict, job: dict, reference_bytes: bytes | None, *, transport=None) -> dict:
    """Execute one newly queued job, shared by initial generation and explicit retries."""
    from .image_recovery import save_receipt, record_error
    graph_id = graph["id"]
    worker = f"storyboard:image:{job['id']}"
    try:
        lease = jobs.acquire_lease(job["id"], worker, ttl_seconds=IMAGE_LEASE_SECONDS, require_queued=True)
    except jobs.JobRejected as error:
        if error.code != "JOB_NOT_QUEUED":
            raise
        return project(get_graph(graph_id))
    bound = _update_shot(graph_id, shot["shot_index"], expected_image_job_id=shot.get("image_job_id"),
                         image_state="sending", image_job_id=job["id"])
    current_shot = next(s for s in bound["shots"] if s["shot_index"] == shot["shot_index"])
    if current_shot.get("image_job_id") != job["id"] or lease["expires_at"] <= jobs._now():
        return project(bound)
    reference = graph.get("identity_reference")
    try:
        produced = person_image.generate(prompt=shot["image_prompt"], size=graph["image_size"],
            model=graph["image_model"], transport=transport, reference_image_bytes=reference_bytes,
            reference_sha256=reference["asset_hash"] if reference else "",
            reference_uri=reference["storage_uri"] if reference else "",
            on_response=lambda request, response: save_receipt(graph, shot, job["id"], request, response))
        image = _save_image(graph, shot, produced)
        jobs.complete(job["id"], worker, {"image": image})
        _update_shot(graph_id, shot["shot_index"], expected_image_job_id=job["id"],
                     image_state="succeeded", image=image, error=None)
    except Exception as error:
        completed = jobs.get_job(job["id"])
        if completed and completed.get("state") == "succeeded":
            raise
        view = record_error(job["id"], worker, error)
        return project(_update_shot(graph_id, shot["shot_index"], expected_image_job_id=job["id"],
                                   image_state="unknown", error=view))
    return project(get_graph(graph_id))


def poll_shot(graph_id: str, shot_index: int, *, transport=None) -> dict:
    from .media import save_clip
    from ..digital_human import providers
    graph = get_graph(graph_id)
    shot = next((s for s in graph["shots"] if s["shot_index"] == shot_index), None)
    if not shot:
        raise StoryboardError("STORYBOARD_SHOT_NOT_FOUND", "镜头不存在", 404)
    job = jobs.get_job(shot.get("video_job_id") or "") or {}
    from .video_regeneration import restore
    if shot["video_state"] in ("sending", "unknown") and restore(graph, shot, job):
        graph = get_graph(graph_id)
        shot = next(s for s in graph["shots"] if s["shot_index"] == shot_index)
    current = get_graph(graph_id)
    active = next(s for s in current["shots"] if s["shot_index"] == shot_index)
    if active.get("video_job_id") != job.get("id"):
        return project(current)
    if shot["video_state"] in ("succeeded", "failed", "pending", "blocked"):
        return project(graph)
    if not shot.get("remote_task_id"):
        if shot.get("video_job_id"):
            saved = jobs.mark_unknown(shot["video_job_id"], reason="缺少原 Wan task_id，不得重提", preserve_terminal=True)
            if saved["state"] in jobs.TERMINAL_STATES:
                restore(graph, shot, saved)
                return project(get_graph(graph_id))
        return project(_update_shot(graph_id, shot_index, expected_video_job_id=job.get("id"), expected_video_job_state="unknown", video_state="unknown"))
    if not shot.get("endpoint"):
        raise StoryboardError("STORYBOARD_ENDPOINT_MISSING", "任务缺少原工作空间地址，请先核对")
    request = wan_provider.build_query_request(task_id=shot["remote_task_id"], api_key=wan_provider.credentials(),
                                               base_url=shot["endpoint"])
    transport = transport or wan_provider.urllib_transport
    try:
        response = transport(request)
        output = wan_provider._document(response).get("output") or {}
        if output.get("task_id") and output["task_id"] != shot["remote_task_id"]:
            return project(get_graph(graph_id))
        parsed = wan_provider.parse_query_response(response)
    except Exception:
        return project(get_graph(graph_id))
    current = get_graph(graph_id)
    active = next(s for s in current["shots"] if s["shot_index"] == shot_index)
    if active.get("video_job_id") != job.get("id") or active.get("remote_task_id") != shot.get("remote_task_id"):
        return project(current)
    if parsed["state"] == "unavailable":
        return project(current)
    if parsed["state"] == "running":
        with st.transaction() as conn:
            saved = jobs.get_job(job["id"])
            if saved["state"] not in jobs.TERMINAL_STATES:
                saved.update(state="waiting_provider", lease=None, updated_at=now_iso(),
                    last_query={"remote_task_id": shot["remote_task_id"], "remote_status": parsed.get("remote_status"), "at": now_iso()})
                st.insert_row(conn, "provider_jobs", job["id"], saved, {"work_id": graph["work_id"]})
            else:
                return project(current)
        return project(_update_shot(graph_id, shot_index, expected_video_job_id=job["id"], expected_video_job_state="waiting_provider", video_state="waiting_provider", error=None))
    if parsed["state"] == "unknown":
        saved = jobs.mark_unknown(shot["video_job_id"], reason=parsed.get("reason", "原 Wan 任务状态未知"), preserve_terminal=True)
        if saved["state"] in jobs.TERMINAL_STATES:
            restore(graph, shot, saved)
            return project(get_graph(graph_id))
        return project(_update_shot(graph_id, shot_index, expected_video_job_id=job["id"], expected_video_job_state="unknown", video_state="unknown"))
    if parsed["state"] == "failed":
        error = {"code": parsed.get("error_code") or "WAN_REMOTE_FAILED", "message": parsed["reason"]}
        saved = _settle(job, success=False, result={"remote_task_id": shot["remote_task_id"], "error": error})
        if saved["state"] == "succeeded":
            restore(graph, shot, saved)
            return project(get_graph(graph_id))
        return project(_update_shot(graph_id, shot_index, expected_video_job_id=job["id"], expected_video_job_state="failed", video_state="failed", error=error))
    downloaded = providers.download_and_verify(parsed["video_url"], transport,
                                                declared_duration_ms=shot["duration_seconds"] * 1000)
    clip = save_clip(graph, shot, downloaded["data"])
    _settle(job, success=True, result={"clip": clip, "remote_task_id": shot["remote_task_id"]})
    return project(_update_shot(graph_id, shot_index, expected_video_job_id=job["id"], expected_video_job_state="succeeded", video_state="succeeded", clip=clip, error=None))


def _settle(job: dict, *, success: bool, result: dict) -> dict:
    """An original task query settles evidence and the terminal state atomically."""
    with st.transaction() as conn:
        saved = jobs.get_job(job["id"])
        if saved["state"] in jobs.TERMINAL_STATES:
            return saved
        saved.update(state="succeeded" if success else "failed", result=result,
            result_digest=jobs.args_digest(result), lease=None, settlement_required=bool(saved.get("billable")),
            updated_at=now_iso(), reconciliation={"at": now_iso(), "evidence": {"source": "wan_original_task_query", **result}})
        if not success:
            saved["error"] = {**(result.get("error") or {"code": "WAN_REMOTE_FAILED"}), **result}
        st.insert_row(conn, "provider_jobs", saved["id"], saved, {"work_id": saved["work_id"]})
        return saved


def approve_clips(graph_id: str, *, actor_id: str, clip_digest: str, review_ack: bool) -> dict:
    from .media import verify_clips
    graph = get_graph(graph_id)
    assert_current(graph)
    if (not str(actor_id).startswith("user") or review_ack is not True or _clip_digest(graph) != clip_digest
            or not all(s["video_state"] == "succeeded" and s.get("clip") for s in graph["shots"])):
        raise StoryboardError("STORYBOARD_CLIP_REVIEW_INVALID", "请观看全部镜头，确认画面、原生声音与衔接")
    verify_clips(graph)
    return project(_mutate(graph_id, lambda g: g.update(clip_approval={"actor_id": actor_id,
        "clip_digest": clip_digest, "reviewed_at": now_iso()})))


def queue_stitch(graph_id: str) -> dict:
    with _paid_claim(graph_id) as claim:
        return _queue_stitch(graph_id, claim=claim)


def _queue_stitch(graph_id: str, *, claim) -> dict:
    from .media import verify_clips
    graph = get_graph(graph_id)
    assert_current(graph)
    if (graph.get("clip_approval") or {}).get("clip_digest") != _clip_digest(graph):
        raise StoryboardError("STORYBOARD_CLIP_REVIEW_REQUIRED", "全部原片审核通过后才能拼接")
    if graph.get("state") == "succeeded":
        return project(graph)
    verify_clips(graph)
    _renew_claim(claim)
    current = get_graph(graph_id)
    if _clip_digest(current) != _clip_digest(graph) or (current.get("clip_approval") or {}).get("clip_digest") != _clip_digest(graph):
        raise StoryboardError("STORYBOARD_CLIP_REVIEW_REQUIRED", "原片或审核已变化，请重新确认后拼接")
    previous = jobs.get_job(graph.get("stitch_job_id") or "")
    # A cancelled unsent attempt must release its idempotency key.  Otherwise
    # clicking stitch again replays the cancelled job and puts the graph back
    # into stitch_queued with work that the Worker can never lease.
    attempt = int(graph.get("stitch_attempt") or 0) + (
        1 if previous and previous.get("state") in ("failed", "cancelled") else 0
    )
    key = f"{graph_id}:stitch:{_clip_digest(graph)}"
    if attempt:
        key += f":attempt:{attempt}"
    job = jobs.submit(work_id=graph["work_id"], capability="storyboard_stitch", provider="local_ffmpeg", model="ffmpeg",
                      args={"graph_id": graph_id, "clip_digest": _clip_digest(graph)},
                      idempotency_key=key, namespace="wan-storyboard")
    return project(_mutate(graph_id, lambda g: g.update(stitch_job_id=job["id"], stitch_attempt=attempt,
                                                      state="stitch_queued", error=None)))


def confirm_and_deliver(graph_id: str, *, actor_id: str, review_ack: bool, video_id: str, output_digest: str) -> dict:
    from .approval import confirm_and_deliver as confirm
    return confirm(graph_id, actor_id=actor_id, review_ack=review_ack, video_id=video_id, output_digest=output_digest)


def approve_videos(graph_id: str, *, actor_id: str, task_graph_digest: str, paid_ack: bool,
                   image_review_ack: bool, total_ceiling_fen: int) -> dict:
    graph = get_graph(graph_id)
    assert_current(graph)
    if (not str(actor_id).startswith("user") or paid_ack is not True or image_review_ack is not True or _task_digest(graph) != task_graph_digest
            or not all(s["image_state"] == "succeeded" for s in graph["shots"])
            or total_ceiling_fen != graph["estimate"]["video_total_fen"]):
        raise StoryboardError("STORYBOARD_VIDEO_APPROVAL_INVALID", "请审阅全部镜头图片，并确认当前整批 Wan 费用")
    _verify_images(graph)
    return project(_mutate(graph_id, lambda g: g.update(video_approval={"actor_id": actor_id,
        "task_graph_digest": task_graph_digest, "total_ceiling_fen": total_ceiling_fen, "approved_at": now_iso()})))


def _verify_images(graph: dict) -> None:
    from ..digital_human.wan_inputs import local_image_path
    for shot in graph["shots"]:
        local_image_path(shot["image"])


def _batch_budget(graph: dict, *, stage: str) -> None:
    field = "image_state" if stage == "image" else "video_state"
    amount = graph["estimate"]["image_ceiling_total_fen" if stage == "image" else "video_total_fen"]
    policy = config.budget_policy()
    remaining = [s for s in graph["shots"] if s[field] == "pending"]
    if stage == "video":
        unreserved = []
        for shot in remaining:
            row = st.conn().execute("SELECT job_id FROM provider_jobs_idem WHERE idem_key=?", [f"{graph['id']}:wan:{shot['shot_index']}"]).fetchone()
            prior = jobs.get_job(row["job_id"]) if row else None
            if not prior or prior.get("state") == "blocked":
                unreserved.append(shot)
        remaining = unreserved
    if not remaining:
        _assert_no_other_unresolved_graphs(graph)
        return
    pending_cost = (len(remaining) * graph["image_ceiling_fen_per_shot"] if stage == "image" else
                    sum(p["ceiling_fen"] for p in graph["estimate"]["shot_prices"]
                        if p["shot_index"] in {s["shot_index"] for s in remaining}))
    day = now_iso()[:10]
    reserved = [j for j in st.all_rows("provider_jobs") if j.get("billable")
                and str(j.get("created_at", ""))[:10] == day and j.get("state") != "blocked"
                and not (j.get("state") == "cancelled" and not j.get("settlement_required"))]
    unlimited = _storyboard_budget_unlimited()
    if (not unlimited and (amount > policy.per_task_limit_fen
            or sum(int(j.get("amount_fen") or 0) for j in reserved) + pending_cost > policy.daily_limit_fen
            or len(reserved) + len(remaining) > policy.max_billable_submissions)):
        raise StoryboardError("STORYBOARD_BATCH_BUDGET_BLOCKED", "整批费用或调用次数超出已授权预算，尚未开始新调用")
    _assert_no_other_unresolved_graphs(graph)


def _assert_no_other_unresolved_graphs(graph: dict) -> None:
    for prior in st.all_rows("media_jobs", "work_id=?", [graph["work_id"]]):
        if prior.get("kind") != KIND or prior["id"] == graph["id"]:
            continue
        if any(s.get("image_state") in ("sending", "unknown") or s.get("video_state") in ("sending", "unknown", "waiting_provider")
               for s in prior.get("shots", [])):
            raise StoryboardError("STORYBOARD_RECONCILIATION_REQUIRED", "该作品还有未核对的制作作业，请先处理原任务图")


def submit_videos(graph_id: str, *, transport=None) -> dict:
    with _paid_claim(graph_id) as claim:
        return _submit_videos(graph_id, transport=transport, claim=claim)


def _submit_videos(graph_id: str, *, transport=None, claim) -> dict:
    graph = get_graph(graph_id)
    assert_current(graph, paid=True)
    if (graph.get("video_approval") or {}).get("task_graph_digest") != _task_digest(graph):
        raise StoryboardError("STORYBOARD_VIDEO_APPROVAL_REQUIRED", "请先审阅任务图并确认整批 Wan 费用")
    if any(s["video_state"] in ("unknown", "failed", "blocked", "sending", "waiting_provider") for s in graph["shots"]):
        return project(graph)
    _batch_budget(graph, stage="video")
    _verify_images(graph)
    endpoint, key = wan_provider.endpoint(), wan_provider.credentials()
    transport = transport or wan_provider.urllib_transport
    for shot, estimate in zip(graph["shots"], graph["estimate"]["shot_prices"]):
        if shot["video_state"] != "pending":
            continue
        _renew_claim(claim)
        current = get_graph(graph_id)
        assert_current(current, paid=True)
        _verify_images(current)
        from .video_regeneration import args as video_args, send, restore
        args = video_args(graph, shot, endpoint)
        job = jobs.submit(work_id=graph["work_id"], capability="storyboard_wan", provider=wan_provider.PROVIDER,
                          model=wan_provider.MODEL, args=args, idempotency_key=f"{graph_id}:wan:{shot['shot_index']}",
                          billable=True, amount_fen=estimate["ceiling_fen"], namespace="wan-storyboard")
        if job.get("replayed"):
            bound = _update_shot(graph_id, shot["shot_index"], expected_video_job_id=shot.get("video_job_id"),
                video_job_id=job["id"], endpoint=endpoint)
            shot = next(s for s in bound["shots"] if s["shot_index"] == shot["shot_index"])
            if shot.get("video_job_id") != job["id"]:
                return project(bound)
            matches = job.get("args_digest") == jobs.args_digest(args)
            if matches and restore(bound, shot, job):
                return project(get_graph(graph_id))
            if matches and job["state"] == "queued":
                return send(bound, shot, job, transport=transport)
            jobs.mark_unknown(job["id"], reason="Wan 提交意图已存在但镜头缺少受理记录，请核对原作业")
            return project(_update_shot(graph_id, shot["shot_index"], expected_video_job_id=job["id"], video_state="unknown", video_job_id=job["id"],
                error={"code": "STORYBOARD_WAN_RECONCILIATION_REQUIRED", "message": "请核对原作业，不会自动重提"}))
        if job["state"] == "blocked":
            return project(_update_shot(graph_id, shot["shot_index"], video_state="blocked", video_job_id=job["id"],
                                       error={"code": job["block_code"], "message": job["block_detail"]}))
        return send(graph, shot, job, transport=transport)
    return project(get_graph(graph_id))
