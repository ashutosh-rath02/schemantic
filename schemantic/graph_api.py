"""Deterministic query layer over the enriched schematic payload -- the
knowledge-graph tool API.

Every function here is a pure lookup/traversal over facts the parser proved
from the PDF; no AI anywhere in this module. This is the boundary that keeps
the chat agent honest: it can only answer through these tools, so every
claim it makes about connectivity is a claim this module computed from the
verified netlist, never something recalled from model weights.

All functions take the enriched payload dict (the /api/schematic shape) so
they're trivially testable against a cached board with no server running.
"""

from __future__ import annotations

from collections import deque
from typing import Any

Payload = dict[str, Any]

# Traversal cap: the deepest meaningful signal path on a single board is a
# handful of hops; anything longer is almost certainly a wrong route through
# a shared rail that slipped classification.
_MAX_PATH_HOPS = 12


def _components(payload: Payload) -> list[dict]:
    return payload["components"]


def _label(component: dict) -> str:
    return component["display_label"] or component["encoded_refdes"]


def _find_component(payload: Payload, query: str) -> dict | None:
    """Exact-ish match: ref_token, display label, or encoded refdes,
    case-insensitive. Returns None rather than guessing on ambiguity."""
    q = query.strip().lower()
    for c in _components(payload):
        if q in (
            c["ref_token"].lower(),
            (c["display_label"] or "").lower(),
            c["encoded_refdes"].lower(),
        ):
            return c
    return None


def _find_net(payload: Payload, query: str) -> tuple[str, dict] | None:
    q = query.strip().lower()
    for key, net in payload["nets"].items():
        if key.lower() == q or any(n.lower() == q for n in net["names"]):
            return key, net
    return None


# ---- tool functions (exposed to the chat agent) ----


def search_components(payload: Payload, query: str) -> list[dict]:
    """Substring search over labels, part numbers, and function text.
    Compact records, capped -- the agent should follow up with
    get_component for detail rather than receive everything at once."""
    q = query.strip().lower()
    hits = []
    for c in _components(payload):
        identity = c.get("identity") or {}
        haystacks = (
            _label(c).lower(),
            (identity.get("likely_part_number") or "").lower(),
            (identity.get("function") or "").lower(),
        )
        if any(q in h for h in haystacks):
            hits.append(
                {
                    "ref": _label(c),
                    "ref_token": c["ref_token"],
                    "part_number": identity.get("likely_part_number"),
                    "function": identity.get("function"),
                    "region": c["region"] or "unlabeled area",
                }
            )
        if len(hits) >= 12:
            break
    return hits


def get_component(payload: Payload, ref: str) -> dict:
    c = _find_component(payload, ref)
    if c is None:
        return {"error": f"no component matching {ref!r} on this board"}
    identity = c.get("identity") or {}
    return {
        "ref": _label(c),
        "ref_token": c["ref_token"],
        "region": c["region"] or "unlabeled area",
        "pin_count": c["pin_count"],
        "part_number": identity.get("likely_part_number"),
        "manufacturer": identity.get("manufacturer"),
        "package": identity.get("package_type"),
        "function": identity.get("function"),
        "identity_confidence": identity.get("confidence"),
        "identity_is_ai_guess": bool(identity.get("likely_part_number")),
        "pin_count_mismatch_flag": c.get("pin_count_mismatch", False),
        "signal_nets": [n["key"] for n in c["nets"] if not n["is_power"]],
        "power_rails": [n["key"] for n in c["nets"] if n["is_power"]],
    }


def get_connections(payload: Payload, ref: str) -> dict:
    """Everything electrically connected to a component, grouped by net,
    signal nets first. Power rails are listed but their (huge) peer lists
    are summarized as counts."""
    c = _find_component(payload, ref)
    if c is None:
        return {"error": f"no component matching {ref!r} on this board"}
    signal, power = [], []
    for n in c["nets"]:
        net = payload["nets"][n["key"]]
        peers = [
            _label(pc)
            for m in net["members"]
            if m != c["ref_token"]
            for pc in _components(payload)
            if pc["ref_token"] == m
        ]
        if n["is_power"]:
            power.append({"net": n["key"], "peer_count": len(peers)})
        else:
            signal.append({"net": n["key"], "aliases": net["names"], "peers": peers})
    return {"ref": _label(c), "ref_token": c["ref_token"], "signal_nets": signal, "power_rails": power}


