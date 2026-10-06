"""测试隔离：每个进程使用独立的 PostgreSQL schema，不触碰运行库，不发起任何网络请求。"""
from __future__ import annotations

import os
import sys
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# 每个 pytest 进程使用独立库；不删除其他并行测试或历史证据。
TMP_DB = os.path.join(os.environ.get("AVATAR_STUDIO_TEST_ROOT") or os.path.join(ROOT, "runtime", "test-sessions"),
                      f"{os.getpid()}-{uuid.uuid4().hex}", "studio.db")

os.environ["AVATAR_STUDIO_DB"] = TMP_DB  # 仅作 schema 隔离键：每个 pytest 进程独立 schema
os.environ.setdefault("AVATAR_STUDIO_DATABASE_URL",
                      "postgresql://avatar:avatar_studio_local@127.0.0.1:15432/avatar_studio")
os.environ["AVATAR_STUDIO_MODE"] = "mock"
os.environ["AVATAR_STUDIO_MODEL_BACKEND"] = "deterministic"
os.environ["AVATAR_STUDIO_ORCHESTRATOR"] = "agents"
os.environ["AVATAR_STUDIO_BUDGET_FEN"] = "0"

from app import storage as st  # noqa: E402

st.init_db()
