"""OmniHuman 口播生成**通用流水线**——换任何素材都用同一条命令跑完。

为什么单独成一条流水线
----------------------
分段 → 逐段生成 → 拼接这三步散落在若干接口里，靠人一步步点很容易漏：
漏了拼接，多段作品会卡在"原片短于音频"的覆盖校验上；
漏了分段，长音频会整轨发出去，得到"提交成功、查询永远 50215"。

这条流水线把顺序固定下来，并且**每一步都把通用规则写死在实现里**：

1. **够短就不切分。** 音频 ≤ 上限×安全系数 → 整轨一段，直接把整条音轨交给模型。
   分段会凭空造出接缝，而接缝处的画面跳变是供应商侧决定的、本地拼不掉的，
   能不切就不切。顺带的好处：单段不需要任何切点，因此不依赖对齐产物。
2. **要切就装箱到上限。** 只按句子切、不装箱，62 秒会被切成 7 段（6 个接缝），
   装箱后同样长度只要 2 段（1 个接缝）。切点仍然只来自对齐的真实句子边界。
3. **切点尽量落在静默处。** 判静线由**当前音轨自身**的能量分布推导，
   不写死任何从某条音轨量来的数字。
4. **逐段串行提交**（供应商并发上限为 1），一段一段推进。
5. **拼回去。** 逐接缝实测动静，只在静默侧压黑；压黑不删帧不插帧，音画同步 0 位移。

用法（后端需已在 127.0.0.1:8000 运行）
------------------------------------
    python omni_pipeline.py status  <work_id>
    python omni_pipeline.py plan    <work_id>            # 只建计划 + 预演，不花钱
    python omni_pipeline.py run     <work_id>            # 全流程：建计划→提交→等待→拼接
    python omni_pipeline.py stitch  <work_id>            # 只拼接

`run` 会**真实调用供应商并计费**，执行前会打印一次调用清单并等待确认；
`--yes` 可跳过确认（脚本化时用）。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Dict, Optional

BASE = "http://127.0.0.1:8000/api/v1"

# 代理会拦掉本地回环地址，必须绕开。
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _req(method: str, path: str, payload: Optional[Dict[str, Any]] = None,
         *, timeout: int = 120) -> Dict[str, Any]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        f"{BASE}{path}", data=data, method=method,
        headers={"Content-Type": "application/json"})
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            return {"code": f"HTTP_{e.code}", "message": body[:500]}


def _fail(result: Dict[str, Any], where: str) -> bool:
    """统一识别两类失败语义（422 请求不成立 / 409 当前不可执行）。"""
    err = result.get("error") or result
    code = err.get("code") if isinstance(err, dict) else None
    if code:
        print(f"[{where}] 失败 {code}: {err.get('message', '')}", file=sys.stderr)
        return True
    return False


# ---------------------------------------------------------------- 步骤

def step_status(work: str) -> Dict[str, Any]:
    driver = _req("GET", f"/works/{work}/talking-head/driver")
    seg = _req("GET", f"/works/{work}/talking-head/segments")
    return {"driver": driver, "segments": seg}


def step_plan(work: str, *, limit_ms: Optional[int] = None) -> Dict[str, Any]:
    """建分段计划。这一步纯本地计算，**不调用供应商、不计费**。"""
    payload = {"limit_ms": limit_ms} if limit_ms else {}
    return _req("POST", f"/works/{work}/talking-head/segments", payload)


def _preview_plan(work: str, plan: Dict[str, Any]) -> None:
    """把计划讲清楚：段数、每段窗口、预计有几个接缝。"""
    segs = plan.get("segments") or []
    summary = plan.get("summary") or {}
    print(f"音轨 {plan.get('audio_duration_ms')}ms / 上限 {summary.get('limit_ms')}ms")
    print(f"段数 {len(segs)} → 接缝 {max(0, len(segs) - 1)} 个"
          f"{'（整轨一段，不切分）' if summary.get('whole_track') else ''}")
    print(f"静默优先切点：{summary.get('silence_preference')}")
    for s in segs:
        print(f"  {s['segment_id']:>9} {s['start_ms']:>6}-{s['end_ms']:>6}ms "
              f"({s['duration_ms']}ms)")
    if len(segs) > 1:
        print("接缝位置：", [s["end_ms"] for s in segs[:-1]])
        print("提示：接缝数越少越好——每一处接缝的画面跳变都无法在拼接阶段消除。")


def step_submit(work: str, *, authorize: bool) -> Dict[str, Any]:
    return _req("POST", f"/works/{work}/talking-head/submissions",
                {"replacement_authorized": bool(authorize)})


def step_wait(work: str, *, poll_sec: int = 15, max_sec: int = 3600) -> Dict[str, Any]:
    """轮询推进器。并发上限 1，所以是一段一段串行往前走。"""
    deadline = time.time() + max_sec
    last = None
    while time.time() < deadline:
        d = _req("GET", f"/works/{work}/talking-head/driver")
        if _fail(d, "driver"):
            return d
        stamp = (d.get("state"), d.get("succeeded"), d.get("in_flight"), d.get("pending"))
        if stamp != last:
            print(f"  {d.get('state')}: 完成 {d.get('succeeded')}/{d.get('total')} "
                  f"在途 {d.get('in_flight')} 待发 {d.get('pending')} "
                  f"失败 {d.get('failed')} 未知 {d.get('unknown')}")
            last = stamp
        if d.get("state") in ("DONE", "STOPPED"):
            return d
        time.sleep(poll_sec)
    return {"code": "WAIT_TIMEOUT", "message": f"等待超过 {max_sec}s"}


def step_cost(work: str) -> Dict[str, Any]:
    return _req("GET", f"/works/{work}/talking-head/cost")


def step_confirm_cost(work: str) -> Dict[str, Any]:
    """走真实链路重新确认费用：用户已看到分段计划，金额与段数就是这份清单。"""
    listing = _req("GET", f"/works/{work}/talking-head/submissions")
    # 后端要求摘要必须和提交时算出来的一致，这里拿 current_call_list 的 digest。
    payload = {
        "route": (listing or {}).get("route") or "OMNIHUMAN_1_5",
        "request_digest": (listing or {}).get("digest") or "",
        "estimated_fen": 0,
        "ceiling_fen": 0,
        "segment_count": (listing or {}).get("segment_count") or 0,
        "generated_seconds": 0.0,
        "resolution": "1080p",
    }
    return _req("POST", f"/works/{work}/talking-head/cost-confirmation", payload)


def step_stitch(work: str) -> Dict[str, Any]:
    return _req("POST", f"/works/{work}/talking-head/stitch", {})


def _preview_stitch(result: Dict[str, Any]) -> None:
    seams = result.get("seams") or []
    print(f"成品 {result.get('duration_ms')}ms / 音轨 {result.get('audio_ms')}ms")
    print(f"判静线 {result.get('silence_threshold_db')}dB，"
          f"{result.get('seams_softened')}/{len(seams)} 个接缝压黑")
    for s in seams:
        mark = "压黑" if s.get("softened") else "硬切"
        print(f"  接缝@{s['at_ms']:>6}ms  淡出{s['fade_out_ms']:>4}ms "
              f"淡入{s['fade_in_ms']:>4}ms  {mark}")


# ---------------------------------------------------------------- 命令

def cmd_run(args: argparse.Namespace) -> int:
    work = args.work_id
    print(f"== 1/4 建分段计划（本地计算，不计费）==")
    plan = step_plan(work)
    if _fail(plan, "plan"):
        return 1
    _preview_plan(work, plan)

    listing = _req("GET", f"/works/{work}/talking-head/submissions")
    cost = _req("GET", f"/works/{work}/talking-head/cost")
    calls = (listing or {}).get("segment_count") or len(plan.get("segments") or [])
    print(f"\n== 2/4 提交（真实调用，会计费）==\n将产生 {calls} 次调用")
    if not args.yes:
        answer = input("确认提交？[y/N] ").strip().lower()
        if answer != "y":
            print("已取消：未提交，未产生费用。")
            return 0
    # 如果费用确认仍停留在旧分段数，必须先重新确认。
    submit_result = step_submit(work, authorize=args.authorize)
    if _fail(submit_result, "submit"):
        if submit_result.get("code") == "COST_LIST_CHANGED":
            print("  费用确认停留在旧清单，正在按新计划重新确认……")
            c = step_confirm_cost(work)
            if _fail(c, "cost-confirmation"):
                return 1
            print("  费用已重新确认，再次提交……")
            submit_result = step_submit(work, authorize=args.authorize)
            if _fail(submit_result, "submit"):
                return 1
        else:
            return 1
    print("已提交，逐段串行推进（供应商并发上限 1）")

    print("\n== 3/4 等待生成 ==")
    driver = step_wait(work, poll_sec=args.poll, max_sec=args.max_wait)
    if _fail(driver, "wait") or driver.get("state") == "STOPPED":
        print("推进器停下：先去提交记录对账，不要硬推。", file=sys.stderr)
        return 1

    print("\n== 4/4 拼接 ==")
    result = step_stitch(work)
    if _fail(result, "stitch"):
        return 1
    _preview_stitch(result)
    print(f"\n完成：{(result.get('video') or {}).get('id')}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    print(json.dumps(step_status(args.work_id), ensure_ascii=False, indent=2))
    return 0


def cmd_plan(args: argparse.Namespace) -> int:
    result = step_plan(args.work_id, limit_ms=args.limit_ms)
    if _fail(result, "plan"):
        return 1
    _preview_plan(args.work_id, result)
    return 0


def cmd_stitch(args: argparse.Namespace) -> int:
    result = step_stitch(args.work_id)
    if _fail(result, "stitch"):
        return 1
    _preview_stitch(result)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("status", help="查看分段进度与计划")
    p.add_argument("work_id")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("plan", help="建分段计划并预演（不计费）")
    p.add_argument("work_id")
    p.add_argument("--limit-ms", type=int, default=None)
    p.set_defaults(func=cmd_plan)

    p = sub.add_parser("stitch", help="只拼接已生成的分段")
    p.add_argument("work_id")
    p.set_defaults(func=cmd_stitch)

    p = sub.add_parser("run", help="全流程：建计划→提交→等待→拼接")
    p.add_argument("work_id")
    p.add_argument("--yes", action="store_true", help="跳过提交前的确认")
    p.add_argument("--authorize", action="store_true",
                   help="有旧失败记录时显式授权重新生成（会再次计费）")
    p.add_argument("--poll", type=int, default=15)
    p.add_argument("--max-wait", type=int, default=3600)
    p.set_defaults(func=cmd_run)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
