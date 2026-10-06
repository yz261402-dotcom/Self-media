"""口播 Provider 适配器：OmniHuman 1.5（云）与 LivePortrait / MuseTalk（M2 本地）。

设计取向与 T04 的 MiniMax 适配器一致——**请求构造是纯函数，网络是注入的**。
这样"签名对不对、请求体长什么样、坏响应怎么判"都能在离线状态下被钉住，
不必等到花了钱、跑了 GPU 才发现解析写错了。

诚实性约束（AC-TH-01/06）：

- `DECLARED` 是供应商/历史核验的规格，`VERIFIED` 是本项目真实测过的。
  声明 15 秒"建议值"不得被写成硬上限，实测没做就保持空。
- 只要视频模型自己生成音轨，就可能换声/改稿；音频是否仍是那一条已确认 TTS
  必须由门禁核对，**不能靠"波形高相关"代替**嘴型、身份与身体质量。
- 提交成功只说明受理（`data.task_id`）；`done` 单独不构成成功，
  必须同时满足业务 code 正常、`video_url` 非空、文件能解出真实视频轨。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import struct
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

from .errors import TalkingHeadBlocked, TalkingHeadRejected
from .routes import OMNIHUMAN_LIMIT

# ---------- 火山视觉 API（OmniHuman 1.5） ----------

VOLC_HOST = "visual.volcengineapi.com"
VOLC_ENDPOINT = f"https://{VOLC_HOST}/"
VOLC_REGION = "cn-north-1"
VOLC_SERVICE = "cv"
VOLC_API_VERSION = "2022-08-31"
VOLC_ACTION_SUBMIT = "CVSubmitTask"
VOLC_ACTION_QUERY = "CVGetResult"
OMNIHUMAN_REQ_KEY = "jimeng_realman_avatar_picture_omni_v15"

SECRET_ENV_AK = "AVATAR_STUDIO_VOLC_ACCESS_KEY"
SECRET_ENV_SK = "AVATAR_STUDIO_VOLC_SECRET_KEY"

# M2 远端 GPU Worker（算力云）。未配置即 blocked，不在本机伪造推理结果。
M2_URL_ENV = "AVATAR_STUDIO_M2_WORKER_URL"
M2_TOKEN_ENV = "AVATAR_STUDIO_M2_WORKER_TOKEN"

# 供应商声明（供能力视图展示；不代表本项目已实测）
OMNIHUMAN_DECLARED = dict(OMNIHUMAN_LIMIT)
# 本项目实测结论。空 = 未实测，不允许用声明填满。
OMNIHUMAN_VERIFIED: Dict[str, Any] = {}
OMNIHUMAN_EVIDENCE = (
    "TH-COMP-003（2026-09-21 历史短片）：输入 9.36 秒音频 → 9.48 秒原片，"
    "704×1248/25fps；输入/输出音轨统一解码为 24kHz 单声道后最佳偏移 0ms、"
    "重叠区 Pearson 相关系数 0.99996223。该样本不是当前人物 B，"
    "不证明全片嘴型同步、身体动作、任意手势或分钟级长片达标。"
)

M2_DECLARED = {
    "stage_1": "LivePortrait 动作迁移",
    "stage_2": "MuseTalk 1.5 口型",
    "weights_pinned": True,
}
M2_VERIFIED = {
    "exp_001": {
        "output_seconds": 15.20,
        "liveportrait_seconds": 53.505,
        "musetalk_seconds": 181.672,
        "gpu_cost_yuan_estimate": 0.103217,
        "liveportrait_commit": "9b294b3d0536135442ea73cb01e6cb3ca7029dd3",
        "musetalk_commit": "0a89dec45a0192b824e3cf4daf96c239440c5ed8",
        "liveportrait_weights_revision": "KlingTeam/LivePortrait@82a4fa6735ca58432b6ce39301b4b9ee066dea47",
        "scope": "隔离环境实测；非账单、非生产单价；约 9.13 秒循环参考未批准为长期母版",
    },
    "mw_001": {
        "date": "2026-09-24",
        "channel": "HTTP worker + SSH 本地端口转发（AutoDL 允许 direct-tcpip，healthz 实测 200）",
        "motion_job_id": "bd9bf651889c4e29",
        "motion_seconds": 126.7,
        "plate_sha256": "b55f723dea1cf4d2e32a608cd15bb795b350c645125b8697698090494ec7325c",
        "lipsync_job_id": "2ab8b5433b5c4c45",
        "lipsync_seconds": 217.9,
        "output_sha256": "648f97125f7d26fd9dd41938c85346cdd0f92767e1698fd4c4476f709f240879",
        "output_seconds": 23.187,
        "audio_offset_samples": 0,
        "audio_corr": 0.9995,
        "evidence": "experiments/M2-Worker链路/MW-001/",
        "scope": ("真实 GPU 两阶段出片已验证；但提交走 worker_client.transport，"
                  "未经 HTTP 端点登记台账，故 HTTP→台账那段由「真 worker + 假 runner」测试覆盖，"
                  "二者不互相冒充。身份一致性（硬保护区零漂移属 compose 能力）与口型质量仍未正式"
                  "验收，看检结论由人承担；单 GPU 并发排队与进程重启恢复未测。"),
    },
}

# ---------- 签名与请求构造 ----------


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _hmac(key: bytes, msg: str, hexdigest: bool = False):
    d = hmac.new(key, msg.encode("utf-8"), hashlib.sha256)
    return d.hexdigest() if hexdigest else d.digest()


def canonical_query(query: Dict[str, str]) -> str:
    return "&".join(f"{quote(str(k), safe='-_.~')}={quote(str(v), safe='-_.~')}"
                    for k, v in sorted(query.items()))


def sign_request(*, ak: str, sk: str, method: str, query: Dict[str, str], body: bytes,
                 x_date: str, host: str = VOLC_HOST, region: str = VOLC_REGION,
                 service: str = VOLC_SERVICE, path: str = "/") -> Dict[str, str]:
    """火山视觉 HMAC-SHA256 签名（v4 风格）。

    这是纯函数：给定 ak/sk/x_date 与请求内容，输出确定性 headers，
    因此签名正确性可以在不联网的前提下被测试钉住。
    """
    if not ak or not sk:
        raise TalkingHeadBlocked("OMNIHUMAN_CREDENTIALS_MISSING",
                                 f"未配置 {SECRET_ENV_AK}/{SECRET_ENV_SK}，不发起真实请求")
    payload_hash = _sha256_hex(body)
    canonical_headers = (
        f"content-type:application/json\n"
        f"host:{host}\n"
        f"x-content-sha256:{payload_hash}\n"
        f"x-date:{x_date}\n"
    )
    signed_headers = "content-type;host;x-content-sha256;x-date"
    canonical_request = "\n".join([
        method.upper(), path, canonical_query(query),
        canonical_headers, signed_headers, payload_hash,
    ])
    date = x_date[:8]
    credential_scope = f"{date}/{region}/{service}/request"
    string_to_sign = "\n".join([
        "HMAC-SHA256", x_date, credential_scope, _sha256_hex(canonical_request.encode("utf-8")),
    ])
    k_date = _hmac(sk.encode("utf-8"), date)
    k_region = _hmac(k_date, region)
    k_service = _hmac(k_region, service)
    k_signing = _hmac(k_service, "request")
    signature = _hmac(k_signing, string_to_sign, hexdigest=True)
    return {
        "Content-Type": "application/json",
        "Host": host,
        "X-Content-Sha256": payload_hash,
        "X-Date": x_date,
        "Authorization": (
            f"HMAC-SHA256 Credential={ak}/{credential_scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}"
        ),
    }


def utc_x_date(ts: Optional[float] = None) -> str:
    """火山要求的基本 ISO8601 UTC，形如 20260923T080000Z。"""
    dt = datetime.fromtimestamp(ts, timezone.utc) if ts is not None else datetime.now(timezone.utc)
    return dt.strftime("%Y%m%dT%H%M%SZ")


def assert_omnihuman_input(*, audio_duration_ms: int, prompt: str = "",
                           resolution: Optional[str] = None) -> Dict[str, Any]:
    """按**已声明**规格做提交前拦截。声明未复核不影响拦截思路，但会如实标注。"""
    limit = OMNIHUMAN_LIMIT
    if audio_duration_ms <= 0:
        raise TalkingHeadRejected("OMNIHUMAN_AUDIO_DURATION_UNKNOWN",
                                 "音频时长未知，不得提交：时长上限校验不成立")
    if audio_duration_ms >= limit["audio_max_seconds_exclusive"] * 1000:
        raise TalkingHeadRejected(
            "OMNIHUMAN_AUDIO_TOO_LONG",
            f"音频 {audio_duration_ms}ms 不满足严格小于 "
            f"{limit['audio_max_seconds_exclusive']} 秒的当前规范；"
            "长音频不再自动分段，请缩短音频后再提交；禁止加速或截字",
            {"audio_duration_ms": audio_duration_ms},
        )
    if len(prompt or "") > limit["prompt_max_chars"]:
        raise TalkingHeadRejected(
            "OMNIHUMAN_PROMPT_TOO_LONG",
            f"表演 prompt {len(prompt)} 字符超过声明上限 {limit['prompt_max_chars']}",
            {"prompt_chars": len(prompt)},
        )
    if resolution and resolution not in limit["resolutions"]:
        raise TalkingHeadRejected(
            "OMNIHUMAN_RESOLUTION_UNSUPPORTED",
            f"分辨率 {resolution} 不在声明选项 {limit['resolutions']} 内（实际像素仍须测量）",
            {"resolution": resolution},
        )
    return {
        "audio_duration_ms": audio_duration_ms,
        "prompt_chars": len(prompt or ""),
        "resolution": resolution,
        "note": "按 2026-09-21 核验的声明规格拦截；正式提交前须复核当前账户规格与配额",
        "recommended_seconds_exceeded":
            audio_duration_ms > limit["audio_recommended_seconds"] * 1000,
    }


def normalize_resolution(value: Any) -> Optional[int]:
    """项目内部分辨率表示 → 火山 API 的 int。

    官方契约（2026-09-23 核验）：output_resolution 为 int，可选值 [720, 1080]，
    默认 1080。字符串 "1080p" 会被云端预处理拒绝（invalid output_resolution，
    2026-09-23 实测踩坑），因此字符串必须在提交前转成 int。
    """
    if value is None:
        return None
    if isinstance(value, int):
        return value if value in (720, 1080) else None
    text = str(value).strip().lower().replace("p", "")
    return int(text) if text in ("720", "1080") else None


def build_submit_request(*, image_url: str, audio_url: str, prompt: str = "",
                         seed: Optional[int] = None,
                         output_resolution: Optional[str] = None,
                         pe_fast_mode: Optional[bool] = None,
                         ak: str, sk: str, x_date: Optional[str] = None) -> Dict[str, Any]:
    """构造 CVSubmitTask 请求。输入一律是**可读取的媒体 URL**，不是内部路径。"""
    if not image_url or not audio_url:
        raise TalkingHeadRejected("OMNIHUMAN_INPUT_MISSING",
                                 "OmniHuman 必填 image_url 与 audio_url，缺一不可")
    body_json: Dict[str, Any] = {
        "req_key": OMNIHUMAN_REQ_KEY,
        "image_url": image_url,
        "audio_url": audio_url,
    }
    if prompt:
        body_json["prompt"] = prompt
    if seed is not None:
        body_json["seed"] = int(seed)
    if output_resolution is not None:
        resolution_int = normalize_resolution(output_resolution)
        if resolution_int is None:
            raise TalkingHeadRejected(
                "OMNIHUMAN_RESOLUTION_UNSUPPORTED",
                f"分辨率 {output_resolution!r} 无法转换为火山 API 的 int 取值"
                "（合法值：720 / 1080）；不得提交会被预处理拒绝的参数",
                {"resolution": str(output_resolution)})
        body_json["output_resolution"] = resolution_int
    if pe_fast_mode is not None:
        body_json["pe_fast_mode"] = bool(pe_fast_mode)
    body = json.dumps(body_json, ensure_ascii=False).encode("utf-8")
    query = {"Action": VOLC_ACTION_SUBMIT, "Version": VOLC_API_VERSION}
    headers = sign_request(ak=ak, sk=sk, method="POST", query=query, body=body,
                           x_date=x_date or utc_x_date())
    # Action/Version 必须真实出现在请求 URL 上：签名覆盖的查询串与实际发送的
    # 查询串必须一致，否则网关报 "missing Action parameter"（2026-09-23 实测踩坑）。
    return {"url": VOLC_ENDPOINT + "?" + canonical_query(query), "method": "POST",
            "headers": headers, "body": body, "json": body_json, "query": query}


def build_query_request(*, task_id: str, ak: str, sk: str,
                        x_date: Optional[str] = None) -> Dict[str, Any]:
    """构造 CVGetResult 查询。**必须用原 task_id**，不得另建替代任务。"""
    if not task_id:
        raise TalkingHeadRejected("OMNIHUMAN_TASK_ID_REQUIRED",
                                 "查询必须携带原 task_id；无 task_id 只能按未知处理")
    body_json = {"req_key": OMNIHUMAN_REQ_KEY, "task_id": task_id}
    body = json.dumps(body_json, ensure_ascii=False).encode("utf-8")
    query = {"Action": VOLC_ACTION_QUERY, "Version": VOLC_API_VERSION}
    headers = sign_request(ak=ak, sk=sk, method="POST", query=query, body=body,
                           x_date=x_date or utc_x_date())
    return {"url": VOLC_ENDPOINT + "?" + canonical_query(query), "method": "POST",
            "headers": headers, "body": body, "json": body_json, "query": query}


# ---------- 响应解析 ----------


def _resp_json(resp: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """取响应 JSON。非 JSON 主体（HTML 错误页/网关页）一律不当成业务成功。"""
    if "json" in resp and isinstance(resp["json"], dict):
        return resp["json"], None
    body = resp.get("body")
    if body is None:
        return None, "响应既无 JSON 也无主体"
    if isinstance(body, str):
        body = body.encode("utf-8", "replace")
    head = body[:512].lstrip().lower()
    if head.startswith(b"<!doctype") or head.startswith(b"<html") or head.startswith(b"<head"):
        return None, "响应是 HTML 页面而不是接口 JSON，判为失败"
    try:
        return json.loads(body.decode("utf-8")), None
    except Exception as e:
        return None, f"响应不是合法 JSON：{type(e).__name__}"


def _classify_http(status_code: Optional[int]) -> Optional[str]:
    if status_code in (401, 403):
        return "OMNIHUMAN_AUTH_FAILED"
    if status_code == 429:
        return "OMNIHUMAN_QUOTA_EXHAUSTED"
    if status_code in (502, 503, 504):
        return "OMNIHUMAN_NETWORK_ERROR"
    if status_code and status_code >= 400:
        return "OMNIHUMAN_HTTP_ERROR"
    return None


def _business_error(doc: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not doc:
        return None
    meta = doc.get("ResponseMetadata") or {}
    err = meta.get("Error") or doc.get("error") or {}
    if isinstance(err, dict) and (err.get("Code") or err.get("Message")):
        return {"code": str(err.get("Code") or ""), "message": str(err.get("Message") or "")}
    code = doc.get("code")
    if code not in (None, 0, "0", 10000) and (doc.get("message") or doc.get("msg")):
        if isinstance(code, int) and 0 < code < 10000:
            return None  # 早期成功码区间
        return {"code": str(code), "message": str(doc.get("message") or doc.get("msg"))}
    return None


def parse_submit_response(resp: Dict[str, Any]) -> Dict[str, Any]:
    """提交结果解析。

    HTTP 200 但没有 task_id 时**不能**报成功，也不能报失败——
    我们既不知道远端是否受理，也不知道有没有产生费用，因此判 unknown。
    """
    status = resp.get("status_code")
    if status == 200:
        doc, err = _resp_json(resp)
        if err:
            return {"state": "unknown", "reason": err, "remote_job_id": None}
        berr = _business_error(doc)
        if berr:
            return {"state": "failed", "reason": berr["message"], "error": berr,
                    "remote_job_id": None}
        data = (doc or {}).get("data") or {}
        task_id = data.get("task_id") or data.get("taskId")
        if not task_id:
            return {"state": "unknown", "remote_job_id": None,
                    "reason": "受理响应未包含 task_id：无法确认是否已受理与计费，需对账"}
        return {"state": "submitted", "remote_job_id": str(task_id),
                "reason": "已受理；受理不等于成功，仍需按原 task_id 查询"}
    code = _classify_http(status)
    doc, _ = _resp_json(resp) if resp.get("body") or resp.get("json") else (None, None)
    berr = _business_error(doc)
    if code == "OMNIHUMAN_AUTH_FAILED":
        raise TalkingHeadBlocked(code, "火山视觉鉴权失败：AK/SK 无效或未授权，不重试计费动作")
    if code == "OMNIHUMAN_QUOTA_EXHAUSTED":
        raise TalkingHeadBlocked(code, "供应商限流/额度不足：不得重试、不得换域名或换模型")
    if code == "OMNIHUMAN_NETWORK_ERROR":
        return {"state": "unknown", "remote_job_id": None,
                "reason": f"网关 {status}：提交结果未知，先对账再决定是否重做"}
    return {"state": "failed", "remote_job_id": None,
            "reason": (berr or {}).get("message") or f"提交失败 HTTP {status}",
            "error": berr}


def parse_query_response(resp: Dict[str, Any]) -> Dict[str, Any]:
    """查询结果解析。`done` 单独不代表成功——还要有非空 video_url。"""
    status = resp.get("status_code")
    if status is None:
        # 连 HTTP 响应都没拿到（DNS/连接/TLS 失败）：我们**什么都没问到**。
        # 这不是"远端给了含糊答案"——把它记成 unknown，会让一次网络抖动
        # 变成一次人工对账，还会挡住重提。保持原状态，下一轮再问。
        return {"state": "unavailable",
                "reason": f"查询未取得响应：{resp.get('message') or '网络错误'}"}
    if status and status != 200:
        code = _classify_http(status)
        if code in ("OMNIHUMAN_AUTH_FAILED", "OMNIHUMAN_QUOTA_EXHAUSTED"):
            raise TalkingHeadBlocked(code, f"查询被拒（HTTP {status}），不得改判成功或失败")
        if code == "OMNIHUMAN_NETWORK_ERROR":
            # 网关 502/503/504：请求没到业务层，关于任务状态我们同样**什么都没问到**。
            # 本机就存在这类拦截（本地代理对不可达上游回 502），因此这不是理论情形。
            return {"state": "unavailable",
                    "reason": f"查询网关错误（HTTP {status}），未获得任何结论"}
        return {"state": "unknown", "reason": f"查询 HTTP {status}，状态未知，需对账"}
    doc, err = _resp_json(resp)
    if err:
        return {"state": "unknown", "reason": err}
    berr = _business_error(doc)
    if berr:
        return {"state": "failed", "reason": berr["message"], "error": berr}
    data = (doc or {}).get("data") or {}
    remote = data.get("status") or data.get("task_status")
    video_url = data.get("video_url") or data.get("videoUrl")
    if remote == "done":
        if not video_url:
            return {"state": "failed", "video_url": None,
                    "reason": "远端报 done 但没有 video_url：不得当成功，文件缺失即失败"}
        return {"state": "succeeded", "video_url": video_url,
                "business": data.get("resp_data") or data.get("respData") or {},
                "reason": "远端完成且返回视频地址；仍须下载校验通过才算原片可用"}
    if remote in ("in_queue", "generating", "running", "processing"):
        return {"state": "running", "remote_status": remote,
                "poll_after_ms": int(data.get("poll_after_ms") or 5000)}
    # not_found / expired / 空状态：远端没给确定结论，本地只能记未知
    return {"state": "unknown", "remote_status": remote,
            "reason": f"远端状态 {remote!r} 不构成确定结论；先对账，不盲目重提"}


# ---------- 媒体文件校验 ----------

_MP4_BRANDS = (b"isom", b"iso2", b"mp41", b"mp42", b"avc1", b"dash", b"MSNV", b"qt  ")


def detect_container(data: bytes) -> str:
    if len(data) < 12:
        return "unknown"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data[:4] == b"\x1a\x45\xdf\xa3":
        return "matroska"
    if data[4:8] == b"ftyp":
        brand = data[8:12]
        return "mp4" if brand in _MP4_BRANDS else "mp4_unknown_brand"
    if data[:3] == b"ID3":
        return "mp3"
    if data[:2] in (b"\xff\xd8",):
        return "jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    return "unknown"


def _walk_boxes(data: bytes, start: int, end: int) -> Tuple[List[Dict[str, Any]], bool]:
    """遍历 MP4 box。返回 (boxes, truncated)。box 长度越过文件末尾即判截断。"""
    boxes: List[Dict[str, Any]] = []
    pos = start
    truncated = False
    while pos + 8 <= end:
        size = struct.unpack(">I", data[pos:pos + 4])[0]
        btype = data[pos + 4:pos + 8]
        header = 8
        if size == 1:
            if pos + 16 > end:
                truncated = True
                break
            size = struct.unpack(">Q", data[pos + 8:pos + 16])[0]
            header = 16
        elif size == 0:
            size = end - pos
        if size < header or pos + size > end:
            truncated = True
            break
        boxes.append({"type": btype.decode("latin-1"), "start": pos, "size": size,
                      "body_start": pos + header, "body_end": pos + size})
        pos += size
    if pos < end and not boxes:
        truncated = True
    return boxes, truncated


def parse_mp4(data: bytes) -> Dict[str, Any]:
    """真实解析 MP4：时长、视频轨/音轨是否存在、像素尺寸、是否截断。

    只读容器结构，不做解码。因此它无法判断"画面好不好"，
    但足以挡住"HTML 错误页 / 空文件 / 无视频轨 / 截断"这几类假成功。
    """
    out: Dict[str, Any] = {"container": "mp4", "has_video_track": False,
                           "has_audio_track": False, "duration_ms": None,
                           "width": None, "height": None, "truncated": False,
                           "ftyp_brand": None}
    if len(data) < 16 or data[4:8] != b"ftyp":
        out["container"] = "not_mp4"
        return out
    out["ftyp_brand"] = data[8:12].decode("latin-1", "replace")
    top, truncated = _walk_boxes(data, 0, len(data))
    out["truncated"] = truncated
    moov = next((b for b in top if b["type"] == "moov"), None)
    if not moov:
        out["duration_ms"] = None
        out["moov_missing"] = True
        return out
    children, tr2 = _walk_boxes(data, moov["body_start"], moov["body_end"])
    out["truncated"] = out["truncated"] or tr2
    timescale = None
    if moov["body_end"] - moov["body_start"] >= 8:
        first = data[moov["body_start"] + 4:moov["body_start"] + 8]
        if first == b"mvhd":
            mvhd_body = moov["body_start"] + 8
            version = data[mvhd_body]
            if version == 0:
                timescale = struct.unpack(">I", data[mvhd_body + 12:mvhd_body + 16])[0]
                duration = struct.unpack(">I", data[mvhd_body + 16:mvhd_body + 20])[0]
            else:
                timescale = struct.unpack(">I", data[mvhd_body + 20:mvhd_body + 24])[0]
                duration = struct.unpack(">Q", data[mvhd_body + 24:mvhd_body + 32])[0]
            if timescale:
                out["duration_ms"] = int(round(duration * 1000.0 / timescale))
            out["timescale"] = timescale
    for b in children:
        if b["type"] != "trak":
            continue
        trak_kids, tr3 = _walk_boxes(data, b["body_start"], b["body_end"])
        out["truncated"] = out["truncated"] or tr3
        mdia = next((k for k in trak_kids if k["type"] == "mdia"), None)
        tkhd = next((k for k in trak_kids if k["type"] == "tkhd"), None)
        handler = None
        if mdia:
            mdia_kids, tr4 = _walk_boxes(data, mdia["body_start"], mdia["body_end"])
            out["truncated"] = out["truncated"] or tr4
            hdlr = next((k for k in mdia_kids if k["type"] == "hdlr"), None)
            if hdlr and hdlr["body_end"] - hdlr["body_start"] >= 12:
                handler = data[hdlr["body_start"] + 8:hdlr["body_start"] + 12].decode("latin-1")
        if handler == "vide":
            out["has_video_track"] = True
            if tkhd:
                w, h = _tkhd_dimensions(data, tkhd)
                if w and h:
                    out["width"], out["height"] = w, h
        elif handler == "soun":
            out["has_audio_track"] = True
    return out


def _tkhd_dimensions(data: bytes, tkhd: Dict[str, Any]) -> Tuple[Optional[int], Optional[int]]:
    body = tkhd["body_start"]
    if body >= tkhd["body_end"]:
        return None, None
    version = data[body]
    off = body + (4 + 84 if version == 1 else 4 + 72)
    if off + 8 > tkhd["body_end"]:
        return None, None
    w = struct.unpack(">I", data[off:off + 4])[0] >> 16
    h = struct.unpack(">I", data[off + 4:off + 8])[0] >> 16
    return (w or None), (h or None)


def parse_wav_info(data: bytes) -> Dict[str, Any]:
    """WAV 头解析（复用 T04 的判据），用于"音轨仍是同一条 TTS"的核对。"""
    if len(data) < 44 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        return {"container": "not_wav"}
    pos = 12
    fmt = None
    data_bytes = None
    while pos + 8 <= len(data):
        cid = data[pos:pos + 4]
        size = struct.unpack("<I", data[pos + 4:pos + 8])[0]
        body = pos + 8
        if body + size > len(data):
            return {"container": "wav", "truncated": True}
        if cid == b"fmt ":
            fmt = data[body:body + size]
        elif cid == b"data":
            data_bytes = body, size
            break
        pos = body + size + (size % 2)
    if not fmt:
        return {"container": "wav", "fmt_missing": True}
    channels, rate = struct.unpack("<HI", fmt[2:8])
    bits = struct.unpack("<H", fmt[14:16])[0] if len(fmt) >= 16 else None
    dur = None
    if data_bytes:
        dur = int(round(data_bytes[1] * 1000.0 / (rate * channels * max(bits or 16, 8) // 8)))
    return {"container": "wav", "channels": channels, "sample_rate": rate,
            "bits": bits, "duration_ms": dur}


def probe_media(data: bytes) -> Dict[str, Any]:
    """媒体探测总入口。空文件与 HTML 页面在这里就被挡住。"""
    if not data:
        raise TalkingHeadRejected("VIDEO_EMPTY", "下载结果为空文件，不得当作视频")
    head = data[:1024].lstrip()
    low = head.lower()
    if low.startswith(b"<!doctype") or low.startswith(b"<html") or b"<html" in low[:256]:
        raise TalkingHeadRejected("VIDEO_IS_HTML",
                                 "下载结果是 HTML 页面（错误页/登录页），不是视频文件")
    container = detect_container(data)
    if container in ("mp4", "mp4_unknown_brand"):
        info = parse_mp4(data)
        info["bytes"] = len(data)
        return info
    if container in ("matroska", "webp"):
        return {"container": container, "bytes": len(data), "has_video_track": True,
                "duration_ms": None, "note": "容器已识别但本适配器不解析其轨信息"}
    if container in ("jpeg", "png"):
        raise TalkingHeadRejected("VIDEO_CONTAINER_MISMATCH",
                                 f"下载结果是图片（{container}），不是视频")
    if container == "mp3":
        raise TalkingHeadRejected("VIDEO_CONTAINER_MISMATCH", "下载结果是音频文件，不是视频")
    raise TalkingHeadRejected("VIDEO_CONTAINER_UNKNOWN",
                             f"无法识别容器（前 12 字节：{data[:12]!r}）")


def assert_usable_video(probe: Dict[str, Any], *, declared_duration_ms: Optional[int] = None,
                        min_duration_ms: int = 300) -> Dict[str, Any]:
    """假成功拦截：能解出容器但无视频轨/截断/时长不合理，都不算视频。"""
    problems: List[str] = []
    if probe.get("truncated"):
        problems.append("文件被截断（box 长度越过文件末尾），下载不完整")
    if probe.get("moov_missing"):
        problems.append("缺少 moov：不是可播放的完整 MP4")
    if not probe.get("has_video_track"):
        problems.append("没有视频轨：结果不是视频")
    dur = probe.get("duration_ms")
    if dur is None:
        problems.append("无法解出时长：下游时间轴不接受估算时长")
    elif dur < min_duration_ms:
        problems.append(f"时长 {dur}ms 短于下限 {min_duration_ms}ms，判为无效输出")
    if declared_duration_ms and dur:
        drift = abs(int(dur) - int(declared_duration_ms))
        if drift > max(1500, int(declared_duration_ms * 0.15)):
            problems.append(
                f"时长与声明偏差过大（解析 {dur}ms vs 声明 {declared_duration_ms}ms），需人工核对")
    if problems:
        raise TalkingHeadRejected("VIDEO_UNUSABLE", "；".join(problems),
                                 {"problems": problems, "probe": probe})
    return probe


def download_and_verify(url: str, transport, *, declared_duration_ms: Optional[int] = None) -> Dict[str, Any]:
    """按给定 URL 下载并校验。URL 过期/中断/内容不符都在这里暴露。"""
    if not url:
        raise TalkingHeadRejected("VIDEO_URL_MISSING", "没有可下载的地址")
    resp = transport({"method": "GET", "url": url})
    status = resp.get("status_code")
    if status and status >= 400:
        if status in (403, 404, 410):
            raise TalkingHeadBlocked("VIDEO_URL_EXPIRED",
                                     f"结果地址不可用（HTTP {status}）：URL 可能已过期，"
                                     "须按原 task_id 重新获取而不是重提任务")
        raise TalkingHeadBlocked("VIDEO_DOWNLOAD_FAILED", f"下载失败 HTTP {status}")
    data = resp.get("body") or b""
    if isinstance(data, str):
        data = data.encode("utf-8", "replace")
    probe = probe_media(data)
    assert_usable_video(probe, declared_duration_ms=declared_duration_ms)
    probe["file_hash"] = "sha256:" + _sha256_hex(data)
    probe["bytes"] = len(data)
    return {"probe": probe, "data": data}


def urllib_transport(req: Dict[str, Any], *, timeout: int = 60) -> Dict[str, Any]:
    """真实 HTTP 调用。接收 build_*_request / download_and_verify 产出的请求字典
    （含 url / method / headers / body），发起真实请求。

    返回 {status_code, body(bytes)}，与 _resp_json 的契约对齐；非 JSON 主体
    由解析层判定为失败，这里只负责把字节拿回来。

    设计约束：不重试、不吞错、不改域名、不伪装成"模型不可用"。
    凭据缺失由调用方（submit_omnihuman / adapter_status）前置拦截，这里不重复判断。
    """
    import urllib.error
    import urllib.request

    method = str(req.get("method", "POST")).upper()
    headers = dict(req.get("headers") or {})
    body = req.get("body")
    request = urllib.request.Request(req["url"], data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            raw = resp.read()
            return {"status_code": int(resp.status), "body": raw}
    except urllib.error.HTTPError as e:
        raw = e.read() if hasattr(e, "read") else b""
        return {"status_code": int(getattr(e, "code", 0) or 0), "body": raw,
                "message": str(e)}
    except Exception as e:  # 网络层异常：明确归类，不伪装成"模型不可用"
        return {"status_code": None, "error": "network_error", "message": f"{type(e).__name__}: {e}"}


# ---------- M2 本地路线适配器 ----------


def m2_worker_status() -> Dict[str, Any]:
    configured = bool(os.environ.get(M2_URL_ENV))
    return {
        "provider": "m2_worker",
        "configured": configured,
        "state": "unverified" if configured else "not_configured",
        "detail": (f"未配置 {M2_URL_ENV}：M2 推理在算力云隔离环境执行，"
                   "不在 Agent 服务本机伪造结果")
                  if not configured else
                  f"已配置 {M2_URL_ENV}；该通道已用真实 GPU 作业跑通（MW-001，2026-09-24），"
                  "但身份/口型未正式验收，状态保持 unverified",
        "declared": M2_DECLARED,
        "verified": M2_VERIFIED,
    }


def build_liveportrait_request(*, source_image_url: str, motion_ref_url: str,
                               output_seconds: float,
                               source_hash: str = "", motion_hash: str = "") -> Dict[str, Any]:
    """LivePortrait 阶段请求：以作品绑定人物为 source，只从动作参考迁移运动。"""
    if not source_image_url or not motion_ref_url:
        raise TalkingHeadRejected("LP_INPUT_MISSING",
                                 "LivePortrait 需要人物源图与已获准动作参考，缺一不可")
    if output_seconds <= 0:
        raise TalkingHeadRejected("LP_OUTPUT_SECONDS_INVALID", "底片时长必须为正数")
    if not source_hash or not motion_hash:
        raise TalkingHeadRejected(
            "LP_INPUT_HASH_MISSING",
            "底片作业必须绑定源图与动作参考的文件哈希：缓存复用与版本错配都靠它判定",
            {"source_hash": bool(source_hash), "motion_hash": bool(motion_hash)},
        )
    return {"stage": "motion", "model": "LivePortrait",
            "args": {"source_image_url": source_image_url, "motion_ref_url": motion_ref_url,
                     "output_seconds": float(output_seconds),
                     "source_hash": source_hash, "motion_hash": motion_hash},
            "heavy": True}


def build_musetalk_request(*, plate_url: str, audio_url: str, plate_hash: str,
                           audio_hash: str) -> Dict[str, Any]:
    """MuseTalk 阶段请求：底片 + 同一条最终音频。不允许在这里替换音频。"""
    if not plate_url or not audio_url:
        raise TalkingHeadRejected("MT_INPUT_MISSING", "MuseTalk 需要动态底片与最终音频")
    if not plate_hash or not audio_hash:
        raise TalkingHeadRejected("MT_INPUT_HASH_MISSING",
                                 "对口型作业必须绑定底片与音频哈希，否则无法核对版本")
    return {"stage": "lipsync", "model": "MuseTalk-1.5",
            "args": {"plate_url": plate_url, "audio_url": audio_url,
                     "plate_hash": plate_hash, "audio_hash": audio_hash},
            "heavy": True}


def parse_m2_response(resp: Dict[str, Any]) -> Dict[str, Any]:
    """M2 worker 响应解析。与云路线同一套状态语义：running/failed/unknown/succeeded。"""
    status = resp.get("status_code")
    if status is None:
        # 同 parse_query_response：没拿到响应不是结论，不得据此改判。
        return {"state": "unavailable",
                "reason": f"查询未取得响应：{resp.get('message') or '网络错误'}"}
    if status and status >= 400:
        return {"state": "unknown", "reason": f"worker HTTP {status}，结果未知需对账"}
    doc, err = _resp_json(resp)
    if err:
        return {"state": "unknown", "reason": err}
    state = (doc or {}).get("state")
    out = (doc or {}).get("output_url")
    if state == "succeeded":
        if not out:
            return {"state": "failed", "reason": "worker 报成功但没有输出地址"}
        return {"state": "succeeded", "output_url": out,
                "output_hash": (doc or {}).get("output_hash", ""),
                "duration_ms": (doc or {}).get("duration_ms"),
                # job_id 必须回填：轮询要按它查原 job，不能拿 output_hash 冒充任务号
                "remote_job_id": (doc or {}).get("job_id") or None}
    if state in ("queued", "running"):
        return {"state": "running", "remote_status": state,
                "remote_job_id": (doc or {}).get("job_id") or None}
    if state in ("failed", "error"):
        return {"state": "failed", "reason": (doc or {}).get("error") or "worker 报失败",
                "error": {"code": str((doc or {}).get("code") or "M2_FAILED"),
                          "message": str((doc or {}).get("error") or "worker 报失败")}}
    return {"state": "unknown", "reason": f"worker 状态 {state!r} 不构成确定结论"}


def adapter_status() -> Dict[str, Any]:
    from .wan_provider import adapter_status as wan_status
    configured = bool(os.environ.get(SECRET_ENV_AK) and os.environ.get(SECRET_ENV_SK))
    return {
        "omnihuman": {
            "provider": "volcengine",
            "model": "omnihuman-1.5",
            "endpoint": VOLC_ENDPOINT,
            "configured": configured,
            # 与 T04 的 minimax / m2_worker 用同一套词汇：没配就是 not_configured，
            # 配了但没跑通才是 unverified。把两者都写成 unverified 会让
            # "缺凭据"看起来像"只是还没验证"，前者要配置、后者要实测，处理方式不同。
            "state": "unverified" if configured else "not_configured",
            "declared": OMNIHUMAN_DECLARED,
            "verified": OMNIHUMAN_VERIFIED,
            "evidence": OMNIHUMAN_EVIDENCE,
            "detail": (f"未配置 {SECRET_ENV_AK}/{SECRET_ENV_SK}：端点与模型名齐全不等于可用")
                      if not configured else
                      "凭据已就位，但本项目尚未用当前账户与人物 B 完成真实任务；"
                      "状态保持 unverified",
        },
        "m2_worker": m2_worker_status(),
        "wan": wan_status(),
    }
