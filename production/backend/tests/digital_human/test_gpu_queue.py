"""重型作业队列：Agent 可并行准备，GPU 重推理串行（AC-AG-04 / AC-TH-06）。

这一组用例守的是"看起来并行、实际串行"这类最容易含糊过去的地方：

- 排队**不是**失败。GPU 被占时作业留在 queued，不能写成 failed——
  否则用户看到的是假失败率，还会以为任务需要重提。
- 未核实结束（leased / unknown）的作业**不释放**资源，
  对账完成才让给冲突作业。
- 云路线（OmniHuman）不占本地 GPU：它标为非重型，因此不与本地重推理互斥。

用例只断言自己造的作业。队列在实现上是全局的（真实 Worker 的视角），
而测试库是同会话共享的，所以这里用 `scoped_queue` 把可见范围限定到本用例的作品；
被限定的只有"看得见哪些作业"，认领与互斥逻辑本身走真实代码。
"""
from __future__ import annotations

import pytest

from dh_helpers import allow_billable, prepare_work
from app import jobs, storage as st
from app.digital_human import queue as q

CAP_CLOUD = "avatar_video_cloud"
CAP_MOTION = "motion"
CAP_LIPSYNC = "lipsync"


@pytest.fixture(autouse=True)
def clean_gpu_lock():
    """GPU 锁是全局单例，而测试库是同会话共享的：每个用例前后强制清空。

    不清的话，前一个用例留下的占用会让后一个用例拿到 GPU_BUSY，
    失败原因与被测逻辑完全无关——这种"假失败"比真失败更难查。
    """
    jobs.release_gpu("", force=True)
    yield
    jobs.release_gpu("", force=True)


@pytest.fixture()
def scoped_queue(work, monkeypatch):
    """把队列可见范围限定到本用例的作品，避免同会话其他用例的残留作业混入。"""
    original = q.queued_heavy

    def scoped():
        return [j for j in original() if j.get("work_id") == work["id"]]

    monkeypatch.setattr(q, "queued_heavy", scoped)
    return work


def _submit_heavy(work, capability, key, *, model="LivePortrait", billable=True,
                  amount_fen=0):
    return jobs.submit(work_id=work["id"], capability=capability, provider="m2_worker",
                       model=model, args={"capability": capability, "key": key},
                       idempotency_key=key, billable=billable, amount_fen=amount_fen,
                       note="测试用重型作业")


def _hold_gpu(work, capability=CAP_MOTION, *, worker="worker_a", key="hold"):
    """真实地占住 GPU：走租约 + 资源锁两步，不用假状态。"""
    job = _submit_heavy(work, capability, key)
    jobs.acquire_lease(job["id"], worker)
    jobs.acquire_gpu(job["id"], worker)
    return jobs.get_job(job["id"])


# ---------- 串行 ----------

def test_heavy_jobs_are_serialized_on_single_gpu(scoped_queue, monkeypatch):
    """两个重型作业连续执行，全程不允许出现重叠——这是"GPU 串行"的正证据。"""
    allow_billable(monkeypatch)
    work = scoped_queue
    a = _submit_heavy(work, CAP_MOTION, "ser:motion", model="LivePortrait")
    b = _submit_heavy(work, CAP_LIPSYNC, "ser:lipsync", model="MuseTalk-1.5")
    assert a["heavy"] and b["heavy"]

    in_flight: list = []
    overlaps: list = []
    gpu_seen: list = []

    def executor(job):
        in_flight.append(job["id"])
        if len(in_flight) > 1:
            overlaps.append(list(in_flight))
        holder = jobs.gpu_holder()
        gpu_seen.append(holder["job_id"] if holder else None)
        in_flight.pop()
        return {"state": "succeeded", "result": {"job": job["id"]}}

    run = q.run_until_idle("worker_1", executor)
    assert run["executed_count"] == 2
    assert overlaps == [], "同一时刻出现了两个重型作业：逻辑并行被误当成 GPU 并行"
    assert gpu_seen == [a["id"], b["id"]], "执行期间 GPU 必须指向正在跑的那个作业"
    assert run["stop_reason"] == "QUEUE_EMPTY"
    assert [s["state"] for s in run["steps"]] == ["succeeded", "succeeded"]
    assert jobs.get_job(a["id"])["state"] == "succeeded"
    assert jobs.get_job(b["id"])["state"] == "succeeded"
    assert jobs.gpu_holder()["released_at"] is not None, "跑完必须把 GPU 让出来"


