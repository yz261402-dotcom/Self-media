#!/usr/bin/env python3
"""按 task_id 查询火山 OmniHuman 任务的原始状态（只读，不计费）。

为什么需要它
------------
OmniHuman 是异步接口：提交拿到 `task_id` 后要靠 `CVGetResult` 轮询。
查询本身**不产生费用**，所以在"提交结果未知、要不要重做"的判断里，
查询是唯一能把状态问清楚的免费手段。

而这个接口的错误码语义相当不直观，实测校准如下（2026-09-28）：

| 输入 | HTTP | 响应 | 含义 |
|---|---|---|---|
| 任意伪造 id（如 `zzz-not-a-task`） | 200 | `status: not_found` | id 格式没问题，服务里没这条记录 |
| 7 天前的历史 id | 200 | `status: not_found` | 记录已被平台清理 |
| 提交时**音频 ≥ 60 秒** | **400** | `code: 50215` `Input invalid for this service.` | **参数不满足本服务要求**（见下） |
| `req_key` 换成无权限的服务 | **401** | `code: 50400` `Access Denied` | 该服务未开通/无权限 |

**50215 的正确读法（2026-09-28 真实踩坑后纠正）**：
官方文档明确写了 `audio_url` 的音频**时长必须小于 60 秒**，且"过长情况下
**提交任务正常，查询任务会报错** `50215`"。所以 50215 的病因在**我们发出去的参数**，
最常见的具体形态就是"把整条 61.7s 音轨当成分段音频发了出去"。

⚠️ 曾经把它误判成"账号开通/额度问题"，并据此去查控制台——**那是错的**，
浪费了一整轮排查。凭据、开通状态、额度都是好的。

正确处置顺序：
1. 先量**实际发给云端的音频**有多长 —— 用 `scripts/check_omnihuman_segment_audio.py <work_id>`，
   它会把每段真实 URL 匿名读回来量时长。这一步免费。
2. 只有确认参数合规后仍报 50215/50400，才去控制台看开通与额度。

判据上的区别：**伪造 id 返回 200 `not_found`；50215 是"服务认得这个 id、但说这个请求
不满足本服务要求"**。

用法（在 production/backend 下）
--------------------------------
    ./.venv/Scripts/python.exe ../scripts/check_omnihuman_task.py <task_id>
    ./.venv/Scripts/python.exe ../scripts/check_omnihuman_task.py <task_id> --req-key jimeng_realman_avatar_picture_omni_v2

task_id 可从 `provider_runs.remote_job_id` 或界面上的运行记录里取。
本脚本只查询、不提交、不重试、不计费。
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

BACKEND = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend")
sys.path.insert(0, os.path.abspath(BACKEND))

# 本机常有 127.0.0.1:7897 代理；代理会干扰签名请求，这里显式清掉。
for _key in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy",
             "ALL_PROXY", "all_proxy"):
    os.environ.pop(_key, None)

from app import config  # noqa: E402


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        print(__doc__)
        return 2
    task_id = args[0]
    req_key = None
    if "--req-key" in sys.argv:
        idx = sys.argv.index("--req-key")
        req_key = sys.argv[idx + 1] if idx + 1 < len(sys.argv) else None

    config.bootstrap_secrets()
    from app.digital_human import providers as pv  # noqa: E402

    req_key = req_key or pv.OMNIHUMAN_REQ_KEY
    ak = os.environ.get("AVATAR_STUDIO_VOLC_ACCESS_KEY", "")
    sk = os.environ.get("AVATAR_STUDIO_VOLC_SECRET_KEY", "")
    if not ak or not sk:
        print("缺凭据：请在密钥清单里补 volc-ak / volc-sk 两行。")
        return 2

    # build_query_request 固定用 OMNIHUMAN_REQ_KEY；要换 req_key 时自己组一次。
    if req_key == pv.OMNIHUMAN_REQ_KEY:
        req = pv.build_query_request(task_id=task_id, ak=ak, sk=sk)
    else:
        body = json.dumps({"req_key": req_key, "task_id": task_id}).encode("utf-8")
        query = {"Action": pv.VOLC_ACTION_QUERY, "Version": pv.VOLC_API_VERSION}
        req = {"url": pv.VOLC_ENDPOINT + "?" + pv.canonical_query(query),
               "method": "POST",
               "headers": pv.sign_request(ak=ak, sk=sk, method="POST", query=query,
                                          body=body, x_date=pv.utc_x_date()),
               "body": body}

    print(f"task_id : {task_id}")
    print(f"req_key : {req_key}")
    print(f"url     : {req['url']}\n")

    http = urllib.request.Request(req["url"], data=req["body"], method=req["method"],
                                  headers=req["headers"])
    try:
        with urllib.request.urlopen(http, timeout=60) as resp:
            status, raw = resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        status, raw = e.code, e.read().decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        print(f"连接失败：{type(e).__name__}: {e}")
        print("（本机代理是否已清掉？本脚本已尝试清 HTTPS_PROXY 等变量）")
        return 1

    print(f"HTTP {status}")
    try:
        doc = json.loads(raw)
        print(json.dumps(doc, ensure_ascii=False, indent=1)[:1500])
    except Exception:  # noqa: BLE001
        print(raw[:1500])
        return 1

    print("\n---- 判读 ----")
    code = doc.get("code")
    data = doc.get("data") or {}
    if status == 200 and code in (10000, "10000"):
        remote = data.get("status")
        if remote == "not_found":
            print("服务里没有这条任务记录：id 格式没问题，但平台查不到它。")
            print("（历史任务过一段时间会被清理，属正常；也可能是从未真正登记。）")
        elif remote in ("done", "success"):
            print(f"任务已完成。video_url = {data.get('video_url')!r}")
            print("注意：done 还要能下载并通过字节校验才算成功产物。")
        else:
            print(f"远端状态 = {remote!r}（仍在进行中，稍后再查）。")
        return 0
    if code == 50215:
        print("**这个请求不满足本服务的要求**（Input invalid for this service）。")
        print("这不是密钥或开通问题：伪造 id 会返回 200 not_found，"
              "而 50215 是「服务认得这个 id、但说这个请求不合规」。")
        print("最常见病因：`audio_url` 的音频 ≥ 60 秒（官方要求严格小于 60s，"
              "且超长时提交正常、只有查询才报 50215）。")
        print("先跑免费核验：../scripts/check_omnihuman_segment_audio.py <work_id>，"
              "把每段真实音频读回来量时长；确认合规后再去控制台看开通与额度。")
        return 1
    if code == 50400 or status == 401:
        print("该 req_key 对应的服务**未开通或无权限**（Access Denied）。")
        print("这与 2026-09-21 开通前的错误码一致：请到控制台开通对应服务。")
        return 1
    if code == 50200:
        print("req_key 不被支持：名字写错了。请核对服务文档里的 req_key。")
        return 1
    print("未归类的响应：按上面的 code/message 判断，不要改判成成功或失败。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
