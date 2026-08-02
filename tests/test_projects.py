"""Project registry -- pure dict mutation, no disk I/O tested directly
(matches test_workspace.py's convention of not testing the thin
load/save file wrappers)."""

from schemantic.projects import (
    DEFAULT_PROJECT_ID,
    create_project,
    ensure_default_project,
    get_project,
    list_projects,
    set_last_active,
)


def _empty_registry() -> dict:
    return {"projects": [], "last_active_project_id": None}


def test_create_project_assigns_unique_prefixed_id():
    reg = _empty_registry()
    a = create_project(reg, "Robot A")
    b = create_project(reg, "Robot B")
    assert a["id"].startswith("proj_")
    assert b["id"].startswith("proj_")
    assert a["id"] != b["id"]
    assert [p["name"] for p in list_projects(reg)] == ["Robot A", "Robot B"]


def test_create_project_auto_names_when_omitted():
    reg = _empty_registry()
    create_project(reg, "Named")
    auto = create_project(reg, None)
    assert auto["name"] == "Project 2"


def test_get_project_returns_none_for_unknown_id():
    reg = _empty_registry()
    create_project(reg, "Only one")
    assert get_project(reg, "proj_doesnotexist") is None


def test_ensure_default_project_is_idempotent():
    reg = _empty_registry()
    first = ensure_default_project(reg)
    second = ensure_default_project(reg)
    assert first["id"] == DEFAULT_PROJECT_ID
    assert second["id"] == DEFAULT_PROJECT_ID
    assert len(reg["projects"]) == 1  # calling it twice didn't create a second default


def test_ensure_default_project_does_not_override_existing_projects():
    reg = _empty_registry()
    create_project(reg, "Already here")
    result = ensure_default_project(reg)
    # registry was non-empty -- ensure_default_project must not silently
    # inject a second "default" project alongside a real one
    assert len(reg["projects"]) == 1
    assert result["name"] == "Already here"


def test_set_last_active_mutates_in_place():
    reg = _empty_registry()
    p = create_project(reg, "X")
    set_last_active(reg, p["id"])
    assert reg["last_active_project_id"] == p["id"]
