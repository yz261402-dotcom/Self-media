"""音色资产作用域与受控登记（AC-TTS-01、AC-TTS-03 的资产侧）。

三条不可违背的规则：

1. **作用域是四元组，不是字符串**。`provider`/`model`/`region`/`remote_voice_id`
   必须作为组合解释。不同服务的同名 voice ID 语义不同，跨作用域复用一律拒收；
   只给一个 `remote_voice_id` 就要求复用，属于来源不清，同样拒收。
2. **预设音色不等于自动合格**。登记预设音色要同时具备：官方作用域、使用条款、
   本项目选择记录、以及本项目的基准合成证据（真实音频 + 哈希）。
   缺少基准合成证据时，资产可以登记，但生产就绪判定必然失败——
   登记保留来源链，不等于获准进入生产。
3. **换声必须新版本**。同一 ID 不得被覆盖；旧作品继续引用旧版本，
   不因为库里出现了新音色而被改写。
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Any, Dict, List, Optional, Tuple

from .. import storage as st
from ..contracts import VoiceAsset, now_iso
from .errors import TtsRejected

_HASH_RE = re.compile(r"^sha256:[0-9a-f]{16,}$")

# 本项目已核验的 MiniMax 作用域。只认官方 .cn 文档域名那条链路：
# "同一官方服务的其他文档域名不能视为均已测通"，因此 region 只允许 cn。
MINIMAX_REGION = "cn"
# 不改为 Turbo：模型名是作用域的一部分，不能悄悄换型号
MINIMAX_TTS_MODEL = "speech-2.8-hd"

PRESET_VOICE_ID = "voice_minimax_warm_bestie_v1"
PRESET_REMOTE_VOICE_ID = "Chinese (Mandarin)_Warm_Bestie"
# 已选样音基础参数（权威来源：specs/selection/03-人物与音色/02 定版记录）
PRESET_SYNTHESIS_PROFILE: Dict[str, Any] = {
    "speed": 1.0,
    "vol": 1.0,
    "pitch": 0,
    "emotion": "calm",
    "language_boost": "Chinese",
}

# 各供应商允许的模型白名单。作用域里的 model 不在白名单即拒收，
# 避免"换个型号继续用同一个音色 ID"这种静默降级。
ALLOWED_MODELS: Dict[str, Tuple[str, ...]] = {
    "minimax": (MINIMAX_TTS_MODEL,),
}

# 已实测支持的表达控制。未列入的能力在调用前报告为不支持，不假装已生效。
SUPPORTED_CONTROLS = ("speed", "vol", "pitch", "emotion", "language_boost", "voice_modify")
# 官方声明不支持的能力：即使通用枚举里出现也不得启用
DECLARED_UNSUPPORTED = ("whisper",)

# 允许出现在参数里的键；未知键一律拒绝而不是透传给供应商
ALLOWED_PROFILE_KEYS = ("speed", "vol", "pitch", "emotion", "language_boost", "seed", "voice_modify")
WORK_CONTROL_KEYS = ("speed", "vol", "pitch", "emotion", "voice_modify")
EMOTIONS = ("happy", "sad", "angry", "fearful", "disgusted", "surprised", "calm")
CONTROL_SCHEMA = {
    "emotion": {"options": ["auto", *EMOTIONS], "auto_omitted": True},
    "speed": {"min": 0.5, "max": 2.0},
    "vol": {"exclusive_min": 0, "max": 10.0},
    "pitch": {"min": -12, "max": 12, "integer": True},
    "voice_modify": {k: {"min": -100, "max": 100, "integer": True}
                     for k in ("pitch", "intensity", "timbre")},
}

# seed 只是参数，不单独构成音色一致证据；且不是所有服务都支持
SEED_IS_PARAMETER_ONLY_NOTE = "固定 seed 只记为参数，不单独算音色一致证据"


def digest_of(obj: Any) -> str:
    payload = obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False, sort_keys=True)
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def scope_key(provider: str, model: str, region: str, remote_voice_id: str) -> str:
    """把四元组规范化成一个可比较的键。顺序固定，避免"参数序不同即不同作用域"。"""
    return "|".join([(provider or "").strip().lower(), (model or "").strip(),
                     (region or "").strip().lower(), (remote_voice_id or "").strip()])


def voice_scope(voice: Dict[str, Any]) -> Dict[str, Any]:
    """读出一个音色资产的作用域四元组与规范化键。"""
    provider = voice.get("provider", "")
    model = voice.get("model", "")
    region = voice.get("region", "")
    remote = voice.get("remote_voice_id", "")
    return {
        "provider": provider, "model": model, "region": region,
        "remote_voice_id": remote,
        "scope_key": scope_key(provider, model, region, remote),
        "complete": all([provider, model, region, remote]),
    }


# ---------- 读取 ----------

def get_voice(voice_id: str) -> Optional[Dict[str, Any]]:
    return st.get("voices", voice_id) if voice_id else None


def list_voices(only_approved: bool = False) -> List[Dict[str, Any]]:
    rows = st.all_rows("voices")
    if only_approved:
        rows = [r for r in rows if r.get("approval_state") == "approved"]
    return rows


# ---------- 作用域校验 ----------

def assert_scope_complete(voice: Dict[str, Any]) -> Dict[str, Any]:
    sc = voice_scope(voice)
    if not sc["complete"]:
        raise TtsRejected(
            "VOICE_SCOPE_INCOMPLETE",
            "音色作用域不完整：provider/model/region/remote_voice_id 必须齐全，"
            "四者作为组合解释，不能仅凭字符串 ID 复用",
            {"voice_asset_id": voice.get("id"), "scope": sc},
        )
    return sc


def require_scope_match(voice: Dict[str, Any], *, provider: Optional[str] = None,
                        model: Optional[str] = None, region: Optional[str] = None,
                        remote_voice_id: Optional[str] = None) -> Dict[str, Any]:
    """调用方声明的作用域必须与资产登记的作用域逐项一致。

    `remote_voice_id` 单独出现时不足以定位一个音色——四元组里的其余三项
    缺任意一项即视为"仅凭字符串 ID 复用"，直接拒收。
    """
    sc = assert_scope_complete(voice)
    supplied = {"provider": provider, "model": model, "region": region,
                "remote_voice_id": remote_voice_id}
    given = {k: v for k, v in supplied.items() if v not in (None, "")}
    if given and set(given) != {"provider", "model", "region", "remote_voice_id"}:
        raise TtsRejected(
            "VOICE_SCOPE_PARTIAL",
            f"作用域声明不完整：只提供了 {sorted(given)}，"
            "必须同时给出 provider/model/region/remote_voice_id，不接受只给 voice ID",
            {"voice_asset_id": voice.get("id"), "supplied": sorted(given)},
        )
    mismatch = {k: {"expected": sc[k], "actual": supplied[k]}
                for k in supplied if supplied[k] not in (None, "") and supplied[k] != sc[k]}
    if mismatch:
        raise TtsRejected(
            "VOICE_SCOPE_MISMATCH",
            "作用域不符：跨作用域复用同一音色是明确负例，必须新建独立 VoiceAsset 并重新验收",
            {"voice_asset_id": voice.get("id"), "mismatch": mismatch},
        )
    return sc


def assert_model_allowed(provider: str, model: str) -> None:
    allowed = ALLOWED_MODELS.get((provider or "").lower())
    if allowed is None:
        raise TtsRejected("VOICE_PROVIDER_UNSUPPORTED",
                          f"供应商未在本工程受支持清单内：{provider}",
                          {"provider": provider})
    if model not in allowed:
        raise TtsRejected(
            "VOICE_MODEL_NOT_ALLOWED",
            f"模型 {model} 未经本项目核验；{provider} 只允许 {list(allowed)}。"
            "型号未经实测即换用属于静默降级",
            {"provider": provider, "model": model, "allowed": list(allowed)},
        )


def assert_region_verified(provider: str, region: str) -> None:
    """只有已实测的 region 可用；其他域名不能因"同一官方服务"而推定已测通。"""
    if (provider or "").lower() == "minimax" and (region or "").lower() != MINIMAX_REGION:
        raise TtsRejected(
            "VOICE_SCOPE_UNVERIFIED",
            f"region={region} 未经实测：MiniMax 仅核验过官方 .cn 链路，"
            "其他域名不得推定已测通，超时后也不自动换域名重提",
            {"provider": provider, "region": region, "verified_region": MINIMAX_REGION},
        )


def normalize_profile(profile: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """校验合成参数：未知键拒收（不透传），不支持的能力显式报告。"""
    if profile is not None and not isinstance(profile, dict):
        raise TtsRejected("VOICE_PROFILE_INVALID", "语音参数必须是对象")
    prof = dict(profile or {})
    # 先判"声明不支持"再判"未知键"：whisper 若落到"未知键"分支，
    # 报错就从"该能力官方不支持"退化成"拼错了字段名"，误导调用方。
    for key in DECLARED_UNSUPPORTED:
        if prof.get(key) not in (None, "", False):
            raise TtsRejected(
                "VOICE_CONTROL_UNSUPPORTED",
                f"{key} 官方声明不支持，不得因通用枚举里出现就启用",
                {"key": key},
            )
    unknown = sorted(set(prof) - set(ALLOWED_PROFILE_KEYS))
    if unknown:
        raise TtsRejected("VOICE_PROFILE_KEY_UNKNOWN",
                          f"合成参数含未声明键：{unknown}",
                          {"unknown": unknown, "allowed": list(ALLOWED_PROFILE_KEYS)})
    for key, lo, hi, integer in (("speed", 0.5, 2, False), ("vol", 0, 10, False),
                                ("pitch", -12, 12, True)):
        if key not in prof:
            continue
        val = prof[key]
        if (isinstance(val, bool) or not isinstance(val, (int, float))
                or (isinstance(val, float) and not math.isfinite(val)) or (integer and not isinstance(val, int))
                or val < lo or val > hi or (key == "vol" and val == 0)):
            raise TtsRejected("VOICE_CONTROL_VALUE_INVALID", f"{key} 超出允许范围或类型无效",
                              {"key": key, "range": CONTROL_SCHEMA[key]})
        if not integer:
            prof[key] = float(val)
    if "emotion" in prof and prof["emotion"] not in ("auto", *EMOTIONS):
        raise TtsRejected("VOICE_CONTROL_VALUE_INVALID", "情绪须为自动或七种支持的情绪")
    if "voice_modify" in prof:
        effect = prof["voice_modify"]
        if not isinstance(effect, dict) or set(effect) - set(CONTROL_SCHEMA["voice_modify"]):
            raise TtsRejected("VOICE_CONTROL_VALUE_INVALID", "声音效果器仅支持 pitch/intensity/timbre")
        for val in effect.values():
            if isinstance(val, bool) or not isinstance(val, int) or not -100 <= val <= 100:
                raise TtsRejected("VOICE_CONTROL_VALUE_INVALID", "声音效果器必须为 -100 到 100 的整数")
        prof["voice_modify"] = dict(effect)
    return prof


def normalize_controls(controls: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(controls, dict) or set(controls) - set(WORK_CONTROL_KEYS):
        raise TtsRejected("VOICE_CONTROL_VALUE_INVALID", "作品参数只允许情绪、语速、音量、音调与声音效果器")
    return normalize_profile(controls)


def effective_profile(work: Optional[Dict[str, Any]], voice: Dict[str, Any]) -> Dict[str, Any]:
    """统一的实际参数：作品覆盖音色默认，不改共享音色资产。"""
    profile = {**PRESET_SYNTHESIS_PROFILE,
               **normalize_profile(voice.get("synthesis_profile") or {}),
               **normalize_controls((work or {}).get("tts_controls") or {})}
    effect = {"pitch": 0, "intensity": 0, "timbre": 0, **profile.get("voice_modify", {})}
    # 零值效果器与不发送效果器语义相同，保持缓存和摘要规范化。
    if any(effect.values()):
        profile["voice_modify"] = effect
    else:
        profile.pop("voice_modify", None)
    return normalize_profile(profile)


def controls_digest(voice: Dict[str, Any], profile: Dict[str, Any]) -> str:
    scope = assert_scope_complete(voice)
    return profile_digest(scope["provider"], scope["model"], scope["region"],
                          scope["remote_voice_id"], profile)


def profile_digest(provider: str, model: str, region: str, remote_voice_id: str,
                   profile: Dict[str, Any], text_digest: str = "") -> str:
    """试听缓存键的原料：声音作用域 + 参数 + 文本摘要，四者任一变化即不命中。"""
    return digest_of({
        "scope": scope_key(provider, model, region, remote_voice_id),
        "profile": normalize_profile(profile),
        "text_digest": text_digest,
    })


# ---------- 登记 ----------

def register_voice(*, voice_id: Optional[str] = None, name: str, provider: str, model: str,
                   remote_voice_id: str, region: str = MINIMAX_REGION, sample_text: str,
                   supported_emotions: Optional[List[str]] = None,
                   synthesis_profile: Optional[Dict[str, Any]] = None,
                   source_record_id: Optional[str] = None, license_id: Optional[str] = None,
                   baseline_audio_asset_id: Optional[str] = None, baseline_audio_hash: str = "",
                   is_preset: bool = False, approval_state: str = "pending",
                   supersedes_id: Optional[str] = None,
                   description: str = "", catalog_group: str = "",
                   note: str = "") -> Dict[str, Any]:
    """受控登记音色。缺作用域、缺来源证据、缺权属、超白名单模型一律拒收。

    预设音色额外要求基准合成证据：没有真实音频与哈希，就不算完成基准验证，
    即使 `approval_state="approved"` 也只表示"用户选定了这组外观与音色"。
    """
    if not (name or "").strip():
        raise TtsRejected("VOICE_NAME_MISSING", "音色名称不能为空")
    if not (sample_text or "").strip():
        raise TtsRejected("VOICE_SAMPLE_TEXT_MISSING",
                          "缺少基准试听短句：音色库必须先用同一短句试听")
    if not source_record_id:
        raise TtsRejected(
            "VOICE_SOURCE_MISSING",
            "来源不清：必须登记本项目的声音来源证据（预设音色选择记录、"
            "声音创建/注册回执或受控素材登记），不接受只提供一个未经本项目建立"
            "来源证据的 remote_voice_id",
            {"remote_voice_id": remote_voice_id},
        )
    # 四元组缺项先于"域名是否实测"判定：否则 region 写空会得到
    # "region 未经实测"这种误导性的报错，而真正的问题是作用域没写全。
    missing = [k for k, v in (("provider", provider), ("model", model),
                              ("region", region), ("remote_voice_id", remote_voice_id))
               if not (v or "").strip()]
    if missing:
        raise TtsRejected(
            "VOICE_SCOPE_INCOMPLETE",
            f"音色作用域缺项：{missing}。provider/model/region/remote_voice_id "
            "必须同时给出，四者作为组合解释",
            {"missing": missing},
        )
    assert_model_allowed(provider, model)
    assert_region_verified(provider, region)
    prof = normalize_profile(synthesis_profile or {})

    if is_preset and not license_id:
        raise TtsRejected("VOICE_LICENSE_MISSING",
                          "预设音色须保存官方使用条款与使用权记录，否则商业使用范围不可追溯")
    if license_id:
        # 登记阶段只挡"明确不可商用"；待核验允许登记以保留来源链，生产准入时再阻塞
        from ..avatar import licensing

        licensing.assert_registrable(license_id, subject=name)

    if is_preset and not (baseline_audio_asset_id and baseline_audio_hash):
        # 允许登记（保留来源链），但就绪判定会失败；此处只做显式提示不抛错，
        # 因为"预设尚未补基准证据"是待办状态而非非法输入。
        note = (note + "；预设尚无基准合成证据，生产就绪判定将失败").strip("；")

    if baseline_audio_hash and not _HASH_RE.match(baseline_audio_hash):
        raise TtsRejected("VOICE_BASELINE_HASH_INVALID",
                          f"基准音频哈希格式不合法：{baseline_audio_hash}")

    prior = get_voice(supersedes_id) if supersedes_id else None
    if supersedes_id and not prior:
        raise TtsRejected("VOICE_SUPERSEDES_NOT_FOUND",
                          f"被替换的音色不存在：{supersedes_id}")
    if prior:
        require_scope_match(prior)
        if scope_key(prior["provider"], prior["model"], prior.get("region", ""),
                     prior["remote_voice_id"]) != scope_key(provider, model, region, remote_voice_id):
            note = (note + "；跨作用域换声，属独立 VoiceAsset 而非新版本").strip("；")

    vid = voice_id or st.new_id("voice")
    existing = get_voice(vid)
    if existing:
        if existing.get("approval_state") == "approved" and not supersedes_id:
            raise TtsRejected(
                "VOICE_FROZEN",
                "已审批的音色不可被覆盖：换声必须创建新版本或独立绑定，旧作品继续引用旧版本",
                {"voice_asset_id": vid, "approval_state": existing.get("approval_state")},
            )
        if not supersedes_id:
            raise TtsRejected("VOICE_EXISTS",
                              f"音色已存在：{vid}。不覆盖旧记录，请显式给出 supersedes_id",
                              {"voice_asset_id": vid})

    asset = VoiceAsset(
        id=vid, name=name, provider=provider, model=model, region=region,
        remote_voice_id=remote_voice_id,
        approval_state="pending" if supersedes_id else approval_state,  # type: ignore[arg-type]
        is_preset=is_preset, sample_text=sample_text,
        description=description, catalog_group=catalog_group,
        supported_emotions=list(supported_emotions or []),
        synthesis_profile=prof,
        profile_digest=digest_of({"scope": scope_key(provider, model, region, remote_voice_id),
                                  "profile": prof}),
        source_record_id=source_record_id, license_id=license_id,
        baseline_audio_asset_id=baseline_audio_asset_id,
        baseline_audio_hash=baseline_audio_hash,
        supersedes_id=supersedes_id,
        version=int(prior["version"]) + 1 if prior else 1,
        note=note,
    )
    row = asset.model_dump()
    row["scope_key"] = scope_key(provider, model, region, remote_voice_id)
    row["created_at"] = now_iso()
    st.put("voices", asset.id, row)
    return row


def attach_baseline_synthesis(voice_id: str, audio_asset_id: str, audio_hash: str,
                              provider_run_id: str = "", note: str = "") -> Dict[str, Any]:
    """补登记基准合成证据。补齐后生产就绪判定才可能通过。"""
    voice = get_voice(voice_id)
    if not voice:
        raise TtsRejected("VOICE_NOT_FOUND", f"音色不存在：{voice_id}")
    if not audio_asset_id:
        raise TtsRejected("VOICE_BASELINE_MISSING", "缺少基准合成音频资产")
    if not _HASH_RE.match(audio_hash or ""):
        raise TtsRejected("VOICE_BASELINE_HASH_INVALID",
                          f"基准音频哈希格式不合法：{audio_hash}")
    voice["baseline_audio_asset_id"] = audio_asset_id
    voice["baseline_audio_hash"] = audio_hash
    if provider_run_id:
        voice["baseline_provider_run_id"] = provider_run_id
    if note:
        voice["note"] = (voice.get("note", "") + "；" + note).strip("；")
    voice["baseline_registered_at"] = now_iso()
    st.put("voices", voice_id, voice)
    return voice


def attach_baseline_from_preview(voice_id: str, work_id: str) -> Dict[str, Any]:
    """把某作品里这条音色的**真实试听**登记为本项目基准合成证据。

    "基准合成证据"不是一个布尔值，而是一个可独立核验的音频文件：
    路径 + SHA-256 + 容器解出的时长/采样率/声道 + 它念的那句短句。
    所以这里只接受真实产物：Mock 占位音频拒收，没试听过也拒收——
    伪造一份哈希填进去，等于把"验证过"这句话变成空话。

    试听短句必须与该音色登记的 `sample_text` 是同一句：换了句子，
    这条"基准"就不能作为后续跨音色比较的锚点，登记下来只会误导。
    """
    from . import audio as au
    from . import preview as pv
    from . import samples as vs

    voice = get_voice(voice_id)
    if not voice:
        raise TtsRejected("VOICE_NOT_FOUND", f"音色不存在：{voice_id}")
    assert_scope_complete(voice)

    rows = [a for a in au.list_audio(work_id, au.KIND_PREVIEW)
            if a.get("voice_asset_id") == voice_id
            and not a.get("is_mock") and not a.get("invalidated")
            and a.get("storage_uri") and a.get("file_hash")]
    if not rows:
        raise TtsRejected(
            "VOICE_BASELINE_NO_AUDITION",
            "这条音色在该作品里还没有真实试听。基准证据必须来自本项目真实合成过的"
            "音频，占位产物不能冒充：请先用同一短句生成一次试听。",
            {"voice_asset_id": voice_id, "work_id": work_id},
        )
    latest = sorted(rows, key=lambda a: a.get("created_at", ""))[-1]

    spoken = (pv.audition_sentence(work_id) or "").strip()
    registered = (voice.get("sample_text") or "").strip()
    initial_clone_baseline = (voice.get("clone_metadata") and voice.get("approval_state") == "pending"
                              and not voice.get("baseline_audio_asset_id") and not voice.get("baseline_audio_hash"))
    if initial_clone_baseline:
        from . import sources
        if (not sources.get_source(voice.get("source_record_id")) or not latest.get("tts_controls_digest")
                or not latest.get("sample_text") or latest["sample_text"] != spoken
                or au.currentness_problems(latest)):
            raise TtsRejected("VOICE_BASELINE_STALE", "首次克隆基准必须来自当前作品参数下的真实试听")
        # 新克隆尚未冻结基准句，可采用该作品已绑定的比较短句。
        # 原始真人参考音频不能进入本路径，候选必须是本项目生成的 preview。
        voice["sample_text"] = registered = spoken or registered
        voice["synthesis_profile"] = normalize_profile(latest["synthesis_profile"])
        voice["profile_digest"] = controls_digest(voice, voice["synthesis_profile"])
    if spoken and registered and spoken != registered:
        raise TtsRejected(
            "VOICE_BASELINE_SAMPLE_TEXT_MISMATCH",
            f"试听短句与音色登记的基准短句不是同一句：该作品用「{spoken}」，"
            f"音色登记的是「{registered}」。换句后两者不可比，不能作为基准。",
            {"work_id": work_id, "audition_sentence": spoken,
             "voice_sample_text": registered},
        )

    path = au.runtime_path(latest["storage_uri"])
    # 同一音色同一哈希已有样本就不重复登记（幂等）；不跨音色复用，
    # 因为样本是"某条音色的基准"，挂到别的音色上就失去归属。
    sample = next((s for s in vs.list_samples(voice_id)
                   if s.get("file_hash") == latest["file_hash"]), None)
    if not sample:
        sample = vs.register_sample(
            voice_id=voice_id, audio_path=path,
            sample_text=registered or spoken,
            source="本项目基准合成（该作品真实试听登记为基准证据）",
            provider_run_id=latest.get("provider_run_id") or "",
            storage_uri=latest["storage_uri"],
            declared_hash=latest["file_hash"],
            note=f"取自作品 {work_id} 的真实试听 {latest['id']}；"
                 "时长与规格由容器解出，非文字转述",
        )

    if initial_clone_baseline:
        st.put("voices", voice_id, voice)
    updated = attach_baseline_synthesis(
        voice_id, sample["id"], sample["file_hash"],
        provider_run_id=latest.get("provider_run_id") or "",
        note=f"基准证据来自作品 {work_id} 的真实试听 {latest['id']}",
    )
    ok, reasons = voice_ready(voice_id)
    return {
        "voice": updated,
        "sample": sample,
        "source_audio_id": latest["id"],
        "work_id": work_id,
        "production_ready": ok,
        "production_blockers": reasons,
        "note": "已登记基准合成证据。生产就绪仍取决于权属核验等其余条件，"
                "不因这一步而自动通过。",
    }


def approve_voice(voice_id: str, reviewer: str = "user", note: str = "") -> Dict[str, Any]:
    """用户显式选定即批准该音色。批准的是"外观与音色"，不是生产质量结论。"""
    voice = get_voice(voice_id)
    if not voice:
        raise TtsRejected("VOICE_NOT_FOUND", f"音色不存在：{voice_id}")
    if not voice.get("is_preset") and not voice.get("baseline_audio_asset_id"):
        raise TtsRejected(
            "VOICE_BASELINE_MISSING",
            "非预设音色经明确选择后须先完成本项目基准试听与评审，才能批准",
            {"voice_asset_id": voice_id},
        )
    voice["approval_state"] = "approved"
    voice["approved_by"] = reviewer
    voice["approved_at"] = now_iso()
    if note:
        voice["note"] = (voice.get("note", "") + "；" + note).strip("；")
    st.put("voices", voice_id, voice)
    return voice


# ---------- 生产就绪 ----------

def voice_ready(voice_id: str) -> Tuple[bool, List[str]]:
    """AC-TTS-01：作用域、权属、基准证据、非 Mock 任一不满足即阻塞。"""
    reasons: List[str] = []
    voice = get_voice(voice_id)
    if not voice:
        return False, [f"音色不存在：{voice_id}"]
    try:
        assert_scope_complete(voice)
    except TtsRejected as e:
        reasons.append(e.message)
    try:
        assert_model_allowed(voice.get("provider", ""), voice.get("model", ""))
        assert_region_verified(voice.get("provider", ""), voice.get("region", ""))
    except TtsRejected as e:
        reasons.append(e.message)

    if voice.get("approval_state") != "approved":
        reasons.append(f"音色未审批（{voice.get('approval_state')}）")
    if not voice.get("source_record_id"):
        reasons.append("缺少音色来源记录")
    if voice.get("is_mock"):
        reasons.append("Mock 音色不得进入生产")
    if not voice.get("license_id"):
        reasons.append("缺少权属记录，商业使用范围不可追溯")
    else:
        from ..avatar import licensing

        try:
            licensing.require(voice["license_id"], subject=voice.get("name"))
        except licensing.LicenseBlocked as e:
            reasons.append(e.message)
    if not voice.get("baseline_audio_asset_id") or not voice.get("baseline_audio_hash"):
        reasons.append("缺少本项目基准合成证据（真实音频与哈希）")
    reasons = list(dict.fromkeys(reasons))
    return (not reasons), reasons


def require_voice_ready(voice_id: str) -> Dict[str, Any]:
    ok, reasons = voice_ready(voice_id)
    if not ok:
        from .errors import TtsBlocked

        raise TtsBlocked("VOICE_NOT_PRODUCTION_READY", "；".join(reasons),
                         {"voice_asset_id": voice_id, "reasons": reasons})
    return get_voice(voice_id)  # type: ignore[return-value]
