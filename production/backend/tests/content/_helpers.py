"""内容测试共用夹具：隔离预算、造作品/参考/提示、按角色脚本化的文本 transport。"""
import json
from uuid import uuid4

from app import storage as st
from app.content import repository as repo
from app.content import contracts as cc


def set_env(monkeypatch, per_flow="100000", daily="100000000"):
    for k, v in {
        "AVATAR_STUDIO_CONTENT_BUDGET_PER_FLOW_FEN": per_flow, "AVATAR_STUDIO_CONTENT_BUDGET_DAILY_FEN": daily,
        "AVATAR_STUDIO_BUDGET_PER_TASK_FEN": "100000", "AVATAR_STUDIO_BUDGET_DAILY_FEN": "100000000",
        "AVATAR_STUDIO_BUDGET_MAX_SUBMISSIONS": "1000000", "AVATAR_STUDIO_ANTHROPIC_API_KEY": "sk-test-a",
        "AVATAR_STUDIO_QWEN_API_KEY": "sk-test-q", "AVATAR_STUDIO_OPENAI_API_KEY": "sk-test-openai",
        "AVATAR_STUDIO_CLAUDE_COMPAT_API_KEY": "sk-test-claude-compat",
        "AVATAR_STUDIO_FX_USD_CNY": "7.2",
    }.items():
        monkeypatch.setenv(k, v)
    # 会话内共用一个库：不同用例复用相同示例稿，跨作品查重会把它们互相判成重复
    monkeypatch.setattr("app.content.dedup.internal_history", lambda work_id: [])
    for r in ("PLANNER", "WRITER", "REPAIR", "REVIEWER"):
        monkeypatch.delenv(f"AVATAR_STUDIO_CONTENT_{r}_PROVIDER", raising=False)
        monkeypatch.delenv(f"AVATAR_STUDIO_CONTENT_{r}_MODEL", raising=False)


MATERIAL = "示例产品发布时宣布上下文窗口提升到了一百万个词元"


def make_work(materials=None, topic="AI 助手怎么用", with_reference=True, with_prompt=False):
    wid = "work_" + uuid4().hex
    brief = {"topic": topic, "audience": "年轻人", "target_duration_ms": 0}
    if materials:
        brief["materials"] = [{"text": t, "source": "官方", "content_hash": "", "added_at": ""} for t in materials]
    work = {"id": wid, "name": "t", "brief": brief, "version": 1, "content_revision": 0, "script_version_id": None}
    st.put("works", wid, work)
    if with_reference:
        ref = {"id": "ref_" + uuid4().hex[:8], "work_id": wid, "platform_work_id": "77", "title": "", "caption": "简介",
               "author": "作者", "fetched_at": "2026-09-29T00:00:00Z", "extraction_version": "redfox-transcript-v1",
               "transcript": {"state": "ready", "text": "参考视频里说了一段独一无二的话用来测试复用检测不要照搬这一整段", "source": "redfox", "task_id": "T"}}
        with st.transaction() as c:
            saved = repo.save_reference(c, ref)
            repo.bump_content_revision(c, wid, reference_snapshot_id=saved["id"])
    return wid


def plan_json(angles=("角度甲", "角度乙", "角度丙")):
    return {"reference_analysis": {"hook_function": "反常识", "structure": ["a"], "rhythm": "快", "viewpoint_path": "p",
                                   "emotional_curve": "e", "reusable_techniques": ["t"],
                                   "do_not_reuse": [{"fragment": "独一无二的话用来测试复用检测", "reason": "原句"},
                                                    {"fragment": "参考里根本没有的杜撰片段", "reason": "假"}]},
            "angle_plan": [{"slot": i + 1, "angle": a, "new_viewpoint": f"新观点{i}", "opening_hint": "h"}
                           for i, a in enumerate(angles)], "prompt_text": "请写有个人观点的口播"}


_TEXTS = {
    1: "先说个我自己的经历：上周我把手机里三个笔记软件全卸了，只留一个，效率居然翻了一倍。原因很简单，选择变少了。",
    2: "很多人觉得早起就是自律，其实我观察下来，真正拉开差距的是晚上几点关灯。睡够了，第二天做事才不拖。",
    3: "如果只能给新手一个建议，我会说别急着买装备。先用手边的东西练三个月，你才知道自己到底喜不喜欢这件事。",
}


def cand(slot, text=None, angle=None, claims=None):
    return {"slot": slot, "angle": angle or f"角度{'甲乙丙'[slot - 1]}", "title": f"标题{slot}",
            "description": f"说明{slot}", "tags": [], "claims": claims or [], "text": text or _TEXTS[slot]}


