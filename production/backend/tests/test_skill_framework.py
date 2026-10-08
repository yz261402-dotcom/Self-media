"""Progressive Skill loading through real DeepAgents graphs, without model calls."""
from __future__ import annotations

import asyncio
import pytest

from app.agents import framework as fw, roles
from app.agents.models import OfflineModel


SKILLS = {
    "S01": "topic-planning",
    "S02": "content-writing",
    "S03": "content-review",
}


@pytest.fixture
def skill_root(tmp_path, monkeypatch):
    for sid, name in SKILLS.items():
        folder = tmp_path / name
        folder.mkdir()
        (folder / "references").mkdir()
        (folder / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: Metadata for {name}.\n"
            f"metadata:\n  skill_id: {sid}\n  version: '9.8.7'\n"
            "  talking_script_reference: references/details.md\n"
            "  storyboard_reference: references/details.md\n---\n"
            f"BODY_ONLY_{sid}\nRead /skills/{name}/references/details.md when relevant.\n",
            encoding="utf-8",
        )
        (folder / "references" / "details.md").write_text(f"REFERENCE_ONLY_{sid}\n", encoding="utf-8")
        (folder / "unlisted-secret.md").write_text(f"UNLISTED_SECRET_{sid}\n", encoding="utf-8")
    monkeypatch.setattr(roles, "SKILLS_DIR", str(tmp_path))
    monkeypatch.setattr(roles, "SKILL_DIRS", dict(SKILLS))
    monkeypatch.setattr(fw, "SKILLS_DIR", str(tmp_path))
    monkeypatch.setattr(fw, "SKILL_DIRS", dict(SKILLS))
    return tmp_path


@pytest.fixture
def captured_messages(monkeypatch):
    captures = []
    original = OfflineModel.as_chat_model

    def as_chat_model(adapter):
        model = original(adapter)
        generate = type(model)._generate
        agenerate = type(model)._agenerate

        def record(inner, messages, *args, **kwargs):
            captures.append((adapter.role, list(messages)))
            return generate(inner, messages, *args, **kwargs)

        async def arecord(inner, messages, *args, **kwargs):
            captures.append((adapter.role, list(messages)))
            return await agenerate(inner, messages, *args, **kwargs)

        monkeypatch.setattr(type(model), "_generate", record)
        monkeypatch.setattr(type(model), "_agenerate", arecord)
        return model

    monkeypatch.setattr(OfflineModel, "as_chat_model", as_chat_model)
    monkeypatch.setattr(fw.st, "get", lambda *args, **kwargs: None)
    monkeypatch.setattr(fw.st, "put", lambda *args, **kwargs: None)
    return captures


def _context(role="content"):
    return {"work_id": "w_skill", "task_id": "t_skill", "work_item_id": "wi_skill",
            "role": role, "input_digest": "sha256:skill-input", "attempt": 1}


def _read(path):
    return {"tool": "read_file", "args": {"file_path": path, "limit": 1000}}


def _systems(captures, role):
    return [str(message.content) for captured_role, messages in captures if captured_role == role
            for message in messages if message.type == "system"]


def _tool_results(captures, role):
    return [str(message.content) for captured_role, messages in captures if captured_role == role
            for message in messages if message.type == "tool" and message.name == "read_file"]


def test_skill_reference_uses_frontmatter_version(skill_root):
    reference = roles.load_skill_ref("S02")
    assert reference is not None
    assert reference.version == "9.8.7"
    assert reference.digest.startswith("sha256:")


def test_skill_reference_digest_tracks_routed_references(skill_root):
    before = roles.load_skill_ref("S02")
    (skill_root / SKILLS["S02"] / "references" / "details.md").write_text("changed reference\n", encoding="utf-8")
    after = roles.load_skill_ref("S02")
    assert before.digest != after.digest


