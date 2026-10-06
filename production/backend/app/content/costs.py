"""内容流程的费用运算、运行配置与台账汇总。

- 原币种价格一律 Decimal；人民币预留向上取分；非人民币缺汇率就报错，不猜。
- 汇总函数必须在调用方的写事务里使用（读到的数字才和随后的写入一致）。
"""
from __future__ import annotations

import json
import os
from decimal import ROUND_CEILING, Decimal
from typing import Any, Dict, Optional

from .errors import ContentRejected

NAMESPACE = "content-v2"

# 已发出（或可能已发出）请求、费用尚未结清的调用继续占用额度
HOLDING_STATES = ("leased", "waiting_external", "unknown")


class CostError(ContentRejected):
    code = "COST_UNCOMPUTABLE"


def _dec(value: Any, name: str) -> Decimal:
    try:
        return Decimal(str(value))
    except Exception as exc:  # noqa: BLE001
        raise CostError(f"价格快照字段 {name} 不是有效数字") from exc


def price_snapshot(*, currency: str, input_per_million: Any, output_per_million: Any,
                   fx_cny_per_unit: Optional[Any] = None, fx_date: str = "",
                   price_date: str = "", source: str = "") -> Dict[str, Any]:
    """价格快照：随每次调用落库，之后改价不改历史。数值存成字符串避免浮点误差。"""
    snap: Dict[str, Any] = {
        "currency": currency.upper(), "input_per_million": str(input_per_million),
        "output_per_million": str(output_per_million), "price_date": price_date, "source": source,
    }
    if fx_cny_per_unit is not None:
        snap["fx_cny_per_unit"] = str(fx_cny_per_unit)
        snap["fx_date"] = fx_date
    return snap


def _fx(snapshot: Dict[str, Any]) -> Decimal:
    if snapshot.get("currency", "CNY").upper() == "CNY":
        return Decimal(1)
    if snapshot.get("fx_cny_per_unit") in (None, ""):
        raise CostError(f"价格币种 {snapshot.get('currency')} 缺少汇率快照，不能换算成人民币，不估算")
    return _dec(snapshot["fx_cny_per_unit"], "fx_cny_per_unit")


def cost_fen(snapshot: Dict[str, Any], input_tokens: int, output_tokens: int) -> int:
    """按 token 用量换算人民币分（向上取整）。用量由调用方取真实 usage，缺失时不要调用本函数。"""
    if input_tokens < 0 or output_tokens < 0:
        raise CostError("token 用量不能为负")
    native = (_dec(snapshot["input_per_million"], "input_per_million") * input_tokens
              + _dec(snapshot["output_per_million"], "output_per_million") * output_tokens) / Decimal(1_000_000)
    return int((native * _fx(snapshot) * 100).to_integral_value(rounding=ROUND_CEILING))


def to_fen(amount_native: Any, snapshot: Dict[str, Any]) -> int:
    """按次计价（如搜索一次）的原币种金额换算成分（向上取整）。"""
    return int((_dec(amount_native, "amount") * _fx(snapshot) * 100).to_integral_value(rounding=ROUND_CEILING))


class ContentLimits:
    def __init__(self, per_flow_fen: Optional[int], daily_fen: Optional[int], max_text_calls: Optional[int],
                 concurrency: int, max_repair_rounds: int):
        self.per_flow_fen = per_flow_fen
        self.daily_fen = daily_fen
        self.max_text_calls = max_text_calls
        self.concurrency = concurrency
        self.max_repair_rounds = max_repair_rounds

    def to_dict(self) -> Dict[str, Optional[int]]:
        return dict(self.__dict__)


def content_limits() -> ContentLimits:
    """内容创作不设费用/调用次数配额；null 表示不限，旧预算环境变量不再生效。"""
    def _int(name: str, default: int) -> int:
        raw = os.environ.get(name, "")
        try:
            return int(raw) if raw != "" else default
        except ValueError:
            return default

    return ContentLimits(
        per_flow_fen=None,
        daily_fen=None,
        max_text_calls=None,
        concurrency=max(1, _int("AVATAR_STUDIO_CONTENT_CONCURRENCY", 2)),
        max_repair_rounds=max(0, _int("AVATAR_STUDIO_CONTENT_MAX_REPAIR_ROUNDS", 2)),
    )


