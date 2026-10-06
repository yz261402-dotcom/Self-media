"""T04 测试夹具：作品、稿件、音色、冻结计划，以及项目内的临时目录。

所有用例都在 mock/离线状态下运行：不触网、不产生费用、不提交 GPU 作业。
供应商响应由 `tests/tts/tts_helpers.py` 的 `make_transport` 构造，
因此"认证失败 / 限流 / 空音频 / 截断音频 / 错误采样率 / 非法时间戳"
这些路径都能被确定性验证。

构造工具放在 `tts_helpers.py` 而不是本文件：`conftest` 这个模块名在
`tests/` 与 `tests/tts/` 下各有一份，测试模块跨目录导入它到底取到哪一份
取决于 sys.path 顺序，不适合承载"供应商响应长什么样"这种关键定义。
"""
from __future__ import annotations

import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from tts_helpers import (  # noqa: E402,F401
    PRESET_VOICE, frozen_plan, make_transport, make_wav, new_work, ok_body,
    preset_voice, script_text_of, wav_hex,
)

from app import storage as st  # noqa: E402


@pytest.fixture()
def tmp_path():
    """项目内临时目录。

    Windows 系统临时目录在本环境不可写（PermissionError / WinError 5），
    因此不用 pytest 内置的 tmp_path；清理也要吞掉 SystemExit——
    本机 CLI 的删除保护 shim 在批量删除时会 raise SystemExit(1)，
    而它不是 Exception，会让整个会话在 fixture teardown 阶段中断。
    """
    import shutil

    d = os.path.join(HERE, ".tmp_tts", st.new_id("t"))
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
    """一个独立的、已采用稿件且已选音色的作品。"""
    return new_work()


@pytest.fixture()
def voice() -> dict:
    return preset_voice()


@pytest.fixture()
def plan(work) -> dict:
    return frozen_plan(work)


@pytest.fixture()
def script_text(work) -> str:
    return script_text_of(work)
