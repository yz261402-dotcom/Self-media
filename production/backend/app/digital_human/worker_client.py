"""M2 远端 GPU Worker 的客户端运输层。

补的是 providers.build_liveportrait_request / build_musetalk_request 与
urllib_transport 之间**缺的那一段**：

    build_*_request() -> {"stage","model","args"}        ← 没有 url！
    urllib_transport(req) -> 读 req["url"]               ← 因此会 KeyError

也就是说，在补这层之前，「把 M2 的 request 直接喂给 urllib_transport」是跑不通的，
而 HTTP 端点又从来没有传过 transport，所以 M2 从界面发起必然是
M2_TRANSPORT_UNAVAILABLE——这与"M2 有没有部署好"无关，纯粹是接线没做。

本模块负责：
  1. 把 stage/model/args 翻译成 POST /v1/jobs 的真实 HTTP 请求；
  2. 提交后轮询 GET /v1/jobs/{id} 直到终态（LivePortrait 实测 53s、
     MuseTalk 182s，同步等待一次调用不合理，必须轮询）；
  3. 让 worker 的 output_url 也能下载（自动补 Bearer token）。
     ——download_and_verify 传进来的只有 {"method","url"}，不加 token 会被 401 拒。

**判定纪律**（与 provider 层一致）：
  - 未配置 worker 地址 → TalkingHeadBlocked（等配置，不是改输入）；
  - 超时未完成 → 报 running/unknown，**绝不改判失败**；作业还在远端跑着，
    排队不是失败，这是本项目既有的红线。
"""

from __future__ import annotations

import json
import os
import hashlib
import re
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from .errors import TalkingHeadBlocked
from .providers import M2_TOKEN_ENV, M2_URL_ENV, urllib_transport
from .. import storage as st

DEFAULT_POLL_INTERVAL_SEC = 5.0
DEFAULT_MAX_WAIT_SEC = 1800.0  # 30 分钟：覆盖实测 182s 并留足排队与冷启动
MAX_WAIT_ENV = "AVATAR_STUDIO_M2_MAX_WAIT_SEC"
POLL_INTERVAL_ENV = "AVATAR_STUDIO_M2_POLL_INTERVAL_SEC"

_SUBMIT_PATH = "/v1/jobs"
_ASSET_CAPABILITIES_PATH = "/v1/healthz"
_LOCAL_MOTION_RE = re.compile(r"^assets/动作参考/用户上传/(?:mup_[0-9a-f]{12}|[0-9a-f]{32})/动作参考\.mp4$")
_MAX_LOCAL_MOTION_BYTES = 100 * 1024 * 1024
_INPUT_FIELDS = {
    "motion": (("source_image_url", "source_hash"), ("motion_ref_url", "motion_hash")),
    "lipsync": (("plate_url", "plate_hash"), ("audio_url", "audio_hash")),
    # 字幕链路（asr/align）复用同一套资产直传：音频直接交给 worker，
    # 由它按 sha256 落到作业私有目录并解析成 file:// 路径。
    # 这样对齐就不必依赖"先把音频发到公开对象存储"那条路——
    # 那条路需要额外一套存储凭据，缺了它整条字幕链路会直接断掉。
    "asr": (("audio_url", "audio_hash"),),
    "align": (("audio_url", "audio_hash"),),
}
_INPUT_EXTENSIONS = {
    "source_image_url": {"png", "jpg", "jpeg", "webp"},
    "motion_ref_url": {"mp4"}, "plate_url": {"mp4"}, "audio_url": {"wav", "mp3"},
}
_ASSET_MIME = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "webp": "image/webp",
               "mp4": "video/mp4", "wav": "audio/wav", "mp3": "audio/mpeg"}
# parse_m2_response 认识的状态词：succeeded / failed / queued / running / error
_TERMINAL = ("succeeded", "failed")


def worker_url() -> str:
    return (os.environ.get(M2_URL_ENV) or "").strip().rstrip("/")


