"""avatar_studio：单用户数字人口播工作台，真实 Provider 与离线模式共用受控业务接口。"""
from __future__ import annotations

import hashlib
import os
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import ValidationError

from . import config, engine, storage as st
from . import delivery, deletion
from .workflow_api import router as workflow_router
from .avatar_draft_api import router as avatar_draft_router
from .avatar_image_api import router as avatar_image_router
from .motion_upload_api import router as motion_upload_router
from .motion_library_api import router as motion_library_router
from .voice_clone_api import router as voice_clone_router
from .wan_api import router as wan_router
from .publishing.api import router as publishing_router
from .content_api import router as content_router
from .agents.media import router as agent_media_router
from .avatar import identity_gate, licensing, motion, person_image, registry, variant_factory
from .avatar.compose import ComposeRejected
from .avatar.licensing import LicenseBlocked
from .avatar.person_image import PersonImageBlocked, PersonImageRejected
from .avatar.registry import AssetRejected, DiscardRejected
from .avatar.variant_factory import VariantBlocked
from .contracts import (
    ApiError,
    AudioAssetInfo,
    AvatarIdentity,
    AvatarVariant,
    Capabilities,
    CostEstimate,
    HERO_ONLY_LIMITATIONS,
    ErrorResponse,
    FeatureEntry,
    PublishPackage,
    QualityCheck,
    ScriptBatch,
    ScriptRepairReceipt,
    ScriptVersion,
    SelectionDecision,
    SelectionKind,
    SelectionOption,
    SelectionRequest,
    Task,
    TaskEvent,
    Work,
    WorkBrief,
    now_iso,
)
from .mockdata import MOCK_NOTE
from . import alignment as alignment_service
from .video.errors import VideoBlocked, VideoRejected
from .tts import audio as tts_audio
from .tts import gate as tts_gate
from .tts import preview as tts_preview
from .tts import provider as tts_provider
from .tts import synthesis as tts_synth
from .tts import voices as tts_voices
from .tts.errors import TtsBlocked, TtsRejected
from .digital_human import cost as dh_cost
from .digital_human import gates as dh_gates
from .digital_human import omni_driver as dh_driver
from .digital_human import plates as dh_plates
from .digital_human import providers as dh_providers
from .digital_human import queue as dh_queue
from .digital_human import routes as dh_routes
from .digital_human import segments as dh_segments
from .digital_human import submission as dh_sub
from .digital_human import videos as dh_videos
from .digital_human import worker_client as dh_worker
from .digital_human.errors import TalkingHeadBlocked, TalkingHeadRejected

# T01：模式经越级拦截后确定；未显式授权时停留在 mock，不允许跳到生产
MODE = config.resolve_mode()


@asynccontextmanager
async def lifespan(_: FastAPI):
    # 密钥仅在显式配置 AVATAR_STUDIO_SECRETS_FILE 时注入；只打印变量名，不打印值
    injected = config.bootstrap_secrets()
    if injected:
        print(f"[startup] 已注入 {len(injected)} 个供应商变量（内容不打印）")

    # 密钥注入后**必须重算模式**：上面那个模块级 MODE 是在密钥可用之前算的，
    # 此时每个供应商都还是 not_configured → _production_ready() 为假 → 一律回落 mock。
    # 不重算的后果不是"少了个标签"：`_create_task` 会把 task.mode 记成 mock，
    # 于是一次真花了钱的调用在台账里写着 mock——审计链上正好是最不该错的一处。
    # （plates/submission/_tts_dry_run 都是调用时才算，所以产出本身不受影响，
    #   受影响的只有记录与对外展示，因此这个 bug 不会自己暴露。）
    global MODE
    MODE = config.resolve_mode()
    print(f"[startup] 运行模式：{MODE}（密钥注入后重算）")
    st.init_db()
    # 音色库不能只有一条可用：把官方预置音色的中文候选补齐（幂等，不覆盖既有行）。
    # 失败不阻塞启动——音色目录缺失是"少几个选项"，起不来是整个工作台不可用。
    try:
        from .tts import catalog as tts_catalog

        cat = tts_catalog.bootstrap_catalog()
        if cat["added"]:
            print(f"[startup] 已登记 {len(cat['added'])} 条官方预置音色候选"
                  f"（目录 {cat['catalog_size']} 条，既有 {len(cat['skipped'])} 条未改动）")
        if cat["failed"]:
            print(f"[startup] 有 {len(cat['failed'])} 条音色候选登记失败（不阻塞启动）")
    except Exception as e:
        print(f"[startup] 音色目录引导未完成：{type(e).__name__}: {e}")
    from .publishing import service as publishing_service
    publishing_service.recover_pending()
    n = engine.recover_pending()
    if n:
        print(f"[startup] 已恢复调度 {n} 个未完成任务")
    # OmniHuman 分段是**串行**推进的（并发上限按账号而定，免费试用为 1），
    # 推进线程随进程消失。重启后按台账把仍有分段在飞的作品重新挂上——
    # 恢复只是续推，不会重发任何已提交的段。
    # 只在 production 起：mock/测试里不启动后台线程，避免测试出现不可控的并发状态改动。
    if MODE == "production":
        try:
            from .digital_human import omni_driver

            resumed = omni_driver.recover_pending()
            if resumed:
                print(f"[startup] 已恢复 OmniHuman 分段推进：{len(resumed)} 个作品")
        except Exception as e:  # noqa: BLE001 - 恢复失败不该让整个工作台起不来
            print(f"[startup] OmniHuman 分段推进恢复未完成：{type(e).__name__}: {e}")
    # 内容调度器：只恢复 content-v2 命名空间的流程；发送后未确认的调用一律标 unknown，绝不自动重发。
    try:
        from .content import references as content_references
        from .content import service as content_service
        from .content import tasks as content_tasks

        content_references.register_handlers()
        content_service.register_handlers()
        from .content import visual as content_visual

        content_visual.register_handlers()
        from .content import media_fetch as content_media_fetch

        content_media_fetch.register_handlers()
        recovered = content_tasks.recover_on_startup()
        if any(recovered.values()):
            print(f"[startup] 内容任务恢复：{recovered}")
        content_tasks.get_scheduler().start()
    except Exception as e:  # noqa: BLE001 - 内容调度失败不该让整个工作台起不来
        print(f"[startup] 内容调度器未启动：{type(e).__name__}: {e}")
    yield
    try:
        from .content import tasks as content_tasks

        content_tasks.get_scheduler().stop(timeout=2.0)
    except Exception:  # noqa: BLE001
        pass


app = FastAPI(
    title="avatar_studio",
    version="0.1.0",
    description="数字人口播工作台：人物、文案、声音、视频与交付",
    lifespan=lifespan,
)
app.include_router(workflow_router)
app.include_router(avatar_draft_router)
app.include_router(avatar_image_router)
app.include_router(motion_upload_router)
app.include_router(motion_library_router)
app.include_router(voice_clone_router)
app.include_router(wan_router)
app.include_router(publishing_router)
app.include_router(content_router)
app.include_router(agent_media_router)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:5173", "http://localhost:5173"],
    allow_origin_regex=r"^https?://(127\.0\.0\.1|localhost)(:\d+)?$",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def fail(code: str, message: str, status: int = 422, action: Optional[str] = None, retryable: bool = False,
         job_id: Optional[str] = None, poll_url: Optional[str] = None):
    err = ApiError(code=code, message=message, action=action, retryable=retryable,
                   job_id=job_id, poll_url=poll_url).model_dump()
    for key in ("job_id", "poll_url"):
        if err.get(key) is None:
            err.pop(key, None)
    raise HTTPException(status_code=status, detail={"error": err})


@app.exception_handler(HTTPException)
async def http_exc_handler(request: Request, exc: HTTPException):
    detail = exc.detail if isinstance(exc.detail, dict) and "error" in exc.detail else {
        "error": {"code": "HTTP_ERROR", "message": str(exc.detail)}
    }
    return JSONResponse(status_code=exc.status_code, content=detail)


@app.exception_handler(deletion.DeletionRejected)
@app.exception_handler(st.DeletedObjectWrite)
async def deletion_exc_handler(request: Request, exc: deletion.DeletionRejected):
    return JSONResponse(status_code=exc.status, content={"error": {
        "code": exc.code, "message": exc.message, "retryable": False}})


@app.middleware("http")
async def reject_deleted_mutations(request: Request, call_next):
    # Historical reads remain available; deleted objects cannot accept new operations.
    if request.method in ("POST", "PUT", "PATCH"):
        parts = request.url.path.strip("/").split("/")
        if len(parts) >= 4 and parts[:2] == ["api", "v1"]:
            table = {"works": "works", "identities": "identities", "avatar-identities": "identities"}.get(parts[2])
            row = st.get(table, parts[3]) if table else None
            if row and row.get("deleted_at"):
                label = "作品" if table == "works" else "人物"
                return JSONResponse(status_code=404, content={"error": {
                    "code": "WORK_DELETED" if table == "works" else "IDENTITY_DELETED",
                    "message": label + "已删除，不能继续使用", "retryable": False}})
    return await call_next(request)


# ---------- 能力与入口状态 ----------

@app.get("/api/v1/capabilities", response_model=Capabilities)
def capabilities():
    return Capabilities(
        mode=MODE,
        routes=[
            {"route": "M2", "label": "M2 · LivePortrait → MuseTalk 1.5", "state": "developing",
             "note": "两阶段 ProviderRun、底片审核、单 GPU 串行队列与失败保留已实现并测试；"
                     "长片、正式动作母版与日产 5 条待真实验收（现仅 EXP-001 15.20 秒样片）"},
            {"route": "OMNIHUMAN_1_5", "label": "OmniHuman 1.5 · 自定义表演", "state": "developing",
             "note": "数字人口播：单次完整音频严格小于 60 秒，prompt ≤ 300 字符，720p/1080p；"
                     "提交前检查当前配置、预算和费用确认，原片仍需审核"},
            {"route": "WAN_3_0", "label": "Wan 3.0 · 创意短视频", "state": "developing",
             "note": "图片、1–15 秒完整音频和画面提示词，480p/720p/1080p；"
                     "官方接口声明支持参考生成，当前账号权限与原音频口型跟随仍待真实样片验证"},
        ],
        providers=[{"provider": provider, "purpose": spec.purpose,
                    "model": config.check_provider(provider).model,
                    "validation_state": "usable" if config.check_provider(provider).usable else config.check_provider(provider).state,
                    "detail": config.check_provider(provider).detail}
                   for provider, spec in config.PROVIDERS.items()],
        budget=config.budget_policy().to_dict(),
    )


@app.get("/api/v1/config/providers")
def provider_status():
    """受控连接状态：只返回脱敏视图，密钥与 base_url 原文不出服务端。"""
    return {"mode": MODE, "contract_version": config.contract_version(),
            "providers": config.provider_states()}


@app.get("/api/v1/config/persistence")
def persistence():
    """持久方案现状：未验证的组合不得标为已部署。"""
    return config.persistence_status()


@app.post("/api/v1/config/providers/{provider}/check")
def provider_check(provider: str, payload: Dict[str, Any]):
    """按能力检查；capability 不在受支持范围即拒绝，不发起真实请求。"""
    return config.check_provider(provider, payload.get("capability")).to_dict()


@app.get("/api/v1/features", response_model=List[FeatureEntry])
def features():
    return [
        FeatureEntry(feature_id="avatar", title="人物形象", feature_state="available",
                     reason="人物库、候选定版、衣景变体与表演要求登记",
                     available_actions=["browse", "generate_candidate", "create_variant"]),
        FeatureEntry(feature_id="script", title="口播稿生成", feature_state="available",
                     reason="三角度候选、逐稿审核与用户选稿", available_actions=["generate", "select", "revise"]),
        FeatureEntry(feature_id="tts", title="TTS 语音", feature_state="available",
                     reason="短句试听、全文合成与用户确认", available_actions=["preview", "synthesize", "confirm"]),
        FeatureEntry(feature_id="video", title="视频合成", feature_state="developing",
                     reason="路线选择、估价确认、口播提交、底片/原片门禁与下载已实现；"
                            "真实调用受凭据与预算授权阻塞，云端与 GPU 效果尚未验收",
                     available_actions=["select_route", "confirm_cost", "submit", "review", "download"]),
        FeatureEntry(feature_id="publish", title="发布", feature_state="developing",
                     reason="抖音创作者中心浏览器投稿：扫码登录、准备上传、用户确认提交；真实账号链路逐项验收",
                     available_actions=["login", "prepare", "confirm", "status", "cancel"]),
        FeatureEntry(feature_id="metrics", title="指标监控", feature_state="planned",
                     reason="未接入平台数据权限，不展示估算或补零数据", available_actions=[]),
        FeatureEntry(feature_id="models", title="领域模型选择", feature_state="developing",
                     reason="V1 仅提供服务端基础配置状态与脱敏报错；可编辑配置页后续开放",
                     available_actions=["view_status"]),
    ]


