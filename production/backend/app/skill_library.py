"""Validated business skills shared by agent discovery and direct content calls."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path, PurePosixPath
import re
from typing import Any, Dict, List, Optional, Sequence, Union

import yaml


SKILL_DIRS = {"S01": "topic-planning", "S02": "content-writing", "S03": "content-review"}
ROUTE_REFERENCES = {"talking_script": "talking_script_reference",
                    "short_video_storyboard": "storyboard_reference"}
_FRONTMATTER = re.compile(r"\A---[ \t]*\n(.*?)\n---[ \t]*(?:\n|\Z)", re.DOTALL)


class SkillConfigurationError(ValueError):
    """An unavailable or invalid skill must block before a paid model call."""


@dataclass(frozen=True)
class Skill:
    skill_id: str
    name: str
    description: str
    version: str
    path: Path
    body: str
    metadata: Dict[str, str]
    text: str
    license: Optional[str] = None
    compatibility: Optional[str] = None


def get_skills_dir() -> Path:
    configured = os.environ.get("AVATAR_STUDIO_SKILLS_DIR", "").strip()
    return Path(configured).resolve() if configured else Path(__file__).resolve().parents[2] / "skills"


def _read(path: Path, skill_id: str) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise SkillConfigurationError(f"Skill {skill_id} 的规则文件缺失或无法读取：{path.name}") from exc


def load_skill(skill_id: str, *, root: Optional[Union[str, Path]] = None) -> Skill:
    directory = SKILL_DIRS.get(skill_id)
    if directory is None:
        raise SkillConfigurationError(f"未登记的 Skill：{skill_id}")
    base = Path(root).resolve() if root is not None else get_skills_dir()
    path = (base / directory / "SKILL.md").resolve()
    if not path.is_relative_to(base):
        raise SkillConfigurationError(f"Skill {skill_id} 的路径超出规则目录")
    text = _read(path, skill_id)
    match = _FRONTMATTER.match(text)
    if match is None:
        raise SkillConfigurationError(f"Skill {skill_id} 缺少 YAML name/description 元数据")
    try:
        header = yaml.safe_load(match.group(1))
    except yaml.YAMLError as exc:
        raise SkillConfigurationError(f"Skill {skill_id} 的 YAML 元数据无效") from exc
    if not isinstance(header, dict):
        raise SkillConfigurationError(f"Skill {skill_id} 的 YAML 元数据必须是对象")
    name, description, metadata = header.get("name"), header.get("description"), header.get("metadata")
    if (not isinstance(name, str) or len(name) > 64 or name != directory
            or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name)):
        raise SkillConfigurationError(f"Skill {skill_id} 的 name 必须与标准英文目录名一致")
    if not isinstance(description, str) or not description.strip() or len(description) > 1024:
        raise SkillConfigurationError(f"Skill {skill_id} 缺少有效的 description")
    if (not isinstance(metadata, dict) or metadata.get("skill_id") != skill_id
            or any(not isinstance(k, str) or not isinstance(v, str) for k, v in metadata.items())):
        raise SkillConfigurationError(f"Skill {skill_id} 的 metadata 必须包含匹配的编号及字符串字段")
    version = metadata.get("version", "")
    if not re.fullmatch(r"\d+\.\d+\.\d+(?:[-+][a-zA-Z0-9.-]+)?", version):
        raise SkillConfigurationError(f"Skill {skill_id} 缺少有效的 metadata.version")
    body = text[match.end():].strip()
    if not body:
        raise SkillConfigurationError(f"Skill {skill_id} 的规则正文为空")
    if skill_id in ("S02", "S03") and any(not metadata.get(key) for key in ROUTE_REFERENCES.values()):
        raise SkillConfigurationError(f"Skill {skill_id} 缺少口播或分镜参考文件声明")
    return Skill(skill_id, name, description.strip(), version, path, body, metadata, text,
                 header.get("license"), header.get("compatibility"))


def discover_skills(skill_ids: Sequence[str], *, root: Optional[Union[str, Path]] = None) -> List[Dict[str, Any]]:
    """Discovery returns metadata, without reading route-specific references."""
    return [{"name": skill.name, "description": skill.description,
             "path": f"/skills/{skill.name}/SKILL.md", "metadata": dict(skill.metadata),
             "license": skill.license, "compatibility": skill.compatibility, "allowed_tools": []}
            for skill in (load_skill(skill_id, root=root) for skill_id in skill_ids)]


def _reference(skill: Skill, relative: str) -> str:
    virtual = PurePosixPath(relative)
    if ("\\" in relative or virtual.is_absolute() or ".." in virtual.parts
            or not virtual.parts or virtual.parts[0] != "references"):
        raise SkillConfigurationError(f"Skill {skill.skill_id} 的参考路径必须位于 references 内")
    path = (skill.path.parent / relative).resolve()
    if not path.is_relative_to(skill.path.parent / "references"):
        raise SkillConfigurationError(f"Skill {skill.skill_id} 的参考路径超出规则目录")
    text = _read(path, skill.skill_id)
    if not text.strip():
        raise SkillConfigurationError(f"Skill {skill.skill_id} 的参考规则为空：{path.name}")
    return text


def skill_instructions(skill_ids: Sequence[str], content_route: str = "talking_script", *,
                       root: Optional[Union[str, Path]] = None) -> str:
    reference_key = ROUTE_REFERENCES.get(content_route)
    if reference_key is None:
        raise SkillConfigurationError("Skill 内容路线必须为口播稿或分镜剧本")
    parts = []
    for skill_id in skill_ids:
        skill = load_skill(skill_id, root=root)
        parts.append(f"[Skill {skill.skill_id}: {skill.name} v{skill.version}]\n{skill.body}")
        relative = skill.metadata.get(reference_key)
        if relative:
            parts.append(f"[Skill {skill.skill_id} reference: {relative}]\n{_reference(skill, relative)}")
    return "\n\n".join(parts)


def skill_files(skill_ids: Sequence[str], *, root: Optional[Union[str, Path]] = None) -> Dict[str, str]:
    """Only registered files enter the read-only virtual agent namespace."""
    files = {}
    for skill_id in skill_ids:
        skill = load_skill(skill_id, root=root)
        prefix = f"/skills/{skill.name}"
        files[f"{prefix}/SKILL.md"] = skill.text
        for key in ROUTE_REFERENCES.values():
            relative = skill.metadata.get(key)
            if relative:
                files[f"{prefix}/{relative}"] = _reference(skill, relative)
    return files


def skill_digest(skill_id: str, *, root: Optional[Union[str, Path]] = None) -> str:
    digest = hashlib.sha256()
    for path, text in sorted(skill_files([skill_id], root=root).items()):
        digest.update(path.encode("utf-8") + b"\0" + text.encode("utf-8") + b"\0")
    return "sha256:" + digest.hexdigest()[:32]