def worker_token() -> str:
    return (os.environ.get(M2_TOKEN_ENV) or "").strip()


def configured() -> bool:
    return bool(worker_url())


def _auth_headers(token: str) -> Dict[str, str]:
    return {"Authorization": f"Bearer {token}"} if token else {}


def build_job_request(stage_request: Dict[str, Any], *, base_url: str,
                      token: str = "", allowed_stages=("motion", "lipsync")) -> Dict[str, Any]:
    """把 build_*_request 的产物翻译成真实 HTTP 请求。

    stage_request 里缺关键的 stage/args 时不补默认值——那是请求不成立，
    应该在 needs_repair 之前就暴露出来。

    `allowed_stages` 默认仍是 M2 两阶段；字幕链路（asr/align）复用同一个
    worker 程序与同一套轮询语义，只是阶段不同，因此显式传入而不是改默认值，
    免得把"M2 只认两个阶段"这条既有约束悄悄放宽。
    """
    stage = str(stage_request.get("stage") or "").strip()
    args = stage_request.get("args")
    if stage not in allowed_stages:
        raise TalkingHeadBlocked(
            "M2_REQUEST_SHAPE_INVALID",
            f"无法构造 worker 请求：stage={stage!r} 不在 {tuple(allowed_stages)}；"
            "这说明 build_*_request 的输出形状与 worker 契约不一致",
            {"stage": stage})
    if not isinstance(args, dict):
        raise TalkingHeadBlocked(
            "M2_REQUEST_SHAPE_INVALID",
            "无法构造 worker 请求：args 缺失或不是对象",
            {"args_type": type(args).__name__})

    body = json.dumps({"stage": stage,
                       "model": stage_request.get("model") or "",
                       "args": args},
                      ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    headers.update(_auth_headers(token))
    return {"method": "POST", "url": f"{base_url}{_SUBMIT_PATH}",
            "headers": headers, "body": body}


def build_status_request(base_url: str, job_id: str, *, token: str = "") -> Dict[str, Any]:
    url = f"{base_url}{_SUBMIT_PATH}/{job_id}"
    headers = {"Accept": "application/json"}
    headers.update(_auth_headers(token))
    return {"method": "GET", "url": url, "headers": headers}


def _inject_token(request: Dict[str, Any], base_url: str, token: str) -> Dict[str, Any]:
    """给指向 worker 的请求补鉴权。外部对象存储 URL 不动——token 不外泄。"""
    if not token:
        return request
    url = str(request.get("url") or "")
    headers = dict(request.get("headers") or {})
    if url.startswith(base_url) and not headers.get("Authorization"):
        headers["Authorization"] = f"Bearer {token}"
        request = dict(request, headers=headers)
    return request


def _workspace_root() -> Path:
    configured = os.environ.get("AVATAR_STUDIO_WORKSPACE", "").strip()
    # project root: production/backend/app/digital_human/worker_client.py
    return Path(configured).expanduser().resolve() if configured else Path(__file__).resolve().parents[4]


def _local_motion_path(uri: str) -> Optional[Path]:
    """只承认上传器保存的动作路径；禁止把任意 Windows 路径交给远端。"""
    if not _LOCAL_MOTION_RE.fullmatch(uri):
        return None
    root = _workspace_root()
    path = (root / Path(uri)).resolve()
    try:
        path.relative_to(root / "assets" / "动作参考" / "用户上传")
    except ValueError:
        return None
    return path if path.is_file() else None


def _registered_input_path(field: str, uri: str, digest: str) -> Path:
    """已登记、真实且哈希匹配，并且只读 workspace assets/runtime 中对应媒体。"""
    table = {"source_image_url": "avatar_assets", "plate_url": "videos", "audio_url": "audios"}[field]
    registered = any(
        row.get("storage_uri") == uri and row.get("file_hash") == digest and not row.get("is_mock")
        and (field != "source_image_url" or row.get("kind") in ("hero", "variant"))
        for row in st.all_rows(table)
    )
    if not registered:
        raise TalkingHeadBlocked("M2_LOCAL_INPUT_UNREGISTERED", "本地推理输入未登记为对应真实资产，或哈希/用途不符；未上传",
                                 {"field": field})
    root = _workspace_root()
    runtime = root / "production" / "backend" / "runtime"
    normalized = uri.replace("\\", "/")
    # 接受记录中的 workspace 绝对路径，但仍必须落在下方媒体白名单目录。
    raw = Path(normalized)
    if not raw.is_absolute() and (":" in normalized or ".." in raw.parts):
        raise TalkingHeadBlocked("M2_LOCAL_INPUT_PATH_INVALID", "本地推理输入路径越界；未上传", {"field": field})
    path = raw if raw.is_absolute() else (
        runtime / raw if normalized.startswith(("video/", "audio/")) else root / raw)
    path = path.resolve()
    allowed = [root / "assets"]
    if field == "plate_url":
        allowed.append(runtime / "video")
    elif field == "audio_url":
        allowed.append(runtime / "audio")
    if not any(path.is_relative_to(base) for base in allowed) or not path.is_file():
        raise TalkingHeadBlocked("M2_LOCAL_INPUT_PATH_INVALID", "本地推理输入不在允许的媒体目录中，或文件已缺失；未上传",
                                 {"field": field})
    if path.suffix.lower().lstrip(".") not in _INPUT_EXTENSIONS[field]:
        raise TalkingHeadBlocked("M2_LOCAL_INPUT_FORMAT_INVALID", "本地推理输入格式与用途不匹配；未上传", {"field": field})
    return path


def _asset_upload_request(digest: str, *, extension: str = "mp4", base_url: str, token: str, body: bytes) -> Dict[str, Any]:
    return {"method": "PUT", "url": f"{base_url}/v1/assets/{digest}.{extension}",
            "headers": {"Authorization": f"Bearer {token}", "Content-Type": _ASSET_MIME[extension],
                        "Content-Length": str(len(body))}, "body": body}


def _prepare_local_inputs(request: Dict[str, Any], *, base_url: str, token: str,
                            send: Callable[[Dict[str, Any]], Dict[str, Any]]) -> Dict[str, Any]:
    """将已登记的本地图像、动作、底片、音频上传到同一个受控 Worker，再提交 job。

    HTTP URL 保持原样；本地路径必须来自上传器约定目录且哈希与作业声明一致。
    Worker v1 无资产协议时明确阻断，不回退到把 Windows 路径塞进远端。
    """
    fields = _INPUT_FIELDS.get(request.get("stage"), ())
    if not fields:
        return request
    args = request.get("args")
    if not isinstance(args, dict):
        return request
    pending = []
    # 所有输入先验证；第二份输入有误时，不先上传第一份。
    for field, hash_field in fields:
        uri = str(args.get(field) or "")
        if uri.startswith(("http://", "https://")):
            continue
        declared = str(args.get(hash_field) or "")
        motion_input = field == "motion_ref_url"
        prefix = "M2_LOCAL_MOTION" if motion_input else "M2_LOCAL_INPUT"
        if motion_input:
            path = _local_motion_path(uri)
            if path is None:
                raise TalkingHeadBlocked(prefix + "_PATH_INVALID", "动作参考必须来自受控用户上传目录；未发送到远端")
        else:
            path = _registered_input_path(field, uri, declared)
        try:
            if path.stat().st_size >= _MAX_LOCAL_MOTION_BYTES:
                raise TalkingHeadBlocked(prefix + "_TOO_LARGE", "推理素材达到 100MiB 上传上限；未发送到远端")
            with path.open("rb") as fh:
                body = fh.read(_MAX_LOCAL_MOTION_BYTES)
        except OSError as exc:
            raise TalkingHeadBlocked(prefix + "_UNREADABLE", "推理素材暂时无法读取；未发送到远端") from exc
        if not body or len(body) >= _MAX_LOCAL_MOTION_BYTES:
            raise TalkingHeadBlocked(prefix + "_TOO_LARGE", "推理素材为空或达到 100MiB 上限；未发送到远端")
        actual = "sha256:" + hashlib.sha256(body).hexdigest()
        if declared != actual:
            raise TalkingHeadBlocked(prefix + "_HASH_MISMATCH", "推理素材已变化，与登记哈希不一致；未提交远端作业",
                                     {"field": field})
        pending.append((field, path.suffix.lower().lstrip("."), actual, body))
    if not pending:
        return request
    if not token:
        raise TalkingHeadBlocked("M2_ASSET_AUTH_REQUIRED", "素材上传需要 worker 鉴权令牌；未发送到远端")
    # Capability probe prevents an old worker from silently accepting an unusable URI.
    cap = send(_inject_token({"method": "GET", "url": f"{base_url}{_ASSET_CAPABILITIES_PATH}"}, base_url, token))
    status = cap.get("status_code")
    doc = _doc_of(cap) or {}
    if status is None:
        raise TalkingHeadBlocked("M2_ASSET_TRANSFER_FAILED", "无法连接 worker 能力探测，未发送动作文件")
    if status >= 400 and status not in (404, 405, 501):
        raise TalkingHeadBlocked("M2_ASSET_TRANSFER_FAILED", f"worker 能力探测失败（HTTP {status}）；未发送动作文件")
    capabilities = doc.get("capabilities")
    if status >= 400 or str(doc.get("version") or "") != "2" or not isinstance(capabilities, dict) or capabilities.get("asset_upload") != "sha256-v1":
        raise TalkingHeadBlocked("ACTION_UPGRADE_REQUIRED",
                                 "远端 worker 不支持安全动作文件上传，请先升级到资产协议 v2；未提交作业",
                                 {"status_code": status, "worker_version": doc.get("version")})
    extensions = capabilities.get("asset_extensions", ["mp4"])
    if not isinstance(extensions, list) or any(ext not in extensions for _, ext, _, _ in pending):
        raise TalkingHeadBlocked("ACTION_UPGRADE_REQUIRED", "远端 worker 尚不支持本次图像/视频/音频素材格式，请更新 Worker；未上传")
    prepared = dict(args)
    for field, extension, actual, body in pending:
        uploaded = send(_asset_upload_request(actual[7:], extension=extension, base_url=base_url, token=token, body=body))
        u_status = uploaded.get("status_code")
        u_doc = _doc_of(uploaded) or {}
        if u_status in (404, 405):
            raise TalkingHeadBlocked("ACTION_UPGRADE_REQUIRED", "远端 worker 未部署对应资产上传端点；未提交作业")
        if u_status is None or u_status >= 400:
            raise TalkingHeadBlocked("M2_ASSET_TRANSFER_FAILED", f"素材上传失败（HTTP {u_status}）；未提交作业", {"field": field})
        asset_uri = str(u_doc.get("asset_uri") or "")
        if asset_uri != f"worker-asset://{actual[7:]}.{extension}" or str(u_doc.get("file_hash") or "") != actual or u_doc.get("bytes") != len(body):
            raise TalkingHeadBlocked("M2_ASSET_RECEIPT_INVALID", "worker 返回的资产回执与文件哈希不一致；未提交作业")
        prepared[field] = asset_uri
    return dict(request, args=prepared)


def _doc_of(resp: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if isinstance(resp.get("json"), dict):
        return resp["json"]
    body = resp.get("body")
    if not body:
        return None
    if isinstance(body, str):
        body = body.encode("utf-8", "replace")
    try:
        parsed = json.loads(body.decode("utf-8"))
    except Exception:  # noqa: BLE001 - 非 JSON 由上层 parse 归类
        return None
    return parsed if isinstance(parsed, dict) else None


def transport(request: Dict[str, Any], *, base_url: Optional[str] = None,
              token: Optional[str] = None, inner: Optional[Callable] = None,
              poll_interval: Optional[float] = None,
              max_wait: Optional[float] = None,
              allowed_stages=("motion", "lipsync"),
              sleep: Callable[[float], None] = time.sleep,
              clock: Callable[[], float] = time.monotonic) -> Dict[str, Any]:
    """提交（必要时轮询）到 worker，返回最终响应（dict 形态，喂给 parse_m2_response）。

    返回体兼容 _resp_json 的两种取法：优先放 "json"，同时保留原始 body。
    """
    base = (base_url if base_url is not None else worker_url()).rstrip("/")
    tok = token if token is not None else worker_token()
    send = inner or urllib_transport

    if not base:
        # transport 存在但没有目标地址：这是"还没配好"，不是"提交被拒"。
        raise TalkingHeadBlocked(
            "M2_TRANSPORT_UNAVAILABLE",
            f"未配置 {M2_URL_ENV}：M2 推理在算力云上执行，未接通时不得假装已提交",
            {"env": M2_URL_ENV})

    # 下载请求：原样透传，只对 worker 域名补 token
    if request.get("url") and "stage" not in request:
        return send(_inject_token(request, base, tok))

    request = _prepare_local_inputs(request, base_url=base, token=tok, send=send)
    submit_req = build_job_request(request, base_url=base, token=tok,
                                   allowed_stages=allowed_stages)
    resp = send(submit_req)
    status = resp.get("status_code")
    if status is None or (status and status >= 400):
        return resp  # 网络层错误/4xx/5xx：交给 parse_m2_response 归类为 unknown

    doc = _doc_of(resp) or {}
    job_id = str(doc.get("job_id") or "")
    state = str(doc.get("state") or "").strip()
    if not job_id:
        return resp  # 没有 job_id 就无法追踪，同样交给上层判定
    if state in _TERMINAL:
        return resp

    interval = float(poll_interval if poll_interval is not None
                     else os.environ.get(POLL_INTERVAL_ENV, "") or DEFAULT_POLL_INTERVAL_SEC)
    budget = float(max_wait if max_wait is not None
                   else os.environ.get(MAX_WAIT_ENV, "") or DEFAULT_MAX_WAIT_SEC)
    started = clock()
    while True:
        remaining = budget - (clock() - started)
        if remaining <= 0:
            break
        sleep(min(interval, remaining))
        poll_resp = send(build_status_request(base, job_id, token=tok))
        p_status = poll_resp.get("status_code")
        if p_status is None:
            return poll_resp  # 网络中断：结果未知，归 unknown
        if p_status >= 400:
            return poll_resp
        poll_doc = _doc_of(poll_resp) or {}
        if str(poll_doc.get("state") or "").strip() in _TERMINAL:
            return {"status_code": 200, "body": json.dumps(
                poll_doc, ensure_ascii=False).encode("utf-8"), "json": poll_doc}

    timed_out = dict(doc, state="running", job_id=job_id, waited_seconds=round(
        clock() - started, 1),
        note=(f"轮询 {budget:.0f}s 内未到终态；作业仍在远端执行，"
              "不改判失败，可稍后继续查询"))
    return {"status_code": 200, "json": timed_out,
            "body": json.dumps(timed_out, ensure_ascii=False).encode("utf-8")}


def make_transport(*, base_url: Optional[str] = None, token: Optional[str] = None,
                   poll_interval: Optional[float] = None,
                   max_wait: Optional[float] = None) -> Callable[[Dict[str, Any]], Dict[str, Any]]:
    """给 HTTP 端点用的工厂：未配置地址时返回 None 语义由调用方处理。

    这里故意不返回一个"看起来能用"的空实现——返回 None 会让既有的
    M2_TRANSPORT_UNAVAILABLE blocked 路径生效，那是正确的行为。
    """
    if not configured() and base_url is None:
        return None  # type: ignore[return-value]

    def _t(request: Dict[str, Any]) -> Dict[str, Any]:
        return transport(request, base_url=base_url, token=token,
                         poll_interval=poll_interval, max_wait=max_wait)
    return _t
