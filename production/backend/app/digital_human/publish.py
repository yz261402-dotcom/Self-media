"""把本地素材发布到火山 TOS 公开读，返回 VOLC 境内可直连 GET 的 https URL。

背景
----
OmniHuman 标准接口只接受 `image_url` / `audio_url` 两个 **http(s) 媒体 URL**
（见 `providers.build_submit_request` 与 `submission._require_public_url`），
本地相对路径一律被拒。本项目资产默认落在本机磁盘，真实链路跑批前必须先发布到
**大陆可达的公开存储**。TOS 与 VOLC 同属火山引擎，境内机房直连、VOLC 必能抓取。

实现
----
使用火山官方 TOS Python SDK（`tos`，即 ve-tos-python-sdk），而非裸 boto3——
TOS 网关不认 boto3 的请求/签名格式（直接调会 400 Bad Request）。
- `endpoint`：`tos-<region>.volces.com`；`region`：`cn-beijing`（华北2·北京）。
- 发布时给对象打 `ACLType.PUBLIC_READ`，使对象可被匿名 GET；桶保持私有。
- 公开访问域名：`<bucket>.tos-<region>.volces.com/<key>`。

地域与桶名都是**账号相关**的，所以都可配
------------------------------------------
`cn-beijing` 只是默认值，不是所有账号都开通了它：实测存在"只有 cn-shanghai
可用、cn-beijing 报 `AccountDisable`"的账号。桶名同理——TOS 桶命名空间
**全局共享**，`douyin-avatar-studio` 这种通用名字很可能已被别人占用。
两者都允许用环境变量覆盖：

- `AVATAR_STUDIO_TOS_REGION`   → 地域（默认 `cn-beijing`）
- `AVATAR_STUDIO_TOS_ENDPOINT` → 端点（默认由地域推导 `tos-<region>.volces.com`）
- `AVATAR_STUDIO_TOS_BUCKET`   → 桶名（默认 `douyin-avatar-studio`）

注意：本模块不替你下"法律结论"——public-read 意味着任何人可下载该对象，
只放脱敏的口播素材（人物图/音频），不放密钥/PII。
"""
from __future__ import annotations

import os
import hashlib
import urllib.request
from typing import Optional

import tos
from tos.enum import ACLType

TOS_DEFAULT_ENDPOINT = "tos-cn-beijing.volces.com"
TOS_DEFAULT_REGION = "cn-beijing"
TOS_DEFAULT_BUCKET = "douyin-avatar-studio"

TOS_REGION_ENV = "AVATAR_STUDIO_TOS_REGION"
TOS_ENDPOINT_ENV = "AVATAR_STUDIO_TOS_ENDPOINT"
TOS_BUCKET_ENV = "AVATAR_STUDIO_TOS_BUCKET"


def resolved_region() -> str:
    """生效地域：环境变量优先。不同账号开通的地域不同，写死会直接 AccountDisable。"""
    return (os.environ.get(TOS_REGION_ENV) or "").strip() or TOS_DEFAULT_REGION


def resolved_endpoint(region: Optional[str] = None) -> str:
    """生效端点：环境变量优先，否则按地域推导。"""
    explicit = (os.environ.get(TOS_ENDPOINT_ENV) or "").strip()
    return explicit or f"tos-{region or resolved_region()}.volces.com"


def resolved_bucket() -> str:
    """生效桶名：环境变量优先。桶名全局唯一，默认值未必归本账号。"""
    return (os.environ.get(TOS_BUCKET_ENV) or "").strip() or TOS_DEFAULT_BUCKET


def public_url(bucket: str, region: str, key: str) -> str:
    """TOS 虚拟主机风格公开访问地址（对象已 PUBLIC_READ 时可直接 GET）。"""
    return f"https://{bucket}.tos-{region}.volces.com/{key}"


class TosPublisher:
    """极简 TOS 发布器：本地文件 -> PUBLIC_READ 对象 -> 公开 https URL。"""

    def __init__(self, ak: str, sk: str, bucket: str, region: Optional[str] = None,
                 endpoint: Optional[str] = None) -> None:
        if not ak or not sk:
            raise ValueError("TOS 发布需要 ak/sk（与 VOLC 共用同一套火山 IAM 凭据）")
        if not bucket:
            raise ValueError("必须指定 TOS bucket 名（全局唯一）")
        self.bucket = bucket
        self.region = (region or "").strip() or resolved_region()
        self.client = tos.TosClientV2(
            ak, sk, (endpoint or "").strip() or resolved_endpoint(self.region), self.region
        )

    def ensure_bucket(self) -> None:
        """不存在则建桶；**本账号已拥有**则忽略；被别的账号占用则直接抛。

        `BucketAlreadyExists` 绝不能吞：TOS 桶命名空间全局共享，这个码的含义是
        "这个名字被别人的账号占了"。吞掉它，代码就会一路往**别人的桶**里写，
        最后只在写入阶段报一个含义模糊的 `AccessDenied`——病因被藏起来了。
        （只有 `BucketAlreadyOwnedByYou` 才是"自己已经有这个桶"。）
        """
        try:
            self.client.create_bucket(self.bucket)
        except Exception as e:  # noqa: BLE001
            code = getattr(e, "code", "")
            if code == "BucketAlreadyOwnedByYou":
                return
            raise

    def url_for(self, key: str) -> str:
        return public_url(self.bucket, self.region, key)

    def verify_public_file(self, url: str, expected_hash: str) -> None:
        """以供应商同样的匿名 GET 方式读取，核对字节，不能只看 HEAD/对象存在。"""
        digest = hashlib.sha256()
        with urllib.request.urlopen(url, timeout=60) as response:
            if response.status != 200:
                raise ValueError('媒体匿名读取未返回 HTTP 200')
            for chunk in iter(lambda: response.read(1024 * 1024), b''):
                digest.update(chunk)
        if 'sha256:' + digest.hexdigest() != expected_hash:
            raise ValueError('远端素材字节与本地登记哈希不一致')

    def publish_file(self, local_path: str, key: Optional[str] = None,
                     content_type: Optional[str] = None) -> str:
        """上传本地文件为 PUBLIC_READ 对象，返回公开 URL。"""
        if not os.path.isfile(local_path):
            raise FileNotFoundError(f"待发布文件不存在: {local_path}")
        if key is None:
            key = os.path.basename(local_path)
        self.client.put_object_from_file(
            self.bucket, key, local_path,
            content_type=content_type, acl=ACLType.ACL_Public_Read,
        )
        return public_url(self.bucket, self.region, key)

    def exists(self, key: str) -> bool:
        """对象是否已存在（用于幂等：已发布则直接返回 URL）。"""
        try:
            self.client.head_object(self.bucket, key)
            return True
        except Exception:  # noqa: BLE001 - 任意 404/403 都视为不存在
            return False
