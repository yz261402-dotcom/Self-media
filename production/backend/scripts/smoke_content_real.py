"""真实 DeepSeek 冒烟：验证 T06「提示词直接生成三稿」链路可用。

只做一个作品的真实调用，用于取得"接口可用"的实测证据；
不生成媒体、不提交计费视频任务。用法：
    AVATAR_STUDIO_SECRETS_FILE=<密钥文件> python scripts/smoke_content_real.py
"""
from __future__ import annotations

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

os.environ.setdefault("AVATAR_STUDIO_MODE", "mock")
os.environ["AVATAR_STUDIO_DB"] = os.path.join(ROOT, "tests", "tmp_real_content.db")
# deepseek-flash 是规划中的目标型号；官方当前可用 id 为 deepseek-chat
os.environ.setdefault("AVATAR_STUDIO_DEEPSEEK_MODEL", "deepseek-chat")

from app import config, storage as st  # noqa: E402

st.init_db()

secrets_file = os.environ.get("AVATAR_STUDIO_SECRETS_FILE", "")
injected = config.load_secrets_from_file(secrets_file)
print("已注入环境变量（只列名，不含值）：", injected)

from app.content import generate as gen  # noqa: E402
from app.content.llm import llm_status  # noqa: E402

print("LLM 状态：", json.dumps(llm_status(), ensure_ascii=False))
if not llm_status()["configured"]:
    print("未取得 DeepSeek 凭据，停止（不伪造产出）")
    sys.exit(2)

result = gen.generate_candidates(
    work_id="work_smoke",
    topic="为什么 AI 读了你的文件，还是会答错",
    content_type="AI 科普",
    audience="对 AI 感兴趣的年轻受众",
    keywords=["RAG", "检索", "切片"],
    target_duration_ms=60000,
    materials=[{"text": "我们把切片从 1200 字改成 400 字后，召回命中率从 55% 提升到 88%",
                "source": "用户提供"}],
)

print("\n=== 生成结果 ===")
print("请求数量：", result["requested"], "实际产出：", result["produced"])
print("是否联网：", result["web_search_used"])
print("缺口说明：", result["shortfall_note"])
for c in result["candidates"]:
    print(f"\n[{c.angle}] {c.title}")
    print("  估算时长：", c.duration_estimate_ms, "ms")
    print("  质量结论：", c.quality_report.overall,
          "| 需复核：", c.quality_report.review_required)
    for chk in c.quality_report.checks:
        print(f"    - {chk.code}: {chk.outcome}"
              f"{'(硬)' if chk.hard_gate else ''} {chk.message}")
    for cl in c.claims:
        print(f"    * 断言[{cl.verdict}] {cl.text}")
    print("  正文：", c.text[:120].replace("\n", " / "), "...")
