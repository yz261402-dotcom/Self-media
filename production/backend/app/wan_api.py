"""Wan 单镜头输入及经过确认的多镜头制作接口。"""
from __future__ import annotations

import hashlib
from pathlib import Path
from urllib.parse import quote, unquote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

from . import delivery, storage as st
from .contracts import now_iso
from .digital_human.errors import TalkingHeadBlocked, TalkingHeadRejected

router = APIRouter()
WORKSPACE_ROOT = Path(__file__).resolve().parents[3]
IMAGE_ROOT = Path(__file__).resolve().parent.parent / "runtime" / "wan_inputs"
MAX_IMAGE_BYTES = 20 * 1024 * 1024


def _storyboard_call(call, *args, **kwargs):
    from .short_video.service import StoryboardError
    try:
        return call(*args, **kwargs)
    except StoryboardError as error:
        _error(error.code, error.message, error.http_status)
    except delivery.DeliveryBlocked as error:
        _error(error.code, error.message, 409)
    except (TalkingHeadBlocked, TalkingHeadRejected) as error:
        _error(error.code, error.message, 409 if isinstance(error, TalkingHeadBlocked) else 422)


def _storyboard(graph_id: str, work_id: str):
    from .short_video import service
    return _storyboard_call(service.get_graph, graph_id, work_id)


@router.get("/api/v1/works/{work_id}/wan/storyboards")
def list_storyboards(work_id: str):
    from .short_video import service
    _work(work_id)
    return {"graphs": service.list_graphs(work_id)}


@router.post("/api/v1/works/{work_id}/wan/storyboards")
def create_storyboard(work_id: str, payload: dict):
    from .short_video import service
    return _storyboard_call(service.create_graph, work_id, resolution=payload.get("resolution", "720p"),
        image_size=payload.get("image_size", "1024x1536"),
        image_ceiling_fen_per_shot=payload.get("image_ceiling_fen_per_shot", 0))


@router.get("/api/v1/works/{work_id}/wan/storyboards/{graph_id}")
def get_storyboard(work_id: str, graph_id: str):
    from .short_video import service
    return service.project(_storyboard(graph_id, work_id))


@router.post("/api/v1/works/{work_id}/wan/storyboards/{graph_id}/images/approve")
def approve_storyboard_images(work_id: str, graph_id: str, payload: dict):
    from .short_video import service
    _storyboard(graph_id, work_id)
    return _storyboard_call(service.approve_images, graph_id, actor_id=payload.get("actor_id", ""),
        plan_digest=payload.get("plan_digest", ""), paid_ack=payload.get("paid_ack") is True,
        total_ceiling_fen=payload.get("total_ceiling_fen"))


@router.post("/api/v1/works/{work_id}/wan/storyboards/{graph_id}/images/generate")
def generate_storyboard_images(work_id: str, graph_id: str):
    from .short_video import service
    _storyboard(graph_id, work_id)
    return _storyboard_call(service.generate_images, graph_id)


@router.post("/api/v1/works/{work_id}/wan/storyboards/{graph_id}/shots/{shot_index}/image/recover")
def recover_storyboard_image(work_id: str, graph_id: str, shot_index: int):
    from .short_video import service
    _storyboard(graph_id, work_id)
    return _storyboard_call(service.recover_image, graph_id, shot_index)


@router.post("/api/v1/works/{work_id}/wan/storyboards/{graph_id}/shots/{shot_index}/image/regenerate")
def regenerate_storyboard_image(work_id: str, graph_id: str, shot_index: int, payload: dict):
    from .short_video import service
    _storyboard(graph_id, work_id)
    return _storyboard_call(service.regenerate_image, graph_id, shot_index,
        actor_id=payload.get("actor_id", ""), plan_digest=payload.get("plan_digest", ""),
        paid_ack=payload.get("paid_ack") is True, duplicate_charge_ack=payload.get("duplicate_charge_ack") is True,
        expected_image_job_id=payload.get("expected_image_job_id", ""), request_id=payload.get("request_id", ""),
        ceiling_fen=payload.get("ceiling_fen"))


