"""PostgreSQL 仓储。以 JSON(TEXT) 列承载聚合体，写入走事务；后续可拆为列式表，不影响接口契约。

业务库是任务、选择、审批与账本的事实来源，Agent 只能经受控工具提交。

连接约定：
- DSN 取自环境变量 AVATAR_STUDIO_DATABASE_URL（生产在 backend/.env 配置）。
- 连接为 autocommit：单条 put() 即一次提交；需要"检查-写入"原子性时用 transaction()。
- transaction() = BEGIN + pg_advisory_xact_lock：复刻原 SQLite `BEGIN IMMEDIATE` 的全局写串行语义，
  租约竞争、预算预留、幂等键、GPU 锁、内容队列领取都依赖它。
- 测试：DB_PATH 非空时，其哈希映射为独立 schema，用例间互不可见，进程退出时清理。
"""
from __future__ import annotations

import atexit
import hashlib
import json
import os
import threading
import uuid
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional

import psycopg
import psycopg.conninfo
from psycopg.rows import dict_row

DB_PATH = os.environ.get("AVATAR_STUDIO_DB", "")
DSN_ENV = "AVATAR_STUDIO_DATABASE_URL"
_WRITE_LOCK_KEY = 0x41564154  # "AVAT"，全局写锁键
_DDL_LOCK_KEY = _WRITE_LOCK_KEY + 1

_local = threading.local()
_test_schemas: set = set()


def dsn() -> str:
    v = os.environ.get(DSN_ENV, "").strip()
    if not v:
        raise RuntimeError(f"未配置 {DSN_ENV}（在 production/backend/.env 中设置 PostgreSQL 连接串）")
    return v


def _schema() -> str:
    if not DB_PATH:
        return "public"
    return "t_" + hashlib.md5(DB_PATH.encode("utf-8")).hexdigest()[:16]


def db_label() -> str:
    """供健康检查展示：不含账号密码。"""
    try:
        info = psycopg.conninfo.conninfo_to_dict(dsn())
        label = f"postgresql://{info.get('host', '')}:{info.get('port', '')}/{info.get('dbname', '')}"
        return label if _schema() == "public" else f"{label}#{_schema()}"
    except Exception:
        return "postgresql://<unconfigured>"


STAGE_LABEL = {
    "topic": "确定选题与来源",
    "script": "三角度写稿与表演说明",
    "content_review": "内容门禁与查重",
    "avatar_variant": "人物与表演准备",
    "tts": "试听选择与全文合成",
    "talking_head": "口播生成",
    "postprocess": "字幕、封面与编码",
    "video_review": "成片终审",
    "publish_package": "冻结发布包",
}

STAGE_ORDER = [
    "topic",
    "script",
    "content_review",
    "avatar_variant",
    "tts",
    "talking_head",
    "postprocess",
    "video_review",
    "publish_package",
]


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _translate(sql: str, params: Optional[list]):
    """SQLite 风格 `?` 占位 → psycopg `%s`。无参数时原样透传（DDL 等）。"""
    if params is None:
        return sql, None
    return sql.replace("%", "%%").replace("?", "%s"), list(params)


class Conn:
    """薄封装：保持 c.execute(sql, [args]).fetchone()["col"] / c.commit() / c.rollback() 的既有调用形态。"""

    def __init__(self, raw: "psycopg.Connection"):
        self.raw = raw

    def execute(self, sql: str, params: Optional[list] = None):
        q, p = _translate(sql, params)
        return self.raw.execute(q, p)

    def executemany(self, sql: str, seq) -> None:
        q, _ = _translate(sql, [])
        with self.raw.cursor() as cur:
            cur.executemany(q, [list(p) for p in seq])

    def close(self) -> None:
        self.raw.close()

    def commit(self) -> None:
        self.raw.commit()

    def rollback(self) -> None:
        self.raw.rollback()

    @property
    def closed(self) -> bool:
        return self.raw.closed or self.raw.broken


def _drop_test_schemas() -> None:
    if not _test_schemas:
        return
    try:
        with psycopg.connect(dsn(), autocommit=True, connect_timeout=5) as c:
            for sch in list(_test_schemas):
                c.execute(f'DROP SCHEMA IF EXISTS "{sch}" CASCADE')
    except Exception:
        pass