# ---------- 作品 ----------

@app.get("/api/v1/works", response_model=List[Work])
def list_works():
    return [Work(**w) for w in st.all_rows("works") if not w.get("deleted_at")]


@app.delete("/api/v1/works/{work_id}")
def delete_work(work_id: str, payload: Dict[str, Any] = Body(default={})):
    return deletion.delete_object("works", work_id, payload.get("confirmed"))


@app.post("/api/v1/works", response_model=Work)
def create_work(payload: Dict[str, Any]):
    topic = (payload.get("topic") or "").strip()
    if not topic:
        fail("VALIDATION_FAILED", "创建作品必须提供内容/选题")
    duration = payload.get("target_duration_ms")
    if duration is None or duration == "":
        duration = 0
    if isinstance(duration, bool) or not isinstance(duration, int) or duration < 0:
        fail("VALIDATION_FAILED", "目标时长应为非负整数毫秒；留空或 0 表示不限定时长")
    w = Work(
        id=st.new_id("work"),
        name=payload.get("name") or topic[:30],
        brief=WorkBrief(
            topic=topic,
            content_type=payload.get("content_type") or "AI 科普",
            audience=payload.get("audience") or "对 AI 感兴趣的年轻受众",
            keywords=payload.get("keywords") or [],
            target_duration_ms=duration,
        ),
        identity_id="av_base_b_v1",
        voice_asset_id="voice_minimax_warm_bestie_v1",
    )
    with st.transaction() as conn:
        available = [i for i in st.all_rows("identities")
                     if not i.get("deleted_at") and not i.get("discarded_at") and i.get("approval_state") == "approved"]
        preferred = next((i for i in available if i["id"] == "av_base_b_v1"), None)
        w.identity_id = (preferred or next(iter(available), {})).get("id")
        st.insert_row(conn, "works", w.id, w.model_dump())
    return w


def _work(work_id: str) -> dict:
    return deletion.require_work(work_id)


@app.post("/api/v1/works/{work_id}/identity", response_model=Work)
def select_identity(work_id: str, payload: Dict[str, Any]):
    # 先落"选择决定"，再改作品。反过来会出现最难查的一种半完成状态：
    # 作品已经换成新人，库里却没有任何"谁在什么时候选了谁"的记录——
    # 而这张表存在的全部意义就是那条记录。kind 取契约里的 "identity"。
    with st.transaction() as conn:
        w = _work(work_id)
        ident = deletion.require_identity(payload.get("identity_id", ""))
        if ident.get("approval_state") != "approved" or ident.get("discarded_at"):
            fail("ASSET_NOT_APPROVED", "未定版的人物不能进入生产")
        _record_selection(work_id, "identity", ident["id"], connection=conn)
        w["identity_id"] = ident["id"]
        w["version"] = w.get("version", 1) + 1
        st.insert_row(conn, "works", w["id"], w)
    return Work(**w)


# ---------- 人物与变体 ----------

@app.get("/api/v1/avatar-identities", response_model=List[AvatarIdentity])
def list_identities(include_discarded: bool = False):
    """人物列表。

    默认**不含已丢弃**的草稿候选：它们只是被移出工作列表，记录与历史仍然保留
    （规格要求资产/授权/审核记录长期保存）。要看得传 `include_discarded=true`。
    """
    rows = st.all_rows("identities")
    rows = [i for i in rows if not i.get("deleted_at")]
    if not include_discarded:
        rows = [i for i in rows if not i.get("discarded_at")]
    return [AvatarIdentity(**i) for i in rows]


@app.delete("/api/v1/avatar-identities/{identity_id}")
def delete_identity(identity_id: str, payload: Dict[str, Any] = Body(default={})):
    return deletion.delete_object("identities", identity_id, payload.get("confirmed"))


@app.get("/api/v1/avatar-identities/readiness")
def identities_readiness(include_discarded: bool = False):
    """一次拿全部人物的"还缺什么"。

    逐条问 N 次会让一屏列表发 N 个请求，也容易出现"列表刷新了、状态还是旧的"。
    这里与单条 readiness 用同一个判定函数，避免两处口径漂移。
    """
    out: Dict[str, Any] = {}
    for i in st.all_rows("identities"):
        ident_id = i.get("id") or ""
        if not ident_id:
            continue
        if i.get("deleted_at"):
            continue
        if i.get("discarded_at") and not include_discarded:
            continue
        ok, reasons = registry.production_ready(ident_id)
        policy = i.get("reference_policy") or {}
        out[ident_id] = {"ready": ok, "reasons": reasons,
                         "limited_reference": policy.get("mode") == "hero_only",
                         "qualify": registry.qualify_steps(ident_id)}
    return out


def _discard_rejected(exc: DiscardRejected):
    fail(exc.code, exc.message, 422,
         action="确认该候选没有被任何作品、变体或动作参考引用后重试")


@app.post("/api/v1/avatar-identities/{identity_id}/discard", response_model=AvatarIdentity)
def discard_identity(identity_id: str, payload: Dict[str, Any]):
    """丢弃草稿候选：移出工作列表，记录谁/何时/为什么。

    刻意**不提供物理删除**：规格要求保留资产与审核记录，下游也可能已有引用。
    已定版、已被作品/变体/动作参考引用的候选一律拒绝，并逐条说明原因。
    """
    try:
        ident = registry.discard_identity(identity_id,
                                         actor=payload.get("actor") or "user_local",
                                         reason=payload.get("reason") or "")
    except DiscardRejected as e:
        _discard_rejected(e)
    return AvatarIdentity(**ident)


@app.post("/api/v1/avatar-identities/{identity_id}/restore", response_model=AvatarIdentity)
def restore_identity(identity_id: str, payload: Dict[str, Any]):
    """把已丢弃的候选放回工作列表。历史事件只追加，不改写原丢弃理由。"""
    try:
        ident = registry.restore_identity(identity_id,
                                         actor=payload.get("actor") or "user_local")
    except DiscardRejected as e:
        _discard_rejected(e)
    return AvatarIdentity(**ident)


def _asset_rejected(exc: AssetRejected):
    fail(exc.code, exc.message, action="补齐资产登记与身份审核，或重新选择人物")


def _license_blocked(exc: LicenseBlocked):
    fail(exc.code, exc.message, action="确认权属与许可后重试，或改用已核验资产")


def _variant(fn, *args, **kwargs):
    """换装/换景变体生产（F02）的两类异常映射，语义与 T04/T05 保持一致。

    `ComposeRejected` → 422：请求本身不成立（mask 尺寸不符、源文件已被替换、
    画框越界、缩放超限、会切到头顶……），改输入才有意义。
    `VariantBlocked` → 409 + retryable：当前条件不具备（mask 未人工目视确认、
    背景权属未核验），等人工动作或权属确认后重提即可，重提不是误操作。

    刻意不合并这两类：把"等人工确认"报成 400，用户会去改参数，改多少遍都没用。
    """
    try:
        return fn(*args, **kwargs)
    except VariantBlocked as e:
        fail(e.code, e.message, 409, retryable=e.retryable,
             action="按提示补齐条件后重试" if e.retryable else "改用合规的操作者身份")
    except ComposeRejected as e:
        fail(e.code, e.message, 422, action="调整 mask / 背景 / 构图参数后重试")


@app.post("/api/v1/avatar-identities/candidates")
def create_identity_candidates(payload: Dict[str, Any]):
    """新建人物候选（仅登记人设，不生成图片）。

    想要真的出一张主图，用下面的 `/candidates/with-image`：那条会调用文生图服务、
    把图落盘并登记为待审资产。这里明确不做图，避免"点了一下，以为有图其实没有"。
    """
    gender = payload.get("gender") or "未指定"
    age = payload.get("age") or "成年"
    persona = "；".join([p for p in [payload.get("appearance"), payload.get("free_text")] if p]) or "未填写描述"
    ident = registry.create_identity_candidate(
        name=f"候选 {gender}·{age}",
        persona=persona,
        keywords={"gender": gender, "age": age,
                  "appearance": payload.get("appearance") or "",
                  "temperament": payload.get("temperament") or "",
                  "free_text": payload.get("free_text") or ""},
        note="候选：待真实生成、登记原图与参考集并通过身份审核后定版",
    )
    return [AvatarIdentity(**{k: v for k, v in ident.items() if k != "keywords"})]


@app.post("/api/v1/avatar-identities/candidates/with-image")
def create_identity_candidate_with_image(payload: Dict[str, Any]):
    """新建候选并**真的生成一张主图**。

    产生的 hero 资产与身份都仍是 pending：图是 AI 生成的，是否长得对、是否可以作为
    这个人，只能由人判断后 `freeze_identity` 定版。出图不等于合格。
    失败语义：请求不成立 422；缺凭据/网络/远端报错 409（按 retryable 决定是否可重来）。
    """
    try:
        result = person_image.generate_candidate_hero(
            identity_id=payload.get("identity_id") or None,
            gender=payload.get("gender") or "",
            age=payload.get("age") or "",
            appearance=payload.get("appearance") or "",
            free_text=payload.get("free_text") or "",
            size=payload.get("size") or person_image.DEFAULT_SIZE,
            transport=_image_transport(),
        )
    except PersonImageRejected as e:
        fail(e.code, e.message, 422, action="补齐人物描述或改用受支持的尺寸")
    except PersonImageBlocked as e:
        fail(e.code, e.message, 409, retryable=e.retryable,
             action="配置图像生成凭据后重试" if not e.retryable else "稍后重试")
    return {
        "identity": result["identity"],
        "asset": result["asset"],
        "prompt": result["prompt"],
        "request_digest": result["request_digest"],
        "usage": result["usage"],
        "image_url": _media_url(result["asset"].get("storage_uri", "")),
        # 明示三件事：图是 AI 生成的、没有做过身份一致性检测、身份未定版
        "warnings": [
            "AI 文生图产出，未经任何身份一致性检测",
            "身份未定版，须经人工审核后才能进入生产",
            "同一人物不得用该服务换装换景（中转会重绘人脸导致身份漂移）",
        ],
    }


def _image_transport():
    """图像生成通道。

    默认返回 None → `person_image` 走真实 httpx 通道；未配置凭据时由那里抛出
    IMAGE_PROVIDER_NOT_CONFIGURED。这里故意不做"假通道兜底"：返回看得见的成功会让
    界面显示一张不存在的图。
    """
    return None


def _media_url(uri: str) -> str:
    import urllib.parse

    return f"/api/v1/media?uri={urllib.parse.quote(uri)}" if uri else ""


@app.get("/api/v1/media")
def serve_media(uri: str):
    """按工作区相对路径返回本地媒体文件（供前端显示已登记的图/音频片段）。

    只读，且**路径必须落在工作区内**：`..`、绝对路径、工作区外一律拒绝，
    不给任意文件读取能力。
    """
    target = person_image.resolve_media_path(uri)
    if not target:
        fail("MEDIA_PATH_INVALID", "不合法的媒体路径：必须落在工作区内且为受支持的扩展名", 422)
    if not os.path.exists(target):
        fail("MEDIA_NOT_FOUND", "媒体文件不存在，可能已被改动或未落盘", 404)
    return FileResponse(target)


@app.post("/api/v1/identities/{identity_id}/assets")
def register_asset(identity_id: str, payload: Dict[str, Any]):
    """受控登记资产：缺图、哈希缺失或来源不清一律拒绝。"""
    try:
        asset = registry.register_asset(
            identity_id=identity_id,
            kind=payload.get("kind") or "reference",
            storage_uri=payload.get("storage_uri") or "",
            file_hash=payload.get("file_hash") or "",
            source_evidence=payload.get("source_evidence") or "",
            is_mock=bool(payload.get("is_mock")),
            license_id=payload.get("license_id"),
            supersedes_id=payload.get("supersedes_id"),
            note=payload.get("note") or "",
            width=payload.get("width"),
            height=payload.get("height"),
        )
    except AssetRejected as e:
        _asset_rejected(e)
    except LicenseBlocked as e:
        _license_blocked(e)
    return asset


@app.post("/api/v1/identities/{identity_id}/assets/{asset_id}/review")
def review_asset(identity_id: str, asset_id: str, payload: Dict[str, Any]):
    """记录身份审核。摘要绑定资产版本与文件哈希，任一变化即失效。"""
    asset = registry.get_asset(asset_id)
    if not asset or asset.get("identity_id") != identity_id:
        fail("ASSET_NOT_FOUND", "资产不存在或不属于该人物", 404)
    try:
        return registry.record_review(
            asset_id=asset_id,
            reviewer=payload.get("reviewer") or "user",
            outcome=payload.get("outcome") or "review",
            identity_digest=payload.get("identity_digest") or "",
            method=payload.get("method") or "",
            calibration_id=payload.get("calibration_id"),
            note=payload.get("note") or "",
        )
    except AssetRejected as e:
        _asset_rejected(e)


