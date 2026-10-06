"""官方预置音色目录（MiniMax 系统音色）的受控登记。

**来源证据**（不靠转述，靠本项目自己抓到的快照）：

- 只读请求 `GET https://api.minimaxi.com/v1/get_voice`；
- 2026-09-21，HTTP 200 / `business_code=0`，返回 **303** 条 `system_voice`；
- 快照 `experiments/人物与音色选型/AV-001/官方音色目录.json`；
- 核验记录 `experiments/人物与音色选型/AV-001/音色目录核验.json`
  （`billable_generation_calls=0`，本次读取不产生计费）。

**本模块只登记"中文口播可用的成年音色"**（26 条 `Chinese (Mandarin)_*`
＋ 19 条经典中文成年音色）。刻意排除的几类，理由写在 `EXCLUDED_NOTE`：
童声/卡通/机器人角色音不适合口播；`-beta` 与主条目同源属重复；
粤语条目作用域不同语言，需单独验收。

**登记不等于可用**。每条都以候选（`approval_state="pending"`）落库：

- 作用域四元组齐全 → 可试听（试听只要求来源记录，不要求审批）；
- 有来源记录与权属记录 → 来源链可追溯；
- **没有**基准合成证据 → `voice_ready()` 仍会阻塞生产，这是预期结果。
  补齐基准证据需要真实调用供应商，不能由本模块伪造一个哈希填进去。

用户显式选定某条候选时走 `voices.approve_voice()`，含义是
"用户确认了这组音色"，不是"这条音色已通过生产质量验收"。
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from .. import storage as st
from ..avatar import licensing
from ..contracts import now_iso
from . import sources as src
from . import voices as vc

# 目录分组：前端按这个字段分堆展示
GROUP_MANDARIN = "普通话预置"
GROUP_LEGACY = "经典中文"

# 与预设音色 3 使用同一句，保证跨音色比较成立（同句试听是硬规则）
DEFAULT_SAMPLE_TEXT = "大家好，今天我们用三分钟讲清楚一个容易被忽略的细节。"

CATALOG_SOURCE_NOTE = (
    "来源为本项目 AV-001 对 MiniMax 官方 `GET /v1/get_voice` 的只读读取"
    "（2026-09-21，303 条 system_voice）；登记的是官方预置音色，非声音克隆。"
)

LICENSE_SUBJECT = "voice_catalog_minimax_v1"
LICENSE_EVIDENCE = (
    "音色为 MiniMax 开放平台官方预置音色（非声音克隆、非本项目自注册），"
    "通过官方 `POST /v1/t2a_v2` 接口随账户按量计费使用。"
)
LICENSE_NOTE = (
    "结论：本项目**未取得书面法律意见**，故不判定为可商用，scope 保持 unverified。"
    "升级路径（任一）：① 归档官方《服务协议》/《使用条款》原文快照并确认商业使用范围；"
    "② 取得 MiniMax 书面商用授权。未升级前，这些音色的生产就绪判定会持续阻塞。"
)

EXCLUDED_NOTE = (
    "未纳入目录：童声/卡通/机器人角色音（口播场景不适用）、"
    "`*-jingpin` 等与主条目同源的 beta 重复项、以及粤语/外语条目（作用域语言不同，需单独验收）。"
)

# (remote_voice_id, 中文名, 口播风格描述, 分组)
CATALOG: Tuple[Tuple[str, str, str, str], ...] = (
    # ---------- 普通话预置（Chinese (Mandarin)_*，26 条） ----------
    ("Chinese (Mandarin)_Warm_Bestie", "温暖闺蜜",
     "一位温暖清脆的青年女性闺蜜声音，标准普通话，友好而清晰。", GROUP_MANDARIN),
    ("Chinese (Mandarin)_Sweet_Lady", "甜美女声",
     "一位温柔甜美的青年女性声音，标准普通话。", GROUP_MANDARIN),
    ("Chinese (Mandarin)_Crisp_Girl", "清脆少女",
     "一位温暖清脆的少女声音，标准普通话。", GROUP_MANDARIN),
    ("Chinese (Mandarin)_Soft_Girl", "软软女孩",
     "一位温暖柔软的青年女性声音，带有南方口音的中文。", GROUP_MANDARIN),
    ("Chinese (Mandarin)_Warm_Girl", "温暖少女",
     "一位温柔温暖的少年女声，标准普通话。", GROUP_MANDARIN),
    ("Chinese (Mandarin)_Gentle_Senior", "温柔学姐",
     "一位温暖温柔的青年学姐声音，标准普通话。", GROUP_MANDARIN),
    ("Chinese (Mandarin)_Wise_Women", "阅历姐姐",
     "一位富有阅历、声音抒情的中年姐姐声音，标准普通话。", GROUP_MANDARIN),
    ("Chinese (Mandarin)_Mature_Woman", "傲娇御姐",
     "一位妩媚成熟的青年御姐声音，标准普通话。", GROUP_MANDARIN),
    ("Chinese (Mandarin)_News_Anchor", "新闻女声",
     "一位专业、播音腔的中年女性新闻主播，标准普通话。", GROUP_MANDARIN),
    ("Chinese (Mandarin)_Kind-hearted_Antie", "热心大婶",
     "一位温和善良的中年大婶声音，标准普通话，温暖而体贴。", GROUP_MANDARIN),
    ("Chinese (Mandarin)_Kind-hearted_Elder", "花甲奶奶",
     "一位慈祥和蔼的老年女性奶奶声音，标准普通话。", GROUP_MANDARIN),
    ("Chinese (Mandarin)_HK_Flight_Attendant", "港普空姐",
     "一位礼貌的中年女性空乘员声音，带有港式普通话口音，清晰而有礼。", GROUP_MANDARIN),
    ("Chinese (Mandarin)_Gentleman", "温润男声",
     "一位温润磁性的青年男性声音，标准普通话。", GROUP_MANDARIN),
    ("Chinese (Mandarin)_Gentle_Youth", "温润青年",
     "一位温柔的青年男性声音，标准普通话。", GROUP_MANDARIN),
    ("Chinese (Mandarin)_Lyrical_Voice", "抒情男声",
     "一位磁性抒情的青年男性声音，标准普通话，流畅而富有表现力。", GROUP_MANDARIN),
    ("Chinese (Mandarin)_Sincere_Adult", "真诚青年",
     "一位真诚、富有鼓励性的青年男性声音，标准普通话。", GROUP_MANDARIN),
    ("Chinese (Mandarin)_Radio_Host", "电台男主播",
     "一位富有诗意的青年男性电台主播声音，标准普通话，声音流畅引人入胜。", GROUP_MANDARIN),
    ("Chinese (Mandarin)_Male_Announcer", "播报男声",
     "一位富有磁性的中年男性播报员声音，标准普通话，清晰而权威。", GROUP_MANDARIN),
    ("Chinese (Mandarin)_Reliable_Executive", "沉稳高管",
     "一位沉稳可靠的中年男性高管声音，标准普通话，传递出值得信赖的感觉。", GROUP_MANDARIN),
    ("Chinese (Mandarin)_Unrestrained_Young_Man", "不羁青年",
     "一位潇洒不羁的青年男性声音，标准普通话。", GROUP_MANDARIN),
    ("Chinese (Mandarin)_Southern_Young_Man", "南方小哥",
     "一位质朴的青年男性声音，带有南方口音的中文。", GROUP_MANDARIN),
    ("Chinese (Mandarin)_Straightforward_Boy", "率真弟弟",
     "一位认真率真的少年弟弟声音，标准普通话。", GROUP_MANDARIN),
    ("Chinese (Mandarin)_Pure-hearted_Boy", "清澈邻家弟弟",
     "一位认真清澈的邻家少年弟弟声音，标准普通话。", GROUP_MANDARIN),
    ("Chinese (Mandarin)_Stubborn_Friend", "嘴硬竹马",
     "一位嘴硬心软、不羁的青年竹马声音，标准普通话。", GROUP_MANDARIN),
    ("Chinese (Mandarin)_Humorous_Elder", "搞笑大爷",
     "一位爽朗幽默的老年男性大爷声音，带有北方口音的中文，充满个性。", GROUP_MANDARIN),
    ("Chinese (Mandarin)_Cute_Spirit", "憨憨萌兽",
     "一位呆萌可爱的少年男声，适合憨厚的萌兽角色。", GROUP_MANDARIN),
    # ---------- 经典中文（legacy 成年音色，19 条） ----------
    ("danya_xuejie", "淡雅学姐",
     "经典中文音色；本项目 AV-001 首轮实测过同稿试听（音色 2）。", GROUP_LEGACY),
    ("female-yujie", "御姐音色", "经典中文女声，成熟御姐气质。", GROUP_LEGACY),
    ("female-chengshu", "成熟女性音色", "经典中文女声，成熟稳重。", GROUP_LEGACY),
    ("female-tianmei", "甜美女性音色", "经典中文女声，甜美亲和。", GROUP_LEGACY),
    ("female-shaonv", "少女音色", "经典中文女声，清亮少女感。", GROUP_LEGACY),
    ("wumei_yujie", "妩媚御姐", "经典中文女声，妩媚成熟。", GROUP_LEGACY),
    ("qiaopi_mengmei", "俏皮萌妹", "经典中文女声，俏皮活泼。", GROUP_LEGACY),
    ("tianxin_xiaoling", "甜心小玲", "经典中文女声，甜美俏皮。", GROUP_LEGACY),
    ("diadia_xuemei", "嗲嗲学妹", "经典中文女声，软糯亲和。", GROUP_LEGACY),
    ("Arrogant_Miss", "嚣张小姐",
     "一位嚣张自信的青年女性声音，标准普通话，展现出优越感。", GROUP_LEGACY),
    ("male-qn-jingying", "精英青年音色", "经典中文男声，干练精英感。", GROUP_LEGACY),
    ("male-qn-qingse", "青涩青年音色", "经典中文男声，青涩年轻。", GROUP_LEGACY),
    ("male-qn-daxuesheng", "青年大学生音色", "经典中文男声，大学生气质。", GROUP_LEGACY),
    ("male-qn-badao", "霸道青年音色", "经典中文男声，低沉有力度。", GROUP_LEGACY),
    ("junlang_nanyou", "俊朗男友", "经典中文男声，温柔俊朗。", GROUP_LEGACY),
    ("chunzhen_xuedi", "纯真学弟", "经典中文男声，干净纯真。", GROUP_LEGACY),
    ("lengdan_xiongzhang", "冷淡学长", "经典中文男声，冷静克制。", GROUP_LEGACY),
    ("badao_shaoye", "霸道少爷", "经典中文男声，强势有气场。", GROUP_LEGACY),
    ("bingjiao_didi", "病娇弟弟", "经典中文男声，黏人少年感。", GROUP_LEGACY),
)

_MANDARIN_PREFIX = "Chinese (Mandarin)_"


def voice_id_for(remote_voice_id: str) -> str:
    """由官方 voice_id 推出本项目的稳定资产 ID。

    必须是**确定性**的：同一个官方 ID 重复引导要落到同一行，
    否则每次重启都会多出一份重复候选。
    `Chinese (Mandarin)_Warm_Bestie` → `voice_minimax_warm_bestie_v1`
    （与 storage 里既有的预设音色 3 同一个 ID，引导时自然跳过、不覆盖）。
    """
    core = (remote_voice_id or "").strip()
    if core.startswith(_MANDARIN_PREFIX):
        core = core[len(_MANDARIN_PREFIX):]
    slug = re.sub(r"[^0-9a-zA-Z]+", "_", core).strip("_").lower()
    return f"voice_minimax_{slug}_v1"


def _ensure_catalog_license() -> Dict[str, Any]:
    """目录共用一条权属记录：同一份官方条款覆盖全部官方预置音色。"""
    rec = next((r for r in licensing.list_records()
                if r.get("subject") == LICENSE_SUBJECT), None)
    if rec:
        return rec
    return licensing.register(
        subject=LICENSE_SUBJECT, kind="voice_source", scope="unverified",
        evidence=LICENSE_EVIDENCE, verified_by="user（依据官方条款核验）",
        note=LICENSE_NOTE,
    )


def _ensure_source(voice_id: str, remote_voice_id: str) -> Dict[str, Any]:
    """每条音色一条来源记录：官方目录读取，非克隆。"""
    rec = next((r for r in src.list_sources(voice_id)
                if r.get("source_type") == "preset_voice_selection"), None)
    if rec:
        return rec
    return src.register_source(
        subject=voice_id, source_type="preset_voice_selection",
        provider="minimax", model=vc.MINIMAX_TTS_MODEL,
        version="官方预置音色（非克隆）",
        note=f"{CATALOG_SOURCE_NOTE} remote_voice_id={remote_voice_id}",
    )


def _backfill_descriptive(voice: Dict[str, Any], tone: str, group: str) -> bool:
    """只回填**空着**的描述性字段，绝不碰身份字段。

    "同一 ID 不得被改写"约束的是作用域/参数/审批/基准证据这些决定"这是哪条声音"
    的字段；目录分组与风格描述是纯检索元数据，缺了只会让这条音色掉进"其他"分组。
    因此这里逐字段判断"当前为空才写"，且不经过 `register_voice`
    （那条路径会走版本与审批规则，对一次元数据补全来说过重）。
    """
    changed = False
    if not voice.get("description"):
        voice["description"] = tone
        changed = True
    if not voice.get("catalog_group"):
        voice["catalog_group"] = group
        changed = True
    if changed:
        st.put("voices", voice["id"], voice)
    return changed


def bootstrap_catalog(*, force: bool = False) -> Dict[str, Any]:
    """把中文口播可用的官方预置音色登记为候选。幂等，不覆盖既有行。

    已存在的行只做描述字段回填（见 `_backfill_descriptive`），
    作用域、合成参数、审批状态与基准证据一律不动——换声必须新版本。
    """
    added: List[str] = []
    skipped: List[str] = []
    backfilled: List[str] = []
    failed: List[Dict[str, str]] = []

    license_rec = _ensure_catalog_license()

    for remote, cn_name, tone, group in CATALOG:
        vid = voice_id_for(remote)
        existing = vc.get_voice(vid)
        if existing:
            # 既有行（含预设音色 3）不重建；只把空着的描述字段补齐
            if _backfill_descriptive(existing, tone, group):
                backfilled.append(vid)
            skipped.append(vid)
            continue
        try:
            source = _ensure_source(vid, remote)
            row = vc.register_voice(
                voice_id=vid, name=cn_name,
                provider="minimax", model=vc.MINIMAX_TTS_MODEL,
                remote_voice_id=remote, region=vc.MINIMAX_REGION,
                sample_text=DEFAULT_SAMPLE_TEXT,
                supported_emotions=["calm"],
                synthesis_profile=dict(vc.PRESET_SYNTHESIS_PROFILE),
                source_record_id=source["id"], license_id=license_rec["id"],
                is_preset=True, approval_state="pending",
                description=tone, catalog_group=group,
                note=f"官方预置音色候选（{group}）。可试听；"
                     "选定后由用户显式批准。尚无本项目基准合成证据，"
                     "生产就绪判定会阻塞——需真实试听后补登记。",
            )
            added.append(vid)
        except Exception as e:  # 单条失败不影响整批：把原因写进返回值
            failed.append({"voice_id": vid, "remote_voice_id": remote,
                           "error": f"{type(e).__name__}: {e}"})

    return {
        "ok": not failed,
        "catalog_size": len(CATALOG),
        "added": added,
        "backfilled": backfilled,
        "skipped": skipped,
        "failed": failed,
        "license_id": license_rec.get("id"),
        "groups": {GROUP_MANDARIN: sum(1 for c in CATALOG if c[3] == GROUP_MANDARIN),
                   GROUP_LEGACY: sum(1 for c in CATALOG if c[3] == GROUP_LEGACY)},
        "excluded_note": EXCLUDED_NOTE,
        "bootstrapped_at": now_iso(),
    }


def catalog_status() -> Dict[str, Any]:
    """目录覆盖情况：登记了多少、可试听多少、已批准多少。"""
    rows = vc.list_voices()
    by_remote = {r.get("remote_voice_id"): r for r in rows}
    missing = [c[0] for c in CATALOG if c[0] not in by_remote]
    approved = [r["id"] for r in rows if r.get("approval_state") == "approved"]
    return {
        "catalog_size": len(CATALOG),
        "registered": len(CATALOG) - len(missing),
        "missing": missing,
        "approved_ids": approved,
        "total_voices_in_store": len(rows),
        "excluded_note": EXCLUDED_NOTE,
    }


def find_by_remote(remote_voice_id: str) -> Optional[Dict[str, Any]]:
    vid = voice_id_for(remote_voice_id)
    return vc.get_voice(vid)