class VisualLimits:
    def __init__(self, reserve_fen: int, price_fen: Optional[int], max_bytes: int, max_duration_ms: int):
        self.reserve_fen, self.price_fen = reserve_fen, price_fen
        self.max_bytes, self.max_duration_ms = max_bytes, max_duration_ms


def visual_limits() -> VisualLimits:
    """视觉分析的预留额、（可选）已核验单价与素材上限。单价未配置时费用保持待对账，不写 0 元。"""
    def _int(name: str, default: int) -> int:
        raw = os.environ.get(f"AVATAR_STUDIO_CONTENT_VISUAL_{name}", "").strip()
        return int(raw) if raw.isdigit() and int(raw) > 0 else default

    price = os.environ.get("AVATAR_STUDIO_CONTENT_VISUAL_PRICE_FEN", "").strip()
    return VisualLimits(_int("RESERVE_FEN", 80), int(price) if price.isdigit() else None,
                        _int("MAX_BYTES", 200 * 1024 * 1024), _int("MAX_DURATION_MS", 600000))


def counted_fen(job: Dict[str, Any]) -> int:
    """一条 provider job 目前占用多少额度（分）。

    - settled：真实费用；held/reserved：预留额（未知结果、无 usage 都不释放）；
    - released（供应商明确拒绝且未计费、或从未发出即取消）和 blocked：0。
    """
    state = job.get("settlement_state")
    if state == "settled":
        return max(0, int(job.get("actual_cost_fen") or 0))
    if state in ("reserved", "held"):
        return max(0, int(job.get("reserved_fen") or 0))
    return 0


def _content_jobs(c, day: Optional[str] = None):
    for row in c.execute("SELECT data FROM provider_jobs").fetchall():
        item = json.loads(row["data"])
        if item.get("namespace") != NAMESPACE:
            continue
        if day and str(item.get("created_at", ""))[:10] != day:
            continue
        yield item


def totals(c, *, day: Optional[str] = None, parent_id: Optional[str] = None,
           exclude_id: Optional[str] = None) -> Dict[str, int]:
    """事务内汇总 content-v2 的实付、未结清预留与调用次数。"""
    settled = held = calls = 0
    for job in _content_jobs(c, day):
        if job["id"] == exclude_id:
            continue
        if parent_id is not None and job.get("parent_content_job_id") != parent_id:
            continue
        state = job.get("settlement_state")
        if state == "settled":
            settled += counted_fen(job)
        elif state in ("reserved", "held"):
            held += counted_fen(job)
        if state in ("settled", "reserved", "held"):
            calls += 1
    return {"settled_fen": settled, "reserved_fen": held, "calls": calls,
            "total_fen": settled + held}


def global_totals(c, *, day: str, exclude_id: Optional[str] = None) -> Dict[str, int]:
    """全部可计费 provider job 今日占用（与旧 jobs.submit 相同口径），用于叠加现有全局授权门禁。"""
    spent = subs = 0
    for row in c.execute("SELECT data FROM provider_jobs").fetchall():
        item = json.loads(row["data"])
        if item["id"] == exclude_id or not item.get("billable") \
                or str(item.get("created_at", ""))[:10] != day or item.get("state") == "blocked":
            continue
        if item.get("namespace") == NAMESPACE:
            amount = counted_fen(item)
            if item.get("settlement_state") not in ("settled", "reserved", "held"):
                continue
        else:
            if item.get("state") == "cancelled" and not item.get("settlement_required"):
                continue
            amount = max(0, int(item.get("amount_fen") or 0))
        spent += amount
        subs += 1
    return {"spent_fen": spent, "submissions": subs}