@app.post("/api/v1/identities/{identity_id}/reference-policy")
def set_reference_policy(identity_id: str, payload: Dict[str, Any]):
    """声明这个身份用哪套参考依据。受限路径（`hero_only`）的开关在这个端点上。

    受限路径的含义：承认这个身份拿不到多角度真值（例如整张脸由生成服务凭空产出），
    因此不再要求"至少 4 张参考图"，代价是每次出片的一致性结论必须由人给。
    这不是放宽标准，是换一套说得清的证据口径——两个方向都要留痕。
    """
    try:
        return registry.set_reference_policy(
            identity_id=identity_id,
            mode=payload.get("mode") or "",
            operator=payload.get("reviewer") or payload.get("operator") or "",
            reason=payload.get("reason") or "",
            acknowledged=list(payload.get("acknowledged") or []),
        )
    except AssetRejected as e:
        _asset_rejected(e)


@app.post("/api/v1/avatar-licenses/{license_id}/attest")
def attest_license(license_id: str, payload: Dict[str, Any]):
    """人对商用范围作出确认。

    返回值里的 `attest_note` 会原样提示这是"由人确认、非法律意见"——
    前端必须照抄展示，不许改写成"已核验"。
    """
    try:
        rec = licensing.attest_commercial(
            license_id=license_id,
            attestor=payload.get("reviewer") or payload.get("attestor") or "",
            basis=payload.get("basis") or "",
            ack_token=payload.get("ack_token") or "",
        )
    except LicenseBlocked as e:
        _license_blocked(e)
    return {
        "license": rec,
        "attest_token": licensing.ATTEST_TOKEN,
        "attest_note": "由人确认商用范围，非法律意见；不代表已取得书面法律意见",
    }


@app.post("/api/v1/avatar-licenses/{license_id}/revoke")
def revoke_license(license_id: str, payload: Dict[str, Any]):
    """撤销人工确认。撤销这件事本身也要进来源链，历史不能被抹掉。"""
    try:
        rec = licensing.revoke_commercial(
            license_id=license_id,
            operator=payload.get("reviewer") or payload.get("operator") or "",
            reason=payload.get("reason") or "",
        )
    except LicenseBlocked as e:
        _license_blocked(e)
    return {"license": rec}


@app.post("/api/v1/identities/{identity_id}/freeze")
def freeze_identity(identity_id: str, payload: Dict[str, Any]):
    try:
        return registry.freeze_identity(
            identity_id=identity_id,
            hero_asset_id=payload.get("hero_asset_id") or "",
            identity_digest=payload.get("identity_digest") or "",
            reviewer=payload.get("reviewer") or "user",
            license_id=payload.get("license_id"),
        )
    except AssetRejected as e:
        _asset_rejected(e)
    except LicenseBlocked as e:
        _license_blocked(e)


@app.post("/api/v1/identities/{identity_id}/version")
def bump_identity(identity_id: str, payload: Dict[str, Any]):
    """显式换人：产生新版本并清空审定状态，旧作品仍引用原版本。"""
    try:
        return registry.bump_identity_version(identity_id)
    except AssetRejected as e:
        _asset_rejected(e)


@app.get("/api/v1/avatar/policy-options")
def avatar_policy_options():
    """受限路径的限制项原文与人工确认标记，供前端**照抄展示**。

    前端不得自行另写一份措辞：受限路径是"承认拿不到多角度真值"的代价声明，
    确认对象必须是后端这份原文；确认标记版本化，措辞一变旧确认即作废。
    """
    return {
        "reference_policies": [
            {"mode": "multi_view", "label": "多角度参考集",
             "summary": "有多张经审核的参考图可交叉验证；AC-AV-01 要求至少 4 张。",
             "requires_acknowledgement": False},
            {"mode": "hero_only", "label": "受限路径（仅原图为锚）",
             "summary": "承认不存在同一人的第二张真值，因此不再要求 4 张参考图；"
                        "代价是每次出片的一致性结论必须由人给。",
             "requires_acknowledgement": True},
        ],
        "hero_only_limitations": list(HERO_ONLY_LIMITATIONS),
        "attest_token": licensing.ATTEST_TOKEN,
        "attest_note": "由人确认商用范围，非法律意见；不代表已取得书面法律意见",
    }


@app.get("/api/v1/identities/{identity_id}/readiness")
def identity_readiness(identity_id: str):
    """生产就绪清单。

    连同政策口径一并返回：走受限路径的身份，缺参考图不是待办而是已知代价，
    前端得照这个口径给提示，不能一律显示"还差 N 张参考图"。
    """
    ok, reasons = registry.production_ready(identity_id)
    ident = registry.get_identity(identity_id) or {}
    policy = ident.get("reference_policy") or {}
    return {"identity_id": identity_id, "ready": ok, "reasons": reasons,
            "reference_policy": policy or None,
            "limited_reference": (policy.get("mode") == "hero_only"),
            # 定版四步的顺序与完成状态也由后端给：界面不自行推断"下一步点哪个"
            "qualify": registry.qualify_steps(identity_id),
            "gate": "AC-AV-01", "checked_at": now_iso()}


@app.get("/api/v1/identities/{identity_id}/assets")
def list_identity_assets(identity_id: str, kind: Optional[str] = None):
    return registry.list_assets(identity_id, kind)


@app.get("/api/v1/identities/{identity_id}/motions")
def list_motions(identity_id: str):
    return {"catalog": motion.MOTION_CATALOG,
            "approved": motion.list_references(identity_id, approved_only=True)}


@app.post("/api/v1/identities/{identity_id}/motions/resolve")
def resolve_motion(identity_id: str, payload: Dict[str, Any]):
    """把动作意图映射到获准参考；M2 不接受自由描述。"""
    try:
        return motion.resolve_motion_intent(
            identity_id=identity_id,
            intent=payload.get("intent") or {},
            route=payload.get("route") or "M2",
            as_master=bool(payload.get("as_master")),
        )
    except AssetRejected as e:
        _asset_rejected(e)
    except LicenseBlocked as e:
        _license_blocked(e)


@app.get("/api/v1/licenses")
def list_licenses():
    return licensing.list_records()


@app.post("/api/v1/licenses")
def register_license(payload: Dict[str, Any]):
    return licensing.register(
        subject=payload.get("subject") or "",
        kind=payload.get("kind") or "image_source",
        scope=payload.get("scope") or "unverified",
        evidence=payload.get("evidence") or "",
        verified_by=payload.get("verified_by") or "",
        covers_weights=bool(payload.get("covers_weights")),
        note=payload.get("note") or "",
    )


@app.post("/api/v1/identities/{identity_id}/masks")
def create_mask(identity_id: str, payload: Dict[str, Any]):
    """从定妆原图自动分割出 mask 草稿（换景合成的前置）。

    产出**一律是草稿**：这个端点不提供任何"自动通过"路径，必须再由人目视确认
    （见 `/avatar-masks/{id}/confirm`）。理由写在 `estimate_mask` 里——
    指标只能排除已知失效，不能证明 mask 正确；发丝与轮廓的取舍只能由人看。
    """
    return _variant(
        variant_factory.estimate_and_register_mask,
        identity_id=identity_id,
        source_asset_id=payload.get("source_asset_id") or "",
        method=payload.get("method") or "bgmodel",
        # 默认就走 matting：换深色/异色背景时，羽化 alpha 会在轮廓外留原背景灰晕
        alpha_source=payload.get("alpha_source") or "matting",
        component_mode=payload.get("component_mode") or "all",
        hard_protect=payload.get("hard_protect"),
        origin=payload.get("origin") or "algorithm_draft",
    )


@app.get("/api/v1/identities/{identity_id}/masks")
def list_masks(identity_id: str):
    """列出该身份的 mask 登记（含未确认的草稿），供界面选一条确认或用于合成。"""
    return {"identity_id": identity_id,
            "masks": variant_factory.list_mask_specs(identity_id),
            "note": "state=draft 表示尚未人工确认；只有 confirmed 的 mask 能产出定版变体"}


@app.post("/api/v1/avatar-masks/{mask_asset_id}/confirm")
def confirm_mask(mask_asset_id: str, payload: Dict[str, Any]):
    """人工目视确认 mask —— 自动 mask 定版的**唯一**入口。

    权限来自"人"本身：不接受系统/agent 充当确认者，也不接受"指标全绿即通过"。
    确认对象是蒙版边缘，不是"这就是同一个人"。
    """
    return _variant(
        variant_factory.confirm_mask,
        mask_asset_id=mask_asset_id,
        reviewer=payload.get("reviewer") or payload.get("operator") or "",
        outcome=payload.get("outcome") or "review",
        note=payload.get("note") or "",
    )


@app.get("/api/v1/works/{work_id}/variants", response_model=List[AvatarVariant])
def list_variants(work_id: str):
    """列出作品变体。无真实产出时只给占位预览，一律 pending 且标注未审核。"""
    w = _work(work_id)
    rows = st.all_rows("variants", "work_id=?", [work_id])
    # 作品当前绑定的那条必须在列表里：它是底片真正会用的那条，
    # 而它的 work_id 列可能为空（历史登记没回填）。漏掉它会出现最坏的一种界面——
    # 列表里只有两条用不了的占位，真正在用的那条看不见，报错时无从对照。
    bound = w.get("variant_id")
    if bound and all(r.get("id") != bound for r in rows):
        row = st.get("variants", bound)
        if row:
            rows = [row] + rows
    if not rows:
        for outfit, bg in [("news_suit", "studio"), ("knitwear", "ai_lab")]:
            v = AvatarVariant(
                id=st.new_id("var"),
                identity_id=w.get("identity_id") or "av_base_b_v1",
                outfit=outfit,
                background=bg,
                approval_state="pending",
                identity_report={
                    "outcome": "review",
                    "message": "占位预览：未调用参考图编辑，无真实产出，不得作为身份一致结论",
                    "is_mock": True,
                },
                preview_label=f"{bg} · {outfit}",
            )
            st.put("variants", v.id, v.model_dump(), {"work_id": work_id})
        rows = st.all_rows("variants", "work_id=?", [work_id])
    return [AvatarVariant(**v) for v in rows]


@app.post("/api/v1/works/{work_id}/variants", response_model=AvatarVariant)
def create_variant(work_id: str, payload: Dict[str, Any]):
    """登记换装/换背景变体。

    必须提供真实产出的存储位置、文件哈希与来源证据；未调用参考图编辑不得宣称产出。
    变体须通过身份门禁：换脸候选与旧审核摘要一律拒收。
    """
    w = _work(work_id)
    if not w.get("identity_id"):
        fail("SELECTION_REQUIRED", "生成变体前必须选定已定版人物")
    identity_id = w["identity_id"]
    ident = registry.get_identity(identity_id) or {}
    policy = ident.get("reference_policy") or {}
    if policy.get("mode") == "hero_only":
        # 受限身份没有多角度真值，唯一的锚是那张定妆原图。变体只能从它确定性派生；
        # 拿另一张生成图来登记就是换脸——这条路径要挡的就是这件事。
        hero_id = ident.get("hero_asset_id") or ""
        derived_from = payload.get("derived_from") or ""
        if derived_from != hero_id:
            fail("VARIANT_DERIVATION_MISMATCH",
                 f"受限身份的变体只能由定妆原图派生（{hero_id or '尚无原图'}）；"
                 "另起一张脸属于换人，须新建身份版本，不能登记为变体")
    storage_uri = payload.get("storage_uri") or ""
    file_hash = payload.get("file_hash") or ""
    existing_id = payload.get("asset_id") or ""
    if existing_id:
        # 允许直接引用**已登记**的变体资产（例如确定性合成的产物）。
        # 不允许它再走一遍登记：同一张图登记两次会得到两个 id，
        # 于是"这条变体到底对应哪张图"再也追不回来。
        asset = registry.get_asset(existing_id)
        if not asset or asset.get("identity_id") != identity_id:
            fail("ASSET_NOT_FOUND", "引用的变体资产不存在或不属于该人物", 404)
        if asset.get("kind") != "variant":
            fail("ASSET_KIND_MISMATCH",
                 f"引用的资产种类为 {asset.get('kind')}，不能登记为变体")
        if asset.get("is_mock"):
            fail("ASSET_IS_MOCK", "Mock 资产不得登记为可用变体")
    else:
        if not storage_uri or not file_hash:
            fail("ASSET_NOT_PRODUCED",
                 "变体必须来自真实产出：需提供存储位置与文件哈希，占位与 Mock 不得登记为可用变体")
        try:
            asset = registry.register_asset(
                identity_id=identity_id,
                kind="variant",
                storage_uri=storage_uri,
                file_hash=file_hash,
                source_evidence=payload.get("source_evidence") or "",
                license_id=payload.get("license_id"),
                note=payload.get("note") or "换装/换背景变体，需身份审核",
            )
        except AssetRejected as e:
            _asset_rejected(e)
        except LicenseBlocked as e:
            _license_blocked(e)

    outfit = payload.get("outfit") or "news_suit"
    background = payload.get("background") or "studio"
    report = {"outcome": "review", "message": "待身份审核：需与冻结基准比对后才能定版"}
    if payload.get("identity_digest"):
        try:
            review = registry.record_review(
                asset_id=asset["id"], reviewer="user", outcome="review",
                identity_digest=payload["identity_digest"],
                method=payload.get("method") or "人工对照",
            )
            report = {"outcome": review["outcome"], "message": "已记录审核，待复核确认",
                      "review_id": review["id"]}
        except AssetRejected as e:
            _asset_rejected(e)

    v = AvatarVariant(
        id=st.new_id("var"),
        identity_id=identity_id,
        outfit=outfit,
        background=background,
        approval_state="pending",
        identity_report=report,
        preview_label=f"{background} · {outfit}",
        asset_id=asset["id"],
        identity_version=int(registry.get_identity(identity_id).get("identity_version", 1)),
        review_id=report.get("review_id"),
        motion_ref_id=payload.get("motion_ref_id"),
    )
    st.put("variants", v.id, v.model_dump(), {"work_id": work_id})
    return v


