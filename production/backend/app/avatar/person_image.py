"""新人物主图生成（GPT Image 2.5 Sunburst 中转，文生图）。

能力边界与红线（改动前请先读，勿把这里当通用"画图的接口"）：

1. **只能用于"全新人物候选"。** 该中转实测忽略 mask、整图重绘人脸（每次脸型/五官都被重画），
   用来做同一人物换服装或换背景必然产生身份漂移，不满足 AC-AV-02。
   同一人物换装换景走 `app/avatar/compose.py` 的确定性合成，不走这里。
2. **出图 ≠ 合格。** 生成的图登记为 hero 资产，但 `approval_state` 保持 pending；
   身份 `freeze_identity` 仍然只由人执行（选择权与审批权分离）。
3. **权属登记为 unverified。** 证据字段写明"依生成服务条款、未取得书面法律意见"，
   登记保留来源链，但生产准入会被 `licensing.require` 挡住——登记不等于获准生产。
4. **两类失败语义，不合并**：`PersonImageRejected` 是请求不成立（改输入，HTTP 422）；
   `PersonImageBlocked` 是当前不可执行（等条件或重试，HTTP 409 + retryable）。

网络层（`httpx_transport`）与业务层分离，测试用假 transport，无需真密钥、不产生费用。
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import time
import uuid
from functools import wraps
from io import BytesIO
from typing import Any, Callable, Dict, List, Optional

PROVIDER = "gpt-image-2"
DEFAULT_MODEL_ENV = "AVATAR_STUDIO_GPTIMAGE_MODEL"
# Provider key 保持 gpt-image-2 以兼容既有资产和历史运行记录；新请求默认使用
# GPT Image 2.5 Sunburst。可通过 AVATAR_STUDIO_GPTIMAGE_MODEL 显式切换到 Flare
# 或中转已开放的其它兼容模型。
DEFAULT_MODEL = "gpt-image-2.5-sunburst"
KEY_ENV = "AVATAR_STUDIO_GPTIMAGE_API_KEY"
BASE_ENV = "AVATAR_STUDIO_GPTIMAGE_BASE_URL"
GENERATIONS_PATH = "/v1/images/generations"
EDITS_PATH = "/v1/images/edits"
DEFAULT_TIMEOUT_S = 300

# 9:16 竖版是本项目人物主图的既定构图（既有人物 B 主图为 941×1672）；
# 尺寸白名单而不是任意值：中转会对陌生尺寸重构构图。
ALLOWED_SIZES = ("1024x1536", "1536x1024", "1024x1024")
DEFAULT_SIZE = "1024x1536"
MAX_COUNT = 4

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(os.path.dirname(_HERE))
PRODUCTION_ROOT = os.path.dirname(_BACKEND)
GENERATED_ASSETS_REL = "assets/生成候选"
WORKSPACE_ENV = "AVATAR_STUDIO_WORKSPACE"
_DEFAULT_WORKSPACE_ROOT = os.path.dirname(PRODUCTION_ROOT)


def workspace_root() -> str:
    """工作区根目录。`AVATAR_STUDIO_WORKSPACE` 仅用于把测试产物导向临时目录。"""
    env = os.environ.get(WORKSPACE_ENV, "").strip()
    return os.path.abspath(env) if env else _DEFAULT_WORKSPACE_ROOT

STYLE_SUFFIX = (
    "写实摄影风格，竖构图半身人像，正面视角，柔和均匀的棚拍光线，"
    "颈部与发丝边缘自然，背景干净无杂物，皮肤质感真实不过度磨皮。"
)
NEGATIVE_NOTE = "不要添加文字与水印，不要夸张妆容与配饰，不要变形手指，不要多人同框"


class PersonImageRejected(Exception):
    """请求不成立：参数或提示词本身有问题，改输入才可能成功（→ HTTP 422）。"""

    def __init__(self, code: str, message: str, detail: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail or {}


class PersonImageBlocked(Exception):
    """当前不可执行：缺凭据、网络不通、远端报错或未回图（→ HTTP 409）。"""

    def __init__(self, code: str, message: str, retryable: bool = True,
                 detail: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.detail = detail or {}


def request_digest(payload: Dict[str, Any]) -> str:
    """请求摘要：同一份入参必然得到同一个摘要，用于把产出的图追溯到这次请求。"""
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def compose_prompt(*, gender: str = "", age: str = "", appearance: str = "",
                   free_text: str = "") -> str:
    """把表单碎片拼成一句完整提示词。空输入不兜底编人设，直接让上层拒绝。"""
    parts = [p.strip() for p in (gender, age, appearance, free_text) if p and p.strip()]
    if not parts:
        raise PersonImageRejected(
            "IMAGE_PROMPT_EMPTY",
            "缺少人物描述：性别、年龄感、外貌关键词至少要填一项",
        )
    head = "，".join(parts[:2])
    tail = "，".join(parts[2:])
    return f"{head}的人物，{tail}。{STYLE_SUFFIX}".replace("。。", "。")


def compose_reference_prompt(*, appearance: str = "", free_text: str = "") -> str:
    details = "，".join(p.strip() for p in (appearance, free_text) if p and p.strip())
    return ("以参考图片中的人物为基础，优先保留其主要面部特征、发型与年龄感，"
            "生成为自然专业的主播形象。" + (details + "。" if details else "") + STYLE_SUFFIX)


def build_request(*, prompt: str, size: str = DEFAULT_SIZE, n: int = 1,
                  model: Optional[str] = None, reference_sha256: str = "",
                  reference_uri: str = "") -> Dict[str, Any]:
    """构造生成请求。校验只在这里做，网络层不再重复。"""
    if not prompt or not prompt.strip():
        raise PersonImageRejected("IMAGE_PROMPT_EMPTY", "提示词为空，无法生成主图")
    if size not in ALLOWED_SIZES:
        raise PersonImageRejected(
            "IMAGE_SIZE_UNSUPPORTED",
            f"不支持的尺寸 {size}；可用：{'、'.join(ALLOWED_SIZES)}",
            {"size": size, "allowed": list(ALLOWED_SIZES)},
        )
    if not isinstance(n, int) or n < 1 or n > MAX_COUNT:
        raise PersonImageRejected(
            "IMAGE_COUNT_OUT_OF_RANGE",
            f"单次生成张数需在 1–{MAX_COUNT} 之间（当前 {n}）",
        )
    request = {
        "provider": PROVIDER,
        "endpoint": EDITS_PATH if reference_sha256 else GENERATIONS_PATH,
        "model": model or os.environ.get(DEFAULT_MODEL_ENV, "") or DEFAULT_MODEL,
        "prompt": prompt,
        "negative_note": NEGATIVE_NOTE,
        "size": size,
        "n": n,
    }
    if reference_sha256:
        request.update(reference_sha256=reference_sha256, reference_uri=reference_uri)
    return request


def httpx_transport(request: Dict[str, Any], timeout: int = DEFAULT_TIMEOUT_S) -> Dict[str, Any]:
    """真实调用。缺凭据属于"当前不可执行"，在触网之前就拦住，不做 silent fallback。"""
    api_key = os.environ.get(KEY_ENV, "").strip()
    base_url = os.environ.get(BASE_ENV, "").strip().rstrip("/")
    if not api_key or not base_url:
        raise PersonImageBlocked(
            "IMAGE_PROVIDER_NOT_CONFIGURED",
            "图像生成未配置：缺少中转 API Key 或 base_url，不触网、不猜测默认值",
            retryable=False,
        )

    import httpx

    endpoint = request["endpoint"]
    if base_url.endswith("/v1") and endpoint.startswith("/v1/"):
        endpoint = endpoint[3:]
    url = f"{base_url}{endpoint}"
    payload = {
        "model": request["model"],
        "prompt": f"{request['prompt']}。{request['negative_note']}",
        "size": request["size"],
        # n 必须是整数：中转 JSON 体里给字符串会 400 invalid n field type
        # （图编辑接口走 multipart/form-data 才接受字符串，两个端点不一样）。
        "n": request["n"],
    }
    headers = {"Authorization": f"Bearer {api_key}"}
    try:
        with httpx.Client(timeout=timeout) as client:
            if request["endpoint"] == EDITS_PATH:
                files = {"image": ("reference.png", request["image_bytes"], "image/png")}
                data = {**payload, "n": str(payload["n"])}
                resp = client.post(url, data=data, files=files, headers=headers)
            else:
                resp = client.post(url, json=payload, headers={**headers, "Content-Type": "application/json"})
    except Exception as exc:  # 网络类故障不重试计费动作，交给调用方决定是否重来
        raise PersonImageBlocked(
            "IMAGE_PROVIDER_UNREACHABLE",
            f"调用图像生成服务失败：{type(exc).__name__}（未取得结论，未重试）",
            retryable=True,
            detail={"stage": "generation", "exception_type": type(exc).__name__},
        ) from exc

    try:
        body = resp.json()
    except Exception:
        body = None
    if resp.status_code != 200:
        raise PersonImageBlocked(
            "IMAGE_PROVIDER_HTTP_ERROR",
            f"图像生成服务返回 HTTP {resp.status_code}，未自动重试",
            retryable=resp.status_code in (408, 429, 500, 502, 503, 504),
            detail={"http_status": resp.status_code},
        )
    return {"http_status": resp.status_code, "body": body}


def _extract_bytes(item: Dict[str, Any], timeout: int = DEFAULT_TIMEOUT_S) -> bytes:
    if not isinstance(item, dict):
        raise PersonImageBlocked("IMAGE_PROVIDER_RESPONSE_INVALID", "图片条目结构无效")
    if item.get("b64_json"):
        try:
            return base64.b64decode(item["b64_json"], validate=True)
        except (binascii.Error, ValueError, TypeError):
            raise PersonImageBlocked("IMAGE_PROVIDER_IMAGE_INVALID", "图片编码无效，未登记产物") from None
    url = item.get("url")
    if not url:
        raise PersonImageBlocked(
            "IMAGE_PROVIDER_NO_IMAGE",
            "图像生成服务未返回图片数据（既无 b64_json 也无 url），不得以占位图顶替",
            retryable=True,
        )
    import httpx

    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.get(url)
    except Exception as exc:
        raise PersonImageBlocked(
            "IMAGE_PROVIDER_UNREACHABLE",
            f"下载生成结果失败：{type(exc).__name__}",
            retryable=True,
            detail={"stage": "download", "exception_type": type(exc).__name__},
        ) from exc
    if resp.status_code != 200:
        raise PersonImageBlocked(
            "IMAGE_PROVIDER_HTTP_ERROR",
            f"下载生成结果返回 HTTP {resp.status_code}",
            retryable=True,
            detail={"stage": "download", "http_status": resp.status_code},
        )
    return resp.content


def generate(*, prompt: str, size: str = DEFAULT_SIZE, n: int = 1,
             model: Optional[str] = None, transport: Optional[Callable[..., Dict[str, Any]]] = None,
             timeout: int = DEFAULT_TIMEOUT_S, reference_image_bytes: Optional[bytes] = None,
             reference_sha256: str = "", reference_uri: str = "",
             on_response: Optional[Callable] = None) -> Dict[str, Any]:
    """生成主图并返回字节与证据。transport 可注入：测试与真实调用走同一条路径。"""
    if reference_image_bytes is not None:
        actual_hash = "sha256:" + hashlib.sha256(reference_image_bytes).hexdigest()
        if not reference_image_bytes or reference_sha256 != actual_hash:
            raise PersonImageRejected("IMAGE_REFERENCE_HASH_MISMATCH", "参考图片哈希不匹配")
    elif reference_sha256:
        raise PersonImageRejected("IMAGE_REFERENCE_MISSING", "缺少参考图片内容")
    request = build_request(prompt=prompt, size=size, n=n, model=model,
                            reference_sha256=reference_sha256, reference_uri=reference_uri)
    xport = transport or httpx_transport
    transport_request = dict(request, image_bytes=reference_image_bytes) if reference_image_bytes is not None else request
    resp = xport(transport_request, timeout=timeout)
    return parse_generation_response(request, resp, timeout=timeout, on_response=on_response)


def parse_generation_response(request: Dict[str, Any], resp: Dict[str, Any], *,
                              timeout: int = DEFAULT_TIMEOUT_S,
                              on_response: Optional[Callable] = None) -> Dict[str, Any]:
    """Parse an existing response; recovery never submits a generation request."""
    digest = request_digest(request)

    # 状态码校验放在业务层而不是只放在 httpx_transport 里：
    # 通道可以替换（测试假通道、将来的其他 SDK），"HTTP 非 200 就是没成功"这条不能跟着丢。
    # 只在通道内部校验的后果是：换个通道后 5xx 会一路走到"响应里没有图片条目"，
    # 把服务端故障说成"模型没给图"，误导排查方向。
    if not isinstance(resp, dict):
        raise PersonImageBlocked("IMAGE_PROVIDER_RESPONSE_INVALID", "图像生成响应结构无效")
    try:
        status = int(resp.get("http_status", 200) or 0)
    except (ValueError, TypeError):
        raise PersonImageBlocked("IMAGE_PROVIDER_RESPONSE_INVALID", "图像生成响应状态无效") from None
    if status != 200:
        raise PersonImageBlocked(
            "IMAGE_PROVIDER_HTTP_ERROR",
            f"图像生成服务返回 HTTP {status}，未自动重试",
            retryable=status in (408, 429, 500, 502, 503, 504),
            detail={"http_status": status},
        )

    body = resp.get("body")
    if not isinstance(body, dict):
        raise PersonImageBlocked("IMAGE_PROVIDER_RESPONSE_INVALID", "图像生成响应不是 JSON 对象")
    items = body.get("data") or []
    if not isinstance(items, list):
        raise PersonImageBlocked("IMAGE_PROVIDER_RESPONSE_INVALID", "图像生成响应缺少图片列表")
    if not items:
        raise PersonImageBlocked(
            "IMAGE_PROVIDER_NO_IMAGE",
            "图像生成服务响应中没有图片条目，不得以占位图顶替",
            retryable=True,
        )
    if on_response is not None:
        on_response(request, resp)
    image_bytes = _extract_bytes(items[0], timeout=timeout)
    if not image_bytes:
        raise PersonImageBlocked(
            "IMAGE_PROVIDER_EMPTY_IMAGE",
            "取到的图片为空字节，不得当作成功产物",
            retryable=True,
        )
    try:
        width, height = _image_size(image_bytes)
    except Exception:
        raise PersonImageBlocked("IMAGE_PROVIDER_IMAGE_INVALID", "返回字节不是完整图片，未登记产物") from None
    return {
        "request": request,
        "request_digest": digest,
        "image_bytes": image_bytes,
        "width": width,
        "height": height,
        "usage": (resp.get("body") or {}).get("usage") or {},
        "response_id": (resp.get("body") or {}).get("id") or (resp.get("body") or {}).get("created"),
    }


def _image_size(image_bytes: bytes) -> tuple:
    """用 Pillow 读真实尺寸，不信任响应体里的声明。"""
    from PIL import Image

    with Image.open(BytesIO(image_bytes)) as im:
        im.load()
        return int(im.width), int(im.height)


# ---------- 本地媒体读取 ----------

MEDIA_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp", ".wav", ".mp3", ".mp4")


def resolve_media_path(uri: str) -> Optional[str]:
    """把工作区相对路径解析成绝对路径；不在工作区内或类型不受支持则返回 None。

    这是给前端看图用的只读通道，不是文件服务器：`..`、绝对路径、工作区外一律拒绝。
    """
    if not uri or uri.startswith("/") or uri.startswith("\\") or ":" in uri.split("/")[0]:
        return None
    if ".." in uri.replace("\\", "/").split("/"):
        return None
    if os.path.splitext(uri)[1].lower() not in MEDIA_SUFFIXES:
        return None
    root = os.path.realpath(workspace_root())
    target = os.path.realpath(os.path.join(root, uri))
    if target != root and not target.startswith(root + os.sep):
        return None
    return target


def save_generated(*, identity_id: str, asset_id: str, image_bytes: bytes) -> Dict[str, str]:
    """落盘并返回工作区相对路径。相对路径与既有 hero 资产一致（工作区根起算）。"""
    rel_dir = f"{GENERATED_ASSETS_REL}/{identity_id}"
    abs_dir = os.path.join(workspace_root(), rel_dir)
    os.makedirs(abs_dir, exist_ok=True)
    name = f"{asset_id}.png"
    abs_path = os.path.join(abs_dir, name)
    with open(abs_path, "wb") as f:
        f.write(image_bytes)
    return {
        "storage_uri": f"{rel_dir}/{name}",
        "file_hash": "sha256:" + hashlib.sha256(image_bytes).hexdigest(),
        "abs_path": abs_path,
    }


def register_generated_license(*, identity_name: str, request: Dict[str, Any],
                               request_digest: str, response_id: Any = "") -> Dict[str, Any]:
    """生成图的权属登记。

    故意登记为 unverified：AI 生成物的商用范围取决于生成服务条款，本项目没有取得书面
    法律意见，因此不做法律结论，只把来源链留下。生产准入由 licensing.require 在别处挡。
    """
    from . import licensing

    return licensing.register(
        subject=f"{identity_name} 生成主图来源（AI {'图生图' if request.get('reference_sha256') else '文生图'}）",
        kind="image_source",
        scope="unverified",
        evidence=(
            f"provider={request.get('provider')} model={request.get('model')} "
            f"endpoint={request.get('endpoint')} size={request.get('size')} "
            f"request_digest={request_digest} response_id={response_id} "
            f"reference_sha256={request.get('reference_sha256', '')} reference_uri={request.get('reference_uri', '')}"
        ),
        verified_by="",
        note="依生成服务条款使用，未取得书面法律意见；商用范围待核验，核验前不进入生产",
    )


def _protect_existing_identity(function):
    @wraps(function)
    def guarded(*args, **kwargs):
        from .. import deletion, storage as st

        identity_id = kwargs.get("identity_id")
        if not identity_id:
            return function(*args, **kwargs)
        key, owner = "identity_image:" + identity_id, uuid.uuid4().hex
        with st.transaction() as conn:
            try:
                identity = deletion.require_identity(identity_id)
            except deletion.DeletionRejected as error:
                if error.code == "IDENTITY_NOT_FOUND":
                    raise PersonImageRejected(error.code, error.message) from error
                raise
            if identity.get("frozen"):
                raise PersonImageRejected("IDENTITY_FROZEN", "身份已冻结，不得替换主图；换人必须显式产生新版本")
            lock = st.get("resource_locks", key) or {}
            if lock.get("expires_at", 0) > time.time():
                raise PersonImageBlocked("IDENTITY_IMAGE_BUSY", "人物主图正在生成，请等待原请求返回", retryable=False)
            st.insert_row(conn, "resource_locks", key, {"id": key, "owner": owner,
                "identity_id": identity_id, "expires_at": time.time() + int(kwargs.get("timeout", DEFAULT_TIMEOUT_S)) * 2 + 120})
        try:
            return function(*args, **kwargs)
        finally:
            with st.transaction() as conn:
                lock = st.get("resource_locks", key) or {}
                if lock.get("owner") == owner:
                    conn.execute("DELETE FROM resource_locks WHERE id=?", [key])
    return guarded


@_protect_existing_identity
def generate_candidate_hero(*, identity_id: Optional[str] = None, gender: str = "", age: str = "",
                            appearance: str = "", free_text: str = "", size: str = DEFAULT_SIZE,
                            n: int = 1, model: Optional[str] = None,
                            transport: Optional[Callable[..., Dict[str, Any]]] = None,
                            timeout: int = DEFAULT_TIMEOUT_S, reference_image_bytes: Optional[bytes] = None,
                            reference_sha256: str = "", reference_uri: str = "",
                            request_id: str = "") -> Dict[str, Any]:
    """给一个候选人物真的生成一张主图，并把它登记为待审的 hero 资产。

    · 身份本身**不冻结**：出图不等于定版，这里是让人有东西可看、可对照、可决定是否定版。
    · 不传 `identity_id` 时先建立候选身份；**先出图再建身份**是刻意的顺序——
      出图失败则什么都不留下，不会留一个"空壳候选"让人误以为已经建好。
    """
    from .. import storage as st
    from .registry import create_identity_candidate, get_identity, register_asset

    if reference_image_bytes is not None:
        if identity_id:
            raise PersonImageRejected("IMAGE_REFERENCE_NEW_IDENTITY_ONLY", "图生图只能创建新主播候选")
        prompt = compose_reference_prompt(appearance=appearance, free_text=free_text)
    else:
        prompt = compose_prompt(gender=gender, age=age, appearance=appearance, free_text=free_text)
    produced = generate(prompt=prompt, size=size, n=n, model=model,
                        transport=transport, timeout=timeout, reference_image_bytes=reference_image_bytes,
                        reference_sha256=reference_sha256, reference_uri=reference_uri)
    request = produced["request"]
    digest = produced["request_digest"]

    if identity_id:
        ident = get_identity(identity_id)
        if not ident:
            raise PersonImageRejected("IDENTITY_NOT_FOUND", f"人物不存在：{identity_id}")
        if ident.get("frozen"):
            # 冻结即"这张脸已定版"，换主图等于换人，必须显式走 bump_identity_version
            raise PersonImageRejected("IDENTITY_FROZEN",
                                      "身份已冻结，不得替换主图；换人必须显式产生新版本")
    else:
        persona = "；".join([p for p in (appearance, free_text) if p and p.strip()]) or "未填写描述"
        ident = create_identity_candidate(
            name=f"候选 {gender or '未指定'}·{age or '成年'}",
            persona=persona,
            keywords={"gender": gender, "age": age, "appearance": appearance,
                      "free_text": free_text, "size": size},
            note="候选：主图已由 AI 生成，待身份审核后定版",
            # 事实标记：这张脸是凭空生成的，不存在多角度真值。
            # 只是标记，不放宽任何要求——仍须人声明政策并完成审核才能定版。
            origin="generated",
        )
        identity_id = ident["id"]

    asset_id = st.new_id("img")
    saved = save_generated(identity_id=identity_id, asset_id=asset_id,
                           image_bytes=produced["image_bytes"])
    license_rec = register_generated_license(
        identity_name=ident.get("name", identity_id),
        request=request, request_digest=digest, response_id=produced.get("response_id"),
    )
    asset = register_asset(
        identity_id=identity_id,
        kind="hero",
        asset_id=asset_id,
        storage_uri=saved["storage_uri"],
        file_hash=saved["file_hash"],
        width=produced.get("width"),
        height=produced.get("height"),
        license_id=license_rec["id"],
        source_evidence=(
            f"provider={request.get('provider')} model={request.get('model')} "
            f" size={request.get('size')} request_digest={digest} "
            f"response_id={produced.get('response_id')} "
            f"reference_sha256={request.get('reference_sha256', '')} reference_uri={request.get('reference_uri', '')} "
            f"request_id={request_id}"
        ),
        note="AI 生成产出，未经身份一致性检测；同一人物不得据此换装换景",
    )

    # 主图必须挂回身份：否则资产只是孤儿，readiness 永远报"缺少定妆原片资产"，
    # 冻结时也只能靠调用方手动补 asset_id——链路一旦断开，故障现象离根因很远。
    previous_hero = ident.get("hero_asset_id") or ""
    ident["hero_asset_id"] = asset_id
    ident["license_id"] = license_rec["id"]
    ident["note"] = (
        "主图已由 AI 生成并登记为待审资产，须人工审核后才能定版"
        + (f"；已替换此前未定版的主图 {previous_hero}" if previous_hero else "")
    )
    st.put("identities", identity_id, ident)

    return {
        "identity": ident,
        "asset": asset,
        "license": license_rec,
        "prompt": prompt,
        "request": request,
        "request_digest": digest,
        "usage": produced.get("usage") or {},
        "response_id": produced.get("response_id"),
    }
