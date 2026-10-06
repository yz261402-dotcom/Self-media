"""走 HTTP 端点验一遍「人物定版四步链」——也就是前端界面实际打的那条路。

为什么要单独有这个脚本：
    直接调 `registry.*` 验证的是**函数**能通，界面上点按钮走的是**端点和请求体**。
    两者会分叉（字段名、大小写、422/409 映射、勾选项没传全），而分叉只有在界面上才会被发现。
    所以定版链必须在"真端点 + 真请求体"上过一次。

用法（务必指向**库快照**起的服务，别对着运行库跑写操作）：
    python scripts/verify_freeze_chain_http.py --base http://127.0.0.1:8010/api/v1 --identity av_xxx

退出码 0 = 四步全通且 `ready=True`；非 0 = 卡在哪一步、后端原话一并打印。
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request


def call(base: str, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json; charset=utf-8")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except json.JSONDecodeError:
            return e.code, {"detail": raw[:400]}


def show(label: str, status: int, body: dict) -> None:
    mark = "OK " if status < 400 else "FAIL"
    print(f"  [{mark}] {label} http={status}")
    if status >= 400:
        print("        " + json.dumps(body, ensure_ascii=False)[:400])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8010/api/v1")
    ap.add_argument("--identity", required=True)
    ap.add_argument("--reviewer", default="user_verify")
    args = ap.parse_args()
    base, av, who = args.base, args.identity, args.reviewer

    print(f"目标 {base}  身份 {av}  操作人 {who}")
    print("=" * 66)

    _, ident = call(base, "GET", f"/avatar-identities")
    row = next((x for x in ident if x["id"] == av), None) if isinstance(ident, list) else None
    if not row:
        print("身份不存在，终止")
        return 2
    print(f"现状：state={row['approval_state']} frozen={row.get('frozen')} "
          f"hero={row.get('hero_asset_id') or '(空)'} lic={row.get('license_id') or '(空)'}")

    if row.get("frozen"):
        print("该身份已冻结；本脚本不覆盖已定版身份。")
        return 3

    # 端点给出的是权威口径，脚本不自己另写一份（前端同理）
    _, opts = call(base, "GET", "/avatar/policy-options")
    limitations = list(opts.get("hero_only_limitations") or [])
    token = opts.get("attest_token") or ""
    print(f"后端口径：受限路径需逐条确认 {len(limitations)} 项；确认标记 {token}")
    if not limitations or not token:
        print("拿不到权威口径，终止（不接受前端/脚本另写一份措辞）")
        return 4

    _, assets = call(base, "GET", f"/identities/{av}/assets")
    assets = assets if isinstance(assets, list) else []
    hero = next((a for a in assets if a.get("kind") == "hero"), None)
    if not hero:
        print("没有原片（hero）资产，无法审核。终止。")
        return 5
    digest = hero.get("file_hash") or ""
    print(f"原片 {hero['id']} {hero.get('width')}x{hero.get('height')} 摘要 {digest[:24]}…")
    print("-" * 66)

    # 第 1 步 目检审核：摘要绑定原图哈希
    st, b = call(base, "POST", f"/identities/{av}/assets/{hero['id']}/review", {
        "reviewer": who, "outcome": "pass", "identity_digest": digest,
        "method": "人工目检：与期望人设一致",
    })
    show("1 原片身份审核", st, b)
    if st >= 400:
        return 6

    # 第 2 步 声明参考依据口径（受限路径），逐条勾确认项
    st, b = call(base, "POST", f"/identities/{av}/reference-policy", {
        "reviewer": who, "mode": "hero_only",
        "reason": "整张脸由文生图服务凭空产出，不存在同一人的第二张真值",
        "acknowledged": limitations,
    })
    show("2 声明参考依据口径(hero_only)", st, b)
    if st >= 400:
        return 7

    # 第 3 步 商用范围人工确认
    lic = row.get("license_id") or ""
    st, b = call(base, "POST", f"/avatar-licenses/{lic}/attest", {
        "reviewer": who, "basis": "该生成服务条款允许商用", "ack_token": token,
    })
    show("3 商用范围人工确认", st, b)
    if st >= 400:
        return 8
    rec = (b or {}).get("license") or {}
    print(f"        scope={rec.get('scope')} verified_by={rec.get('verified_by')} "
          f"note={str(rec.get('note'))[:60]}")

    # 第 4 步 冻结定版
    st, b = call(base, "POST", f"/identities/{av}/freeze", {
        "reviewer": who, "hero_asset_id": hero["id"], "identity_digest": digest,
        "license_id": lic,
    })
    show("4 冻结定版", st, b)
    if st >= 400:
        return 9
    print(f"        frozen={b.get('frozen')} state={b.get('approval_state')} "
          f"policy={(b.get('reference_policy') or {}).get('mode')}")

    print("-" * 66)
    st, ready = call(base, "GET", f"/identities/{av}/readiness")
    ok = bool(ready.get("ready"))
    print(f"生产就绪 = {ok}")
    for r in ready.get("reasons") or []:
        print("   - " + str(r))
    print("=" * 66)
    print("四步链 HTTP 路径" + ("全通" if ok else "走完但未就绪"))
    return 0 if ok else 10


if __name__ == "__main__":
    sys.exit(main())