@router.post("/api/v1/works/{work_id}/wan/storyboards/{graph_id}/shots/{shot_index}/video/regenerate")
def regenerate_storyboard_video(work_id: str, graph_id: str, shot_index: int, payload: dict):
    from .short_video import service
    _storyboard(graph_id, work_id)
    return _storyboard_call(service.regenerate_video, graph_id, shot_index,
        actor_id=payload.get("actor_id", ""), task_graph_digest=payload.get("task_graph_digest", ""),
        paid_ack=payload.get("paid_ack") is True, duplicate_charge_ack=payload.get("duplicate_charge_ack") is True,
        expected_video_job_id=payload.get("expected_video_job_id", ""), request_id=payload.get("request_id", ""),
        ceiling_fen=payload.get("ceiling_fen"))


@router.post("/api/v1/works/{work_id}/wan/storyboards/{graph_id}/videos/approve")
def approve_storyboard_videos(work_id: str, graph_id: str, payload: dict):
    from .short_video import service
    _storyboard(graph_id, work_id)
    return _storyboard_call(service.approve_videos, graph_id, actor_id=payload.get("actor_id", ""),
        task_graph_digest=payload.get("task_graph_digest", ""), paid_ack=payload.get("paid_ack") is True,
        image_review_ack=payload.get("image_review_ack") is True,
        total_ceiling_fen=payload.get("total_ceiling_fen"))


@router.post("/api/v1/works/{work_id}/wan/storyboards/{graph_id}/videos/submit")
def submit_storyboard_videos(work_id: str, graph_id: str):
    from .digital_human import wan_provider
    from .short_video import service
    _storyboard(graph_id, work_id)
    return _storyboard_call(service.submit_videos, graph_id, transport=wan_provider.urllib_transport)


@router.post("/api/v1/works/{work_id}/wan/storyboards/{graph_id}/shots/{shot_index}/poll")
def poll_storyboard_shot(work_id: str, graph_id: str, shot_index: int):
    from .digital_human import wan_provider
    from .short_video import service
    _storyboard(graph_id, work_id)
    return _storyboard_call(service.poll_shot, graph_id, shot_index, transport=wan_provider.urllib_transport)


@router.post("/api/v1/works/{work_id}/wan/storyboards/{graph_id}/clips/approve")
def approve_storyboard_clips(work_id: str, graph_id: str, payload: dict):
    from .short_video import service
    _storyboard(graph_id, work_id)
    return _storyboard_call(service.approve_clips, graph_id, actor_id=payload.get("actor_id", ""),
        clip_digest=payload.get("clip_digest", ""), review_ack=payload.get("review_ack") is True)


@router.post("/api/v1/works/{work_id}/wan/storyboards/{graph_id}/stitch")
def queue_storyboard_stitch(work_id: str, graph_id: str):
    from .short_video import service
    _storyboard(graph_id, work_id)
    return _storyboard_call(service.queue_stitch, graph_id)


@router.post("/api/v1/works/{work_id}/wan/storyboards/{graph_id}/confirm")
def confirm_storyboard_final(work_id: str, graph_id: str, payload: dict):
    from .short_video import service
    _storyboard(graph_id, work_id)
    return _storyboard_call(service.confirm_and_deliver, graph_id, actor_id=payload.get("actor_id", ""),
        review_ack=payload.get("review_ack") is True, video_id=payload.get("video_id", ""),
        output_digest=payload.get("output_digest", ""))


@router.post("/api/v1/works/{work_id}/wan/storyboards/{graph_id}/subtitles")
def queue_storyboard_subtitles(work_id: str, graph_id: str, payload: dict):
    from .short_video import service
    _storyboard(graph_id, work_id)
    return _storyboard_call(service.queue_subtitles, graph_id, actor_id=payload.get("actor_id", ""),
        compute_ack=payload.get("compute_ack") is True, expected_video_id=payload.get("expected_video_id", ""),
        output_digest=payload.get("output_digest", ""), request_id=payload.get("request_id", ""))


@router.get("/api/v1/works/{work_id}/wan/storyboards/{graph_id}/subtitles/files/{kind}")
def download_storyboard_subtitle(work_id: str, graph_id: str, kind: str):
    from .short_video import approval, media
    graph = _storyboard(graph_id, work_id)
    _storyboard_call(approval.require_video, st.get("videos", graph.get("video_id") or "") or {}, _work(work_id))
    item = next((f for f in (graph.get("output") or {}).get("subtitle_files", []) if f.get("kind") == kind), None)
    if kind not in ("subtitles_ass", "subtitles_srt") or item is None:
        _error("STORYBOARD_SUBTITLE_NOT_FOUND", "字幕文件尚未生成", 404)
    path = _storyboard_call(media.local_clip, item)
    return FileResponse(path, filename="字幕." + ("ass" if kind == "subtitles_ass" else "srt"), media_type="text/plain; charset=utf-8")