atexit.register(_drop_test_schemas)


def conn() -> Conn:
    c: Optional[Conn] = getattr(_local, "conn", None)
    if c is None or c.closed:
        raw = psycopg.connect(dsn(), autocommit=True, row_factory=dict_row, connect_timeout=5)
        schema = _schema()
        if schema != "public":
            raw.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
            raw.execute(f'SET search_path TO "{schema}"')
            _test_schemas.add(schema)
        c = Conn(raw)
        _local.conn = c
    return c


_DDL = """
CREATE SEQUENCE IF NOT EXISTS studio_ins_seq;
CREATE TABLE IF NOT EXISTS works (id TEXT PRIMARY KEY, data TEXT NOT NULL, ins_seq BIGINT NOT NULL DEFAULT nextval('studio_ins_seq'), version INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS identities (id TEXT PRIMARY KEY, data TEXT NOT NULL, ins_seq BIGINT NOT NULL DEFAULT nextval('studio_ins_seq'));
CREATE TABLE IF NOT EXISTS variants (id TEXT PRIMARY KEY, work_id TEXT, data TEXT NOT NULL, ins_seq BIGINT NOT NULL DEFAULT nextval('studio_ins_seq'));
CREATE TABLE IF NOT EXISTS voices (id TEXT PRIMARY KEY, data TEXT NOT NULL, ins_seq BIGINT NOT NULL DEFAULT nextval('studio_ins_seq'));
CREATE TABLE IF NOT EXISTS script_batches (work_id TEXT PRIMARY KEY, data TEXT NOT NULL, ins_seq BIGINT NOT NULL DEFAULT nextval('studio_ins_seq'));
CREATE TABLE IF NOT EXISTS audios (id TEXT PRIMARY KEY, work_id TEXT, data TEXT NOT NULL, ins_seq BIGINT NOT NULL DEFAULT nextval('studio_ins_seq'));
CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, work_id TEXT, data TEXT NOT NULL, ins_seq BIGINT NOT NULL DEFAULT nextval('studio_ins_seq'));
CREATE TABLE IF NOT EXISTS task_events (task_id TEXT, seq INTEGER, data TEXT NOT NULL, PRIMARY KEY (task_id, seq));
CREATE TABLE IF NOT EXISTS packages (id TEXT PRIMARY KEY, work_id TEXT, data TEXT NOT NULL, ins_seq BIGINT NOT NULL DEFAULT nextval('studio_ins_seq'));
CREATE TABLE IF NOT EXISTS publish_tasks (id TEXT PRIMARY KEY, work_id TEXT, data TEXT NOT NULL, ins_seq BIGINT NOT NULL DEFAULT nextval('studio_ins_seq'));
CREATE TABLE IF NOT EXISTS confirmations (id TEXT PRIMARY KEY, work_id TEXT, data TEXT NOT NULL, ins_seq BIGINT NOT NULL DEFAULT nextval('studio_ins_seq'));
CREATE TABLE IF NOT EXISTS selections (id TEXT PRIMARY KEY, work_id TEXT, data TEXT NOT NULL, ins_seq BIGINT NOT NULL DEFAULT nextval('studio_ins_seq'));
CREATE TABLE IF NOT EXISTS agent_work_items (id TEXT PRIMARY KEY, task_id TEXT, data TEXT NOT NULL, ins_seq BIGINT NOT NULL DEFAULT nextval('studio_ins_seq'));
CREATE TABLE IF NOT EXISTS agent_results (id TEXT PRIMARY KEY, work_item_id TEXT, data TEXT NOT NULL, ins_seq BIGINT NOT NULL DEFAULT nextval('studio_ins_seq'));
CREATE TABLE IF NOT EXISTS agent_tool_audit (id TEXT PRIMARY KEY, work_item_id TEXT, data TEXT NOT NULL, ins_seq BIGINT NOT NULL DEFAULT nextval('studio_ins_seq'));
CREATE TABLE IF NOT EXISTS performance_plans (id TEXT PRIMARY KEY, work_id TEXT, data TEXT NOT NULL, ins_seq BIGINT NOT NULL DEFAULT nextval('studio_ins_seq'));
CREATE TABLE IF NOT EXISTS quality_decisions (id TEXT PRIMARY KEY, work_id TEXT, data TEXT NOT NULL, ins_seq BIGINT NOT NULL DEFAULT nextval('studio_ins_seq'));
CREATE TABLE IF NOT EXISTS memory_candidates (id TEXT PRIMARY KEY, work_id TEXT, data TEXT NOT NULL, ins_seq BIGINT NOT NULL DEFAULT nextval('studio_ins_seq'));
CREATE TABLE IF NOT EXISTS media_jobs (id TEXT PRIMARY KEY, work_id TEXT, data TEXT NOT NULL, ins_seq BIGINT NOT NULL DEFAULT nextval('studio_ins_seq'));
CREATE TABLE IF NOT EXISTS provider_jobs (id TEXT PRIMARY KEY, work_id TEXT, data TEXT NOT NULL, ins_seq BIGINT NOT NULL DEFAULT nextval('studio_ins_seq'));
CREATE TABLE IF NOT EXISTS provider_jobs_idem (idem_key TEXT PRIMARY KEY, job_id TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS resource_locks (id TEXT PRIMARY KEY, data TEXT NOT NULL, ins_seq BIGINT NOT NULL DEFAULT nextval('studio_ins_seq'));
CREATE TABLE IF NOT EXISTS memory_entries (id TEXT PRIMARY KEY, data TEXT NOT NULL, ins_seq BIGINT NOT NULL DEFAULT nextval('studio_ins_seq'));
CREATE TABLE IF NOT EXISTS mask_specs (id TEXT PRIMARY KEY, identity_id TEXT, data TEXT NOT NULL, ins_seq BIGINT NOT NULL DEFAULT nextval('studio_ins_seq'));
CREATE TABLE IF NOT EXISTS lineage (id TEXT PRIMARY KEY, work_id TEXT, data TEXT NOT NULL, ins_seq BIGINT NOT NULL DEFAULT nextval('studio_ins_seq'));
CREATE TABLE IF NOT EXISTS avatar_assets (id TEXT PRIMARY KEY, identity_id TEXT, data TEXT NOT NULL, ins_seq BIGINT NOT NULL DEFAULT nextval('studio_ins_seq'));
CREATE TABLE IF NOT EXISTS identity_reviews (id TEXT PRIMARY KEY, asset_id TEXT, data TEXT NOT NULL, ins_seq BIGINT NOT NULL DEFAULT nextval('studio_ins_seq'));
CREATE TABLE IF NOT EXISTS motion_references (id TEXT PRIMARY KEY, identity_id TEXT, data TEXT NOT NULL, ins_seq BIGINT NOT NULL DEFAULT nextval('studio_ins_seq'));
CREATE TABLE IF NOT EXISTS motion_uploads (id TEXT PRIMARY KEY, identity_id TEXT, data TEXT NOT NULL, ins_seq BIGINT NOT NULL DEFAULT nextval('studio_ins_seq'));
CREATE TABLE IF NOT EXISTS avatar_reference_images (id TEXT PRIMARY KEY, data TEXT NOT NULL, ins_seq BIGINT NOT NULL DEFAULT nextval('studio_ins_seq'));
CREATE TABLE IF NOT EXISTS avatar_image_generations (id TEXT PRIMARY KEY, data TEXT NOT NULL, ins_seq BIGINT NOT NULL DEFAULT nextval('studio_ins_seq'));
CREATE TABLE IF NOT EXISTS license_records (id TEXT PRIMARY KEY, data TEXT NOT NULL, ins_seq BIGINT NOT NULL DEFAULT nextval('studio_ins_seq'));
CREATE TABLE IF NOT EXISTS provider_evidence (id TEXT PRIMARY KEY, data TEXT NOT NULL, ins_seq BIGINT NOT NULL DEFAULT nextval('studio_ins_seq'));
CREATE TABLE IF NOT EXISTS voice_previews (id TEXT PRIMARY KEY, work_id TEXT, data TEXT NOT NULL, ins_seq BIGINT NOT NULL DEFAULT nextval('studio_ins_seq'));
CREATE TABLE IF NOT EXISTS audition_sentences (id TEXT PRIMARY KEY, work_id TEXT, data TEXT NOT NULL, ins_seq BIGINT NOT NULL DEFAULT nextval('studio_ins_seq'));
CREATE TABLE IF NOT EXISTS alignment_records (id TEXT PRIMARY KEY, audio_id TEXT, data TEXT NOT NULL, ins_seq BIGINT NOT NULL DEFAULT nextval('studio_ins_seq'));
CREATE TABLE IF NOT EXISTS source_records (id TEXT PRIMARY KEY, subject TEXT, data TEXT NOT NULL, ins_seq BIGINT NOT NULL DEFAULT nextval('studio_ins_seq'));
CREATE TABLE IF NOT EXISTS audio_reviews (id TEXT PRIMARY KEY, audio_id TEXT, data TEXT NOT NULL, ins_seq BIGINT NOT NULL DEFAULT nextval('studio_ins_seq'));
CREATE TABLE IF NOT EXISTS voice_samples (id TEXT PRIMARY KEY, voice_id TEXT, data TEXT NOT NULL, ins_seq BIGINT NOT NULL DEFAULT nextval('studio_ins_seq'));
CREATE TABLE IF NOT EXISTS voice_clone_jobs (id TEXT PRIMARY KEY, data TEXT NOT NULL, ins_seq BIGINT NOT NULL DEFAULT nextval('studio_ins_seq'));
CREATE TABLE IF NOT EXISTS videos (id TEXT PRIMARY KEY, work_id TEXT, data TEXT NOT NULL, ins_seq BIGINT NOT NULL DEFAULT nextval('studio_ins_seq'));
CREATE TABLE IF NOT EXISTS provider_runs (id TEXT PRIMARY KEY, work_id TEXT, data TEXT NOT NULL, ins_seq BIGINT NOT NULL DEFAULT nextval('studio_ins_seq'));
CREATE TABLE IF NOT EXISTS plate_reviews (id TEXT PRIMARY KEY, video_id TEXT, data TEXT NOT NULL, ins_seq BIGINT NOT NULL DEFAULT nextval('studio_ins_seq'));
CREATE TABLE IF NOT EXISTS video_reviews (id TEXT PRIMARY KEY, video_id TEXT, data TEXT NOT NULL, ins_seq BIGINT NOT NULL DEFAULT nextval('studio_ins_seq'));
CREATE TABLE IF NOT EXISTS audio_track_checks (id TEXT PRIMARY KEY, video_id TEXT, data TEXT NOT NULL, ins_seq BIGINT NOT NULL DEFAULT nextval('studio_ins_seq'));
CREATE TABLE IF NOT EXISTS price_snapshots (id TEXT PRIMARY KEY, route TEXT, data TEXT NOT NULL, ins_seq BIGINT NOT NULL DEFAULT nextval('studio_ins_seq'));
CREATE TABLE IF NOT EXISTS cost_confirmations (id TEXT PRIMARY KEY, work_id TEXT, data TEXT NOT NULL, ins_seq BIGINT NOT NULL DEFAULT nextval('studio_ins_seq'));
CREATE TABLE IF NOT EXISTS segment_plans (id TEXT PRIMARY KEY, work_id TEXT, data TEXT NOT NULL, ins_seq BIGINT NOT NULL DEFAULT nextval('studio_ins_seq'));
CREATE TABLE IF NOT EXISTS content_references (id TEXT PRIMARY KEY, work_id TEXT NOT NULL, data TEXT NOT NULL, ins_seq BIGINT NOT NULL DEFAULT nextval('studio_ins_seq'));
CREATE TABLE IF NOT EXISTS content_prompts (id TEXT PRIMARY KEY, work_id TEXT NOT NULL, data TEXT NOT NULL, ins_seq BIGINT NOT NULL DEFAULT nextval('studio_ins_seq'));
CREATE TABLE IF NOT EXISTS script_versions (id TEXT PRIMARY KEY, work_id TEXT NOT NULL, data TEXT NOT NULL, ins_seq BIGINT NOT NULL DEFAULT nextval('studio_ins_seq'));
CREATE TABLE IF NOT EXISTS content_batches (id TEXT PRIMARY KEY, work_id TEXT NOT NULL, data TEXT NOT NULL, ins_seq BIGINT NOT NULL DEFAULT nextval('studio_ins_seq'));
CREATE TABLE IF NOT EXISTS content_style_profiles (id TEXT PRIMARY KEY, data TEXT NOT NULL, ins_seq BIGINT NOT NULL DEFAULT nextval('studio_ins_seq'));
CREATE TABLE IF NOT EXISTS content_media_assets (id TEXT PRIMARY KEY, work_id TEXT NOT NULL, data TEXT NOT NULL, ins_seq BIGINT NOT NULL DEFAULT nextval('studio_ins_seq'));
CREATE INDEX IF NOT EXISTS idx_cmedia_work ON content_media_assets(work_id);
CREATE TABLE IF NOT EXISTS content_jobs (
id TEXT PRIMARY KEY, work_id TEXT NOT NULL, kind TEXT NOT NULL, request_id TEXT NOT NULL,
input_digest TEXT NOT NULL, state TEXT NOT NULL, data TEXT NOT NULL, ins_seq BIGINT NOT NULL DEFAULT nextval('studio_ins_seq'),
UNIQUE (work_id, kind, request_id));
CREATE INDEX IF NOT EXISTS idx_cref_work ON content_references(work_id);
CREATE INDEX IF NOT EXISTS idx_cprompt_work ON content_prompts(work_id);
CREATE INDEX IF NOT EXISTS idx_sver_work ON script_versions(work_id);
CREATE INDEX IF NOT EXISTS idx_cbatch_work ON content_batches(work_id);
CREATE INDEX IF NOT EXISTS idx_cjob_work ON content_jobs(work_id, state);
CREATE INDEX IF NOT EXISTS idx_video_work ON videos(work_id);
CREATE INDEX IF NOT EXISTS idx_prun_work ON provider_runs(work_id);
CREATE INDEX IF NOT EXISTS idx_plate_review ON plate_reviews(video_id);
CREATE INDEX IF NOT EXISTS idx_video_review ON video_reviews(video_id);
CREATE INDEX IF NOT EXISTS idx_segplan_work ON segment_plans(work_id);
CREATE INDEX IF NOT EXISTS idx_vpreview_work ON voice_previews(work_id);
CREATE INDEX IF NOT EXISTS idx_align_audio ON alignment_records(audio_id);
CREATE INDEX IF NOT EXISTS idx_srcrec_subject ON source_records(subject);
CREATE INDEX IF NOT EXISTS idx_audio_review ON audio_reviews(audio_id);
CREATE INDEX IF NOT EXISTS idx_vsample_voice ON voice_samples(voice_id);
CREATE INDEX IF NOT EXISTS idx_awi_task ON agent_work_items(task_id);
CREATE INDEX IF NOT EXISTS idx_asset_identity ON avatar_assets(identity_id);
CREATE INDEX IF NOT EXISTS idx_review_asset ON identity_reviews(asset_id);
CREATE INDEX IF NOT EXISTS idx_motion_identity ON motion_references(identity_id);
CREATE INDEX IF NOT EXISTS idx_audit_item ON agent_tool_audit(work_item_id);
CREATE INDEX IF NOT EXISTS idx_pjob_work ON provider_jobs(work_id);
"""


