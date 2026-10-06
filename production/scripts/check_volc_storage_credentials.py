#!/usr/bin/env python3
"""核对火山对象存储（TOS）凭据是否可用。

为什么需要它
------------
OmniHuman 是**云端 API**：远端火山服务要自己去公网下载人物图与配音，
所以素材必须先放到**公网可读**的对象存储。本项目里这条通路只有 TOS
（`app/digital_human/publish.py::TosPublisher`）。凭据一旦不可用，
提交就会在 `OMNIHUMAN_MEDIA_UPLOAD_FAILED` 上被拦下（尚未提交、不产生费用）。

而 TOS 的错误码能把病因分得很细，值得单独看清楚：

- `InvalidAccessKeyId`    → AK 本身不存在（写错了，或密钥已删除/停用）
- `SignatureDoesNotMatch` → AK 存在，但 SK 与它不配对（SK 复制错/不完整）
- `AccountDisable`        → **该地域**没有开通 TOS（"The account does not open tos service"）
- `BucketAlreadyExists`   → 桶名已被**别的账号**占用（TOS 桶命名空间全局共享）
- `AccessDenied`          → 凭据有效、服务已开通，但该桶没有读写权限
- 连接类错误               → 网络/代理问题（本机有 127.0.0.1:7897 代理时要注意）

两个最容易踩的坑都不是"权限问题"：地域（账号级）和桶名（全局唯一）。
本脚本因此显式打印生效的地域/端点/桶名，并且**直接调用产品用的
`TosPublisher`**，不做第二份实现——否则脚本说 OK、产品仍失败，毫无意义。

用法（在 production/backend 下）
--------------------------------
    ./.venv/Scripts/python.exe ../scripts/check_volc_storage_credentials.py

凭据来源与产品一致：`AVATAR_STUDIO_SECRETS_FILE` 指向的密钥清单，
由 `app.config.bootstrap_secrets()` 注入 `AVATAR_STUDIO_VOLC_ACCESS_KEY/_SECRET_KEY`。
地域/端点/桶名同样与产品一致：`AVATAR_STUDIO_TOS_REGION` /
`AVATAR_STUDIO_TOS_ENDPOINT` / `AVATAR_STUDIO_TOS_BUCKET`。
本脚本**只打印错误码与字符串形态，绝不打印密钥内容**。
"""
from __future__ import annotations

import os
import sys

BACKEND = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend")
sys.path.insert(0, os.path.abspath(BACKEND))

from app import config  # noqa: E402


def describe(label: str, value: str) -> None:
    """只描述形态：长度、字符类、首尾几位。不泄露内容。"""
    if not value:
        print(f"  {label}: 空（未注入）")
        return
    kinds = set()
    for ch in value:
        if ch.isascii() and ch.isalnum():
            kinds.add("alnum")
        elif ch == "=":
            kinds.add("base64填充")
        elif ch in "-_":
            kinds.add("urlsafe")
        elif ch.isascii():
            kinds.add("其他ASCII")
        else:
            kinds.add("非ASCII")
    print(f"  {label}: 长度={len(value)} 字符类={sorted(kinds)} 首4={value[:4]} 尾2={value[-2:]}")


