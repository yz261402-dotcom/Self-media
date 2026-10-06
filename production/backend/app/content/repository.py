"""口播稿升级的存储层：不可变记录、指针 CAS、版本单调。

所有写函数接收调用方事务提供的 connection，本模块不开事务、不 commit，也不做网络请求。
不可变表一律 INSERT：同 ID 只允许"完全相同"的重放，不同内容抛 ImmutableRecordConflict，
绝不 INSERT OR REPLACE 覆盖。
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from .. import storage as st
from ..contracts import ScriptBatch, ScriptVersion, now_iso
from . import contracts as cc

# 共享创作输入：只有这些字段的改动才推进 content_revision。
# 稿件选用、音频等下游绑定不在此列，防止调用方借这个入口改写它们。
SHARED_INPUT_FIELDS = ("brief", "reference_snapshot_id", "writing_prompt_id",
                       "writer_profile_id", "style_snapshot")

_PROJECTION_ONLY = {"review_state", "pickable", "unpickable_reasons"}


class RepositoryError(Exception):
    code = "REPOSITORY_ERROR"

    def __init__(self, message: str, code: str = ""):
        super().__init__(message)
        self.message = message
        if code:
            self.code = code


class ImmutableRecordConflict(RepositoryError):
    code = "IMMUTABLE_RECORD_CONFLICT"


class StaleContentWrite(RepositoryError):
    code = "CONTENT_REVISION_STALE"


def _canon(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True)


def _row(c: Any, table: str, key: str) -> Optional[dict]:
    r = c.execute(f"SELECT data FROM {table} WHERE id=?", [key]).fetchone()
    return json.loads(r["data"]) if r else None


def _insert_immutable(c: Any, table: str, record: dict,
                      extra: Optional[Dict[str, Any]] = None) -> dict:
    existing = _row(c, table, record["id"])
    if existing is not None:
        if _canon(existing) == _canon(record):
            return existing
        raise ImmutableRecordConflict(
            f"{table} 记录 {record['id']} 已存在且内容不同：不可变记录不能被覆盖，请使用新 ID")
    names = ["id"] + list((extra or {}).keys()) + ["data"]
    vals = [record["id"]] + list((extra or {}).values()) + [json.dumps(record, ensure_ascii=False)]
    c.execute(f"INSERT INTO {table} ({','.join(names)}) VALUES ({','.join('?' * len(names))})", vals)
    return record


# ---------------------------------------------------------------- 参考快照

def save_reference(connection, snapshot: dict) -> dict:
    ref = cc.ReferenceSnapshot(**snapshot).model_dump()
    ref["content_digest"] = cc.reference_digest(ref)  # 摘要由服务端复算，忽略调用方给的值
    return _insert_immutable(connection, "content_references", ref, {"work_id": ref["work_id"]})


def get_reference(reference_id: str) -> Optional[dict]:
    return st.get("content_references", reference_id)


def list_references(work_id: str) -> List[dict]:
    return _all(st.conn(), "content_references", work_id)


# ---------------------------------------------------------------- 提示

def save_prompt(connection, prompt: dict) -> dict:
    doc = cc.PromptDocument(**prompt).model_dump()
    return _insert_immutable(connection, "content_prompts", doc, {"work_id": doc["work_id"]})


def get_prompt(prompt_id: str) -> Optional[dict]:
    return st.get("content_prompts", prompt_id)


def list_prompts(work_id: str) -> List[dict]:
    return _all(st.conn(), "content_prompts", work_id)


# ---------------------------------------------------------------- 稿件版本

def save_script_version(connection, version: dict) -> dict:
    rec = cc.ScriptVersionRecord(**version).model_dump(exclude=_PROJECTION_ONLY)
    rec["text_digest"] = cc.candidate_digest(rec)
    return _insert_immutable(connection, "script_versions", rec, {"work_id": rec["work_id"]})


def get_script_version(script_id: str) -> Optional[dict]:
    return st.get("script_versions", script_id)


# ---------------------------------------------------------------- 批次

def save_batch(connection, batch: dict) -> dict:
    rec = cc.BatchRecord(**batch).model_dump()
    if not rec["members"]:
        raise RepositoryError("批次至少要有一个候选版本")
    slots = [m["slot"] for m in rec["members"]]
    ids = [m["candidate_id"] for m in rec["members"]]
    if len(set(slots)) != len(slots) or len(set(ids)) != len(ids):
        raise RepositoryError("批次内 slot 或候选版本重复")
    for m in rec["members"]:
        version = _row(connection, "script_versions", m["candidate_id"])
        if version is None:
            raise RepositoryError(f"批次成员 {m['candidate_id']} 不是已保存的稿件版本")
        if version["work_id"] != rec["work_id"]:
            raise RepositoryError(f"批次成员 {m['candidate_id']} 不属于作品 {rec['work_id']}")
        if version.get("content_route", "talking_script") != rec["content_route"]:
            raise RepositoryError("批次成员内容路线不一致")
        m["text_digest"] = version["text_digest"]  # 由已存版本复算，不采信调用方
    rec["set_digest"] = cc.set_digest(rec["members"])
    return _insert_immutable(connection, "content_batches", rec, {"work_id": rec["work_id"]})


def get_batch(batch_id: str) -> Optional[dict]:
    return st.get("content_batches", batch_id)


def list_batches(work_id: str) -> List[dict]:
    return _all(st.conn(), "content_batches", work_id)


def _all(c: Any, table: str, work_id: str) -> List[dict]:
    rows = c.execute(f"SELECT data FROM {table} WHERE work_id=? ORDER BY ins_seq", [work_id]).fetchall()
    return [json.loads(r["data"]) for r in rows]


def batch_candidates(batch: dict) -> List[dict]:
    """批次成员展开成稿件版本（按 slot 排序）。找不到的成员直接报错，不静默丢稿。"""
    out = []
    for m in sorted(batch["members"], key=lambda x: x["slot"]):
        v = get_script_version(m["candidate_id"])
        if v is None:
            raise RepositoryError(f"批次 {batch['id']} 的成员 {m['candidate_id']} 缺失")
        out.append(v)
    return out


# ---------------------------------------------------------------- Work 指针与 CAS

def _work(c: Any, work_id: str) -> dict:
    work = _row(c, "works", work_id)
    if work is None:
        raise RepositoryError(f"作品不存在：{work_id}", "WORK_NOT_FOUND")
    if work.get("deleted_at"):
        raise RepositoryError("作品已删除，不能继续修改", "WORK_DELETED")
    return work


def bump_content_revision(connection, work_id: str, *, expected_revision: Optional[int] = None,
                          **fields: Any) -> dict:
    """事务内修改共享创作输入并令 content_revision += 1（单调，A→B→A 也只增不减）。"""
    bad = [k for k in fields if k not in SHARED_INPUT_FIELDS]
    if bad:
        raise RepositoryError(f"不允许通过共享输入入口修改字段：{'、'.join(bad)}")
    work = _work(connection, work_id)
    current = int(work.get("content_revision") or 0)
    if expected_revision is not None and expected_revision != current:
        raise StaleContentWrite(f"创作输入已变化（当前 {current}，请求基于 {expected_revision}）")
    work.update(fields)
    work["content_revision"] = current + 1
    work["version"] = int(work.get("version") or 1) + 1
    st.insert_row(connection, "works", work_id, work)
    if work.get("script_version_id"):
        # 共享创作输入真的变了：旧选用依据的审核全部失效，下游一并撤销（保留文件与费用）
        from . import selection
        selection.revoke_adoption(connection, work, "创作输入（主题/资料/参考/风格/提示）已变更")
        st.insert_row(connection, "works", work_id, work)
    return work


def bind_current_batch(connection, work_id: str, batch_id: str, expected_revision: int,
                       expected_job_id: Optional[str]) -> dict:
    """把批次投影为当前 script_batches。回写方必须同时匹配 revision 与父任务 ID。

    只比较摘要不够：A→B→A 后摘要相同但 revision 已前进，旧任务的晚到结果必须被拒。
    expected_job_id=None 仅用于用户改稿：没有父任务，只校验 revision。
    """
    work = _work(connection, work_id)
    if int(work.get("content_revision") or 0) != expected_revision \
            or (expected_job_id is not None and work.get("current_content_job_id") != expected_job_id):
        raise StaleContentWrite("该结果基于旧的创作输入或已被取代的任务，不能绑定为当前批次")
    batch = _row(connection, "content_batches", batch_id)
    if batch is None or batch["work_id"] != work_id:
        raise RepositoryError(f"批次不存在或不属于该作品：{batch_id}")
    if batch["content_revision"] != expected_revision:
        raise StaleContentWrite("批次生成时的创作输入版本与当前不一致")
    candidates = []
    for m in sorted(batch["members"], key=lambda x: x["slot"]):
        v = _row(connection, "script_versions", m["candidate_id"])
        if v is None:
            raise RepositoryError(f"批次成员 {m['candidate_id']} 缺失")
        v = {k: val for k, val in v.items() if k != "work_id"}
        v["batch_id"] = batch_id
        candidates.append(ScriptVersion(**v).model_dump())
    view = ScriptBatch(
        id=batch_id, work_id=work_id, candidates=candidates,
        content_route=batch.get("content_route", "talking_script"),
        shortfall_note=batch.get("shortfall_note"), source_kind=batch.get("source_kind"),
        repair_note=batch.get("repair_note"), repair_history=batch.get("repair_history") or [],
        materials_count=batch.get("materials_count"), requested=batch.get("requested"),
        produced=len(candidates), prompt_id=batch.get("prompt_id") or None,
        content_revision=batch["content_revision"], job_id=batch.get("job_id") or None,
        set_digest=batch["set_digest"],
    ).model_dump()
    st.insert_row(connection, "script_batches", work_id, view)
    return view


# ---------------------------------------------------------------- 风格

def save_style_profile(connection, profile: dict, expected_revision: Optional[int] = None) -> dict:
    model = cc.StyleProfile(**profile)
    existing = _row(connection, "content_style_profiles", model.id)
    if existing is None:
        if expected_revision not in (None, 0):
            raise StaleContentWrite("要更新的风格模板不存在")
        rev = 1
    else:
        if expected_revision is None or expected_revision != int(existing.get("revision") or 0):
            raise StaleContentWrite(
                f"风格模板已被修改（当前 revision {existing.get('revision')}），请刷新后再改")
        rev = int(existing["revision"]) + 1
    data = model.model_copy(update={"revision": rev, "updated_at": now_iso()}).model_dump()
    st.insert_row(connection, "content_style_profiles", data["id"], data)
    return data


def get_style_profile(profile_id: str) -> Optional[dict]:
    return st.get("content_style_profiles", profile_id)


def list_style_profiles() -> List[dict]:
    return st.all_rows("content_style_profiles")


def style_snapshot(profile: dict) -> dict:
    """作品使用的风格快照：复制而非引用，共享模板之后的修改不影响已应用的作品。"""
    snap = {k: json.loads(json.dumps(profile.get(k))) for k in cc._STYLE_DIGEST_FIELDS}
    snap.update(profile_id=profile["id"], name=profile.get("name", ""),
                revision=int(profile.get("revision") or 0))
    snap["digest"] = cc.style_digest(snap)
    return snap
