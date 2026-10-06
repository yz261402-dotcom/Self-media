"""内容写作、审核与总控：职责、允许工具、Skill 绑定。

对应 10 文档 §2「角色配置与最小权限」：总控的全部权限不继承给角色；
角色只能调用白名单内的工具，写入边界由 tools.py 的执行器强制。
Skill 版本与摘要从 production/skills 下真实文件计算，文件缺失即阻断。
"""
from __future__ import annotations

import hashlib
import os
from typing import Dict, List, Optional

from .contracts import RoleId, SkillRef

SKILLS_DIR = os.environ.get(
    "AVATAR_STUDIO_SKILLS_DIR",
    os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "skills")),
)

# Skill ID → 目录名
SKILL_DIRS = {
    "S01": "选题与新意",
    "S02": "口播写作",
    "S03": "事实与重复审核",
}

SKILL_VERSION = "0.2.0"


def load_skill_ref(skill_id: str) -> Optional[SkillRef]:
    """从真实 SKILL.md 计算摘要；文件不存在返回 None（调用方必须阻断）。"""
    d = SKILL_DIRS.get(skill_id)
    if not d:
        return None
    path = os.path.join(SKILLS_DIR, d, "SKILL.md")
    if not os.path.exists(path):
        return None
    with open(path, "rb") as f:
        body = f.read()
    return SkillRef(skill_id=skill_id, version=SKILL_VERSION, digest="sha256:" + hashlib.sha256(body).hexdigest()[:32])


def require_skill_refs(ids: List[str]) -> List[SkillRef]:
    refs = []
    missing = []
    for i in ids:
        r = load_skill_ref(i)
        if r is None:
            missing.append(i)
        else:
            refs.append(r)
    if missing:
        raise FileNotFoundError(f"Skill 缺失或未定版：{', '.join(missing)}（目录 {SKILLS_DIR}）")
    return refs


class RoleSpec:
    def __init__(self, id: RoleId, title: str, duties: str, tools: List[str], skills: List[str],
                 write_boundary: str):
        self.id = id
        self.title = title
        self.duties = duties
        self.tools = tools
        self.skill_ids = skills
        self.write_boundary = write_boundary

    def system_prompt(self) -> str:
        return (
            f"你是 avatar_studio 的角色「{self.title}」（id={self.id}）。\n"
            f"职责：{self.duties}\n"
            f"写入边界：{self.write_boundary}\n"
            f"只允许调用这些工具：{', '.join(self.tools) or '（无）'}。\n"
            "禁止：代替用户做选择、修改他人已审原件、伪造证据或指标、声明已完成但未产出引用。"
        )


ROLES: Dict[str, RoleSpec] = {
    "supervisor": RoleSpec(
        id="supervisor", title="总控",
        duties="分解内容任务、委派写作与审核、读取报告；媒体与画面分析工具只准备受用户费用确认约束的意图",
        tools=["plan_work_items", "get_progress", "read_report", "search_douyin",
               "list_approved_assets", "get_presenter_profile", "compile_performance_plan", "compile_speech_expression",
               "submit_tts", "prepare_motion_assets", "submit_motion", "submit_lipsync",
               "submit_avatar_video", "get_media_job", "get_media_result", "analyze_reference_visual"],
        skills=[],
        write_boundary="经业务校验登记计划与汇总；付费媒体调用必须有匹配的用户费用确认",
    ),
    "content": RoleSpec(
        id="content", title="策划文案",
        duties="整理用户选择的 Redfox 参考，按口播稿或短视频分镜路线写三个候选；不联网搜索或抓取",
        tools=["register_script_candidates", "propose_memory"],
        skills=["S01", "S02"],
        write_boundary="只在自己的草稿空间写入；提交稿件新版本，不审核自己的稿件",
    ),
    "quality": RoleSpec(
        id="quality", title="质量审核",
        duties="独立审核三份内容候选，登记问题与返工目标；只审核内容",
        tools=["search_content_history", "submit_quality_report", "propose_memory"],
        skills=["S03"],
        write_boundary="只写受版本与规则校验的审核记录；不能改被审原件，不能清除硬门禁失败",
    ),
}

ROLE_IDS = list(ROLES.keys())


def role_tools(role: str) -> List[str]:
    spec = ROLES.get(role)
    return list(spec.tools) if spec else []


def role_system_prompt(role: str) -> str:
    spec = ROLES.get(role)
    return spec.system_prompt() if spec else ""
