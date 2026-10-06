"""Redfox（红狐）抖音数据接口适配。

契约来源：官方公开文档页与 SDK 0.6.0 源码（2026-09-29 只读调研，未用本账号实测）。
以下均为 UNVERIFIED：广域库搜索无排序参数（本模块只在已取回的页内排序，并如实标注）、
转写 status 只知 succeeded/processing/failed、4004/429 限流阈值、详情是否含视频直链。
不使用 SDK，因此默认重试天然关闭；每个函数只发一次请求。
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..errors import ContentBlocked
from ..tasks import NotSent, ProviderRefused

CONTRACT_STATUS = "unverified"
DEFAULT_BASE_URL = "https://redfox.hk"
KEY_ENV = "AVATAR_STUDIO_REDFOX_API_KEY"
SUCCESS_CODE = 2000
PATH_SEARCH = "/story/api/dy/data/searchWork"
PATH_DETAIL = "/story/api/dy/data/workDetail"
PATH_TRANSCRIPT_SUBMIT = "/story/api/parseWork/audioTextExtract/submit/douyin"
PATH_TRANSCRIPT_RESULT = "/story/api/parseWork/audioTextExtract/result/douyin"
PATH_VIDEO_DOWNLOAD = "/story/api/parseWork/videoDownload/douyin"
EXTRACTION_VERSION = "redfox-transcript-v1"
PAGE_SIZE = 50
SOURCE_TAG = "avatar-studio"

Transport = Callable[[str, Dict[str, str], bytes, int], Tuple[int, bytes]]

# 业务码 → (本地错误码, 是否可能已计费)。失败请求官方声明不计费；未列出的码保守视为费用未知。
_BUSINESS_ERRORS = {
    400: ("BAD_REQUEST", False), 401: ("AUTH_FAILED", False), 403: ("AUTH_FAILED", False),
    1001: ("BAD_REQUEST", False), 1002: ("BAD_REQUEST", False), 1003: ("BAD_REQUEST", False),
    3103: ("AUTH_FAILED", False), 3105: ("AUTH_FAILED", False),
    3201: ("QUOTA_EXHAUSTED", False), 3202: ("QUOTA_EXHAUSTED", False),
    4004: ("RATE_LIMITED", False),
}


def base_url() -> str:
    return (os.environ.get("AVATAR_STUDIO_REDFOX_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")


def api_key() -> str:
    key = os.environ.get(KEY_ENV, "")
    if not key:
        raise NotSent(f"未配置 {KEY_ENV}，不发起 Redfox 请求", "PROVIDER_NOT_CONFIGURED")
    return key


def status() -> Dict[str, Any]:
    return {"provider": "redfox", "configured": bool(os.environ.get(KEY_ENV)), "contract": CONTRACT_STATUS,
            "capabilities": ["search", "detail", "transcript", "video_download"], "sort": "client_side_within_page"}


def _default_transport(url: str, headers: Dict[str, str], body: bytes, timeout: int) -> Tuple[int, bytes]:
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def post(path: str, body: Dict[str, Any], *, transport: Optional[Transport] = None,
         timeout: int = 60) -> Dict[str, Any]:
    """发一次 POST。HTTP/业务失败抛 ProviderRefused；响应形状不符抛 ValueError（由调用层记为 unknown）。"""
    key = api_key()
    headers = {"content-type": "application/json", "X-API-KEY": key}
    payload = dict(body, source=SOURCE_TAG)
    status_code, raw = (transport or _default_transport)(
        base_url() + path, headers, json.dumps(payload, ensure_ascii=False).encode("utf-8"), timeout)
    if status_code in (401, 403):
        raise ProviderRefused("Redfox 鉴权失败", "AUTH_FAILED", status_code, charged=False)
    if status_code == 429:
        raise ProviderRefused("Redfox 限流", "RATE_LIMITED", status_code, charged=False)
    if status_code >= 500:
        raise ProviderRefused(f"Redfox 服务错误 HTTP {status_code}", "PROVIDER_ERROR", status_code, charged=None)
    if status_code >= 400:
        raise ProviderRefused(f"Redfox 请求被拒 HTTP {status_code}", "BAD_REQUEST", status_code, charged=False)
    try:
        data = json.loads(raw.decode("utf-8"))
        code = int(data["code"])
    except (ValueError, KeyError, TypeError, UnicodeDecodeError):
        raise ValueError("Redfox 响应不是 {code,msg,data} 结构") from None
    if code != SUCCESS_CODE:
        local, charged = _BUSINESS_ERRORS.get(code, ("PROVIDER_ERROR", None))
        raise ProviderRefused(f"Redfox 业务码 {code}", local, status_code, charged=charged)
    return data


# ---------------------------------------------------------------- 归一

def _int(value: Any) -> Optional[int]:
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


_VIDEO_URL_FIELDS = ("videoUrl", "playUrl", "downloadUrl", "video_url")


def _video_url(raw: Dict[str, Any]) -> Optional[str]:
    """只采用上游明确给出的直链字段，且必须是 http(s)；不猜短链、不从页面地址推导。"""
    for field in _VIDEO_URL_FIELDS:
        value = raw.get(field)
        if isinstance(value, str) and re.match(r"^https?://\S+$", value.strip(), re.I):
            return value.strip()
    return None


def normalize_item(raw: Dict[str, Any], fetched_at: str) -> Optional[Dict[str, Any]]:
    """上游作品 → 归一条目。缺失互动数保持 None（不是 0）；没有作品 ID 的条目丢弃。"""
    vid = str(raw.get("videoId") or raw.get("workId") or "").strip()
    if not vid:
        return None
    vtype = str(raw.get("videoType")) if raw.get("videoType") is not None else ""
    is_video = vtype in ("", "0", "None")
    duration = _int(raw.get("duration"))
    return {
        "video_url": _video_url(raw) if is_video else None,
        "platform": "douyin", "platform_work_id": vid,
        "title": "", "caption": str(raw.get("content") or raw.get("title") or ""),
        "author": str(raw.get("authorName") or raw.get("accountName") or ""),
        "cover_url": raw.get("coverUrl") or None,
        "source_url": raw.get("opusUrl") or raw.get("workUrl") or f"https://www.douyin.com/video/{vid}",
        "metrics": {"likes": _int(raw.get("likeCount")), "comments": _int(raw.get("commentCount")),
                    "shares": _int(raw.get("shareCount")), "collects": _int(raw.get("collectCount"))},
        "published_at": raw.get("publishTime") or None, "duration_ms": duration,
        "media_kind": "video" if is_video else "image_set",
        "transcribable": is_video,
        "tags": [t.get("tagName") for t in (raw.get("tagList") or []) if isinstance(t, dict) and t.get("tagName")],
        "fetched_at": fetched_at,
    }


def sort_items(items: List[Dict[str, Any]], sort: str) -> List[Dict[str, Any]]:
    """Redfox 广域库没有排序参数：只在已取回的这一页内排序；缺互动数的排在最后。"""
    if sort == "likes":
        return sorted(items, key=lambda i: (i["metrics"]["likes"] is None, -(i["metrics"]["likes"] or 0)))
    if sort == "recent":
        return sorted(items, key=lambda i: i["published_at"] or "", reverse=True)
    return list(items)


def encode_cursor(page: int) -> str:
    return f"page:{page}"


def decode_cursor(cursor: Optional[str]) -> int:
    if not cursor:
        return 1
    m = re.fullmatch(r"page:(\d{1,4})", cursor)
    if not m or int(m.group(1)) < 1:
        raise ContentBlocked("cursor 无效", "VALIDATION_FAILED", retryable=False)
    return int(m.group(1))


def search_works(*, keyword: str, start_date: str, end_date: str, page: int, fetched_at: str,
                 exclude_ids: Optional[List[str]] = None, sort: str = "recommended",
                 transport: Optional[Transport] = None) -> Dict[str, Any]:
    body = {"keyword": keyword, "exactMatch": False, "startDate": start_date, "endDate": end_date,
            "pageNum": page, "pageSize": PAGE_SIZE}
    data = post(PATH_SEARCH, body, transport=transport).get("data")
    if not isinstance(data, dict) or not isinstance(data.get("list", []), list):
        raise ValueError("Redfox 搜索响应缺少 data.list")
    seen = set(exclude_ids or [])
    items: List[Dict[str, Any]] = []
    dup = 0
    for raw in data.get("list", []):
        item = normalize_item(raw, fetched_at) if isinstance(raw, dict) else None
        if item is None:
            continue
        if item["platform_work_id"] in seen:
            dup += 1
            continue
        seen.add(item["platform_work_id"])
        items.append(item)
    total = _int(data.get("total"))
    size = _int(data.get("pageSize")) or PAGE_SIZE
    fetched_upto = (page - 1) * size + len(data.get("list", []))
    has_more = bool(total is not None and fetched_upto < total and data.get("list"))
    return {"items": sort_items(items, sort), "page": page, "total": total, "duplicates_dropped": dup,
            "next_cursor": encode_cursor(page + 1) if has_more else None, "fetched_at": fetched_at,
            "sorted_scope": "page" if sort != "recommended" else "upstream",
            "contract": CONTRACT_STATUS}


def work_detail(video_id: str, fetched_at: str, transport: Optional[Transport] = None) -> Dict[str, Any]:
    data = post(PATH_DETAIL, {"videoId": video_id}, transport=transport).get("data")
    if not isinstance(data, dict):
        raise ValueError("Redfox 详情响应缺少 data 对象")
    item = normalize_item(data, fetched_at)
    if item is None:
        raise ValueError("Redfox 详情响应缺少作品 ID")
    return item


def submit_transcript(url: str, transport: Optional[Transport] = None) -> str:
    data = post(PATH_TRANSCRIPT_SUBMIT, {"url": url}, transport=transport).get("data")
    task_id = (data or {}).get("taskId") if isinstance(data, dict) else None
    if not task_id:
        raise ValueError("Redfox 转写提交响应缺少 taskId")
    return str(task_id)


def query_transcript(task_id: str, transport: Optional[Transport] = None) -> Dict[str, Any]:
    """返回 {state: processing|succeeded|failed, text, segments, fail_reason}。未知 status 一律当作 processing。"""
    data = post(PATH_TRANSCRIPT_RESULT, {"taskId": task_id}, transport=transport).get("data")
    if not isinstance(data, dict):
        raise ValueError("Redfox 转写结果缺少 data 对象")
    st = str(data.get("status") or "").lower()
    if st == "succeeded":
        return {"state": "succeeded", "text": str(data.get("text") or ""),
                "segments": data.get("stampSents") or [], "fail_reason": None, "task_id": task_id}
    if st == "failed":
        return {"state": "failed", "text": "", "segments": [], "task_id": task_id,
                "fail_reason": str(data.get("failReason") or "转写失败")[:200]}
    return {"state": "processing", "text": "", "segments": [], "fail_reason": None, "task_id": task_id}


def download_video(page_url: str, transport: Optional[Transport] = None) -> Dict[str, Any]:
    """官方短视频下载器（去水印）：只返回临时直链，不落库、不进日志。

    响应形状（2026-09-30 用本账号实测一条视频）：data.videoUrl == data.resources[0].downloadUrl，
    duration 为空。没有 http(s) 直链一律当作失败，由调用层记为已确认失败而不是 unknown。
    """
    data = post(PATH_VIDEO_DOWNLOAD, {"url": page_url}, transport=transport).get("data")
    if not isinstance(data, dict):
        raise ValueError("Redfox 下载响应缺少 data 对象")
    resources = data.get("resources") if isinstance(data.get("resources"), list) else []
    candidates = [data.get("videoUrl")] + [r.get("downloadUrl") for r in resources
                                           if isinstance(r, dict) and str(r.get("type") or "video") == "video"]
    for value in candidates:
        if isinstance(value, str) and re.match(r"^https?://\S+$", value.strip(), re.I):
            return {"video_url": value.strip()}
    raise ProviderRefused("Redfox 没有返回可下载的视频直链（可能是图集或已下架）", "VIDEO_URL_UNAVAILABLE", 200,
                          charged=None)


_VIDEO_URL = re.compile(r"douyin\.com/video/(\d{8,25})|iesdouyin\.com/share/video/(\d{8,25})")


def video_id_from_url(url: str) -> Optional[str]:
    """只识别含作品 ID 的抖音链接；短链/口令需要服务端解析，此处不猜。"""
    m = _VIDEO_URL.search(url or "")
    return (m.group(1) or m.group(2)) if m else None