def create_schema() -> None:
    """建表/索引（幂等，不写种子数据）。数据迁移脚本在灌入历史数据前调用它。"""
    c = conn()
    c.execute("BEGIN")
    try:
        c.execute("SELECT pg_advisory_xact_lock(%s)" % _DDL_LOCK_KEY)
        c.execute(_DDL)
        c.execute("COMMIT")
    except Exception:
        c.execute("ROLLBACK")
        raise


def init_db() -> None:
    create_schema()
    _seed()


def _seed() -> None:
    """初始预设来自本项目已定版资产（人物 B / 音色 3），登记不继承生产质量通过结论。"""
    c = conn()
    n = c.execute("SELECT COUNT(*) AS n FROM identities").fetchone()["n"]
    if n == 0:
        put(
            "identities",
            "av_base_b_v1",
            {
                "id": "av_base_b_v1",
                "name": "人物 B",
                "persona": "约 22 岁成年女性，专业、亲和、自然；正面半身构图",
                "hero_asset_id": "img_hero_b_v1",
                "identity_digest": "sha256:preset-identity-b-v1",
                "default_voice_asset_id": "voice_minimax_warm_bestie_v1",
                "approval_state": "approved",
                "is_preset": True,
                "note": "本项目已选初始预设；原图 941×1672，参考集与变体仍待补齐验收",
            },
        )
    n = c.execute("SELECT COUNT(*) AS n FROM voices").fetchone()["n"]
    if n == 0:
        sample = "大家好，今天我们用三分钟讲清楚一个容易被忽略的细节。"
        put(
            "voices",
            "voice_minimax_warm_bestie_v1",
            {
                "id": "voice_minimax_warm_bestie_v1",
                "name": "音色 3 · Warm Bestie",
                "provider": "minimax",
                "model": "speech-2.8-hd",
                "region": "cn",
                "remote_voice_id": "Chinese (Mandarin)_Warm_Bestie",
                "approval_state": "approved",
                "is_preset": True,
                "sample_text": sample,
                "supported_emotions": ["calm"],
                "note": "已选初始预设；其他情绪、多稿件与长稿仍待验收。"
                        "作用域/来源/权属/基准合成证据由 tts.preset.bootstrap_preset_voice 补登记",
            },
        )
        put(
            "voices",
            "voice_minimax_calm_v1",
            {
                "id": "voice_minimax_calm_v1",
                "name": "音色 2 · Calm（候选）",
                "provider": "minimax",
                "model": "speech-2.8-hd",
                "region": "cn",
                "remote_voice_id": "Chinese (Mandarin)_Calm_Woman",
                "approval_state": "pending",
                "is_preset": False,
                "sample_text": sample,
                "supported_emotions": ["calm"],
                "note": "候选：remote_voice_id 未经本项目实测，且缺来源/权属/基准合成证据，"
                        "不得直接用于生产；经明确选择后须创建独立绑定并重新验收",
            },
        )

    # 预设音色 3 的受控登记（作用域/来源/权属/基准合成证据）。幂等；
    # 缺少素材时只登记能登记的部分并把缺口写进 note，不静默跳过。
    try:
        from .tts import preset as tts_preset

        tts_preset.bootstrap_preset_voice()
    except Exception as e:  # 初始化不得因缺素材而整体失败
        print(f"[seed] 预设音色基准证据登记未完成：{type(e).__name__}: {e}")


