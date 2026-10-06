from app.agents.roles import ROLES, ROLE_IDS, SKILL_DIRS
from app import mcp
from app.agents import tools
from app import storage as st
import pytest


def test_active_agent_graph_has_only_content_specialists():
    assert set(ROLE_IDS) == {"supervisor", "content", "quality"}
    assert "editor" not in ROLES
    assert set(SKILL_DIRS) == {"S01", "S02", "S03"}


def test_v24_content_cannot_search_web_and_postprocess_is_not_agent_mcp():
    assert "search_sources" not in ROLES["content"].tools
    assert "fetch_source" not in ROLES["quality"].tools
    assert "align_subtitles" not in mcp.REGISTRY
    assert "render_video" not in mcp.REGISTRY
    assert "build_publish_package" not in mcp.REGISTRY
    assert "render" not in mcp.REGISTRY


def test_v24_quality_keeps_internal_history_search_only():
    assert "search_content_history" in ROLES["quality"].tools
    assert "search_content_history" not in ROLES["content"].tools


def test_removed_role_is_denied_and_audited_without_role_schema_crash():
    with pytest.raises(tools.ToolDenied) as caught:
        tools.invoke("editor", "align_subtitles", {}, {"work_item_id": "wi_removed_role"})
    assert caught.value.code == "ROLE_UNKNOWN"
    audit = st.all_rows("agent_tool_audit", "work_item_id=?", ["wi_removed_role"])
    assert audit and audit[-1]["outcome"] == "denied"
    assert audit[-1]["role"] == "editor"


@pytest.mark.parametrize("removed", ["search_sources", "fetch_source", "draft_edit_plan",
                                     "align_subtitles", "render_video", "build_publish_package"])
def test_removed_tools_cannot_be_reached_through_any_current_role(removed):
    for role in ROLE_IDS:
        with pytest.raises(tools.ToolDenied) as caught:
            tools.invoke(role, removed, {}, {"work_item_id": "wi_no_removed_tools"})
        assert caught.value.code == "TOOL_NOT_ALLOWED"
    assert removed not in tools.TOOLS


def test_visual_analysis_tool_is_supervisor_only_and_writer_reviewer_are_denied():
    assert "analyze_reference_visual" in ROLES["supervisor"].tools
    for role in ("content", "quality"):
        assert "analyze_reference_visual" not in ROLES[role].tools
        with pytest.raises(tools.ToolDenied) as caught:
            tools.invoke(role, "analyze_reference_visual", {"work_id": "w"}, {"work_item_id": "wi_visual_deny"})
        assert caught.value.code == "TOOL_NOT_ALLOWED"