@app.post("/api/v1/works/{work_id}/variants/{variant_id}/admit")
def admit_variant(work_id: str, variant_id: str, payload: Dict[str, Any]):
    """变体准入判定：换脸候选、Mock 后代、种类不符均返回可定位原因。"""
    row = st.get("variants", variant_id)
    if not row:
        fail("VARIANT_NOT_FOUND", "变体不存在", 404)
    asset_id = payload.get("asset_id") or row.get("asset_id")
    if not asset_id:
        fail("ASSET_NOT_FOUND", "该变体没有对应资产，无法准入")
    return identity_gate.admit(asset_id, row["identity_id"],
                               expected_kind="variant",
                               declared_style=payload.get("style") or "")


@app.post("/api/v1/identities/{identity_id}/variants/compose")
def compose_variant(identity_id: str, payload: Dict[str, Any]):
    """确定性合成换装/换景变体 —— 界面「生成变体」真正的产出者。

    为什么必须走确定性合成、而不是"参考图编辑"：gpt-image 系列中转会忽略 mask 参数、
    整图重绘，换景必然连着把脸一起重画（身份漂移，AC-AV-02 不通过）。这里改为
    人物像素 1:1 复制、只换背景与画框；硬保护区内最大通道差严格为 0 ——
    这是"身份零漂移"唯一可验证的依据，不是主观描述也不是阈值近似。

    `commit=False`：只产出候选预览，**不登记**资产，用于先看构图再决定。
    `commit=True`（默认）：要求 mask 已人工确认，产物登记为 kind=variant 的待审资产。
    """
    return _variant(
        variant_factory.compose_variant,
        identity_id=identity_id,
        mask_asset_id=payload.get("mask_asset_id") or "",
        background_asset_id=payload.get("background_asset_id") or "",
        edge_mode=payload.get("edge_mode") or "unmix",
        person_scale=float(payload.get("person_scale") or 1.0),
        anchor=payload.get("anchor"),
        frame=payload.get("frame"),
        allow_frame_reveal=bool(payload.get("allow_frame_reveal")),
        outfit=payload.get("outfit") or "",
        background_label=payload.get("background_label") or "",
        declared_style=payload.get("declared_style") or "",
        commit=bool(payload.get("commit", True)),
        note=payload.get("note") or "",
    )


# ---------- 音色、试听与音频（T04） ----------

def _tts(fn, *args, **kwargs):
    """把 T04 的两类异常映射为可区分的 HTTP 错误。

    请求不成立（422）与当前不可执行（409 + 可重试）必须分开：
    前者改输入才有意义，后者要等配置/预算/上游确认变化。
    """
    try:
        return fn(*args, **kwargs)
    except TtsBlocked as e:
        fail(e.code, e.message, 409, retryable=True)
    except TtsRejected as e:
        fail(e.code, e.message, 422)


def _dh(fn, *args, **kwargs):
    """口播（T05）的两类异常映射。语义与 T04 一致，只是模块不同。

    `TalkingHeadRejected` → 422：请求本身不成立（未选路线、纯文生视频、
    版本错配、无视频轨），改输入才有意义。
    `TalkingHeadBlocked` → 409 + retryable：当前不可执行（预算未授权、
    GPU 忙、凭据缺失、底片未审），要等外部条件变化，重提不是误操作。
    """
    try:
        return fn(*args, **kwargs)
    except TalkingHeadBlocked as e:
        fail(e.code, e.message, 409, retryable=True)
    except TalkingHeadRejected as e:
        fail(e.code, e.message, 422,
             action="confirm_cost" if e.code == "COST_LIST_CHANGED" else None)


def _alignment(fn, *args, **kwargs):
    """对齐入口横跨 TTS / 口播 / 后期三类异常，按同一取向映射（409 等条件，422 改输入）。"""
    try:
        return fn(*args, **kwargs)
    except TalkingHeadBlocked as e:
        fail(e.code, e.message, 409, retryable=True)
    except TalkingHeadRejected as e:
        fail(e.code, e.message, 422)
    except TtsBlocked as e:
        fail(e.code, e.message, 409, retryable=True)
    except TtsRejected as e:
        fail(e.code, e.message, 422)
    except VideoBlocked as e:
        fail(e.code, e.message, 409, retryable=True)
    except VideoRejected as e:
        fail(e.code, e.message, 422)


def _audio_out(row: dict) -> Dict[str, Any]:
    """音频对外视图：契约字段 + 字词时间与归属作品。

    不直接 `AudioAssetInfo(**row)`：仓储行还带 work_id/words 等附加键，
    而 AudioAssetInfo 是 extra=forbid 的严格契约，会直接报错。
    """
    out = {k: v for k, v in row.items() if k in AudioAssetInfo.model_fields}
    out["work_id"] = row.get("work_id")
    out["words"] = row.get("words", [])
    # 仓储只记 storage_uri；播放器必须使用本站可读取的已验证字节。
    # 云端上传后的资产也可能保留本地副本，不能因 URI 已换为 TOS 而丢掉试听。
    out["url"] = None
    if not row.get("is_mock") and row.get("file_hash"):
        uri = str(row.get("storage_uri") or "").replace("\\", "/")
        candidates = [uri if uri.startswith("production/") else "production/backend/runtime/" + uri]
        if row.get("work_id") and row.get("id"):
            candidates.append(f"production/backend/runtime/audio/{row['work_id']}/{row['id']}.wav")
        for candidate in candidates:
            path = person_image.resolve_media_path(candidate)
            if not path or not os.path.isfile(path):
                continue
            try:
                with open(path, "rb") as stream:
                    digest = "sha256:" + hashlib.file_digest(stream, "sha256").hexdigest()
                if digest == row["file_hash"]:
                    out["url"] = _media_url(candidate)
                    break
            except OSError:
                continue
    return out


def _tts_dry_run() -> bool:
    """mock 模式下不触网：占位产物带 is_mock，永不能被确认为最终音频。"""
    return config.resolve_mode() == "mock"


@app.get("/api/v1/voices", response_model=List[dict])
def list_voices():
    out: List[Dict[str, Any]] = []
    for v in tts_voices.list_voices():
        row = dict(v)
        row["scope"] = tts_voices.voice_scope(v)
        row["baseline_sample"] = tts_preview.baseline_sample(v["id"])
        ok, reasons = tts_voices.voice_ready(v["id"])
        row["production_ready"] = ok
        row["production_blockers"] = reasons
        out.append(row)
    return out


@app.post("/api/v1/voices/{voice_id}/approve", response_model=Dict[str, Any])
def approve_voice(voice_id: str, payload: Optional[Dict[str, Any]] = None):
    """用户显式选定候选音色即批准该音色。

    批准的是"用户确认了这组音色"，**不是**生产质量结论：
    生产就绪仍由 `voice_ready()` 单独判定（基准合成证据 / 权属核验），
    所以批准之后 `production_ready` 可能仍为 false，这不是 bug。
    """
    p = payload or {}
    return _tts(tts_voices.approve_voice, voice_id,
                reviewer=str(p.get("reviewer") or "user"),
                note=str(p.get("note") or ""))


@app.post("/api/v1/voices/{voice_id}/baseline", response_model=Dict[str, Any])
def register_voice_baseline(voice_id: str, payload: Dict[str, Any]):
    """把该作品里这条音色的真实试听登记为本项目基准合成证据。

    基准证据只能来自真实合成：Mock 占位产物、以及"还没试听过"一律拒收。
    登记成功也不等于生产就绪——权属核验等其余条件仍单独判定。
    """
    work_id = str((payload or {}).get("work_id") or "")
    if not work_id:
        fail("VALIDATION_FAILED", "缺少 work_id：基准证据必须指明取自哪个作品的试听")
    _work(work_id)
    return _tts(tts_voices.attach_baseline_from_preview, voice_id, work_id)


@app.get("/api/v1/voices/catalog-status")
def voices_catalog_status():
    """官方音色目录的覆盖情况，以及确认商用范围所需的当前口径。

    `attest_token` 随口径版本变化：前端必须回传当前值，旧版本的确认自动失效。
    """
    from .tts import catalog as tts_catalog

    return {
        **tts_catalog.catalog_status(),
        "attest_token": licensing.ATTEST_TOKEN,
        "attest_note": "由人确认商用范围，非法律意见；不代表已取得书面法律意见",
    }


@app.get("/api/v1/tts/capabilities")
def tts_capabilities():
    """TTS 能力视图：供应商声明与本项目实测分开列，并给出当前可用性。"""
    return {
        "adapter": tts_provider.adapter_status(),
        "supported_controls": list(tts_voices.SUPPORTED_CONTROLS),
        "declared_unsupported": list(tts_voices.DECLARED_UNSUPPORTED),
        "control_schema": tts_voices.CONTROL_SCHEMA,
        "note": "未核验项不因列入声明而被当成已支持；不支持的控制在调用前报告",
    }


@app.get("/api/v1/works/{work_id}/tts-controls")
def get_work_tts_controls(work_id: str):
    work = _work(work_id)
    voice = tts_voices.get_voice(work.get("voice_asset_id"))
    if not voice:
        fail("VOICE_NOT_FOUND", "请先选择音色")
    controls = _tts(tts_voices.effective_profile, work, voice)
    return {"controls": controls, "digest": tts_voices.controls_digest(voice, controls)}


@app.put("/api/v1/works/{work_id}/tts-controls", response_model=Work)
def save_work_tts_controls(work_id: str, payload: Dict[str, Any]):
    _work(work_id)
    if set(payload) != {"controls"}:
        fail("VALIDATION_FAILED", "请求须仅包含 controls 对象")
    return Work(**_tts(tts_gate.set_work_controls, work_id, payload["controls"]))


@app.post("/api/v1/voice-previews", response_model=Dict[str, Any])
def create_voice_preview(payload: Dict[str, Any]):
    """短句试听。同一作品内必须用同一短句，否则跨音色比较不成立。"""
    work_id = payload.get("work_id")
    voice_id = payload.get("voice_asset_id")
    if not work_id or not voice_id:
        fail("VALIDATION_FAILED", "缺少 work_id 或 voice_asset_id")
    _work(work_id)
    r = _tts(tts_preview.create_preview, work_id=work_id, voice_id=voice_id,
             sample_text=payload.get("sample_text"),
             force_new=bool(payload.get("force_new")),
             amount_fen=int(payload.get("amount_fen") or 0),
             dry_run=_tts_dry_run())
    return {"cached": r["cached"], "message": r["message"], "note": r["note"],
            "sample_sentence": tts_preview.audition_sentence(work_id),
            "audio": _audio_out(r["audio"])}


@app.post("/api/v1/works/{work_id}/voice", response_model=Work)
def select_voice(work_id: str, payload: Dict[str, Any]):
    w = _work(work_id)
    voice_id = payload.get("voice_asset_id", "")
    voice = st.get("voices", voice_id)
    if not voice:
        fail("VOICE_SCOPE_MISMATCH", "音色不存在")
    if voice["approval_state"] != "approved":
        fail("ASSET_NOT_APPROVED", "未审批的音色不能用于生产")
    previous = w.get("voice_asset_id")
    if previous and previous != voice_id:
        # 换声使已确认音频与下游口型/字幕/成片失效；旧记录与费用保留
        _tts(tts_gate.on_voice_change, work_id, voice_id)
    elif not previous:
        w["voice_asset_id"] = voice_id
        st.put("works", w["id"], w)
    _record_selection(work_id, "voice", voice_id)
    return Work(**st.get("works", work_id))