@contextmanager
def transaction():
    """写事务：租约竞争与预算预留必须在同一事务内检查-写入，
    否则两个 Worker 会同时读到"空闲"并各自占槽。全局 advisory 锁保证同一时刻只有一个写事务。
    """
    c = conn()
    c.execute("BEGIN")
    try:
        c.execute("SELECT pg_advisory_xact_lock(%s)" % _WRITE_LOCK_KEY)
        yield c
        c.execute("COMMIT")
    except Exception:
        c.execute("ROLLBACK")
        raise


def put(table: str, key: str, data: dict, extra: Optional[Dict[str, Any]] = None) -> None:
    c = conn()
    try:
        insert_row(c, table, key, data, extra)
    except Exception:
        c.rollback()
        raise
    c.commit()


PK = {
    "works": "id", "identities": "id", "variants": "id", "voices": "id",
    "audios": "id", "tasks": "id", "packages": "id", "publish_tasks": "id", "confirmations": "id",
    "selections": "id", "script_batches": "work_id",
    "agent_work_items": "id", "agent_results": "id", "agent_tool_audit": "id",
    "performance_plans": "id", "quality_decisions": "id",
    "memory_candidates": "id", "media_jobs": "id",
    "provider_jobs": "id", "memory_entries": "id", "lineage": "id",
    "resource_locks": "id", "avatar_assets": "id", "identity_reviews": "id",
    "motion_references": "id", "license_records": "id",
    "motion_uploads": "id",
    "avatar_reference_images": "id", "avatar_image_generations": "id",
    "mask_specs": "id",
    "provider_evidence": "id",
    "voice_previews": "id", "audition_sentences": "id",
    "alignment_records": "id", "source_records": "id",
    "audio_reviews": "id", "voice_samples": "id", "voice_clone_jobs": "id",
    "videos": "id", "provider_runs": "id", "plate_reviews": "id",
    "video_reviews": "id", "audio_track_checks": "id",
    "price_snapshots": "id",
    "cost_confirmations": "id", "segment_plans": "id",
    "content_references": "id", "content_prompts": "id", "script_versions": "id",
    "content_batches": "id", "content_style_profiles": "id", "content_media_assets": "id",
    "content_jobs": "id",
}


