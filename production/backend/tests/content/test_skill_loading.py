"""Skill discovery, route-specific disclosure and real content prompt integration."""
import json
from pathlib import Path

import pytest

from app.content import prompts
from app.content.errors import ContentBlocked


@pytest.fixture
def skill_root(tmp_path, monkeypatch):
    definitions = (("S01", "topic-planning"), ("S02", "content-writing"), ("S03", "content-review"))
    for skill_id, name in definitions:
        directory = tmp_path / name
        directory.mkdir()
        reference_metadata = ""
        if skill_id != "S01":
            references = directory / "references"
            references.mkdir()
            for route in ("talking-script", "storyboard"):
                (references / f"{route}.md").write_text(f"DETAIL-{skill_id}-{route}", encoding="utf-8")
            reference_metadata = (
                "  talking_script_reference: references/talking-script.md\n"
                "  storyboard_reference: references/storyboard.md\n"
            )
        (directory / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: Use {name} for its content stage.\n"
            f"metadata:\n  skill_id: {skill_id}\n  version: '0.4.0'\n{reference_metadata}"
            f"---\n\nBODY-{skill_id}\n", encoding="utf-8",
        )
    monkeypatch.setenv("AVATAR_STUDIO_SKILLS_DIR", str(tmp_path))
    return tmp_path


def test_discovery_exposes_metadata_without_body_or_loading_references(skill_root, monkeypatch):
    from app import skill_library

    reads = []
    original = Path.read_text

    def tracked(path, *args, **kwargs):
        reads.append(path)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", tracked)
    catalog = skill_library.discover_skills(["S01", "S02"])
    assert [item["name"] for item in catalog] == ["topic-planning", "content-writing"]
    assert catalog[1]["path"] == "/skills/content-writing/SKILL.md"
    assert "BODY-" not in str(catalog) and "DETAIL-" not in str(catalog)
    assert all(path.name == "SKILL.md" for path in reads)


@pytest.mark.parametrize("route,selected,excluded", [
    ("talking_script", "talking-script", "storyboard"),
    ("short_video_storyboard", "storyboard", "talking-script"),
])
def test_instructions_load_only_the_selected_route(skill_root, monkeypatch, route, selected, excluded):
    from app import skill_library

    reads = []
    original = Path.read_text

    def tracked(path, *args, **kwargs):
        reads.append(path)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", tracked)
    instructions = skill_library.skill_instructions(["S02"], route)
    assert "BODY-S02" in instructions and f"DETAIL-S02-{selected}" in instructions
    assert f"DETAIL-S02-{excluded}" not in instructions and "BODY-S03" not in instructions
    assert not any(path.name == f"{excluded}.md" for path in reads)


@pytest.mark.parametrize("stage,skill_id", [("planner", "S01"), ("writer", "S02"),
                                           ("repair", "S02"), ("reviewer", "S03")])
@pytest.mark.parametrize("route", ["talking_script", "short_video_storyboard"])
def test_actual_content_messages_include_only_stage_skills(skill_root, stage, skill_id, route):
    common = dict(brief={"topic": "t", "content_route": route}, reference=None, style=None, materials=[])
    if stage == "planner":
        system, user = prompts.planner_messages(**common)
    elif stage == "writer":
        system, user = prompts.writer_messages(**common, prompt={"prompt_text": "requirements"})
    elif stage == "repair":
        system, user = prompts.repair_messages(**common, prompt={"prompt_text": "requirements"}, targets=[])
    else:
        system, user = prompts.reviewer_messages(**common, candidates=[], review_ids=[])
    assert f"BODY-{skill_id}" in system
    assert all(f"BODY-{other}" not in system for other in ("S01", "S02", "S03") if other != skill_id)
    assert "BODY-" not in user
    if stage != "planner":
        selected = "storyboard" if route == "short_video_storyboard" else "talking-script"
        excluded = "talking-script" if route == "short_video_storyboard" else "storyboard"
        assert f"DETAIL-{skill_id}-{selected}" in system
        assert f"DETAIL-{skill_id}-{excluded}" not in system


def test_invalid_skill_is_a_clear_block_before_any_content_request(skill_root):
    (skill_root / "content-writing" / "SKILL.md").write_text("# No metadata", encoding="utf-8")
    with pytest.raises(ContentBlocked) as exc:
        prompts.writer_messages(brief={"topic": "t"}, prompt={"prompt_text": "p"},
                                reference=None, style=None, materials=[])
    assert exc.value.code == "SKILL_INVALID" and exc.value.retryable is False


def test_virtual_skill_files_are_limited_to_requested_skills(skill_root):
    from app import skill_library

    files = skill_library.skill_files(["S03"])
    assert set(files) == {"/skills/content-review/SKILL.md",
                          "/skills/content-review/references/talking-script.md",
                          "/skills/content-review/references/storyboard.md"}
    assert files["/skills/content-review/references/storyboard.md"] == "DETAIL-S03-storyboard"


def test_skill_digest_includes_reference_changes(skill_root):
    from app import skill_library

    original = skill_library.skill_digest("S02")
    (skill_root / "content-writing" / "references" / "storyboard.md").write_text("Updated rules", encoding="utf-8")
    assert skill_library.skill_digest("S02") != original
    assert skill_library.load_skill("S02").version == "0.4.0"