def test_cloud_job_is_not_heavy_and_does_not_take_local_gpu(scoped_queue, monkeypatch):
    """OmniHuman 走云端，不占本地卡：GPU 被本地作业占住时它仍应正常流转。"""
    allow_billable(monkeypatch)
    work = scoped_queue
    holder = _hold_gpu(work, CAP_MOTION, key="cloud:hold")

    cloud = jobs.submit(work_id=work["id"], capability=CAP_CLOUD, provider="volcengine",
                        model="omnihuman-1.5", args={"route": "OMNIHUMAN_1_5"},
                        idempotency_key="cloud:1", billable=True, amount_fen=100)
    assert cloud["heavy"] is False, "云口播不占本地 GPU，不应被标为重型"

    jobs.acquire_lease(cloud["id"], "worker_cloud")
    done = jobs.complete(cloud["id"], "worker_cloud", {"video_url": "https://x/v.mp4"})
    assert done["state"] == "succeeded"
    assert jobs.gpu_holder()["job_id"] == holder["id"], \
        "云作业的完成不得动本地 GPU 锁"


# ---------- 排队不是失败 ----------

def test_gpu_busy_keeps_job_queued_not_failed(scoped_queue, monkeypatch):
    allow_billable(monkeypatch)
    work = scoped_queue
    holder = _hold_gpu(work, CAP_MOTION, key="busy:hold")
    waiting = _submit_heavy(work, CAP_LIPSYNC, "busy:wait", model="MuseTalk-1.5")

    out = q.claim_next_heavy("worker_2")
    assert out["claimed"] is None
    assert out["reason"] == "GPU_BUSY"
    assert "排队不构成失败" in out["note"]

    row = jobs.get_job(waiting["id"])
    assert row["state"] == "queued", "GPU 忙只能让作业排队，不能判失败"
    assert row["lease"] is None, "认领失败必须把租约交还，否则作业被自己锁死"
    assert "error" not in row, "排队不产生错误记录"
    assert "GPU_BUSY" in (row.get("release_reason") or ""), "交还原因要留痕，便于排查"
    assert jobs.get_job(holder["id"])["state"] == "leased"


def test_queue_empty_is_distinct_from_gpu_busy(scoped_queue):
    """三种结果必须能区分：没有待办 ≠ 有活干不了。"""
    out = q.claim_next_heavy("worker_1")
    assert out["claimed"] is None and out["reason"] == "QUEUE_EMPTY"
    assert "没有待执行的重型作业" in out["note"]


def test_released_lease_can_be_claimed_again_with_attempt_history(scoped_queue, monkeypatch):
    """排队重试要看得见次数：attempts 是"排了多久、试了几次"的唯一依据。"""
    allow_billable(monkeypatch)
    work = scoped_queue
    job = _submit_heavy(work, CAP_MOTION, "retry:1")
    jobs.acquire_lease(job["id"], "worker_1")
    released = jobs.release_lease(job["id"], "worker_1", next_state="queued",
                                  reason="GPU_BUSY: 先让给别人")
    assert released["state"] == "queued" and released["attempts"] == 1
    assert released["release_reason"] == "GPU_BUSY: 先让给别人"

    jobs.acquire_lease(job["id"], "worker_2")
    assert jobs.get_job(job["id"])["attempts"] == 2, "第二次认领应累计尝试次数"


