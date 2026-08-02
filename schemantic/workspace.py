"""Multi-board workspace: which boards are loaded, and how they're linked.

Board-to-board connections are NOT in the schematics -- which connector
mates with which is a physical harness decision that exists in neither PDF.
So links follow the propose -> verify -> human-gate pattern used everywhere
else in this project: an agent proposes matings with pin-level evidence
(schemantic/agents/mate_proposer.py), every proposal is mechanically
validated against the real connectors before it's even stored, and only a
human click turns a proposal into a confirmed edge the graph will traverse.

Persistence is one JSON file per project, keyed by project id, with boards
inside it keyed by content-hash -- so a workspace survives restarts and is
immune to file renames. Before multi-project, there was exactly one such
file (LEGACY_WORKSPACE_PATH); it's kept around only as a migration source
(see schemantic/projects.py's docstring for why the default project id is
a fixed constant rather than random -- this is the other half of that).
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any

from schemantic.pipeline import CACHE_DIR, SCHEMA_VERSION, _pdf_hash

LEGACY_WORKSPACE_PATH = CACHE_DIR / "workspace.json"

MATE_STATUSES = ("proposed", "confirmed", "rejected")


def workspace_path_for(project_id: str) -> Path:
    return CACHE_DIR / "projects" / f"{project_id}.json"


def load_legacy_workspace() -> dict | None:
    """The pre-multi-project single global workspace file, if it exists --
    read only by the one-time migration in schemantic/web/app.py, never by
    normal request handling."""
    if LEGACY_WORKSPACE_PATH.exists():
        return json.loads(LEGACY_WORKSPACE_PATH.read_text(encoding="utf-8"))
    return None


def board_id_for(pdf_path: str) -> str:
    return _pdf_hash(pdf_path)


def board_name_for(pdf_path: str) -> str:
    stem = Path(pdf_path).stem
    # uploaded files are saved as <hash16>_<originalname>
    if len(stem) > 17 and stem[16] == "_" and all(c in "0123456789abcdef" for c in stem[:16]):
        stem = stem[17:]
    return stem


def load_workspace(project_id: str) -> dict:
    path = workspace_path_for(project_id)
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"boards": [], "mates": []}


def save_workspace(ws: dict, project_id: str) -> None:
    path = workspace_path_for(project_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(ws, indent=2), encoding="utf-8")


def add_board(ws: dict, pdf_path: str) -> dict:
    bid = board_id_for(pdf_path)
    for b in ws["boards"]:
        if b["id"] == bid:
            b["pdf_path"] = str(pdf_path)  # refresh path if file moved
            return ws
    ws["boards"].append(
        {"id": bid, "pdf_path": str(pdf_path), "name": board_name_for(pdf_path)}
    )
    return ws


def payload_cache_path(pdf_path: str) -> Path:
    return CACHE_DIR / f"{_pdf_hash(pdf_path)}_v{SCHEMA_VERSION}.json"


def load_board_payloads(ws: dict) -> dict[str, dict]:
    """board_id -> enriched payload, for boards whose cache exists. Boards
    with a missing cache are skipped, not errored -- the caller decides
    whether that matters."""
    payloads: dict[str, dict] = {}
    for board in ws["boards"]:
        cache = payload_cache_path(board["pdf_path"])
        if cache.exists():
            payloads[board["id"]] = json.loads(cache.read_text(encoding="utf-8"))
    return payloads


# ---- mates ----


def _connector(payload: dict, ref_token_or_label: str) -> dict | None:
    for c in payload["components"]:
        if ref_token_or_label in (c["ref_token"], c["display_label"], c["encoded_refdes"]):
            return c
    return None


def validate_proposal(
    proposal: dict, payload_a: dict, payload_b: dict
) -> tuple[bool, str]:
    """Mechanical gate before a proposal may be stored: both connectors must
    exist and every pin match must name real pins with their real nets. An
    agent proposal that fails this never reaches the human."""
    conn_a = _connector(payload_a, proposal["board_a_connector"])
    conn_b = _connector(payload_b, proposal["board_b_connector"])
    if conn_a is None:
        return False, f"connector {proposal['board_a_connector']!r} not on board A"
    if conn_b is None:
        return False, f"connector {proposal['board_b_connector']!r} not on board B"
    pins_a = {p["pin_id"]: p.get("net") for p in conn_a["pins"]}
    pins_b = {p["pin_id"]: p.get("net") for p in conn_b["pins"]}
    for match in proposal["pin_matches"]:
        if match["a_pin"] not in pins_a:
            return False, f"pin {match['a_pin']!r} not on {proposal['board_a_connector']}"
        if match["b_pin"] not in pins_b:
            return False, f"pin {match['b_pin']!r} not on {proposal['board_b_connector']}"
        if pins_a[match["a_pin"]] != match["a_net"]:
            return False, (
                f"pin {match['a_pin']} of {proposal['board_a_connector']} is on net "
                f"{pins_a[match['a_pin']]!r}, not {match['a_net']!r}"
            )
        if pins_b[match["b_pin"]] != match["b_net"]:
            return False, (
                f"pin {match['b_pin']} of {proposal['board_b_connector']} is on net "
                f"{pins_b[match['b_pin']]!r}, not {match['b_net']!r}"
            )
    return True, "ok"


def store_proposals(
    ws: dict,
    board_a_id: str,
    board_b_id: str,
    proposals: list[dict],
    payloads: dict[str, dict],
) -> tuple[int, int]:
    """Validate and store. Returns (stored, dropped_invalid)."""
    stored = dropped = 0
    for proposal in proposals:
        ok, reason = validate_proposal(
            proposal, payloads[board_a_id], payloads[board_b_id]
        )
        if not ok:
            dropped += 1
            print(f"mate proposal dropped (failed mechanical validation): {reason}")
            continue
        ws["mates"].append(
            {
                "id": uuid.uuid4().hex[:12],
                "board_a": board_a_id,
                "board_b": board_b_id,
                "board_a_connector": proposal["board_a_connector"],
                "board_b_connector": proposal["board_b_connector"],
                "pin_matches": proposal["pin_matches"],
                "rationale": proposal["reasoning"],
                "confidence": proposal["confidence"],
                "status": "proposed",
                "proposed_at": time.time(),
            }
        )
        stored += 1
    return stored, dropped


def set_mate_status(ws: dict, mate_id: str, status: str) -> bool:
    if status not in MATE_STATUSES:
        return False
    for mate in ws["mates"]:
        if mate["id"] == mate_id:
            mate["status"] = status
            mate["decided_at"] = time.time()
            return True
    return False


def confirmed_mates(ws: dict) -> list[dict]:
    return [m for m in ws["mates"] if m["status"] == "confirmed"]


def mates_between(ws: dict, *board_ids: str) -> list[dict[str, Any]]:
    wanted = set(board_ids)
    return [
        m for m in ws["mates"] if {m["board_a"], m["board_b"]} <= wanted or not wanted
    ]