def get_net(payload: Payload, name: str) -> dict:
    found = _find_net(payload, name)
    if found is None:
        return {"error": f"no net named {name!r} on this board"}
    key, net = found
    members = []
    by_token = {c["ref_token"]: c for c in _components(payload)}
    for m in net["members"]:
        c = by_token.get(m)
        if c:
            members.append(_label(c))
    return {
        "net_key": key,
        "aliases": net["names"],
        "is_power_rail": net["is_power"],
        "pin_count": len(net["pins"]),
        "members": members,
    }


def path_between(payload: Payload, ref_a: str, ref_b: str, include_power: bool = False) -> dict:
    """Shortest electrical path A -> net -> component -> net -> ... -> B via
    BFS. Power rails are excluded by default: everything connects through
    GND, so a path through it is true but tells you nothing."""
    a = _find_component(payload, ref_a)
    b = _find_component(payload, ref_b)
    if a is None or b is None:
        missing = ref_a if a is None else ref_b
        return {"error": f"no component matching {missing!r} on this board"}
    if a["ref_token"] == b["ref_token"]:
        return {"error": "same component on both ends"}

    nets = payload["nets"]
    comp_nets: dict[str, list[str]] = {
        c["ref_token"]: [
            n["key"] for n in c["nets"] if include_power or not n["is_power"]
        ]
        for c in _components(payload)
    }
    by_token = {c["ref_token"]: c for c in _components(payload)}

    queue: deque[tuple[str, list[str]]] = deque([(a["ref_token"], [_label(a)])])
    seen = {a["ref_token"]}
    while queue:
        token, path = queue.popleft()
        if len(path) > _MAX_PATH_HOPS * 2:
            break
        for net_key in comp_nets[token]:
            for member in nets[net_key]["members"]:
                if member in seen:
                    continue
                seen.add(member)
                next_path = path + [f"net:{net_key}", _label(by_token[member])]
                if member == b["ref_token"]:
                    return {
                        "found": True,
                        "hops": (len(next_path) - 1) // 2,
                        "path": next_path,
                        "power_rails_excluded": not include_power,
                    }
                queue.append((member, next_path))
    return {
        "found": False,
        "power_rails_excluded": not include_power,
        "note": "no signal path; they may only share power rails "
        "(retry with include_power=true to confirm)",
    }


def get_datasheet(payload: Payload, ref: str) -> dict:
    """Verified, page-cited facts from the part's fetched datasheet. Every
    fact's quote was mechanically checked against the cited page; facts that
    failed that check were dropped before this data was stored."""
    c = _find_component(payload, ref)
    if c is None:
        return {"error": f"no component matching {ref!r} on this board"}
    sheet = c.get("datasheet")
    if not sheet:
        identity = c.get("identity") or {}
        return {
            "ref": _label(c),
            "no_datasheet": True,
            "note": "no datasheet was found/fetched for this part",
            "part_number": identity.get("likely_part_number"),
        }
    return {"ref": _label(c), **sheet}


def list_regions(payload: Payload) -> list[dict]:
    counts: dict[str | None, int] = {}
    for c in _components(payload):
        counts[c["region"]] = counts.get(c["region"], 0) + 1
    out = [
        {
            "region": name,
            "component_count": counts.get(name, 0),
            "explanation": info.get("explanation"),
        }
        for name, info in payload["regions"].items()
    ]
    if counts.get(None):
        out.append(
            {
                "region": "unlabeled area",
                "component_count": counts[None],
                "explanation": "The schematic draws no section title near these components.",
            }
        )
    return out


def board_summary(payload: Payload) -> dict:
    from pathlib import Path

    return {
        "source_file": Path(payload["source_file"]).name,
        "component_count": len(payload["components"]),
        "net_count": len(payload["nets"]),
        "region_count": len(payload["regions"]),
    }


# ---- workspace-level (multi-board) queries ----
#
# Cross-board edges come ONLY from human-confirmed mates: a confirmed mate's
# pin matches identify "net X on board A is electrically continuous with
# net Y on board B". Proposed-but-unconfirmed mates are visible as data but
# never traversed.


