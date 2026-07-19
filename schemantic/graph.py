"""Connectivity graph + drawable geometry, derived entirely from the parsed
netlist -- the "knowledge graph" layer between the parser and the canvas.

Everything here is deterministic. Wire routing uses a Euclidean minimum
spanning tree over each net's real pin coordinates: not the original drawn
wire path (the PDF's vector wires aren't reliably attributable to nets), but
a faithful topological rendering -- every segment connects two pins that ARE
electrically connected, and every pin on the net is reachable. That is the
honest contract: topology is exact, the specific line path is synthetic.

Power rails (GND, 3V3, 5V, VDD..., and anything with very high fanout) are
flagged so the canvas doesn't bury the signal wiring under a 146-pin ground
web -- they render only when explicitly selected.
"""

from __future__ import annotations

import math
import re

from schemantic.parser.models import Schematic

# Backstop for unnamed-but-huge nets; name-based match below catches the
# usual rails even at low fanout. Kept above typical bus sizes (this board's
# I2C bus alone has 12 pins) so real signal buses stay visible by default.
HIGH_FANOUT_PIN_THRESHOLD = 16

_POWER_NAME = re.compile(
    r"^(a?gnd|pgnd\d*|\d+v\d*.*|vdd.*|vcc.*|vin|vout.*|vbus.*|dc_in|pwr.*|power)$",
    re.IGNORECASE,
)


def is_power_net(names: list[str], pin_count: int) -> bool:
    return pin_count >= HIGH_FANOUT_PIN_THRESHOLD or any(
        _POWER_NAME.match(n) for n in names
    )


def _mst_edges(points: list[tuple[float, float]]) -> list[tuple[int, int]]:
    """Prim's algorithm, squared-distance weights. O(n^2) -- the largest net
    on the reference board is GND at 146 pins, which is trivial at n^2."""
    n = len(points)
    if n < 2:
        return []
    in_tree = [False] * n
    best_dist = [math.inf] * n
    best_parent = [-1] * n
    best_dist[0] = 0.0
    edges: list[tuple[int, int]] = []
    for _ in range(n):
        u = min((i for i in range(n) if not in_tree[i]), key=lambda i: best_dist[i])
        in_tree[u] = True
        if best_parent[u] >= 0:
            edges.append((best_parent[u], u))
        ux, uy = points[u]
        for v in range(n):
            if not in_tree[v]:
                d = (points[v][0] - ux) ** 2 + (points[v][1] - uy) ** 2
                if d < best_dist[v]:
                    best_dist[v] = d
                    best_parent[v] = u
    return edges


def build_graph(schematic: Schematic) -> tuple[dict, dict[str, str]]:
    """Returns (nets_payload, pin_token -> net_key map)."""
    pin_owner = {p.ref_token: (c, p) for c in schematic.components for p in c.pins}

    nets_payload: dict[str, dict] = {}
    pin_to_net: dict[str, str] = {}

    for net in schematic.nets:
        key = "/".join(net.names)
        pins = []
        for pt in net.pin_tokens:
            if pt not in pin_owner:
                continue
            component, pin = pin_owner[pt]
            pins.append(
                {
                    "component": component.ref_token,
                    "pin_id": pin.pin_id,
                    "x": pin.x,
                    "y": pin.y,
                }
            )
            pin_to_net[pt] = key

        points = [(p["x"], p["y"]) for p in pins]
        segments = [
            [points[i][0], points[i][1], points[j][0], points[j][1]]
            for i, j in _mst_edges(points)
        ]
        nets_payload[key] = {
            "names": net.names,
            "pins": pins,
            "segments": segments,
            "members": sorted({p["component"] for p in pins}),
            "is_power": is_power_net(net.names, len(pins)),
        }

    return nets_payload, pin_to_net


def component_box(component, pad: float = 4.0) -> dict:
    """Body rectangle from the bounding box of the component's real pin
    positions -- a 2-pin capacitor gets a small box, the 39-pin ESP32 a
    large one, matching physical intuition without any layout algorithm."""
    xs = [p.x for p in component.pins] or [component.x]
    ys = [p.y for p in component.pins] or [component.y]
    return {
        "x0": min(xs) - pad,
        "y0": min(ys) - pad,
        "x1": max(xs) + pad,
        "y1": max(ys) + pad,
    }