def test_reference_cannot_escape_the_skill_directory(skill_root):
    from app import skill_library

    entry = skill_root / "content-writing" / "SKILL.md"
    entry.write_text(entry.read_text(encoding="utf-8").replace("references/storyboard.md", "../../outside.md"),
                     encoding="utf-8")
    with pytest.raises(skill_library.SkillConfigurationError):
        skill_library.skill_instructions(["S02"], "short_video_storyboard")


def test_missing_selected_reference_blocks_instead_of_dropping_rules(skill_root):
    from app import skill_library

    (skill_root / "content-writing" / "references" / "storyboard.md").unlink()
    with pytest.raises(skill_library.SkillConfigurationError):
        skill_library.skill_instructions(["S02"], "short_video_storyboard")


@pytest.mark.parametrize("skill_id,directory", [("S02", "content-writing"), ("S03", "content-review")])
@pytest.mark.parametrize("route,filename", [("talking_script", "talking-script.md"),
                                           ("short_video_storyboard", "storyboard.md")])
def test_blank_selected_reference_is_invalid(skill_root, skill_id, directory, route, filename):
    from app import skill_library

    (skill_root / directory / "references" / filename).write_text(" \n\t", encoding="utf-8")
    with pytest.raises(skill_library.SkillConfigurationError):
        skill_library.skill_instructions([skill_id], route)


@pytest.mark.parametrize("direct", [False, True])
def test_normal_and_direct_generation_send_shared_rules_to_the_transport(skill_root, monkeypatch, direct):
    from app import engine
    from app.content import direct_generation, service, tasks
    from tests.content._helpers import Script, cand, make_work, review_reply, run_job, set_env

    set_env(monkeypatch)
    monkeypatch.setattr(engine, "schedule", lambda *_: pytest.fail("content generation invoked the agent engine"))
    work_id = make_work(with_reference=False)
    script = Script()
    script.q["writer"].append({"candidates": [cand(1), cand(2), cand(3)]})
    script.q["reviewer"].append(review_reply())
    messages = []

    def capture(url, headers, body, timeout):
        messages.append(json.loads(body)["messages"])
        return script(url, headers, body, timeout)

    if direct:
        service.register_handlers()
        monkeypatch.setattr("app.content.providers.text._default_transport", capture)
        accepted = direct_generation.submit(work_id, {}, legacy=True)
        completed = tasks.run_inline(accepted["id"], wait_seconds=5)
    else:
        prompt = service.ensure_prompt(work_id)
        completed = run_job("generation", work_id, {"prompt_id": prompt["id"]}, service.handle_generation, capture)
    assert completed["state"] == "succeeded", completed.get("error")
    assert script.calls == {"planner": 0, "writer": 1, "reviewer": 1}
    assert "BODY-S02" in messages[0][0]["content"] and "DETAIL-S02-talking-script" in messages[0][0]["content"]
    assert "BODY-S03" in messages[1][0]["content"] and "DETAIL-S03-talking-script" in messages[1][0]["content"]
    assert all("DETAIL-S02-storyboard" not in str(message) for message in messages)


@pytest.mark.parametrize("directory", ["content-writing", "content-review"])
def test_invalid_rules_never_create_a_provider_request(skill_root, monkeypatch, directory):
    from app import jobs
    from app.content import service
    from tests.content._helpers import Script, cand, make_work, run_job, set_env

    set_env(monkeypatch)
    work_id = make_work(with_reference=False)
    prompt = service.ensure_prompt(work_id)
    (skill_root / directory / "SKILL.md").write_text("# Missing metadata", encoding="utf-8")
    script = Script()
    script.q["writer"].append({"candidates": [cand(1), cand(2), cand(3)]})
    completed = run_job("generation", work_id, {"prompt_id": prompt["id"]}, service.handle_generation, script)
    assert completed["state"] == "blocked" and completed["error"]["code"] == "SKILL_INVALID"
    assert sum(script.calls.values()) == 0 and jobs.content_calls(completed["id"]) == []


def test_repair_prechecks_review_rules_before_charging_for_a_new_draft(skill_root, monkeypatch):
    from app import jobs, storage as st
    from app.content import service
    from tests.content._helpers import Script, cand, make_work, repair_reply, review_reply, run_job, set_env

    set_env(monkeypatch)
    work_id = make_work(with_reference=False)
    prompt = service.ensure_prompt(work_id)
    initial = Script()
    initial.q["writer"].append({"candidates": [cand(1), cand(2), cand(3)]})
    initial.q["reviewer"].append(review_reply())
    assert run_job("generation", work_id, {"prompt_id": prompt["id"]}, service.handle_generation, initial)["state"] == "succeeded"
    batch = st.get("script_batches", work_id)
    (skill_root / "content-review" / "SKILL.md").write_text("# Missing metadata", encoding="utf-8")
    rewrite = Script()
    rewrite.q["writer"].append(repair_reply(1))
    completed = run_job("repair", work_id,
                        {"batch_id": batch["id"], "candidate_ids": [batch["candidates"][0]["id"]], "instruction": "简化开头"},
                        service.handle_repair, rewrite)
    assert completed["state"] == "blocked" and completed["error"]["code"] == "SKILL_INVALID"
    assert sum(rewrite.calls.values()) == 0 and jobs.content_calls(completed["id"]) == []