def test_release_lease_refuses_terminal_state(scoped_queue, monkeypatch):
    """交还租约不得顺手把作业改成终态：终态只能由真实结果产生。"""
    allow_billable(monkeypatch)
    job = _submit_heavy(scoped_queue, CAP_MOTION, "term:1")
    jobs.acquire_lease(job["id"], "worker_1")
    with pytest.raises(jobs.JobRejected) as e:
        jobs.release_lease(job["id"], "worker_1", next_state="failed")
    assert e.value.code == "VALIDATION_FAILED"


def test_release_lease_refuses_foreign_worker(scoped_queue, monkeypatch):
    allow_billable(monkeypatch)
    job = _submit_heavy(scoped_queue, CAP_MOTION, "foreign:1")
    jobs.acquire_lease(job["id"], "worker_1")
    with pytest.raises(jobs.JobRejected) as e:
        jobs.release_lease(job["id"], "worker_2")
    assert e.value.code == "LEASE_NOT_HELD"


# ---------- 未核实结束不释放 ----------

def test_unknown_job_does_not_release_gpu(scoped_queue, monkeypatch):
    """结果未知时既不改判，也不把卡让出去——重复计费往往就发生在这一步。"""
    allow_billable(monkeypatch)
    work = scoped_queue
    unknown = _hold_gpu(work, CAP_MOTION, worker="worker_1", key="unk:hold")
    jobs.mark_unknown(unknown["id"], "worker_1", reason="提交后未收到确定响应")

    with pytest.raises(jobs.JobRejected) as e:
        jobs.release_gpu(unknown["id"])
    assert e.value.code == "GPU_BUSY_UNVERIFIED"
    assert jobs.gpu_holder()["released_at"] is None, "未核实前 GPU 必须一直占着"

    waiting = _submit_heavy(work, CAP_LIPSYNC, "unk:wait", model="MuseTalk-1.5")
    out = q.claim_next_heavy("worker_2")
    assert out["reason"] == "GPU_BUSY"
    assert jobs.get_job(waiting["id"])["state"] == "queued"

    recon = jobs.reconcile(unknown["id"])
    assert recon["requires_reconciliation"] is True
    assert recon["may_create_replacement"] is False, "对账阶段不得创建替代计费任务"


def test_resolve_unknown_releases_gpu_after_verification(scoped_queue, monkeypatch):
    allow_billable(monkeypatch)
    work = scoped_queue
    unknown = _hold_gpu(work, CAP_MOTION, worker="worker_1", key="res:hold")
    jobs.mark_unknown(unknown["id"], "worker_1", reason="网络中断")

    jobs.resolve_unknown(unknown["id"], "failed",
                         {"source": "provider_api", "remote_id": "task-1",
                          "note": "远端确认未受理，不计费"})
    assert jobs.gpu_holder()["released_at"] is not None

    waiting = _submit_heavy(work, CAP_LIPSYNC, "res:wait", model="MuseTalk-1.5")
    out = q.claim_next_heavy("worker_2")
    assert out["reason"] == "CLAIMED" and out["claimed"]["id"] == waiting["id"]


def test_resolve_unknown_demands_evidence(scoped_queue, monkeypatch):
    """无证据改判是禁止的：对账必须有来源，不能凭"应该成了"落定。"""
    allow_billable(monkeypatch)
    job = _hold_gpu(scoped_queue, CAP_MOTION, key="ev:hold")
    jobs.mark_unknown(job["id"], "worker_1", reason="超时")
    with pytest.raises(jobs.JobRejected) as e:
        jobs.resolve_unknown(job["id"], "succeeded", {})
    assert e.value.code == "EVIDENCE_REQUIRED"


# ---------- 执行器契约 ----------