def main() -> int:
    injected = config.bootstrap_secrets()
    print("密钥清单注入结果：", injected or "（未注入任何变量）")

    ak = os.environ.get("AVATAR_STUDIO_VOLC_ACCESS_KEY", "")
    sk = os.environ.get("AVATAR_STUDIO_VOLC_SECRET_KEY", "")
    print("凭据形态：")
    describe("AK", ak)
    describe("SK", sk)

    if not ak or not sk:
        print("\n结论：缺凭据。请在密钥清单里补 **volc-ak** / **volc-sk** 两行。")
        return 2

    try:
        import tos  # noqa: F401 - 仅确认依赖存在
    except ImportError:
        print("\n结论：当前解释器没有 tos SDK，请在 backend 的 venv 里跑。")
        return 2

    # 与产品同一份解析结果（这里绝不自己拼地域/桶名，否则脚本会和产品脱节）
    from app.digital_human import publish as pub  # noqa: E402

    region = pub.resolved_region()
    endpoint = pub.resolved_endpoint(region)
    bucket = pub.resolved_bucket()
    print("生效目标（与产品一致）：")
    print(f"  地域 region   : {region}")
    print(f"  端点 endpoint : {endpoint}")
    print(f"  桶   bucket   : {bucket}")

    results: list[tuple[str, str]] = []

    def probe(label: str, call) -> str:
        try:
            call()
            print(f"\n[{label}] OK")
            results.append((label, "ok"))
            return "ok"
        except Exception as exc:  # noqa: BLE001 - 这里就是要如实报出供应商错误
            code = getattr(exc, "code", "") or type(exc).__name__
            status = getattr(exc, "status_code", "")
            message = str(getattr(exc, "message", "") or exc)[:300]
            print(f"\n[{label}] 失败 code={code} status={status}")
            if message:
                print(f"  message: {message}")
            results.append((label, code))
            return code

    # 直接复用产品实现：TosPublisher 内部就是 TosClientV2(ak, sk, endpoint, region)。
    # 先单独列一次桶，用来区分"AK/SK 配对错"和"服务/桶的问题"。
    try:
        publisher = pub.TosPublisher(ak, sk, bucket)
    except Exception as exc:  # noqa: BLE001
        print(f"\n结论：无法构造 TOS 客户端：{exc}")
        return 1

    probe("列桶（校验 AK/SK 配对）", publisher.client.list_buckets)
    probe("建桶（本账号已拥有则忽略）", publisher.ensure_bucket)

    import hashlib  # noqa: E402
    import tempfile  # noqa: E402

    probe_key = "_healthcheck/probe.txt"
    probe_body = b"avatar-studio-tos-healthcheck"
    expected = "sha256:" + hashlib.sha256(probe_body).hexdigest()

    tmp_path = ""
    handle, tmp_path = tempfile.mkstemp(suffix=".txt")
    os.close(handle)
    with open(tmp_path, "wb") as fh:
        fh.write(probe_body)

    put_code = probe(
        "写入对象（PUBLIC_READ）",
        lambda: publisher.publish_file(tmp_path, key=probe_key, content_type="text/plain"),
    )

    url = publisher.url_for(probe_key)
    print(f"  公开 URL: {url}")
    probe("匿名读取并核对字节（远端火山要这样抓素材）",
          lambda: publisher.verify_public_file(url, expected))

    if put_code == "ok":
        probe("清理探测对象", lambda: publisher.client.delete_object(bucket, probe_key))
    if tmp_path and os.path.isfile(tmp_path):
        os.remove(tmp_path)

    codes = {code for _, code in results}
    print("\n---- 结论 ----")
    if codes == {"ok"}:
        print("凭据可用，TOS 通路完整（建桶 → 写入 → 匿名读取均通过）。")
        print("OmniHuman 的素材上传现在应当可以走通；重启后端后重试提交即可。")
        return 0
    # 顺序很重要：AccountDisable 会在建桶时先出现，且它**不是**权限问题，
    # 而是**这个地域**没开通对象存储。先判它，否则会被误报成"没权限"。
    if "AccountDisable" in codes:
        print(f"账号在 **{region}** 地域尚未开通对象存储 TOS"
              "（供应商原文：The account does not open tos service）。")
        print("TOS 的开通是**按地域**生效的：实测本账号在 cn-shanghai 可用、cn-beijing 不可用。")
        print("二选一：")
        print(f"  a) 到火山控制台 → 对象存储 TOS，把 {region} 开通（需实名认证，按量计费）；")
        print("  b) 或改用已开通的地域：在 backend/.env 里设 "
              "AVATAR_STUDIO_TOS_REGION=cn-shanghai（桶名也要换成本账号拥有的）。")
        return 1
    if "BucketAlreadyExists" in codes:
        print(f"桶名 `{bucket}` 已被**别的火山账号**占用（TOS 桶命名空间全局共享，先到先得）。")
        print("这不是权限问题：旧版代码会把这个码当成「已存在就忽略」，"
              "然后一路往别人的桶里写，最后只报一个含义模糊的 AccessDenied。")
        print("请换一个带唯一后缀的桶名，例如 `douyin-avatar-studio-sh-<你的账号ID>`，"
              "写进 backend/.env 的 AVATAR_STUDIO_TOS_BUCKET 后重跑本脚本。")
        print("注意：TOS 删桶后该名字有一段冷却期（同样报这个码），"
              "如果你刚删过同名桶，换个名字即可，不必等。")
        return 1
    if "SignatureDoesNotMatch" in codes:
        print("AK 存在但 **SK 与它不配对**：请到火山控制台 → 访问控制 IAM → 访问密钥，")
        print("复制该 AK 对应的 Secret Access Key（只创建时显示一次；找不到就新建一对）。")
        return 1
    if "InvalidAccessKeyId" in codes:
        print("AK 本身不存在：密钥可能已删除/停用，或抄写有误。请核对或新建。")
        return 1
    if "AccessDenied" in codes:
        print("凭据有效、服务也已开通，但该密钥没有对应桶的读写权限：")
        print("请在 IAM 里给这个密钥授予 TOS 读写权限，或用控制台把桶权限放开。")
        return 1
    print("部分步骤失败：按上面的 code/message 排查（注意本机代理是否干扰）。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
