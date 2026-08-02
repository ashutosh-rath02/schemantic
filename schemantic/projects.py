"""Project registry: named, isolated containers of boards + workspace state.

Phase 1 of multi-tenant: one operator, multiple projects (like ChatGPT's
Projects), no user accounts yet. `DEFAULT_PROJECT_ID` is a fixed constant
rather than a random id specifically so the fresh-install bootstrap path
(ensure_default_project) and the legacy-data migration path (moving a
pre-existing single global workspace.json/chat_memory.db into "the default
project") can each independently target the same id without passing state
between them.

Persistence follows the same idiom as workspace.py: load the whole registry
dict, mutate it in place, save the whole thing back -- no locking, no
partial writes, consistent with every other piece of state this project
persists.
"""

from __future__ import annotations

import json
import time
import uuid

from schemantic.pipeline import CACHE_DIR

REGISTRY_PATH = CACHE_DIR / "projects.json"

DEFAULT_PROJECT_ID = "proj_default"
DEFAULT_PROJECT_NAME = "Default"


def load_registry() -> dict:
    if REGISTRY_PATH.exists():
        return json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    return {"projects": [], "last_active_project_id": None}


def save_registry(reg: dict) -> None:
    REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
    REGISTRY_PATH.write_text(json.dumps(reg, indent=2), encoding="utf-8")


def list_projects(reg: dict) -> list[dict]:
    return reg["projects"]


def get_project(reg: dict, project_id: str) -> dict | None:
    for p in reg["projects"]:
        if p["id"] == project_id:
            return p
    return None


def create_project(reg: dict, name: str | None = None) -> dict:
    """Mutates reg in place and returns the new entry -- same contract as
    workspace.py's add_board."""
    project = {
        "id": "proj_" + uuid.uuid4().hex[:12],
        "name": name or f"Project {len(reg['projects']) + 1}",
        "created_at": time.time(),
    }
    reg["projects"].append(project)
    return project


def set_last_active(reg: dict, project_id: str) -> None:
    reg["last_active_project_id"] = project_id


def ensure_default_project(reg: dict, name: str = DEFAULT_PROJECT_NAME) -> dict:
    """Idempotent: if the registry already has any project, this is a
    no-op that just returns the existing default (or the first project, if
    the fixed default id was somehow never created). Only creates
    DEFAULT_PROJECT_ID when the registry is genuinely empty -- this is the
    fresh-install / first-ever-visit path."""
    existing = get_project(reg, DEFAULT_PROJECT_ID)
    if existing is not None:
        return existing
    if reg["projects"]:
        return reg["projects"][0]
    project = {"id": DEFAULT_PROJECT_ID, "name": name, "created_at": time.time()}
    reg["projects"].append(project)
    return project
