"""百炼官方 Wan 3.0 协议；网络、响应和输入错误均不触发自动重试。"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Dict
from urllib.parse import quote, urlsplit

from .errors import TalkingHeadBlocked, TalkingHeadRejected

MODEL = "wan3.0-video"
PROVIDER = "dashscope_wan"
SECRET_ENV = "AVATAR_STUDIO_QWEN_API_KEY"
BASE_URL_ENV = "AVATAR_STUDIO_WAN_BASE_URL"
WORKSPACE_ENV = "AVATAR_STUDIO_WAN_WORKSPACE_ID"
SUBMIT_PATH = "/services/aigc/video-generation/video-synthesis"
AUDIO_INSTRUCTION_VERSION = "wan-reference-audio-v1"
AUDIO_INSTRUCTION = "参考图1主体，严格按音频1完整台词、语速、停顿驱动口型和动作，不改写或重念台词，不新增人声。"
QUOTA_REJECTION_MESSAGE = "百炼额度、余额或并发限制拒绝了本次请求；请核实账户，余额不足时充值后可显式重新生成。"


def endpoint(base_url: str | None = None) -> str:
    value = base_url if base_url is not None else os.environ.get(BASE_URL_ENV, "")
    if not value:
        workspace = os.environ.get(WORKSPACE_ENV, "").strip()
        if workspace and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*", workspace):
            value = f"https://{workspace}.cn-beijing.maas.aliyuncs.com/api/v1"
        else:
            raise TalkingHeadBlocked("WAN_ENDPOINT_NOT_CONFIGURED",
                                     "请配置 Wan 北京工作空间 ID 或官方 API 地址；通义密钥本身不包含工作空间信息。")
    value = value.strip().rstrip("/")
    try:
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        official = bool(re.fullmatch(r"[a-z0-9][a-z0-9-]*\.cn-beijing\.maas\.aliyuncs\.com", host))
        valid = (parsed.scheme == "https" and official and parsed.port in (None, 443)
                 and parsed.path == "/api/v1" and not parsed.query and not parsed.fragment
                 and not parsed.username and not parsed.password)
    except ValueError:
        valid = False
    if not valid:
        raise TalkingHeadBlocked("WAN_ENDPOINT_INVALID",
                                 "Wan 地址必须是官方北京工作空间 HTTPS API 根地址，不得包含账号、查询参数或第三方域名。")
    return value


def credentials() -> str:
    key = os.environ.get(SECRET_ENV, "").strip()
    if not key:
        raise TalkingHeadBlocked("WAN_CREDENTIALS_MISSING", "未配置通义 API 密钥；尚未提交 Wan 生成。")
    return key


def normalize_resolution(value: str) -> str:
    normalized = str(value).strip().lower()
    if normalized not in ("480p", "720p", "1080p"):
        raise TalkingHeadRejected("WAN_RESOLUTION_INVALID", "Wan 首版支持 480P、720P、1080P。")
    return normalized


def assert_native_input(*, duration_seconds: int = 5, prompt: str, resolution: str) -> Dict[str, Any]:
    from .routes import WAN_LIMIT, assert_wan_duration_allowed
    seconds = assert_wan_duration_allowed(duration_seconds)
    if not isinstance(prompt, str) or not prompt.strip():
        raise TalkingHeadRejected("WAN_PROMPT_REQUIRED", "请描述画面、动作及希望生成的对白或声音。")
    if len(prompt.strip()) > WAN_LIMIT["prompt_max_chars"]:
        raise TalkingHeadRejected("WAN_PROMPT_TOO_LONG", f"提示词不能超过 {WAN_LIMIT['prompt_max_chars']} 字符。")
    return {"resolution": normalize_resolution(resolution), "prompt": prompt.strip(),
            "effective_prompt": prompt.strip(), "duration_seconds": seconds, "generated_seconds": seconds}


def build_native_submit_request(*, image_url: str, prompt: str, duration_seconds: int = 5,
                                resolution: str = "720p", api_key: str,
                                base_url: str | None = None) -> Dict[str, Any]:
    params = assert_native_input(duration_seconds=duration_seconds, prompt=prompt, resolution=resolution)
    root = endpoint(base_url)
    if not api_key:
        raise TalkingHeadBlocked("WAN_CREDENTIALS_MISSING", "未配置通义 API 密钥。")
    body = {"model": MODEL, "input": {"prompt": params["prompt"], "media": [
        {"type": "reference_image", "url": _media_url(image_url)}]},
        "parameters": {"resolution": params["resolution"].upper(), "ratio": "9:16",
                       "duration": params["generated_seconds"], "audio": True}}
    return {"url": root + SUBMIT_PATH, "method": "POST", "json": body,
            "body": json.dumps(body, ensure_ascii=False).encode("utf-8"),
            "headers": {"Authorization": f"Bearer {api_key}", "X-DashScope-Async": "enable",
                        "Content-Type": "application/json"}}


def assert_input(*, audio_duration_ms: int, prompt: str, resolution: str) -> Dict[str, Any]:
    from .routes import WAN_LIMIT, assert_wan_audio_duration_allowed, wan_generation_seconds
    assert_wan_audio_duration_allowed(audio_duration_ms)
    if not isinstance(prompt, str) or not prompt.strip():
        raise TalkingHeadRejected("WAN_PROMPT_REQUIRED", "Wan 需要画面提示词；请描述人物与动作。")
    effective_prompt = AUDIO_INSTRUCTION + "\n画面描述：" + prompt.strip()
    if len(effective_prompt) > WAN_LIMIT["prompt_max_chars"]:
        raise TalkingHeadRejected("WAN_PROMPT_TOO_LONG",
            f"画面描述加上固定原音频驱动指令后不能超过 {WAN_LIMIT['prompt_max_chars']} 字符；请缩短画面描述。")
    return {"resolution": normalize_resolution(resolution), "prompt": prompt.strip(),
            "effective_prompt": effective_prompt, "audio_instruction_version": AUDIO_INSTRUCTION_VERSION,
            "generated_seconds": wan_generation_seconds(audio_duration_ms)}


def _media_url(value: str) -> str:
    parsed = urlsplit(value or "")
    if parsed.scheme not in ("https", "http") or not parsed.hostname or parsed.username or parsed.password:
        raise TalkingHeadRejected("WAN_MEDIA_URL_INVALID", "Wan 需要可公开读取的人物图和原音频地址。")
    return value


def build_submit_request(*, image_url: str, audio_url: str, prompt: str,
                         audio_duration_ms: int, resolution: str = "720p",
                         api_key: str, base_url: str | None = None) -> Dict[str, Any]:
    params = assert_input(audio_duration_ms=audio_duration_ms, prompt=prompt, resolution=resolution)
    root = endpoint(base_url)
    if not api_key:
        raise TalkingHeadBlocked("WAN_CREDENTIALS_MISSING", "未配置通义 API 密钥。")
    body = {"model": MODEL, "input": {"prompt": params["effective_prompt"], "media": [
        {"type": "reference_image", "url": _media_url(image_url)},
        {"type": "reference_audio", "url": _media_url(audio_url)}]},
        "parameters": {"resolution": params["resolution"].upper(), "ratio": "9:16",
                       "duration": params["generated_seconds"], "audio": True}}
    return {"url": root + SUBMIT_PATH, "method": "POST", "json": body,
            "body": json.dumps(body, ensure_ascii=False).encode("utf-8"),
            "headers": {"Authorization": f"Bearer {api_key}", "X-DashScope-Async": "enable",
                        "Content-Type": "application/json"}}


def build_query_request(*, task_id: str, api_key: str, base_url: str | None = None) -> Dict[str, Any]:
    if not task_id or not isinstance(task_id, str):
        raise TalkingHeadRejected("WAN_TASK_ID_REQUIRED", "查询必须使用原 Wan task_id。")
    if not api_key:
        raise TalkingHeadBlocked("WAN_CREDENTIALS_MISSING", "未配置通义 API 密钥。")
    return {"url": endpoint(base_url) + "/tasks/" + quote(task_id, safe=""), "method": "GET",
            "headers": {"Authorization": f"Bearer {api_key}"}}


def _document(resp: Dict[str, Any]) -> Dict[str, Any]:
    doc = resp.get("json")
    if not isinstance(doc, dict):
        try:
            doc = json.loads(resp.get("body") or b"")
        except (TypeError, ValueError, UnicodeDecodeError):
            return {}
    return doc if isinstance(doc, dict) else {}


def _is_quota_error(code: Any) -> bool:
    return any(word in str(code or "").lower() for word in ("quota", "throttl", "arrear", "balance"))


def _definitive_error(status: Any, doc: Dict[str, Any]) -> None:
    code = str(doc.get("code") or "").lower()
    if _is_quota_error(code):
        raise TalkingHeadBlocked("WAN_QUOTA_EXHAUSTED", QUOTA_REJECTION_MESSAGE)
    if status in (401, 403) or code in ("invalidapikey", "accessdenied", "forbidden", "unauthorized"):
        raise TalkingHeadBlocked("WAN_AUTH_FAILED", "百炼拒绝认证或该工作空间未开放 Wan 权限；本次请求已被明确拒绝。")
    if status == 429:
        raise TalkingHeadBlocked("WAN_QUOTA_EXHAUSTED", QUOTA_REJECTION_MESSAGE)
    if status in (400, 404, 405, 413, 422) or code in ("invalidparameter", "invalidparametervalue", "datainspectionfailed"):
        raise TalkingHeadRejected("WAN_INPUT_REJECTED", "百炼明确拒绝了输入、模型或接口参数；请核对工作空间、输入限制和模型权限。")


def parse_submit_response(resp: Dict[str, Any]) -> Dict[str, Any]:
    status = resp.get("status_code")
    doc = _document(resp)
    if status is None or status >= 500 or status == 408:
        return {"state": "unknown", "reason": "提交响应不可核对，可能已受理和计费；先对账。"}
    _definitive_error(status, doc)
    out = doc.get("output")
    if 200 <= status < 300 and isinstance(out, dict) and isinstance(out.get("task_id"), str) and out["task_id"]:
        return {"state": "submitted", "remote_job_id": out["task_id"]}
    return {"state": "unknown", "reason": "未取得原任务 ID，不能判定未受理；先对账。"}


def parse_query_response(resp: Dict[str, Any]) -> Dict[str, Any]:
    status = resp.get("status_code")
    if status is None or status >= 500 or status in (408, 429, 401, 403):
        return {"state": "unavailable", "reason": "查询通道暂不可用，保留原任务状态。"}
    if status in (404, 410):
        return {"state": "unknown", "reason": "原任务不存在或已超过 24 小时查询有效期；保留 task_id，先对账。"}
    doc = _document(resp)
    out = doc.get("output")
    if not 200 <= status < 300 or not isinstance(out, dict):
        return {"state": "unavailable", "reason": "查询响应无法核对，保留原任务。"}
    remote = str(out.get("task_status") or "").upper()
    if remote in ("PENDING", "RUNNING"):
        return {"state": "running", "remote_status": remote}
    if remote in ("FAILED", "CANCELED", "CANCELLED"):
        if remote == "FAILED" and _is_quota_error(out.get("code")):
            return {"state": "failed", "error_code": "WAN_QUOTA_EXHAUSTED",
                    "reason": QUOTA_REJECTION_MESSAGE}
        return {"state": "failed", "reason": "Wan 原任务明确失败或取消；不会自动重提。"}
    if remote == "SUCCEEDED" and out.get("video_url"):
        return {"state": "done", "video_url": _media_url(out["video_url"])}
    return {"state": "unknown", "reason": "原任务状态未知或成功响应缺少视频地址；保留 task_id。"}


# Same vocabulary as the orchestration entry point.
parse_poll_response = parse_query_response


def adapter_status() -> Dict[str, Any]:
    result = {"provider": PROVIDER, "model": MODEL, "configured": False,
              "state": "not_configured", "declared": True, "verified": False,
              "endpoint": None, "detail": ""}
    try:
        result["endpoint"] = endpoint()
        credentials()
    except TalkingHeadBlocked as error:
        result["detail"] = error.message
        result["code"] = error.code
        return result
    result.update(configured=True, state="unverified",
                  detail="已配置官方工作空间与通义密钥；Wan 邀测权限、真实生成与口型效果尚未实测。")
    return result


def urllib_transport(req: Dict[str, Any], *, timeout: int = 60) -> Dict[str, Any]:
    """单次 HTTP 请求；认证请求限定官方根地址且禁止跨域重定向。"""
    import urllib.error
    import urllib.request
    headers = dict(req.get("headers") or {})
    if any(key.lower() == "authorization" for key in headers):
        parsed = urlsplit(req["url"])
        endpoint(f"{parsed.scheme}://{parsed.netloc}/api/v1")
        if parsed.query or parsed.fragment or not parsed.path.startswith("/api/v1/"):
            raise TalkingHeadBlocked("WAN_ENDPOINT_INVALID", "认证请求不是官方 Wan API 请求。")

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, request, fp, code, msg, response_headers, newurl):
            return None

    request = urllib.request.Request(req["url"], data=req.get("body"), headers=headers,
                                     method=req.get("method", "GET"))
    try:
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=timeout) as response:
            return {"status_code": response.status, "body": response.read()}
    except urllib.error.HTTPError as error:
        return {"status_code": error.code, "body": error.read() if error.fp else b""}
    except Exception:
        return {"status_code": None, "error": "network_error"}
