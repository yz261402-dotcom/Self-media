"""T05 测试夹具：作品、路线、底片、已确认音频，以及项目内临时目录。

所有用例在 mock/离线状态下运行：不触网、不产生费用、不提交 GPU 作业。
供应商响应与 MP4 字节由 `dh_helpers.py` 构造，因此
"认证失败 / 限流 / 无视频轨 / HTML 错误页 / 提交丢响应 / URL 过期"
这些路径都能被确定性验证。
"""
from __future__ import annotations

import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from dh_helpers import (  # noqa: E402,F401
    HASH_A, HASH_B, HASH_C, HASH_D, PRESET_IDENTITY, PRESET_VOICE, allow_billable,
    authorize_cost, binary_response, html_error_page, install_alignment, install_final_audio,
    install_identity, install_motion_ref, install_plate, install_task, install_variant,
    long_text, m2_ok, make_mp4, make_transport, make_words, new_work, prepare_work,
    script_text_of, volc_query, volc_submit_no_task_id, volc_submit_ok,
)

from app import storage as st  # noqa: E402


@pytest.fixture()
def tmp_path():
    """项目内临时目录。

    Windows 系统临时目录在本环境不可写（PermissionError / WinError 5），
    因此不用 pytest 内置的 tmp_path；清理也要吞掉 SystemExit——
    本机 CLI 的删除保护 shim 在批量删除时会 raise SystemExit(1)，
    而它不是 SystemExit 之外的 Exception，会让整个会话在 teardown 阶段中断。
    """
    import shutil

    d = os.path.join(HERE, ".tmp_dh", st.new_id("t"))
    os.makedirs(d, exist_ok=True)
    try:
        yield d
    finally:
        try:
            shutil.rmtree(d, ignore_errors=True)
        except SystemExit:
            pass


@pytest.fixture()
def work() -> dict:
    """OmniHuman 路线：身份/变体已审、计划冻结、最终音频已确认、任务已确认路线。"""
    return prepare_work()


@pytest.fixture()
def m2_work() -> dict:
    """M2 路线：额外具备已获准动作参考与已审动态底片。"""
    return prepare_work(route="M2", with_plate=True)


@pytest.fixture()
def segmented_work() -> dict:
    """约 75 秒音频 + 对齐产物：用于验证分段（严格小于 60 秒）。"""
    return prepare_work(audio_ms=75000, with_alignment=True,
                        text=long_text(sentences=10, per_sentence=24))
