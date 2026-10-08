"""服务端配置、模式越级控制、预算策略与供应商受控连接检查（T01）。

设计约束（05 实施计划 T01）：
- 默认预算 0、可计费提交次数 0；任何计费动作先过预算策略。
- 键名齐全不等于模型可用：连接检查区分 未配置 / 认证失败 / 额度不足 /
  模型不存在 / 能力不支持 / 网络故障 / 未实测，不把"配置项存在"报成可用。
- 密钥不返回前端、不写日志：对外只暴露脱敏视图。
- 运行目录与依赖目录属于排除目录，不得作为契约或资产来源被扫描。
"""
from __future__ import annotations

import os
import sys
from typing import Any, Dict, List, Literal, Optional, Tuple

from .contracts import Mode


def _load_local_env_file() -> None:
    """零依赖读取项目根目录 .env（若存在），仅补充尚未显式设置的环境变量。

    设计取舍：不在生产代码引入 python-dotenv 依赖；只 setdefault，保证 shell / CI
    显式传入的变量优先级最高。测试期（pytest 已导入）不加载，避免污染 budget/mode 等默认值。
    """
    from pathlib import Path
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if not env_path.is_file():
        return
    with env_path.open("r", encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key, value = key.strip(), value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


# 运行期补充 .env（仅填充缺失项）；测试期跳过，避免改变默认值
if "pytest" not in sys.modules:
    _load_local_env_file()

MODES: List[str] = ["mock", "integration", "production"]

# 排除目录：不纳入版本控制、不作为 Skill/资产/契约来源被扫描
EXCLUDED_DIR_NAMES = ["runtime", ".venv", "venv", "node_modules", "__pycache__", ".git", ".pytest_cache"]

ConnectionState = Literal[
    "not_configured",
    "auth_failed",
    "quota_exhausted",
    "model_not_found",
    "capability_unsupported",
    "network_error",
    "unverified",
    "ready",
]

# 只有 ready 才代表可发起真实请求；unverified 表示配置项存在但未经实测
USABLE_STATES = ("ready",)


class BudgetPolicy:
    """预算策略。默认全 0：未授权即不得产生计费副作用。"""

    def __init__(self, per_task_limit_fen: int = 0, daily_limit_fen: int = 0,
                 max_billable_submissions: int = 0, spent_today_fen: int = 0,
                 submissions_today: int = 0):
        self.per_task_limit_fen = per_task_limit_fen
        self.daily_limit_fen = daily_limit_fen
        self.max_billable_submissions = max_billable_submissions
        self.spent_today_fen = spent_today_fen
        self.submissions_today = submissions_today

    def allows_billable(self, amount_fen: int = 0) -> bool:
        if self.max_billable_submissions <= 0:
            return False
        if self.submissions_today >= self.max_billable_submissions:
            return False
        if amount_fen > self.per_task_limit_fen:
            return False
        if self.spent_today_fen + amount_fen > self.daily_limit_fen:
            return False
        return True

    def to_dict(self) -> Dict[str, int]:
        return {
            "per_task_limit_fen": self.per_task_limit_fen,
            "daily_limit_fen": self.daily_limit_fen,
            "max_billable_submissions": self.max_billable_submissions,
            "spent_today_fen": self.spent_today_fen,
            "submissions_today": self.submissions_today,
        }


# 火山引擎 OmniHuman 用 AK/SK 签名（一对凭据），不经通用单 Key 机制。
# 与 digital_human/providers.py 的 SECRET_ENV_AK / SECRET_ENV_SK 保持一致。
VOLC_ACCESS_KEY_ENV = "AVATAR_STUDIO_VOLC_ACCESS_KEY"
VOLC_SECRET_KEY_ENV = "AVATAR_STUDIO_VOLC_SECRET_KEY"


class ProviderSpec:
    def __init__(self, id: str, purpose: str, env_prefix: str, default_model: str,
                 capabilities: List[str], required_env: Optional[Tuple[str, ...]] = None):
        self.id = id
        self.purpose = purpose
        self.env_prefix = env_prefix
        self.default_model = default_model
        self.capabilities = capabilities
        # 受控连接检查所需的全部环境变量；缺任一即 not_configured。
        # 大多数供应商是单一 API Key（key_env）；火山引擎是一对 AK/SK。
        self.required_env = required_env if required_env is not None else (self.key_env,)

    @property
    def key_env(self) -> str:
        return f"AVATAR_STUDIO_{self.env_prefix}_API_KEY"

    @property
    def base_env(self) -> str:
        return f"AVATAR_STUDIO_{self.env_prefix}_BASE_URL"

    @property
    def model_env(self) -> str:
        return f"AVATAR_STUDIO_{self.env_prefix}_MODEL"


PROVIDERS: Dict[str, ProviderSpec] = {
    "deepseek": ProviderSpec("deepseek", "文本决策", "DEEPSEEK", "deepseek-flash", ["chat", "tool_call"]),
    # provider key 保持 gpt-image-2 以兼容既有资产与历史运行记录；默认模型升级到
    # Image 2.5 Sunburst。若中转侧更适合速度优先，可用 AVATAR_STUDIO_GPTIMAGE_MODEL
    # 覆盖为 gpt-image-2.5-flare。
    "gpt-image-2": ProviderSpec("gpt-image-2", "人像生成与编辑", "GPTIMAGE", "gpt-image-2.5-sunburst",
                                ["text_to_image", "image_edit"]),
    "anthropic": ProviderSpec("anthropic", "内容写作", "ANTHROPIC", "claude-sonnet-5-5", ["chat"]),
    "claude_compat": ProviderSpec("claude_compat", "Claude OpenAI兼容写作", "CLAUDE_COMPAT",
                                   "claude-sonnet-5-5", ["chat"]),
    "openai": ProviderSpec("openai", "GPT 内容写作", "OPENAI", "gpt-6.1-sol", ["chat"]),
    "qwen": ProviderSpec("qwen", "参考拆解与独立审稿", "QWEN", "qwen-plus", ["chat"]),
    "redfox": ProviderSpec("redfox", "抖音参考检索与转写", "REDFOX", "redfox-api", ["search", "transcript"]),
    "minimax": ProviderSpec("minimax", "TTS", "MINIMAX", "speech-2.8-hd", ["tts", "voice_clone"]),
    "m2_worker": ProviderSpec("m2_worker", "口播推理", "M2WORKER", "LivePortrait+MuseTalk1.5",
                              ["motion", "lipsync"], required_env=("AVATAR_STUDIO_M2_WORKER_URL",)),
    # OmniHuman 走火山视觉官方服务，用 AK/SK 签名而不是单一 Bearer Key。
    # 因此连接检查只看 X 前缀下的配置项是否存在，凭据本身用
    # AVATAR_STUDIO_VOLC_ACCESS_KEY / _SECRET_KEY 注入，不经本通用键机制。
    "volcengine": ProviderSpec("volcengine", "数字人口播（云）", "VOLC", "omnihuman-1.5",
                               ["image_audio_to_video"],
                               required_env=(VOLC_ACCESS_KEY_ENV, VOLC_SECRET_KEY_ENV)),
    # 与文本/ASR 共用百炼 Key，但视频模型和服务端地址必须独立配置。
    "dashscope_wan": ProviderSpec("dashscope_wan", "创意短视频（百炼官方）", "WAN",
                                  "wan3.0-video", ["image_audio_to_video"],
                                  required_env=("AVATAR_STUDIO_QWEN_API_KEY",)),
}


class ProviderCheck:
    """连接检查结果。detail 不含任何密钥片段。"""

    def __init__(self, provider: str, state: ConnectionState, detail: str,
                 model: str = "", capabilities: Optional[List[str]] = None,
                 configured: bool = False):
        self.provider = provider
        self.state = state
        self.detail = detail
        self.model = model
        self.capabilities = list(capabilities or [])
        self.configured = configured

    @property
    def usable(self) -> bool:
        return self.state in USABLE_STATES

    def to_dict(self) -> Dict[str, Any]:
        return {
            "provider": self.provider,
            "state": self.state,
            "usable": self.usable,
            "model": self.model,
            "capabilities": self.capabilities,
            "configured": self.configured,
            "detail": self.detail,
        }


# 密钥文件里的名称 → 本工程供应商 env_prefix
SECRET_NAME_MAP: Dict[str, str] = {
    "gpt-image": "GPTIMAGE",
    "openai": "OPENAI",
    "gpt": "OPENAI",
    "deepseek": "DEEPSEEK",
    "minmax": "MINIMAX",
    "minimax": "MINIMAX",
    "qwen": "QWEN",
    "千问": "QWEN",
    "百炼": "QWEN",
    "claude": "ANTHROPIC",
    "anthropic": "ANTHROPIC",
    "redfox": "REDFOX",
}


def load_secrets_from_file(path: str) -> List[str]:
    """从本地密钥清单注入环境变量。

    约定：文件内形如 `**名称**:值` 的键值对；键为 URL 时绑定到上一个名称。
    只注入已知供应商，未知名称忽略；**返回值只包含已注入的 env 名，不含任何密钥内容**。
    密钥不写日志、不返回前端、不落库。
    """
    import re

    if not path or not os.path.exists(path):
        return []
    injected: List[str] = []
    current: Optional[str] = None
    volc_pending = False
    kv = re.compile(r"\*\*([^*]+)\*\*\s*[:：]\s*(\S.*)$")
    aks = re.compile(r"^(AK|SK)\s*[:：]\s*(\S+)\s*$")
    with open(path, encoding="utf-8") as f:
        for raw in f:
            # 剔除零宽字符（U+200B/U+200C/U+200D/U+FEFF 等），再按常规空白裁剪；
            # 密钥文件部分行以零宽空格 + tab 起头，str.strip() 不会移除它，会破坏续行解析。
            line = re.sub(r"[\u200b\u200c\u200d\ufeff]", "", raw).strip()
            if not line:
                continue
            m = kv.match(line)
            if m:
                name, value = m.group(1).strip(), m.group(2).strip()
                if name.upper() == "URL":
                    if current:
                        env = f"AVATAR_STUDIO_{current}_BASE_URL"
                        os.environ.setdefault(env, value)
                        injected.append(env)
                    continue
                key = name.lower().split()[0]
                norm = key.replace("_", "-")
                # 火山引擎 OmniHuman 用 AK/SK 签名（一对凭据），不经通用单 Key 机制。
                # 与 digital_human/providers.py 的 SECRET_ENV_AK / SECRET_ENV_SK 保持一致。
                if norm == "volc-ak":
                    os.environ.setdefault("AVATAR_STUDIO_VOLC_ACCESS_KEY", value)
                    injected.append("AVATAR_STUDIO_VOLC_ACCESS_KEY")
                    continue
                if norm == "volc-sk":
                    os.environ.setdefault("AVATAR_STUDIO_VOLC_SECRET_KEY", value)
                    injected.append("AVATAR_STUDIO_VOLC_SECRET_KEY")
                    continue
                # 「OmniHuman1.5 / 火山」区块：AK/SK 以内联或独立续行给出（密钥文件用中文冒号、分行书写）。
                if "omnihuman" in norm or norm.startswith("volc") or key.startswith("omnihuman"):
                    ak = re.search(r"AK\s*[:：]\s*(\S+)", value)
                    if ak:
                        os.environ.setdefault("AVATAR_STUDIO_VOLC_ACCESS_KEY", ak.group(1))
                        injected.append("AVATAR_STUDIO_VOLC_ACCESS_KEY")
                    volc_pending = True
                    current = "VOLC"
                    continue
                current = SECRET_NAME_MAP.get(key)
                if current:
                    env = f"AVATAR_STUDIO_{current}_API_KEY"
                    os.environ.setdefault(env, value)
                    injected.append(env)
                continue
            # 紧跟 OmniHuman 区块的裸 AK/SK 续行（如 `SK：<token>`）。
            if volc_pending:
                m3 = aks.match(line)
                if m3:
                    token = m3.group(2)
                    if m3.group(1) == "AK":
                        os.environ.setdefault("AVATAR_STUDIO_VOLC_ACCESS_KEY", token)
                        injected.append("AVATAR_STUDIO_VOLC_ACCESS_KEY")
                    else:
                        os.environ.setdefault("AVATAR_STUDIO_VOLC_SECRET_KEY", token)
                        injected.append("AVATAR_STUDIO_VOLC_SECRET_KEY")
                    volc_pending = False
                    current = None
                    continue
            m2 = re.match(r"URL\s*[:：]\s*(\S.*)$", line)
            if m2 and current:
                env = f"AVATAR_STUDIO_{current}_BASE_URL"
                os.environ.setdefault(env, m2.group(1).strip())
                injected.append(env)
    return injected


def bootstrap_secrets() -> List[str]:
    """启动时按需注入：仅当显式给出密钥文件路径时生效，不猜测用户目录。"""
    return load_secrets_from_file(os.environ.get("AVATAR_STUDIO_SECRETS_FILE", ""))


def redact(value: Optional[str]) -> str:
    """脱敏：固定掩码 + 末 2 位。掩码长度固定，避免连密钥长度都泄露出去。"""
    if not value:
        return ""
    if len(value) <= 4:
        return "****"
    return "********" + value[-2:]


def get_mode() -> Mode:
    raw = os.environ.get("AVATAR_STUDIO_MODE", "mock").strip().lower()
    return raw if raw in MODES else "mock"  # type: ignore[return-value]


def resolve_mode(requested: Optional[str] = None) -> Mode:
    """模式解析与越级拦截。

    mock → 任意环境可进入；integration / production 必须显式授权，
    否则回落 mock 并记录原因，不允许靠改一个环境变量就跳到生产。
    """
    target = (requested or get_mode()).strip().lower()
    if target not in MODES:
        return "mock"
    if target == "mock":
        return "mock"
    flag = f"AVATAR_STUDIO_ALLOW_{target.upper()}"
    if os.environ.get(flag) != "1":
        return "mock"
    if target == "production" and not _production_ready():
        return "mock"
    return target  # type: ignore[return-value]


def _production_ready() -> bool:
    """production 额外要求：预算已授权且关键供应商实测可用。"""
    if budget_policy().max_billable_submissions <= 0:
        return False
    return any(check_provider(p).usable for p in PROVIDERS)


def budget_policy() -> BudgetPolicy:
    def _int(name: str, default: int = 0) -> int:
        raw = os.environ.get(name, "")
        try:
            return int(raw) if raw != "" else default
        except ValueError:
            return default

    return BudgetPolicy(
        per_task_limit_fen=_int("AVATAR_STUDIO_BUDGET_PER_TASK_FEN", 0),
        daily_limit_fen=_int("AVATAR_STUDIO_BUDGET_DAILY_FEN", 0),
        max_billable_submissions=_int("AVATAR_STUDIO_BUDGET_MAX_SUBMISSIONS", 0),
    )


def classify_provider_error(status_code: Optional[int] = None, code: Optional[str] = None,
                            message: str = "") -> ConnectionState:
    """把供应商返回的错误归一到六种状态，避免把未知错误笼统报成"不可用"。"""
    c = (code or "").upper()
    m = (message or "").lower()
    if status_code in (401, 403) or c in ("AUTH_FAILED", "INVALID_API_KEY", "UNAUTHORIZED"):
        return "auth_failed"
    if status_code == 429 or c in ("QUOTA_EXHAUSTED", "INSUFFICIENT_BALANCE", "RATE_LIMITED"):
        return "quota_exhausted"
    if status_code == 404 or c in ("MODEL_NOT_FOUND", "MODEL_NOT_EXIST"):
        return "model_not_found"
    if c in ("CAPABILITY_UNSUPPORTED", "UNSUPPORTED_OPERATION"):
        return "capability_unsupported"
    if status_code in (502, 503, 504) or c in ("NETWORK_ERROR", "TIMEOUT", "CONNECTION_ERROR") \
            or "timeout" in m or "connect" in m:
        return "network_error"
    if status_code is None and not code and not message:
        return "network_error"
    return "network_error"


def _provider_model(provider: str) -> str:
    """返回当前供应商模型；未知供应商返回空字符串。"""
    spec = PROVIDERS.get(provider)
    if spec is None:
        return ""
    return os.environ.get(spec.model_env, spec.default_model)


def _evidence_matches_current_model(provider: str, evidence: Dict[str, Any],
                                    current_model: str) -> bool:
    """判断证据是否能证明当前模型。

    目前只有 GPT Image provider 发生了模型升级且仍复用旧 provider key；旧记录
    没有 model 字段，或记录的是 gpt-image-2，不能为 Image 2.5 背书。其它供应商
    的历史证据保持原有兼容行为。
    """
    if provider != "gpt-image-2":
        return True
    return evidence.get("model") == current_model


def record_provider_evidence(provider: str, capability: str, outcome: str,
                             evidence: str, detail: str = "") -> Dict[str, Any]:
    """登记一次真实调用证据。

    能力连通性与产出质量分开：这里只记「接口是否可用」，
    产出是否可用由资产审核门禁决定（例如接口可用但身份漂移，仍不得进生产）。
    """
    from . import storage as st
    from .contracts import now_iso

    rec = {
        "id": st.new_id("pev"),
        "provider": provider,
        "capability": capability,
        "outcome": outcome,  # succeeded / failed / unsupported
        "model": _provider_model(provider),
        "evidence": evidence,
        "detail": detail,
        "checked_at": now_iso(),
    }
    st.put("provider_evidence", rec["id"], rec)
    return rec


def provider_evidence(provider: str, capability: Optional[str] = None) -> List[Dict[str, Any]]:
    try:
        from . import storage as st

        rows = st.all_rows("provider_evidence")
    except Exception:
        return []
    rows = [r for r in rows if r.get("provider") == provider]
    if capability:
        rows = [r for r in rows if r.get("capability") == capability]
    return rows


def check_provider(provider: str, capability: Optional[str] = None,
                   error: Optional[Dict[str, Any]] = None) -> ProviderCheck:
    """受控连接检查。

    不主动发起真实请求：无密钥即 not_configured；有密钥但未实测即 unverified。
    调用方传入 error（真实调用失败）时才归因为具体失败状态。
    """
    spec = PROVIDERS.get(provider)
    if spec is None:
        return ProviderCheck(provider, "capability_unsupported",
                             "供应商未在本工程受支持清单内，未做任何连接尝试")
    configured = all(os.environ.get(e) for e in spec.required_env)
    model = os.environ.get(spec.model_env, spec.default_model)
    if error:
        state = classify_provider_error(error.get("status_code"), error.get("code"),
                                        error.get("message", ""))
        return ProviderCheck(provider, state, f"真实调用失败：{state}（不重试计费动作）",
                             model, spec.capabilities, configured)
    if capability and capability not in spec.capabilities:
        return ProviderCheck(provider, "capability_unsupported",
                             f"能力 {capability} 不在 {spec.id} 受支持范围内",
                             model, spec.capabilities, configured)
    if not configured:
        return ProviderCheck(provider, "not_configured",
                             f"未配置 {', '.join(spec.required_env)}；URL/Key/model 文本齐全不等于模型可用",
                             model, spec.capabilities, False)

    # 有实测证据才判 ready：配置项存在本身只算 unverified
    rows = sorted(provider_evidence(provider, capability), key=lambda r: r.get("checked_at", ""))
    rows = [r for r in rows if _evidence_matches_current_model(provider, r, model)]
    succeeded = [r for r in rows if r.get("outcome") == "succeeded"]
    if succeeded:
        last = succeeded[-1]
        return ProviderCheck(provider, "ready",
                             f"已实测可用（{last.get('capability')}）：{last.get('evidence')}",
                             model, spec.capabilities, True)
    unsupported = [r for r in rows if r.get("outcome") == "unsupported"]
    if unsupported:
        last = unsupported[-1]
        return ProviderCheck(provider, "capability_unsupported",
                             f"实测不支持该能力：{last.get('detail') or last.get('evidence')}",
                             model, spec.capabilities, True)
    return ProviderCheck(provider, "unverified",
                         "配置项存在，但本工程尚未完成真实连通与能力实测",
                         model, spec.capabilities, True)


def public_provider_view(provider: str, capability: Optional[str] = None) -> Dict[str, Any]:
    """对外暴露的供应商状态：不含密钥、不含 base_url 原文。"""
    chk = check_provider(provider, capability)
    d = chk.to_dict()
    env0 = PROVIDERS[provider].required_env[0] if provider in PROVIDERS else ""
    d["api_key"] = redact(os.environ.get(env0)) if provider in PROVIDERS else ""
    d["api_key_present"] = bool(d["api_key"])
    return d


def provider_states() -> List[Dict[str, Any]]:
    return [public_provider_view(p) for p in PROVIDERS]


def excluded_dirs(root: str) -> List[str]:
    """返回 root 下存在的排除目录名（用于校验未被纳入版本控制/资产扫描）。"""
    found = []
    for name in EXCLUDED_DIR_NAMES:
        if os.path.isdir(os.path.join(root, name)):
            found.append(name)
    return found


def assert_not_excluded(path: str) -> None:
    """资产与契约来源不得落在排除目录内。"""
    parts = os.path.normpath(path).replace("\\", "/").split("/")
    hit = [p for p in parts if p in EXCLUDED_DIR_NAMES]
    if hit:
        raise ValueError(f"路径位于排除目录内，不得作为资产或契约来源：{hit}")


def contract_version() -> str:
    from .contracts import CONTRACT_VERSION

    return CONTRACT_VERSION


# T02：持久方案只在取得恢复、事务与并发证据后定版。
# PostgreSQL 与 pgvector 属于"待验证建议组合"，未实测前不得宣称已部署。
PERSISTENCE_BACKEND = "postgresql"
POSTGRES_VERIFIED = True


def persistence_status() -> Dict[str, Any]:
    return {
        "backend": PERSISTENCE_BACKEND,
        "postgres_verified": POSTGRES_VERIFIED,
        "postgres_pgvector_verified": False,
        "note": "业务库已迁移到 PostgreSQL（写事务用 advisory 锁串行）；pgvector 尚未验证，不宣称已部署",
        "evidence": ["task_recovery:reread", "lease_race:transaction", "job_ledger:consistent"],
    }