class DeletedObjectWrite(Exception):
    """A stale writer may not clear an object's persisted deletion marker."""
    def __init__(self, table: str):
        self.code = "WORK_DELETED" if table == "works" else "IDENTITY_DELETED"
        self.message = ("作品" if table == "works" else "人物") + "已删除，旧请求不能继续修改"
        self.status = 409
        super().__init__(self.message)


def insert_row(c: Conn, table: str, key: str, data: dict,
               extra: Optional[Dict[str, Any]] = None) -> None:
    """upsert 但不提交：供 transaction() 内使用。更新时刷新 ins_seq，
    与 SQLite `INSERT OR REPLACE`（删后重插、rowid 变新）的顺序语义一致。"""
    pk = PK[table]
    payload = json.dumps(data, ensure_ascii=False)
    extra = {k: v for k, v in (extra or {}).items() if k != pk}
    names = [pk] + list(extra.keys())
    vals = [key] + list(extra.values()) + [payload]
    marks = ",".join("?" * len(vals))
    sets = ",".join([f"{n}=EXCLUDED.{n}" for n in names[1:]] + ["data=EXCLUDED.data", "ins_seq=nextval('studio_ins_seq')"])
    guard = (f" WHERE COALESCE({table}.data::jsonb->>'deleted_at','')='' "
             f"OR {table}.data::jsonb->>'deleted_at'=EXCLUDED.data::jsonb->>'deleted_at'") if table in ("works", "identities") else ""
    cursor = c.execute(f"INSERT INTO {table} ({','.join(names)},data) VALUES ({marks}) "
                       f"ON CONFLICT ({pk}) DO UPDATE SET {sets}{guard}", vals)
    if guard and cursor.rowcount == 0:
        raise DeletedObjectWrite(table)