def review_json(ids, fail=(), set_distinct=True, rewrite=(), quote=None, storyboard=False):
    out = {"candidates": [], "set": {"distinct": set_distinct, "rewrite_slots": list(rewrite), "findings": []}}
    for i in ids:
        bad = i in fail
        crit = {k: "pass" for k in ("originality", "reference_reuse", "evidence_support", "style_fit",
                                    "shot_feasibility" if storyboard else "spoken_quality",
                                    "identity_consistency" if storyboard else "no_fabrication")}
        if storyboard:
            crit["no_fabrication"] = "pass"
        if bad:
            crit["originality"] = "fail"
        out["candidates"].append({"candidate_id": i, "criteria": crit,
                                  "findings": [{"quote": quote or "", "issue": "缺少新观点" if bad else "",
                                                "suggestion": "补充自己的观点" if bad else ""}] if bad else []})
    return out


class Script:
    """按角色排队的响应；记录每个角色的调用数与收到的 user 文本。"""

    def __init__(self):
        self.q = {"planner": [], "writer": [], "reviewer": []}
        self.calls = {"planner": 0, "writer": 0, "reviewer": 0}
        self.seen = {"planner": [], "writer": [], "reviewer": []}
        self.review_hook = None

    def __call__(self, url, headers, body, timeout):
        b = json.loads(body)
        if "anthropic" in url or b.get("model") in ("claude-sonnet-5-5", "gpt-6.1-sol"):
            role = "writer"
            user = b["messages"][-1]["content"]
        else:
            user = b["messages"][1]["content"]
            role = "reviewer" if "独立的口播稿审稿人" in b["messages"][0]["content"] else "planner"
        self.calls[role] += 1
        self.seen[role].append(user)
        item = self.q[role].pop(0)
        if callable(item):
            item = item(user)
        if isinstance(item, Exception):
            raise item
        text = json.dumps(item, ensure_ascii=False)
        if role == "writer":
            return 200, json.dumps({"id": "m", "model": b["model"],
                                    "choices": [{"message": {"content": text}, "finish_reason": "stop"}],
                                    "usage": {"prompt_tokens": 3000, "completion_tokens": 1500}}).encode()
        return 200, json.dumps({"id": "c", "model": b["model"], "choices": [{"message": {"content": text}, "finish_reason": "stop"}],
                                "usage": {"prompt_tokens": 4000, "completion_tokens": 800}}).encode()


def ids_in(user_text):
    import re
    return re.findall(r"candidate_id=(script_[0-9a-f]+)", user_text)


def review_reply(fail=(), **kw):
    """reviewer 响应：从 user 文本里取 review_ids，按 slot 决定谁失败。fail 里放 slot 号。"""
    import re

    def make(user):
        m = re.search(r"review_ids[^\[]*(\[[^\]]*\])", user)
        ids = json.loads(m.group(1))
        slot_of = {}
        for cid, slot in re.findall(r"candidate_id=(script_[0-9a-f]+)｜slot (\d)", user):
            slot_of[cid] = int(slot)
        return review_json(ids, fail=[i for i in ids if slot_of.get(i) in fail],
                           storyboard='"shot_feasibility"' in user, **kw)
    return make


_REPAIRED = {
    1: "换个讲法：我以前也爱囤软件，后来发现工具越多越焦虑，现在只留最顺手的那一个，反而每天都能做完事。",
    2: "重新想了想，早起这件事被神话了。我更在意的是作息稳定，哪怕七点起也一样能把一天安排得明明白白。",
    3: "再补一句实在话：新手最缺的不是设备，是坚持。先把基础动作练熟，再谈升级，这条路走下来最省钱。",
}


def repair_reply(*slots, text_of=None):
    def make(user):
        return {"candidates": [{"slot": s, "text": (text_of or (lambda s: _REPAIRED[s]))(s),
                                "claims": [], "repair_note": "重写"} for s in slots]}
    return make


def run_job(kind, work_id, payload, handler, transport, request_id=None):
    from app.content import tasks
    job = tasks.submit_job(work_id, kind, request_id or uuid4().hex[:8], payload)
    tasks.set_progress(job["id"], state="running")
    try:
        handler(job, transport=transport)
    except Exception as exc:  # 走与调度器相同的终态映射
        tasks.run_job_error(job, exc)
    return tasks.get_job(job["id"])


def human_approve(work_id, script_id):
    from app.content import human_review

    batch = st.get("script_batches", work_id)
    return human_review.submit(work_id, {
        "request_id": uuid4().hex, "script_id": script_id, "action": "adopt", "rubric": "core5",
        "scores": dict.fromkeys(human_review.CORE, 5), "expected_batch_id": batch["id"],
        "expected_content_revision": int(st.get("works", work_id).get("content_revision") or 0),
    })