def _error(code: str, message: str, status: int = 422):
    raise HTTPException(status, detail={"error": {"code": code, "message": message}})


def _work(work_id: str) -> dict:
    work = st.get("works", work_id)
    if not work:
        _error("WORK_NOT_FOUND", "作品不存在", 404)
    if work.get("deleted_at"):
        _error("WORK_DELETED", "作品已删除，不能继续制作", 404)
    return work


def _view(source: dict | None) -> dict:
    if not source:
        return {"source": None}
    uri = str(source.get("storage_uri") or "").replace("\\", "/")
    return {"source": {key: source.get(key) for key in
                       ("asset_id", "asset_hash", "storage_uri", "label", "width", "height")}
            | {"preview_url": "/api/v1/media?uri=" + quote(uri, safe="/")}}


@router.get("/api/v1/works/{work_id}/wan/image")
def image_status(work_id: str):
    from .digital_human.wan_inputs import resolve_source
    work = _work(work_id)
    try:
        return _view(resolve_source(work))
    except (TalkingHeadBlocked, TalkingHeadRejected) as error:
        return {"source": None, "error": {"code": error.code, "message": error.message}}


@router.post("/api/v1/works/{work_id}/wan/image")
async def upload_image(work_id: str, request: Request):
    from .digital_human.wan_media import validate_image
    _work(work_id)
    declared = request.headers.get("content-length")
    if declared:
        try:
            size = int(declared)
        except ValueError:
            _error("WAN_IMAGE_SIZE_INVALID", "图片大小无效")
        if size > MAX_IMAGE_BYTES:
            _error("WAN_IMAGE_SIZE_INVALID", "图片不能超过 20 MB", 413)
    asset_id = st.new_id("wan_image")
    folder = IMAGE_ROOT / asset_id
    folder.mkdir(parents=True, exist_ok=False)
    temporary = folder / "image.upload"
    total = 0
    try:
        with temporary.open("xb") as stream:
            async for chunk in request.stream():
                total += len(chunk)
                if total > MAX_IMAGE_BYTES:
                    _error("WAN_IMAGE_SIZE_INVALID", "图片不能超过 20 MB", 413)
                stream.write(chunk)
        meta = validate_image(temporary)
        suffix = {"PNG": ".png", "JPEG": ".jpg", "WEBP": ".webp"}[meta["format"]]
        target = folder / ("image" + suffix)
        temporary.replace(target)
        digest = "sha256:" + hashlib.sha256(target.read_bytes()).hexdigest()
        uri = target.resolve().relative_to(WORKSPACE_ROOT).as_posix()
        source = {"work_id": work_id, "kind": "wan_image", "asset_id": asset_id,
                  "asset_hash": digest, "storage_uri": uri, "identity_id": None, "variant_id": None,
                  "width": meta["width"], "height": meta["height"], "size_bytes": total,
                  "label": unquote(request.headers.get("x-filename", "上传图片"))[:150],
                  "created_at": now_iso()}
        # 重新读取作品，避免慢上传覆盖同时修改的稿件、路线或任务。
        with st.transaction() as connection:
            work = _work(work_id)
            work["wan_image"] = source
            st.insert_row(connection, "works", work_id, work)
        return _view(source)
    except (TalkingHeadBlocked, TalkingHeadRejected) as error:
        _error(error.code, error.message, 409 if isinstance(error, TalkingHeadBlocked) else 422)
    finally:
        temporary.unlink(missing_ok=True)


@router.post("/api/v1/works/{work_id}/wan/videos/{video_id}/confirm")
def confirm_native_video(work_id: str, video_id: str, payload: dict):
    from .digital_human.wan_native import confirm_and_deliver
    _work(work_id)
    try:
        return confirm_and_deliver(work_id, video_id, actor_id=payload.get("actor_id", ""),
                                   review_ack=payload.get("review_ack") is True)
    except (TalkingHeadBlocked, TalkingHeadRejected, delivery.DeliveryBlocked) as error:
        _error(error.code, error.message, 409)