def get(table: str, key: str) -> Optional[dict]:
    c = conn()
    row = c.execute(f"SELECT data FROM {table} WHERE {PK[table]}=?", [key]).fetchone()
    return json.loads(row["data"]) if row else None


def all_rows(table: str, where: Optional[str] = None, args: Optional[list] = None) -> List[dict]:
    c = conn()
    sql = f"SELECT data FROM {table}"
    if where:
        sql += f" WHERE {where}"
    sql += " ORDER BY task_id, seq" if table == "task_events" else " ORDER BY ins_seq"
    rows = c.execute(sql, args or []).fetchall()
    return [json.loads(r["data"]) for r in rows]


_EVENT_SQL = ("INSERT INTO task_events (task_id,seq,data) VALUES (?,?,?) "
              "ON CONFLICT (task_id,seq) DO UPDATE SET data=EXCLUDED.data")


def add_event_tx(c: Conn, task_id: str, event: dict) -> dict:
    """事务内插入事件：供 transaction() 内使用。"""
    seq = c.execute("SELECT COALESCE(MAX(seq),0)+1 AS s FROM task_events WHERE task_id=?", [task_id]).fetchone()["s"]
    event["seq"] = seq
    c.execute(_EVENT_SQL, [task_id, seq, json.dumps(event, ensure_ascii=False)])
    return event


def add_event(task_id: str, event: dict) -> dict:
    with transaction() as c:  # 全局写锁保证 MAX(seq)+1 不并发撞号
        return add_event_tx(c, task_id, event)


def events_since(task_id: str, after_seq: int = 0) -> List[dict]:
    c = conn()
    rows = c.execute(
        "SELECT data FROM task_events WHERE task_id=? AND seq>? ORDER BY seq", [task_id, after_seq]
    ).fetchall()
    return [json.loads(r["data"]) for r in rows]


def stage_rows(stage: str, state: str) -> List[dict]:
    idx = STAGE_ORDER.index(stage) if stage in STAGE_ORDER else 0
    out = []
    for i, s in enumerate(STAGE_ORDER):
        st = "pending"
        if state == "failed" and i == idx:
            st = "failed"
        elif state == "blocked" and i == idx:
            st = "blocked"
        elif i < idx:
            st = "done"
        elif i == idx:
            st = "done" if state == "succeeded" else "active"
        out.append({"stage": s, "label": STAGE_LABEL[s], "state": st, "detail": ""})
    return out