def test_worker_exception_lands_in_ledger(scoped_queue, monkeypatch):
    """执行器自身异常也要落账：静默吞掉异常会让作业永远停在 leased。"""
    allow_billable(monkeypatch)
    work = scoped_queue
    job = _submit_heavy(work, CAP_MOTION, "exc:1")

    def boom(_job):
        raise RuntimeError("CUDA out of memory")

    out = q.run_one("worker_1", boom)
    assert out["state"] == "failed"
    assert out["error"]["code"] == "WORKER_EXCEPTION"
    row = jobs.get_job(job["id"])
    assert row["state"] == "failed"
    assert "CUDA out of memory" in row["error"]["message"]
    assert jobs.gpu_holder()["released_at"] is not None, "异常也不许把卡锁死"


def test_illegal_executor_state_is_rejected(scoped_queue, monkeypatch):
    allow_billable(monkeypatch)
    job = _submit_heavy(scoped_queue, CAP_MOTION, "bad:1")

    with pytest.raises(jobs.JobRejected) as e:
        q.run_one("worker_1", lambda _j: {"state": "done"})
    assert e.value.code == "VALIDATION_FAILED"
    assert "succeeded/failed/unknown" in e.value.message
    # 清理：作业被非法结果留在 leased，显式收尾以免影响后续用例
    jobs.fail(job["id"], "worker_1", {"code": "TEST_CLEANUP", "message": "用例收尾"})


def test_executor_unknown_state_keeps_lease_and_gpu(scoped_queue, monkeypatch):
    allow_billable(monkeypatch)
    work = scoped_queue
    job = _submit_heavy(work, CAP_MOTION, "u2:1")
    out = q.run_one("worker_1", lambda _j: {"state": "unknown", "reason": "响应中断"})
    assert out["state"] == "unknown"
    assert jobs.get_job(job["id"])["state"] == "unknown"
    assert jobs.gpu_holder()["released_at"] is None, "未知状态不释放 GPU"


# ---------- 视图与账本 ----------

def test_queue_view_reports_holder_counts_and_serialization(scoped_queue, monkeypatch):
    allow_billable(monkeypatch)
    work = scoped_queue
    holder = _hold_gpu(work, CAP_MOTION, key="view:hold")
    _submit_heavy(work, CAP_LIPSYNC, "view:wait", model="MuseTalk-1.5")

    view = q.queue_view(work["id"])
    assert view["serialized"] is True
    assert view["gpu_holder"]["job_id"] == holder["id"]
    assert view["gpu_holder"]["worker_id"] == "worker_a"
    assert view["counts"]["leased"] == 1
    assert view["counts"]["queued"] == 1
    assert view["counts"]["failed"] == 0, "排队作业不得出现在失败计数里"
    assert "不占本地 GPU" in view["note"]


def test_ledger_stays_consistent_after_queue_operations(scoped_queue, monkeypatch):
    allow_billable(monkeypatch)
    work = scoped_queue
    done = _submit_heavy(work, CAP_MOTION, "led:done")
    waiting = _submit_heavy(work, CAP_LIPSYNC, "led:wait", model="MuseTalk-1.5")

    def executor(job):
        if job["id"] == waiting["id"]:
            return {"state": "unknown", "reason": "响应中断"}
        return {"state": "succeeded", "result": {"ok": True}}

    q.run_until_idle("worker_1", executor)
    ledger = jobs.ledger_consistent(work["id"])
    assert ledger["problems"] == [], f"队列操作后账本出现不一致：{ledger['problems']}"
    assert jobs.get_job(done["id"])["state"] == "succeeded"
    assert jobs.get_job(waiting["id"])["settlement_required"] is True, \
        "未知作业仍需结算核实，不能当成结束"


def test_heavy_capability_set_covers_all_gpu_paths():
    """重型能力清单就是"哪些会抢卡"的定义，遗漏会让串行保证失效。"""
    assert {"motion", "lipsync", "avatar_video", "render"} <= jobs.HEAVY_CAPABILITIES
    assert CAP_CLOUD not in jobs.HEAVY_CAPABILITIES