@app.post("/api/v1/works/{work_id}/audio", response_model=Dict[str, Any])
def synthesize_full_audio(work_id: str, payload: Optional[Dict[str, Any]] = None):
    """全文合成。校验音色作用域、计划冻结、分段与稿件逐字一致后才提交。"""
    _work(work_id)
    _require_script_selected(work_id)
    p = payload or {}
    r = _tts(tts_synth.synthesize_full, work_id=work_id,
             amount_fen=int(p.get("amount_fen") or 0),
             dry_run=_tts_dry_run(), plan_id=p.get("plan_id"),
             force_new_plan=bool(p.get("force_new_plan")), force_new=p.get("force_new") is True)
    return {"audio": _audio_out(r["audio"]), "is_mock": r["is_mock"],
            "duration_ms": r["duration_ms"], "timing_source": r["timing_source"],
            "hard_failures": r["hard_failures"],
            "control_summary": r["compiled"]["control_summary"],
            "unsupported_controls": r["compiled"]["controls"]["unsupported"],
            "note": r["note"]}


@app.post("/api/v1/works/{work_id}/audio-confirmation")
def confirm_audio(work_id: str, payload: Dict[str, Any]):
    """确认为最终音频。必须由用户显式试听确认，系统不做自动放行。"""
    _work(work_id)
    audio_id = payload.get("audio_id")
    if not audio_id:
        fail("VALIDATION_FAILED", "缺少 audio_id")
    r = _tts(tts_gate.confirm_final_audio, work_id, audio_id,
             actor_id=payload.get("actor_id") or "",
             audition_ack=bool(payload.get("audition_ack")))
    _record_selection(work_id, "audio", audio_id)
    task = _current_task(work_id)
    if task:
        task["pending_selection_ids"] = [s for s in task.get("pending_selection_ids", []) if s != "sel_audio"]
        if task["stage"] == "tts":
            task["stage"] = "talking_head"
            task["state"] = "running"
            engine.emit(task["id"], "selection.decided",
                        "最终音频已确认，等待用户选择路线与预算", "talking_head", "running")
            engine._save_task(task)
    return {"ok": True, "message": r["message"], "duration_ms": r["duration_ms"],
            "revoked_previous": r["revoked_previous"],
            "note": "最终音频时长以此为准；字幕对齐可并行启动"}


@app.get("/api/v1/works/{work_id}/audios", response_model=List[dict])
def list_work_audios(work_id: str):
    _work(work_id)
    return [_audio_out(a) for a in tts_audio.list_audio(work_id)]


@app.get("/api/v1/works/{work_id}/audio-gate")
def audio_gate(work_id: str):
    """语音环节总览：是否已选音色、是否已确认最终音频、对齐是否可宣称完成。"""
    _work(work_id)
    return tts_gate.gate_summary(work_id)


@app.post("/api/v1/works/{work_id}/alignment")
def register_alignment(work_id: str, payload: Dict[str, Any]):
    """登记对齐产物。均分时长不在允许来源内。"""
    _work(work_id)
    audio_id = payload.get("audio_id")
    if not audio_id:
        fail("VALIDATION_FAILED", "缺少 audio_id")
    rec = _tts(tts_gate.register_alignment, audio_id,
               source=payload.get("source", ""), method=payload.get("method", ""),
               words=payload.get("words") or [],
               rule_version=payload.get("rule_version", ""))
    return {"ok": True, "alignment_id": rec["id"], "word_count": len(rec["words"]),
            "source": rec["source"],
            "note": "对齐产物版本已登记；时间轴变化时另行登记新版本"}


@app.post("/api/v1/works/{work_id}/alignment/run")
def run_alignment(work_id: str):
    """音频确认后即可并行对齐：为已确认最终音频取真实字词时间轴并登记。

    与上面的 `POST /alignment`（只登记调用方给的时间戳）不同，这里**自己跑**
    ASR/强制对齐。存在的理由：约 60 秒以上的音频走 OmniHuman 必须分段，
    而分段必须基于字词时间戳；后期阶段虽然也做对齐，但它要求先有已审原片，
    对"还没生成口播"的作品构成死循环。
    """
    _work(work_id)
    return _alignment(alignment_service.run_for_work, work_id)


# ---------- 稿件 ----------

def _content_pending(work_id: str):
    """旧同步入口等待超时：原持久任务仍在进行。返回 504 + 原 job_id，绝不重复提交。"""
    from .content import tasks as content_tasks

    active = content_tasks.list_jobs(work_id, active_only=True)
    job_id = active[-1]["id"] if active else None
    fail("CONTENT_JOB_PENDING", "稿件任务仍在后台进行，请稍后按 job_id 查询结果；不会重复生成或重复计费",
         504, retryable=False, job_id=job_id,
         poll_url=f"/api/v1/works/{work_id}/content/jobs/{job_id}" if job_id else None)


def _content_job_error(job: dict, work_id: str):
    """把非成功的内容任务映射成具名错误（含原 job_id）。"""
    from .content import tasks as content_tasks

    err = job.get("error") or {}
    if job["state"] in content_tasks.ACTIVE_STATES:
        _content_pending(work_id)
    status = 409 if job["state"] in ("blocked", "unknown", "superseded", "cancelled") else 422
    fail(err.get("code") or "CONTENT_JOB_FAILED", err.get("message") or f"内容任务未成功（{job['state']}）",
         status, retryable=False, job_id=job["id"], poll_url=f"/api/v1/works/{work_id}/content/jobs/{job['id']}")


@app.post("/api/v1/works/{work_id}/scripts", response_model=ScriptBatch)
async def generate_scripts(work_id: str, payload: Dict[str, Any]):
    """三稿由 content 角色产出：API 只触发编排，不另造一份候选（避免双写导致 ID 失效）。

    可带 `materials`（参考资料，字符串或 {text, source} 列表）。资料落进 `WorkBrief`，
    既是模型引用事实的依据，也是断言判定的唯一依据来源——`materials` 曾经**没有任何
    写入方**，于是"断言有依据"这条路是死的：三稿的断言必然全为 unverified，
    `FACT_EVIDENCE` 硬门禁必然挡住，用户点不动「采用」只能去改稿。留空也允许，
    此时生成侧必须走不依赖外部事实的表达方式（见 content/generate._SYSTEM 第 7 条）。
    """
    w = _work(work_id)
    topic = (payload.get("topic") or w["brief"]["topic"]).strip()
    if not topic:
        fail("VALIDATION_FAILED", "缺少主题")
    new_brief = dict(w["brief"], topic=topic)
    if payload.get("materials") is not None:
        raw_mats = payload.get("materials") or []
        if not isinstance(raw_mats, list):
            fail("VALIDATION_FAILED", "materials 必须是列表（字符串或 {text, source}）")
        materials = []
        for item in raw_mats:
            if isinstance(item, dict):
                text = str(item.get("text", "")).strip()
                src = str(item.get("source", "")).strip()
            else:
                text, src = str(item).strip(), ""
            if not text:
                continue
            # 哈希与时间由服务端复算：调用方给的值一律忽略，否则"依据哪一版资料"
            # 可以由客户端随便填，追溯就是假的。
            materials.append({
                "text": text, "source": src,
                "content_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                "added_at": now_iso(),
            })
        new_brief["materials"] = materials
    # 主题/资料是共享创作输入：真的变了才推进 content_revision（并撤销依赖旧输入的审核与选用）。
    # 只改"添加时间"的等价重提不算变化，否则重复点击会不断使旧任务和审核失效。
    from .content import context as content_context
    from .content import repository as content_repo

    def _fingerprint(brief: dict) -> Any:
        return (content_context.brief_core(brief),
                [m.get("content_hash") or m.get("text") for m in (brief.get("materials") or []) if isinstance(m, dict)])

    if _fingerprint(new_brief) != _fingerprint(w["brief"]):
        with st.transaction() as connection:
            content_repo.bump_content_revision(connection, work_id, brief=new_brief)
    elif new_brief != w["brief"]:
        w["brief"] = new_brief
        st.put("works", w["id"], w)
    w = _work(work_id)
    # 旧采用失效由写入方负责（出稿任务成功替换批次时才清空）。失败不该有副作用。

    from .agents import supervisor

    task = _current_task(work_id)
    if not task:
        task = _create_task(work_id, w.get("route", "M2"))
    # 上一次点击触发的后台推进可能还在把任务推向选择门：先等它落定再重跑，
    # 否则它会在本次把任务置为 running 之后又写回 waiting_selection，让写稿工具被"任务不活跃"拒绝。
    import asyncio as _asyncio

    inflight = engine._scheduled.get(task["id"])
    if inflight is not None and not inflight.done():
        await _asyncio.wait({inflight}, timeout=30)
        task = _current_task(work_id) or task
    task["stage"] = "script"
    task["state"] = "running"
    # 已有批次＝用户点的是「重新生成」。编排默认只跑未完成的工作项，于是这里会**空转**：
    # 返回旧批次、却把 adopted 稿件清空、还留一条"开始写稿"的事件——按钮看起来是假的。
    # 重跑就把本阶段重新排队（旧尝试的 AgentResult 与 attempt 计数都保留）。
    rerun = bool(st.get("script_batches", work_id))
    if rerun:
        from .agents import supervisor as _sup

        reset = _sup.reset_stage(task["id"], "script")
        engine.emit(task["id"], "stage.requeued",
                    f"用户要求重新生成：写稿阶段重新排队（{reset} 个工作项），旧尝试结果保留为证据",
                    "script", "running")
    engine.emit(task["id"], "stage.started", "按提示词生成三个角度的口播稿", "script", "running")
    engine._save_task(task)

    before_batch_id = (st.get("script_batches", work_id) or {}).get("id")
    summary = await supervisor.run_stage(task, w, "script")
    batch = st.get("script_batches", work_id)
    if not batch:
        for f in summary.get("failures") or []:
            if (f.get("error") or {}).get("code") == "CONTENT_JOB_PENDING":
                _content_pending(work_id)
        fail("AGENT_RESULT_MISSING", "策划角色未产出候选稿，不返回兜底内容")
    if summary.get("failures") and batch.get("id") == before_batch_id:
        # 写稿失败时旧批次还在：直接返回它等于把失败冒充成"生成成功"。如实报错。
        f = summary["failures"][0]
        err = f.get("error") or {}
        blocked = f.get("state") == "blocked"
        if err.get("code") == "CONTENT_JOB_PENDING":
            _content_pending(work_id)
        fail(err.get("code") or "AGENT_RESULT_MISSING",
             f"写稿未产出新稿（{f.get('state')}）：{err.get('message') or '未给出原因'}"
             "；未改动已有稿件",
             409 if blocked else 422, retryable=blocked)
    # 契约不一致不能退化成裸 500：`extra=forbid` 下写入方多写一个字段就会被整条拒绝，
    # 而前端只看到非 JSON 的 "Internal Server Error"，无从定位（本项曾因此卡住出稿）。
    # 这里先按契约验一遍再排期，给出具名错误并指向要核查的地方。
    try:
        view = ScriptBatch(**batch)
    except ValidationError as exc:
        fail("AGENT_RESULT_CONTRACT_MISMATCH",
             f"候选稿留痕与契约不符，已拒绝返回（{exc.error_count()} 处）；"
             f"写入方须与 contracts.ScriptBatch 同步：{exc.errors()[0].get('type')} "
             f"@ {'.'.join(str(p) for p in exc.errors()[0].get('loc', ()))}",
             500, action="核对 app/agents/tools.py 写入字段与 app/contracts.py 的 ScriptBatch 定义")
    engine.schedule(task["id"])
    from .content import selection as content_selection

    return ScriptBatch(**content_selection.project_batch(work_id))


@app.get("/api/v1/works/{work_id}/scripts")
def get_scripts(work_id: str):
    from .content import selection as content_selection

    return content_selection.project_batch(work_id)


@app.post("/api/v1/works/{work_id}/script-selection")
async def select_script(work_id: str, payload: Dict[str, Any]):
    """采用稿件：单事务内复验审核绑定（硬门禁 + 单稿独立审核 + 批次审核）后写入选用。

    旧客户端不带 review_id/expected_content_revision 时由服务端查当前有效审核，缺审核一律拒绝。
    """
    from .content import selection as content_selection

    try:
        out = content_selection.adopt(
            work_id, payload.get("script_id") or "",
            expected_revision=payload.get("expected_content_revision"),
            review_id=payload.get("review_id"), set_review_id=payload.get("set_review_id"))
    except content_selection.SelectionRejected as exc:
        fail(exc.code, exc.message, exc.http_status)
    if out.get("advance") and out.get("task_id"):
        engine.schedule(out["task_id"])
    return {"ok": True, "message": out["message"]}