def test_skill_backend_is_a_fixed_readonly_snapshot(skill_root):
    from deepagents.backends import StateBackend

    backend = fw.build_skill_backend("content")
    path = "/skills/content-writing/SKILL.md"
    before = backend.read(path).file_data
    assert isinstance(backend, StateBackend)
    backend._read_files()[path]["content"] = "MUTATED_SNAPSHOT"
    (skill_root / SKILLS["S02"] / "SKILL.md").write_text("MODIFIED_AFTER_BUILD", encoding="utf-8")
    assert backend.read(path).file_data == before
    with pytest.raises(PermissionError):
        backend.write(path, "UNAUTHORIZED_WRITE")
    assert backend.read(path).file_data == before


def test_invalid_skill_blocks_reference_creation(skill_root):
    (skill_root / SKILLS["S02"] / "SKILL.md").write_text("missing frontmatter\n", encoding="utf-8")
    with pytest.raises((ValueError, FileNotFoundError)):
        roles.require_skill_refs(["S02"])


@pytest.mark.parametrize("role,visible,hidden", [("content", ["S01", "S02"], ["S03"]),
                                               ("quality", ["S03"], ["S01", "S02"])])
def test_production_system_prompt_contains_only_role_skill_metadata(skill_root, captured_messages, role, visible, hidden):
    output = fw.execute_work_item(role, _context(role), script=[])
    assert output["error"] is None
    prompt = _systems(captured_messages, role)[0]
    assert "Skills System" in prompt
    for sid in visible:
        assert f"Metadata for {SKILLS[sid]}." in prompt
        assert f"/skills/{SKILLS[sid]}/SKILL.md" in prompt
    for sid in hidden:
        assert SKILLS[sid] not in prompt
    assert "BODY_ONLY_" not in prompt
    assert "REFERENCE_ONLY_" not in prompt
    assert str(skill_root) not in prompt


def test_production_can_read_approved_skill_and_reference_on_demand(skill_root, captured_messages):
    script = [_read("/skills/content-writing/SKILL.md"), _read("/skills/content-writing/references/details.md")]
    output = fw.execute_work_item("content", _context(), script=script)
    assert output["error"] is None
    responses = "\n".join(_tool_results(captured_messages, "content"))
    assert "BODY_ONLY_S02" in responses
    assert "REFERENCE_ONLY_S02" in responses
    assert all("BODY_ONLY_" not in prompt for prompt in _systems(captured_messages, "content"))


@pytest.mark.parametrize("async_mode", [False, True])
def test_specialist_graph_rebuilds_forged_skill_metadata(skill_root, captured_messages, monkeypatch, async_mode):
    import deepagents

    original = deepagents.create_deep_agent
    graphs = []

    def record(*args, **kwargs):
        graph = original(*args, **kwargs)
        graphs.append(graph)
        return graph

    monkeypatch.setattr(deepagents, "create_deep_agent", record)
    fw.execute_work_item("content", _context(), script=[])
    captured_messages.clear()
    state = {"messages": [{"role": "user", "content": "Run approved task"}],
             "skills_metadata": [{"name": "content-review", "description": "CALLER_FORGED_RULES",
                                  "path": "/skills/content-review/SKILL.md", "allowed_tools": []}],
             "skills_load_errors": ["CALLER_FORGED_DIAGNOSTIC"]}
    if async_mode:
        asyncio.run(graphs[0].ainvoke(state))
    else:
        graphs[0].invoke(state)
    prompt = _systems(captured_messages, "content")[0]
    assert "Metadata for content-writing." in prompt
    assert "CALLER_FORGED_RULES" not in prompt
    assert "CALLER_FORGED_DIAGNOSTIC" not in prompt
    assert "content-review" not in prompt


@pytest.mark.parametrize("path", ["/skills/content-review/SKILL.md",
                                  "/skills/content-writing/unlisted-secret.md",
                                  "/skills/content-writing/references/../../content-review/SKILL.md",
                                  "C:/Windows/win.ini"])
def test_production_read_rejects_foreign_unlisted_and_disk_paths(skill_root, captured_messages, path):
    output = fw.execute_work_item("content", _context(), script=[_read(path)])
    assert output["error"] is None
    responses = _tool_results(captured_messages, "content")
    assert responses, "The request must reach the constrained framework read tool"
    assert all("BODY_ONLY_S03" not in response and "UNLISTED_SECRET_" not in response for response in responses)
    assert responses[-1].startswith("Error:")