def search_all_boards(
    payloads: dict[str, Payload], board_names: dict[str, str], query: str
) -> list[dict]:
    hits = []
    for board_id, payload in payloads.items():
        for hit in search_components(payload, query):
            hit["board"] = board_names.get(board_id, board_id)
            hit["board_id"] = board_id
            hits.append(hit)
        if len(hits) >= 16:
            break
    return hits[:16]


def list_board_links(ws_mates: list[dict], board_names: dict[str, str]) -> list[dict]:
    return [
        {
            "id": m["id"],
            "status": m["status"],
            "link": f"{board_names.get(m['board_a'], m['board_a'])}:{m['board_a_connector']}"
            f" <-> {board_names.get(m['board_b'], m['board_b'])}:{m['board_b_connector']}",
            "pin_matches": len(m["pin_matches"]),
            "confidence": m.get("confidence"),
            "rationale": m["rationale"],
        }
        for m in ws_mates
    ]


def cross_board_path(
    payloads: dict[str, Payload],
    board_names: dict[str, str],
    confirmed: list[dict],
    ref_a: str,
    board_a: str,
    ref_b: str,
    board_b: str,
) -> dict:
    """BFS over the union graph: components <-> nets within each board, plus
    zero-cost net-to-net bridges from confirmed mates' pin matches. Node
    namespace is '<board_id>/<token-or-netkey>'. Power rails excluded, same
    rationale as single-board path_between."""

    def resolve_board(name_or_id: str) -> str | None:
        for bid, bname in board_names.items():
            if name_or_id in (bid, bname):
                return bid
        # fuzzy: substring of name
        for bid, bname in board_names.items():
            if name_or_id.lower() in bname.lower():
                return bid
        return None

    bid_a, bid_b = resolve_board(board_a), resolve_board(board_b)
    if bid_a is None or bid_b is None:
        return {"error": f"unknown board {(board_a if bid_a is None else board_b)!r}"}
    comp_a = _find_component(payloads[bid_a], ref_a)
    comp_b = _find_component(payloads[bid_b], ref_b)
    if comp_a is None or comp_b is None:
        missing = ref_a if comp_a is None else ref_b
        return {"error": f"no component matching {missing!r}"}

    # adjacency: comp-node <-> net-node within boards
    adjacency: dict[str, set[str]] = {}

    def link(u: str, v: str) -> None:
        adjacency.setdefault(u, set()).add(v)
        adjacency.setdefault(v, set()).add(u)

    labels: dict[str, str] = {}
    for bid, payload in payloads.items():
        bname = board_names.get(bid, bid)
        for c in payload["components"]:
            node = f"{bid}/{c['ref_token']}"
            labels[node] = f"{_label(c)}@{bname}"
            for n in c["nets"]:
                if n["is_power"]:
                    continue
                net_node = f"{bid}/net:{n['key']}"
                labels[net_node] = f"net {n['key']}"
                link(node, net_node)

    bridges = 0
    for mate in confirmed:
        for match in mate["pin_matches"]:
            u = f"{mate['board_a']}/net:{match['a_net']}"
            v = f"{mate['board_b']}/net:{match['b_net']}"
            if u in adjacency and v in adjacency:
                labels_bridge = (
                    f"[{mate['board_a_connector']}<->{mate['board_b_connector']} link]"
                )
                # bridge via a synthetic node so the path names the mate
                bridge_node = f"mate:{mate['id']}:{match['a_net']}"
                labels[bridge_node] = labels_bridge
                link(u, bridge_node)
                link(bridge_node, v)
                bridges += 1

    start = f"{bid_a}/{comp_a['ref_token']}"
    goal = f"{bid_b}/{comp_b['ref_token']}"
    queue: deque[tuple[str, list[str]]] = deque([(start, [labels[start]])])
    seen = {start}
    while queue:
        node, path = queue.popleft()
        if len(path) > _MAX_PATH_HOPS * 3:
            break
        for neighbor in adjacency.get(node, ()):
            if neighbor in seen:
                continue
            seen.add(neighbor)
            next_path = path + [labels.get(neighbor, neighbor)]
            if neighbor == goal:
                return {"found": True, "path": next_path, "mate_bridges_available": bridges}
            queue.append((neighbor, next_path))
    return {
        "found": False,
        "mate_bridges_available": bridges,
        "note": "no signal path via confirmed board links"
        + ("" if bridges else " -- no confirmed links exist between these boards yet"),
    }