@app.post("/api/v1/works/{work_id}/script-revisions", response_model=ScriptVersion)
def revise_script(work_id: str, payload: Dict[str, Any]):
    """用户手改正文：新版本新 ID，规则重审；不调用模型，也不再暗中多收一次"重抽断言"的费。

    旧选用与下游（音频、口型、字幕）随批次替换一并撤销；新稿没有有效独立审核，需复审后才可采用。
    """
    from .content import service as content_service
    from .content.errors import ContentError

    _work(work_id)
    try:
        revised = content_service.revise_candidate(work_id, payload.get("script_id") or "", payload.get("text") or "")
    except ContentError as exc:
        code = getattr(exc, "code", "VALIDATION_FAILED")
        fail(code, exc.message, 409 if code == "STALE_SELECTION" else 422)
    task = _current_task(work_id)
    if task:
        engine.emit(task["id"], "artifact.invalidated",
                    "稿件修订：已生成新版本并按新正文重新做规则检查；原独立审核对新正文不再有效，需复审；"
                    "下游音频与口型依赖已失效",
                    task["stage"], task["state"])
    return ScriptVersion(**revised)


@app.post("/api/v1/works/{work_id}/script-repairs", response_model=ScriptVersion | ScriptRepairReceipt)
def repair_script(work_id: str, payload: Dict[str, Any]):
    """让模型把某一稿重写到过门禁（用户不必自己改）：委托内容服务的定向返工任务。

    返工产出新版本（新 ID，parent_id 指向原稿）并替换当前批次；其余稿的版本与单稿审核不变。
    """
    from .content import repository as content_repo
    from .content import service as content_service
    from .content.errors import ContentBlocked, ContentError

    _work(work_id)
    current = st.get("script_batches", work_id)
    if not current:
        fail("VALIDATION_FAILED", "尚未生成候选稿件")
    script_id = payload.get("script_id")
    cand = next((c for c in current["candidates"] if c["id"] == script_id), None)
    if cand is None:
        fail("STALE_SELECTION", "稿件不存在或已被新版本取代", 409)
    if content_repo.get_batch(current["id"]) is None:
        fail("LEGACY_BATCH", "这是升级前生成的批次，没有可返工的不可变快照；请重新生成三稿", 409)
    try:
        prompt = content_service.ensure_prompt(work_id)
        job = content_service.run_legacy(
            work_id, "repair", {"batch_id": current["id"], "candidate_ids": [script_id], "prompt_id": prompt["id"]},
            request_id=payload.get("request_id"))
    except ContentBlocked as exc:
        fail(exc.code, exc.message, 409, retryable=getattr(exc, "retryable", True))
    except ContentError as exc:
        fail(exc.code, exc.message, 409 if exc.code == "STALE_SELECTION" else 422)
    if job["state"] not in ("succeeded", "partial"):
        _content_job_error(job, work_id)
    task = _current_task(work_id)
    batch = st.get("script_batches", work_id) or {}
    new = next((c for c in batch.get("candidates", []) if c.get("parent_id") == script_id), None)
    if new is None:
        fail("REPAIR_NO_CHANGE", "返工没有产出新版本，原稿保持不变", 409)
    if task:
        engine.emit(task["id"], "artifact.repaired", batch.get("repair_note") or "模型已按意见重写该稿",
                    task["stage"], task["state"])
    from .content import selection as content_selection
    if not content_selection.project_batch(work_id)["ready_to_present"]:
        return ScriptRepairReceipt(id=new["id"], parent_id=script_id)
    return ScriptVersion(**new)


# ---------- 路线、估价与任务 ----------

@app.post("/api/v1/cost-estimates", response_model=CostEstimate)
def cost_estimate(payload: Dict[str, Any]):
    """按当前价格快照与作品最终音频时长估价。

    旧实现在这里写死了 6000/7200 分（自称 MOCK）——那是一个与任何输入都无关的常数，
    界面一旦用它，用户看到的"估价"就是编的。估价必须由费率 × 实际计费时长算出来；
    没有时长就明确说给不出金额，不能用 0 冒充"免费"。
    """
    route = payload.get("route", "M2")
    if route not in dh_routes.ROUTES:
        fail("VALIDATION_FAILED", "路线取值非法")
    work_id = payload.get("work_id")
    duration_ms = 0
    if work_id:
        duration_ms = int((tts_audio.final_audio(work_id) or {}).get("duration_ms") or 0)
    resolution = payload.get("resolution") or ("720p" if route == dh_routes.ROUTE_WAN else "1080p")
    if not duration_ms and route != dh_routes.ROUTE_WAN:
        spec = dh_routes.route_spec(route)
        return CostEstimate(
            route=route, estimated_fen=0, ceiling_fen=0,
            price_basis=spec["cost_summary"], billable=route != dh_routes.ROUTE_M2,
            note="最终音频未就绪，还没有「生成秒」：这里的 0 表示「未计算」，不是「免费」。"
        )
    wan_options = {"duration_seconds": payload.get("duration_seconds", 5)} if route == dh_routes.ROUTE_WAN else {}
    est = _dh(dh_cost.estimate, route, duration_ms=duration_ms, resolution=resolution, **wan_options)
    return CostEstimate(
        route=route, estimated_fen=int(est["estimated_fen"]),
        ceiling_fen=int(est["ceiling_fen"]),
        price_basis=est["price_basis"], billable=bool(est["billable"]),
        note=est["note"],
    )


@app.post("/api/v1/works/{work_id}/route")
def confirm_route(work_id: str, payload: Dict[str, Any]):
    """确认路线选择。

    三条路线由用户明确选择：M2 低成本、OmniHuman 数字人、Wan 创意短视频。
    高成本那条**选择时**就必须先确认费用提示（`cost_notice_ack`）——
    这是服务端门禁，不是前端弹窗：绕过界面直接打接口同样被拦。

    费用提示确认 ≠ 提交前的调用清单确认：前者决定走不走这条付费路线，
    后者决定这一次具体花多少。两道都要有，缺一不可。
    """
    w = _work(work_id)
    route = payload.get("route")
    if not route:
        fail("ROUTE_NOT_SELECTED", "请明确选择视频路线，不自动选择 M2")
    if route not in dh_routes.ROUTES:
        fail("VALIDATION_FAILED", "路线取值非法")
    spec = dh_routes.route_spec(route)
    notice = dh_cost.cost_notice(route, duration_ms=(tts_audio.final_audio(work_id) or {}).get("duration_ms"),
                                 resolution="720p" if route == dh_routes.ROUTE_WAN else "1080p")

    if spec.get("cost_notice_required") and not bool(payload.get("cost_notice_ack")):
        # 409：请求本身合法，但当前缺一个用户动作——不是"改输入"，是"等人确认"。
        fail("COST_NOTICE_REQUIRED",
             f"{route} 是高成本路线：必须先展示费用提示（{notice['unit_text']}）并取得用户确认，"
             "才能选择该路线；确认后再调用本接口并带 cost_notice_ack=true",
             409, action="acknowledge_cost_notice", retryable=True)

    w["route"] = route
    # 确认记录挂在作品上：路线是作品级决定，而 Task 契约已冻结、不能加字段。
    if spec.get("cost_notice_required"):
        w["cost_notice_ack"] = {
            "route": route, "version": dh_routes.COST_NOTICE_VERSION,
            "acknowledged_at": now_iso(), "actor": "user_ui",
            "unit_text": notice["unit_text"],
            "estimated_fen": notice.get("estimated_fen"),
            "ceiling_fen": notice.get("ceiling_fen"),
        }
    else:
        # 切到低成本路线时清掉高成本路线的确认：留着会让"上次确认过"看起来仍然有效。
        w.pop("cost_notice_ack", None)
    st.put("works", w["id"], w)

    ack_note = ""
    if spec.get("cost_notice_required"):
        amount = "金额待音频就绪后给出" if notice.get("estimated_fen") is None \
            else f"估价 ¥{notice['estimated_fen'] / 100:.2f}（上限 ¥{notice['ceiling_fen'] / 100:.2f}）"
        ack_note = f"；已确认费用提示 v{dh_routes.COST_NOTICE_VERSION}（{amount}）"
    decision = _record_selection(work_id, "route", route,
                                 reason=f"用户确认路线{ack_note}")
    task = _current_task(work_id)
    if task is None and route == dh_routes.ROUTE_WAN:
        task = _create_task(work_id, route)
        task.update(stage="talking_head", state="waiting_selection")
    if task:
        task["route"] = route
        task["route_confirmed"] = True
        task["route_selection_id"] = decision.id  # 冻结：可追溯到具体人工决定
        # 事件名必须只说这里真的做了什么：这一步是**路线选择**。
        # 之前发的是 "cost.confirmed"，而这一步并没有确认任何金额——
        # 界面据此显示"费用已确认"，用户会以为可以直接提交。
        engine.emit(task["id"], "route.selected",
                    f"路线 {route} 已选择{ack_note}", task["stage"], task["state"])
        engine._save_task(task)
    return {"ok": True,
            "cost_notice_acknowledged": bool(w.get("cost_notice_ack")),
            "message": f"已记录路线选择 {route}（可追溯的人工决定）{ack_note}；"
                       "提交前仍需确认本次调用清单与金额"}


@app.post("/api/v1/tasks", response_model=Task)
async def create_task(payload: Dict[str, Any]):
    work_id = payload.get("work_id")
    w = _work(work_id)
    task = _current_task(work_id)
    if not task:
        task = _create_task(work_id, w.get("route", "M2"))
    # 前置不满足一律返回**结构化阻断任务**，与"未确认路线"保持同一种响应形状。
    # 之前这里对未确认路线返回任务对象、对缺少人物/稿件/音频抛 422，
    # 于是"先确认路线还是先建任务"会决定客户端拿到的是任务还是一段错误体——
    # 而这个顺序与用户关心的事毫无关系（T01 的冻结契约测试就因此被绊住）。
    if not task.get("route_confirmed"):
        return _blocked_task(task, "COST_CONFIRMATION_REQUIRED",
                             "提交前必须选择口播路线并确认适用预算", "confirm_cost")
    if w.get("route") == dh_routes.ROUTE_WAN:
        preflight = _dh(dh_routes.preflight, work_id, task=task, route=dh_routes.ROUTE_WAN,
                        require_plate=False)
        if not preflight["ok"]:
            blocker = preflight["blockers"][0]
            return _blocked_task(task, blocker["code"], blocker["message"], "prepare_wan_image")
        task.update(stage="talking_head", state="waiting_selection", block_reason=None, last_error=None)
        engine._save_task(task)
        return Task(**task)
    if not w.get("identity_id"):
        return _blocked_task(task, "SELECTION_REQUIRED",
                             "提交口播前必须选定已定版人物", "select_identity")
    if not _script_selected(work_id):
        return _blocked_task(task, "SELECTION_REQUIRED",
                             "必须先采用一稿通过审核的稿件", "select_script")
    if not any(a["confirmed"] for a in st.all_rows("audios", "work_id=?", [work_id])):
        return _blocked_task(task, "SELECTION_REQUIRED",
                             "最终音频必须经试听确认后才能提交口播", "confirm_audio")
    task["stage"] = "talking_head"
    task["state"] = "running"
    engine.emit(task["id"], "stage.started", "开始口播生成", "talking_head", "running")
    engine._save_task(task)
    engine.schedule(task["id"])
    return Task(**task)


@app.get("/api/v1/tasks/{task_id}", response_model=Task)
def get_task(task_id: str):
    try:
        return Task(**engine.get_task(task_id))
    except KeyError:
        fail("VALIDATION_FAILED", "任务不存在", 404)


@app.get("/api/v1/tasks/{task_id}/events", response_model=List[TaskEvent])
def task_events(task_id: str, after_seq: int = 0):
    return [TaskEvent(**e) for e in st.events_since(task_id, after_seq)]


@app.post("/api/v1/tasks/{task_id}/cancel", response_model=Task)
def cancel_task(task_id: str):
    try:
        task = engine.get_task(task_id)
    except KeyError:
        fail("VALIDATION_FAILED", "任务不存在", 404)
    task["state"] = "cancelled"
    engine.emit(task_id, "task.cancelled", "已停止推进；已受理的外部作业仍需对账", task["stage"], "cancelled")
    return Task(**engine._save_task(task))


# ---------- 发布就绪包 ----------

@app.get("/api/v1/works/{work_id}/publish-package")
def get_package(work_id: str):
    _work(work_id)
    return delivery.current_package(work_id)


@app.get("/api/v1/publish-packages/{package_id}/download")
def download_package(package_id: str):
    try:
        path = delivery.download_path(package_id)
    except delivery.DeliveryBlocked as exc:
        fail(exc.code, exc.message, 409)
    return FileResponse(path, media_type="application/zip", filename="数字人口播交付包.zip")


# ---------- T05：数字人口播 ----------
#
# 这些端点只做"编排与门禁"，不自己发明结论：
# 路线能力、前置阻断、分段、费用确认、原片审核全部来自 `app.digital_human`，
# HTTP 层只负责把两类失败语义映射成 422/409。