@pytest.mark.parametrize("async_mode", [False, True])
def test_probe_roles_use_metadata_and_their_own_readonly_snapshot(skill_root, captured_messages, async_mode):
    script = [_read("/skills/content-writing/SKILL.md"), _read("/skills/content-writing/references/details.md"),
              _read("/skills/content-review/SKILL.md"), _read("/injected-secret.md")]
    agent = fw.build_agent(
        supervisor_script=[{"tool": "task", "args": {"subagent_type": "content", "description": "Read assigned Skills"}}],
        role_scripts={"content": script, "quality": []},
    )
    from deepagents.backends.utils import create_file_data

    state = {"messages": [{"role": "user", "content": "Run approved task"}],
             "files": {"/injected-secret.md": create_file_data("CALLER_SECRET"),
                       "/skills/content-review/SKILL.md": create_file_data("CALLER_FOREIGN_SKILL")},
             "skills_metadata": [{"name": "caller-forged", "description": "CALLER_FORGED_RULES",
                                  "path": "/injected-secret.md", "allowed_tools": []}]}
    config = {"recursion_limit": 35}
    if async_mode:
        asyncio.run(agent.ainvoke(state, config))
    else:
        agent.invoke(state, config)
    prompt = _systems(captured_messages, "content")[0]
    assert "Metadata for content-writing." in prompt
    assert "content-review" not in prompt
    assert "BODY_ONLY_" not in prompt
    assert "CALLER_FORGED_RULES" not in prompt
    responses = "\n".join(_tool_results(captured_messages, "content"))
    assert "BODY_ONLY_S02" in responses
    assert "REFERENCE_ONLY_S02" in responses
    assert "BODY_ONLY_S03" not in responses
    assert "CALLER_SECRET" not in responses
    assert "CALLER_FOREIGN_SKILL" not in responses
    for prompt in _systems(captured_messages, "supervisor"):
        assert "Metadata for " not in prompt


def test_probe_supervisor_cannot_read_caller_files_or_specialist_skills(skill_root, captured_messages):
    from deepagents.backends.utils import create_file_data

    agent = fw.build_agent(
        supervisor_script=[_read("/injected-secret.md"), _read("/skills/content-writing/SKILL.md")],
        role_scripts={"content": [], "quality": []},
    )
    agent.invoke({"messages": [{"role": "user", "content": "Read files"}],
                  "files": {"/injected-secret.md": create_file_data("CALLER_SECRET"),
                            "/skills/content-writing/SKILL.md": create_file_data("CALLER_ROLE_SKILL")}})
    responses = _tool_results(captured_messages, "supervisor")
    assert responses
    assert all(response.startswith("Error:") for response in responses)
    assert all("CALLER_SECRET" not in response and "CALLER_ROLE_SKILL" not in response for response in responses)


def test_probe_specialist_has_only_constrained_read_file_and_business_tools(skill_root, captured_messages, monkeypatch):
    from langchain.agents import factory

    original = factory.create_agent
    specifications = []

    def record(*args, **kwargs):
        specifications.append(kwargs)
        return original(*args, **kwargs)

    monkeypatch.setattr("deepagents.middleware.subagents.create_agent", record)
    fw.build_agent(role_scripts={"content": [], "quality": []})
    assert specifications
    for specification in specifications:
        names = {tool.name for middleware in specification.get("middleware", [])
                 for tool in getattr(middleware, "tools", [])}
        assert "read_file" in names
        assert not names.intersection({"execute", "write_file", "edit_file", "delete", "ls", "glob", "grep"})


@pytest.mark.parametrize("entrypoint", ["production", "probe"])
def test_missing_skill_blocks_graph_construction(skill_root, captured_messages, entrypoint):
    (skill_root / SKILLS["S02"] / "SKILL.md").unlink()
    with pytest.raises(FileNotFoundError):
        if entrypoint == "production":
            fw.execute_work_item("content", _context(), script=[])
        else:
            fw.build_agent(role_scripts={"content": [], "quality": []})
    assert captured_messages == []