@app.get("/api/v1/talking-head/routes")
def talking_head_routes(work_id: Optional[str] = None):
    """路线能力视图：声明（declared）与本项目实测（verified）分列，不合并成一个"可用"。

    带 `work_id` 时把每条路线的**费用提示**一并给出：高成本路线的提示里包含估价
    （音频已就绪时）与未核验风险，界面必须在用户选择前展示它。
    """
    audio_ms = None
    segments = 1
    selected_route = None
    route_confirmed = False
    if work_id:
        audio = tts_audio.final_audio(work_id)
        audio_ms = (audio or {}).get("duration_ms") or None
        w = st.get("works", work_id) or {}
        selected_route = w.get("route") or None
        # 与 _create_task 的回填同源：有没有一条可追溯的人工路线选择。
        # 作品 route 字段本身带默认值 M2，不能拿它判断"选过没有"——
        # 默认值不是授权，界面必须能区分"已选 M2"和"还没选、默认指向 M2"。
        route_confirmed = bool(
            [s for s in st.all_rows("selections", "work_id=?", [work_id])
             if s.get("kind") == "route" and s.get("selected_ref")]
        )
    routes = [
        {**r, "cost_notice": dh_cost.cost_notice(r["route"], duration_ms=audio_ms,
                                                 segment_count=segments,
                                                 resolution="720p" if r["route"] == dh_routes.ROUTE_WAN else "1080p")}
        for r in dh_routes.routes_view()
    ]
    return {
        "routes": routes,
        "price_snapshots": [dh_cost.current_price(r) for r in dh_routes.ROUTES],
        "scenarios": dh_cost.scenario_table(),
        "cost_notice_version": dh_routes.COST_NOTICE_VERSION,
        # 作品当前的路线与"是否已经有一次可追溯的人工确认"。
        # 缺 work_id 时两者都没有意义，如实给空，不要拿默认值填空。
        "selected_route": selected_route,
        "route_confirmed": route_confirmed,
        "note": "declared 是服务方文档口径；verified 为空表示本项目尚未实测。"
                "两者不可相互替代，估价同理不等于付费授权。"
                "高成本路线（cost_notice_required=true）必须先确认费用提示才能选择。",
    }


@app.get("/api/v1/works/{work_id}/talking-head/preflight")
def talking_head_preflight(work_id: str, route: Optional[str] = None):
    """结构化前置判定：返回 blockers 而不是直接抛错，便于逐项展示。"""
    _work(work_id)
    task = _current_task(work_id)
    if route and route not in dh_routes.ROUTES:
        # 允许请求"未实现路线"来查询原因：多阶段路线给 422（请求不成立），
        # 而不是装作它和 OmniHuman 等价。
        _dh(dh_routes.route_spec, route)
    return _dh(dh_routes.preflight, work_id, task=task, route=route)


@app.post("/api/v1/works/{work_id}/motion-intent")
def set_motion_intent(work_id: str, payload: Optional[Dict[str, Any]] = None):
    """登记 M2 的动作意图（挂在作品上）。

    为什么必须有这个写入方：`create_plate` 是从**作品**读 `motion_intent` 的，
    而在此之前全后端没有任何地方写它——界面上那个"动作模板"下拉框只绑了本地变量，
    连请求都不发。结果是 M2 在界面上必然卡在 `MOTION_REQUIRED`：门禁在读一个
    永远为空的作品字段。门禁没错，是缺了它的写入方。

    **先解析、再落库**：解析不过就原样拒收（422/409），不允许先把一个将来
    必然被拒的意图存下来——先存后拒的话，界面上会显示"已选好动作"，
    直到提交才被告知不行，等于把问题推迟到更贵的那一步才暴露。

    解析成功后把 `motion_ref_id` 一并固化：点名的那条参考是**绑定**不是线索，
    否则同一 motion_key 下换一条参考顶上，输出上看不出来。
    OmniHuman 那条路线用自由描述，不走这里（`resolve_motion_intent` 在 M2 下会以
    `MOTION_FREETEXT_UNSUPPORTED` 拒掉自由文本）。
    """
    w = _work(work_id)
    identity_id = w.get("identity_id") or ""
    if not identity_id:
        fail("SELECTION_REQUIRED", "先选定已定版人物，动作意图才有可映射的对象", 422)
    p = payload or {}
    intent: Dict[str, Any] = {"motion_key": p.get("motion_key") or "",
                              "params": p.get("params") or {}}
    # 调用方给的字段必须原样带进解析：只挑自己认识的两个字段，
    # 会让 `free_text` 被**静默丢掉**并返回 200——用户以为登记了自由描述，
    # 实际什么都没登记。带进去才有机会被 M2 明确拒掉（MOTION_FREETEXT_UNSUPPORTED）。
    for key in ("motion_ref_id", "free_text"):
        if p.get(key):
            intent[key] = p[key]
    try:
        resolved = motion.resolve_motion_intent(identity_id, intent, route="M2")
    except AssetRejected as e:
        _asset_rejected(e)
    except LicenseBlocked as e:
        _license_blocked(e)
    previous = w.get("motion_intent") or {}
    new_intent = {"motion_key": resolved["motion_key"],
                  "params": resolved["params"],
                  "motion_ref_id": resolved["motion_ref_id"]}
    changed = any((previous.get(key) or ({} if key == "params" else None)) != value
                  for key, value in new_intent.items())
    w["motion_intent"] = new_intent
    w["version"] = w.get("version", 1) + 1
    Work(**w)  # 先过契约再落库：写入方与契约不同步曾让整表端点 500
    st.put("works", work_id, w)
    if changed:
        _dh(dh_gates.on_motion_change, work_id, resolved["motion_ref_id"])
    return {"work_id": work_id, "motion_intent": w["motion_intent"], "resolved": resolved}


@app.post("/api/v1/works/{work_id}/talking-head/segments")
def talking_head_segments(work_id: str, payload: Optional[Dict[str, Any]] = None):
    """分段提交已经退休；历史分段状态仍可通过 GET 查询。"""
    _work(work_id)
    fail("OMNIHUMAN_SEGMENT_SUBMISSION_RETIRED",
         "OmniHuman 分段生成功能已退休；请使用小于 60 秒的整轨音频", 410)


@app.get("/api/v1/works/{work_id}/talking-head/segments")
def talking_head_segment_status(work_id: str):
    _work(work_id)
    return _dh(dh_segments.segment_status, work_id)


@app.get("/api/v1/works/{work_id}/talking-head/plate")
def talking_head_plate_status(work_id: str, variant_id: Optional[str] = None):
    _work(work_id)
    result = _dh(dh_plates.plate_status, work_id, variant_id=variant_id)
    source = (result.get("preparation") or {}).get("source")
    if source and source.get("storage_uri"):
        uri = source["storage_uri"]
        source["url"] = uri if uri.startswith(("https://", "http://")) else _media_url(uri)
    for plate in result.get("plates", []):
        asset = dh_videos.get_video(plate["id"]) or {}
        uri = asset.get("storage_uri") or ""
        plate["url"] = delivery.media_url(uri) if uri and not asset.get("is_mock") and not uri.startswith(("http://", "https://")) else None
    return result


def _m2_transport():
    """M2 的远端 GPU 通道。

    未配置 worker 地址时返回 None —— 让既有的 M2_TRANSPORT_UNAVAILABLE
    blocked 路径生效。这里故意不放兜底实现：返回一个"看起来能用"的假通道，
    界面上就会显示提交成功，而实际上什么都没执行。
    """
    return dh_worker.make_transport()


@app.post("/api/v1/works/{work_id}/talking-head/plate")
def talking_head_create_plate(work_id: str, payload: Optional[Dict[str, Any]] = None):
    """生成动态底片（M2 第一阶段）。Mock 模式只返回请求构造，不登记任何产物。"""
    work = _work(work_id)
    p = payload or {}
    task = _current_task(work_id)
    selected = p.get("variant_id")
    is_mock = config.resolve_mode() == "mock" if p.get("dry_run") is None else bool(p["dry_run"])
    if selected and selected != work.get("variant_id") and not is_mock:
        preparation = _dh(dh_plates.plate_status, work_id, variant_id=selected)["preparation"]
        if not preparation.get("ok"):
            blocker = preparation["blockers"][0]
            fail(blocker["code"], blocker["message"], 409)
        # 点击生成时的明确选图必须成为本期选图，否则底片生成后仍被旧绑定拒绝。
        current = _work(work_id)
        if current != work:
            fail("STALE_SELECTION", "作品已变化，请刷新后重新选择出镜画面", 409)
        current["variant_id"] = selected
        current["video_asset_id"] = None
        current["version"] = current.get("version", 1) + 1
        st.put("works", work_id, current)
        _record_selection(work_id, "variant", selected,
                          digest=(preparation.get("source") or {}).get("asset_hash", ""),
                          reason="在生成动作底片时选定本期出镜画面")
        from . import lineage
        lineage.invalidate(work_id=work_id, change_type="motion", source_artifact_id=selected, actor="user")
        for video in dh_videos.list_videos(work_id):
            dh_videos.revoke(video["id"], "本期出镜画面已改变，须重新生成并审核")
    return _dh(dh_plates.create_plate, work_id,
               variant_id=p.get("variant_id"),
               output_seconds=p.get("output_seconds"),
               dry_run=p.get("dry_run"),
               transport=_m2_transport(),
               task_id=(task or {}).get("id"))


@app.post("/api/v1/talking-head/plates/{plate_id}/review")
def talking_head_plate_review(plate_id: str, payload: Dict[str, Any]):
    """底片审核。通过只能由用户给出——身份是否漂移只能由人目检。"""
    p = payload or {}
    video = dh_videos.get_video(plate_id) or {}
    audio = tts_audio.final_audio(video.get("work_id") or "")
    audio_ms = p.get("audio_duration_ms")
    if audio_ms is None and audio:
        audio_ms = audio.get("duration_ms")
    return _dh(dh_plates.record_plate_review, plate_id,
               reviewer=p.get("reviewer", ""),
               outcome=p.get("outcome", ""),
               audio_duration_ms=audio_ms,
               identity_digest=p.get("identity_digest", ""),
               checks=p.get("checks"),
               note=p.get("note", ""))


@app.post("/api/v1/works/{work_id}/talking-head/cost-confirmation")
def talking_head_confirm_cost(work_id: str, payload: Dict[str, Any]):
    """费用确认。清单摘要由服务端从当前绑定推导，不接受客户端自报的摘要。"""
    _work(work_id)
    p = payload or {}
    route = p.get("route") or (_work(work_id).get("route")) or dh_routes.ROUTE_OMNIHUMAN
    task = _current_task(work_id)
    prompt = p.get("prompt", "")
    resolution = p.get("resolution") or ("720p" if route == dh_routes.ROUTE_WAN else "1080p")
    wan_options = {"duration_seconds": p.get("duration_seconds", 5)} if route == dh_routes.ROUTE_WAN else {}
    listing = _dh(dh_sub.current_call_list, work_id, route=route, prompt=prompt,
                  resolution=resolution, **wan_options)
    audio = tts_audio.final_audio(work_id)
    est = dh_cost.estimate(route, duration_ms=int((audio or {}).get("duration_ms") or 0),
                           segment_count=listing["segment_count"], resolution=resolution, **wan_options)
    return _dh(dh_cost.confirm_cost, work_id, route=route,
               request_digest=listing["digest"],
               estimated_fen=est["estimated_fen"], ceiling_fen=est["ceiling_fen"],
               segment_count=listing["segment_count"],
               generated_seconds=est.get("generated_seconds", est["audio_seconds"]), resolution=resolution,
               actor_id=p.get("actor_id", "user_ui"),
               task_id=(task or {}).get("id"), call_list=listing)


@app.get("/api/v1/works/{work_id}/talking-head/cost")
def talking_head_cost(work_id: str, route: Optional[str] = None,
                      prompt: str = "", resolution: Optional[str] = None,
                      duration_seconds: int = 5):
    """费用视图。

    估价必须有两个输入：路线（默认作品已绑定路线）与计费时长（最终音频的
    实际时长）。最终音频未就绪时估价值为 null——没有时长就没有"生成秒"，
    任何数字都会是编造；此时界面不显示估价，由前置检查如实给出阻断原因。
    """
    work = _work(work_id)
    audio = tts_audio.final_audio(work_id)
    route = route or work.get("route")
    resolution = resolution or ("720p" if route == dh_routes.ROUTE_WAN else "1080p")
    wan_options = {"duration_seconds": duration_seconds} if route == dh_routes.ROUTE_WAN else {}
    return _dh(dh_cost.cost_summary, work_id,
               route=route,
               duration_ms=(audio or {}).get("duration_ms"),
               prompt=prompt, resolution=resolution, **wan_options)


@app.post("/api/v1/works/{work_id}/talking-head/submissions")
def talking_head_submit(work_id: str, payload: Optional[Dict[str, Any]] = None):
    """提交口播生成。路线取自任务已确认路线；mock 模式默认 dry_run。"""
    work = _work(work_id)
    p = payload or {}
    task = _current_task(work_id)
    route = _dh(dh_routes.assert_route_selected, task, p.get("route"))
    if route == dh_routes.ROUTE_WAN:
        from .digital_human import wan, wan_provider
        return _dh(wan.submit_wan, work_id, task=task, prompt=p.get("prompt", ""),
                   duration_seconds=p.get("duration_seconds", 5),
                   resolution=p.get("resolution") or "720p", dry_run=p.get("dry_run"),
                   transport=wan_provider.urllib_transport,
                   replacement_authorized=bool(p.get("replacement_authorized")))
    audio = tts_audio.final_audio(work_id)
    # 文生视频负例针对的是"这次请求到底有没有固定人物图与音频"。
    # 人物图与音频由**作品绑定**提供，只看 payload 的键会把正常提交
    # （只带表演描述）误判成文生视频；因此这里把作品绑定并入判定依据。
    _dh(dh_routes.reject_text_to_video, {
        "image_asset_id": p.get("image_asset_id") or work.get("variant_id")
                          or work.get("identity_id"),
        "audio_asset_id": p.get("audio_asset_id") or (audio or {}).get("id"),
    })
    if route == dh_routes.ROUTE_OMNIHUMAN:
        return _dh(dh_sub.submit_omnihuman, work_id, task=task,
                   prompt=p.get("prompt", ""), resolution=p.get("resolution") or "1080p",
                   dry_run=p.get("dry_run"),
                   transport=dh_providers.urllib_transport,
                   replacement_authorized=bool(p.get("replacement_authorized")))
    if route == dh_routes.ROUTE_M2:
        return _dh(dh_sub.submit_m2, work_id, task=task, dry_run=p.get("dry_run"),
                   transport=_m2_transport(),
                   replacement_authorized=bool(p.get("replacement_authorized")))
    fail("ROUTE_UNSUPPORTED", "未配置该视频路线的提交实现")


@app.get("/api/v1/works/{work_id}/talking-head/submissions")
def talking_head_submissions(work_id: str):
    _work(work_id)
    return _dh(dh_sub.submission_status, work_id)


@app.get("/api/v1/works/{work_id}/talking-head/driver")
def talking_head_driver(work_id: str):
    """分段进度快照（只读）。界面据此决定要不要显示"继续生成剩余分段"。"""
    _work(work_id)
    return _dh(dh_driver.progress, work_id)


@app.post("/api/v1/works/{work_id}/talking-head/driver/resume")
def talking_head_driver_resume(work_id: str):
    """分段续发接口已退休；历史运行由后台只读查询。"""
    _work(work_id)
    fail("OMNIHUMAN_SEGMENT_SUBMISSION_RETIRED",
         "OmniHuman 分段续发已退休；仅保留已提交作业查询，不能续发新分段", 410)


@app.post("/api/v1/works/{work_id}/talking-head/stitch")
def talking_head_stitch(work_id: str, payload: Optional[Dict[str, Any]] = None):
    """分段视频拼接已经退休；历史资产保留供 GET 查询与人工核对。"""
    _work(work_id)
    fail("OMNIHUMAN_SEGMENT_SUBMISSION_RETIRED",
         "OmniHuman 分段视频拼接已退休；新任务只生成单条整轨视频", 410)


def _run_transport(run_id: str):
    """按持久化 provider 显式选择算力云、火山或百炼通道。

    不能统一给 urllib_transport —— M2 的 request 没有 url 字段，
    直接喂进去只会 KeyError，而那会被当成"服务器内部错误"，
    掩盖真实的"通道没接"。
    """
    run = dh_sub.get_run(run_id)
    provider = (run or {}).get("provider")
    if provider == "m2_worker":
        return _m2_transport()
    if provider == "volcengine":
        return dh_providers.urllib_transport
    if provider == "dashscope_wan":
        from .digital_human.wan_provider import urllib_transport
        return urllib_transport
    return None


@app.post("/api/v1/talking-head/runs/{run_id}/poll")
def talking_head_poll(run_id: str):
    """按原 task_id 查询。提交成功只是"已受理"，不等于成功。"""
    return _dh(dh_sub.poll_run, run_id, transport=_run_transport(run_id))


@app.post("/api/v1/talking-head/runs/{run_id}/settle")
def talking_head_settle(run_id: str, payload: Dict[str, Any]):
    """未知状态对账。不得凭猜测改判成功或失败。"""
    p = payload or {}
    run = dh_sub.get_run(run_id) or {}
    if run.get("provider") == "dashscope_wan":
        from .digital_human.wan import _poll_lock
        with _poll_lock(run_id):
            return _dh(dh_sub.settle_unknown, run_id, p.get("outcome", ""), p.get("evidence") or {})
    return _dh(dh_sub.settle_unknown, run_id, p.get("outcome", ""),
               p.get("evidence") or {})


@app.get("/api/v1/works/{work_id}/talking-head/videos")
def talking_head_videos(work_id: str, kind: Optional[str] = None):
    _work(work_id)
    rows = dh_videos.list_videos(work_id, kind)
    return [dict(r, hard_failures=dh_videos.hard_failures(r),
                 url=delivery.media_url(r["storage_uri"]) if r.get("storage_uri") and not r.get("is_mock")
                     and not r["storage_uri"].startswith(("http://", "https://")) else None) for r in rows]


@app.get("/api/v1/talking-head/videos/{video_id}/gate")
def talking_head_video_gate(video_id: str):
    """原片门禁视图：四维度审核 + 音轨保持分开呈现，互不顶替。"""
    video = dh_videos.get_video(video_id)
    if not video:
        fail("VIDEO_NOT_FOUND", f"视频不存在：{video_id}", 404)
    return {
        "video_id": video_id,
        "kind": video.get("kind"),
        "gate": dh_gates.video_gate(video_id),
        "audio_track": dh_gates.audio_track_status(video_id),
        "hard_failures": dh_videos.hard_failures(video),
        "note": "视频画面通过不蕴含音轨保持；两条门禁分别记录、分别核对",
    }


@app.post("/api/v1/talking-head/videos/{video_id}/review")
def talking_head_video_review(video_id: str, payload: Dict[str, Any]):
    p = payload or {}
    return _dh(dh_gates.record_video_review, video_id,
               reviewer=p.get("reviewer", ""),
               outcome=p.get("outcome", ""),
               dimensions=p.get("dimensions") or {},
               references=p.get("references"),
               issues=p.get("issues"),
               note=p.get("note", ""))


@app.post("/api/v1/talking-head/videos/{video_id}/audio-track-checks")
def talking_head_audio_track_check(video_id: str, payload: Dict[str, Any]):
    p = payload or {}
    return _dh(dh_gates.record_audio_track_check, video_id,
               audio_asset_id=p.get("audio_asset_id", ""),
               method=p.get("method", ""),
               outcome=p.get("outcome", ""),
               measured_offset_ms=p.get("measured_offset_ms"),
               loudness_delta_db=p.get("loudness_delta_db"),
               evidence=p.get("evidence", ""),
               note=p.get("note", ""))


@app.post("/api/v1/works/{work_id}/talking-head/final-video")
def talking_head_confirm_final(work_id: str, payload: Dict[str, Any]):
    """确认最终原片。占位产物不可确认，底片不可冒充原片。"""
    p = payload or {}
    return _dh(dh_gates.confirm_final_video, work_id, p.get("video_id", ""),
               actor_id=p.get("actor_id", ""),
               review_ack=bool(p.get("review_ack")),
               note=p.get("note", ""))


@app.get("/api/v1/works/{work_id}/talking-head/gate")
def talking_head_gate(work_id: str):
    _work(work_id)
    return _dh(dh_gates.gate_summary, work_id)


@app.get("/api/v1/talking-head/queue")
def talking_head_queue(work_id: Optional[str] = None):
    """重型作业队列视图。GPU 忙只是排队，不是失败。"""
    return dh_queue.queue_view(work_id)


@app.get("/api/v1/talking-head/adapters")
def talking_head_adapters():
    """适配器状态：凭据是否就位、是否已实测，逐项如实报告。"""
    return dh_providers.adapter_status()


# ---------- 内部辅助 ----------

def _create_task(work_id: str, route: str) -> dict:
    w = _work(work_id)
    task = Task(id=st.new_id("task"), work_id=work_id, mode=MODE, route=route, stage="topic", state="queued")
    task.input_snapshot_digest = _snapshot_digest(w or {})
    # 若路线先于任务确认，创建任务时把既有的人工决定回填冻结。
    # route_confirmed 必须一起回填：用户已经显式确认过路线，只是任务当时还不存在。
    # 只回填 route_selection_id 而漏掉确认标记，会让提交被"路线尚未确认"拦下——
    # 用户明明做过这个动作，系统却忘了一半。
    prior = [s for s in st.all_rows("selections", "work_id=?", [work_id])
             if s.get("kind") == "route" and s.get("selected_ref")]
    if prior:
        task.route_selection_id = prior[-1]["id"]
        task.route = prior[-1]["selected_ref"]
        task.route_confirmed = True
    task.stage_rows = st.stage_rows("topic", "queued")
    data = task.model_dump()
    with st.transaction() as conn:
        w = _work(work_id)
        st.insert_row(conn, "tasks", data["id"], data, {"work_id": work_id})
        w["current_task_id"] = data["id"]
        st.insert_row(conn, "works", work_id, w)
    engine.emit(data["id"], "task.created", "任务已创建，等待编排", "topic", "queued")
    return data


def _current_task(work_id: str) -> Optional[dict]:
    w = st.get("works", work_id)
    tid = (w or {}).get("current_task_id")
    if not tid:
        return None
    return st.get("tasks", tid)


def _record_selection(work_id: str, kind: SelectionKind, ref: str, digest: str = "",
                      reason: str = "", connection=None) -> SelectionDecision:
    """记录一条人工选择决定。

    actor 恒为 user（契约强制）；调用点全部来自用户显式操作接口，
    Agent 无写入本表的路径。返回的决定 id 可作为 route_selection_id 等冻结引用。

    `kind` 刻意标注为 `SelectionKind` 而不是 `str`：取值必须来自契约枚举，
    不许临时拼一个近义字面量（曾把选人物写成 "avatar"，契约里只有 "identity"，
    于是 SelectionOption 校验失败 → 端点 500 → 界面报"不是合法 JSON"）。
    """
    req = SelectionRequest(
        id=st.new_id("selreq"), work_id=work_id, kind=kind,
        prompt=f"请选择{kind}", options=[SelectionOption(option_id=f"opt_{ref}", kind=kind,
                                                         ref_id=ref, label=ref, digest=digest)],
    )
    writer = (lambda key, data: st.insert_row(connection, "selections", key, data, {"work_id": work_id})) if connection else (
        lambda key, data: st.put("selections", key, data, {"work_id": work_id}))
    writer(req.id, dict(req.model_dump(), state="decided"))
    d = SelectionDecision(id=st.new_id("sel"), request_id=req.id, work_id=work_id, kind=kind,
                          selected_option_id=f"opt_{ref}", selected_ref=ref,
                          selected_digest=digest, reason=reason)
    payload = dict(d.model_dump(), state="selected")
    writer(d.id, payload)
    return d


def _snapshot_digest(w: dict) -> str:
    """输入快照：身份、音色、稿件、路线任一变化即产生新摘要，用于判定产物过期。"""
    import hashlib
    import json

    raw = json.dumps({k: w.get(k) for k in ("identity_id", "voice_asset_id", "script_version_id", "route")},
                     ensure_ascii=False, sort_keys=True)
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _require_script_selected(work_id: str) -> None:
    if not _script_selected(work_id):
        fail("SELECTION_REQUIRED", "必须先采用一稿通过审核的稿件")


def _script_selected(work_id: str) -> bool:
    """当前是否有一份仍然有效的稿件选用。

    历史 selected 记录不算：必须与 work.script_version_id 一致，且该稿当前仍通过门禁与独立审核。
    """
    from .content import selection as content_selection

    return content_selection.current_selection_valid(work_id)


def _blocked_task(task: dict, code: str, message: str, action: str = "") -> Task:
    """把任务置为结构化阻断并持久化，返回可读的 Task。

    阻断原因要能被前端直接渲染（code/message/action），而不是只给一个状态码。
    """
    task["state"] = "blocked"
    task["block_reason"] = code
    task["last_error"] = ApiError(code=code, message=message, action=action).model_dump()
    return Task(**engine._save_task(task))


@app.get("/api/v1/health")
def health():
    return {"ok": True, "mode": MODE, "db": st.db_label()}
