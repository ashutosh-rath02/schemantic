"""KiCad native ingestion: netlist (.net) + PCB placement (.kicad_pcb).

KiCad's PDF export embeds no netlist (verified against a real Eeschema
export), but its source files are plain s-expressions -- so the honest
route to KiCad support is parsing the sources, not computer vision:

- `.net` (exported netlist): authoritative connectivity -- every component
  (ref, value, footprint) and every net with its (ref, pin) nodes. This is
  the same trust level as the Altium token layer: written by the EDA tool,
  parsed mechanically.
- `.kicad_pcb` (board layout): real physical placement -- module positions
  plus per-pad offsets give actual pin coordinates on the board. When the
  PCB file is absent, components fall back to a synthetic grid; topology is
  unaffected either way.

Component values from the netlist ("ESP32-WROOM-32E", "10k") feed the same
identity/datasheet pipeline the PDF path uses.
"""

from __future__ import annotations

import math
from pathlib import Path

from schemantic.parser.models import Component, Net, Pin, Schematic

CANVAS_WIDTH = 842.0  # match the PDF path's coordinate scale so the canvas
CANVAS_MARGIN = 30.0  # and zoom thresholds behave identically


# ---- minimal s-expression reader ----


def parse_sexpr(text: str):
    """Returns nested lists of str. Quoted strings kept as single atoms."""
    tokens: list[str] = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch in "()":
            tokens.append(ch)
            i += 1
        elif ch == '"':
            j = i + 1
            buf = []
            while j < n and text[j] != '"':
                if text[j] == "\\" and j + 1 < n:
                    buf.append(text[j + 1])
                    j += 2
                else:
                    buf.append(text[j])
                    j += 1
            tokens.append("".join(buf))
            i = j + 1
        elif ch.isspace():
            i += 1
        else:
            j = i
            while j < n and not text[j].isspace() and text[j] not in '()"':
                j += 1
            tokens.append(text[i:j])
            i = j

    pos = 0

    def read():
        nonlocal pos
        token = tokens[pos]
        pos += 1
        if token != "(":
            return token
        out = []
        while pos < len(tokens) and tokens[pos] != ")":
            out.append(read())
        pos += 1  # consume ")"
        return out

    return read()


def _children(node: list, tag: str) -> list[list]:
    return [c for c in node if isinstance(c, list) and c and c[0] == tag]


def _child(node: list, tag: str) -> list | None:
    found = _children(node, tag)
    return found[0] if found else None


def _atom(node: list | None, index: int = 1) -> str | None:
    if node is None or len(node) <= index:
        return None
    value = node[index]
    return value if isinstance(value, str) else None


# ---- netlist (.net) ----


def parse_netlist(text: str) -> tuple[list[dict], list[dict]]:
    """-> (components [{ref, value, footprint}], nets [{name, nodes:[(ref,pin)]}])"""
    root = parse_sexpr(text)
    if not (isinstance(root, list) and root and root[0] == "export"):
        raise ValueError(
            "not a KiCad netlist export -- expected an (export ...) s-expression. "
            "In KiCad: File > Export > Netlist."
        )

    components = []
    comps_node = _child(root, "components") or []
    for comp in _children(comps_node, "comp"):
        components.append(
            {
                "ref": _atom(_child(comp, "ref")),
                "value": _atom(_child(comp, "value")),
                "footprint": _atom(_child(comp, "footprint")),
            }
        )

    nets = []
    nets_node = _child(root, "nets") or []
    for net in _children(nets_node, "net"):
        name = _atom(_child(net, "name")) or ""
        nodes = [
            (_atom(_child(node, "ref")), _atom(_child(node, "pin")))
            for node in _children(net, "node")
        ]
        nets.append({"name": name, "nodes": [(r, p) for r, p in nodes if r and p]})

    if len(components) < 3 or not nets:
        raise ValueError("netlist parsed but looks empty -- is this a complete export?")
    return components, nets


# ---- PCB placement (.kicad_pcb) ----


def parse_pcb_positions(text: str) -> dict[str, dict]:
    """ref -> {x, y, rotation, pads: {pad_number: (abs_x, abs_y)}} in mm.
    Pad offsets are rotated by the module angle; a sign error here would
    only distort the drawing, never the connectivity."""
    root = parse_sexpr(text)
    positions: dict[str, dict] = {}
    for module in _children(root, "module") + _children(root, "footprint"):
        at = _child(module, "at")
        if at is None:
            continue
        try:
            mod_x, mod_y = float(at[1]), float(at[2])
            mod_rot = float(at[3]) if len(at) > 3 else 0.0
        except (ValueError, IndexError):
            continue
        ref = None
        for fp_text in _children(module, "fp_text"):
            if _atom(fp_text, 1) == "reference":
                ref = _atom(fp_text, 2)
                break
        if ref is None:
            prop = next(
                (p for p in _children(module, "property") if _atom(p, 1) == "Reference"),
                None,
            )
            ref = _atom(prop, 2) if prop else None
        if ref is None:
            continue
        rad = math.radians(mod_rot)
        cos_r, sin_r = math.cos(rad), math.sin(rad)
        pads: dict[str, tuple[float, float]] = {}
        for pad in _children(module, "pad"):
            number = _atom(pad, 1)
            pad_at = _child(pad, "at")
            if number is None or pad_at is None:
                continue
            try:
                dx, dy = float(pad_at[1]), float(pad_at[2])
            except (ValueError, IndexError):
                continue
            # KiCad y grows downward; module rotation is CCW
            abs_x = mod_x + dx * cos_r + dy * sin_r
            abs_y = mod_y - dx * sin_r + dy * cos_r
            pads[number] = (abs_x, abs_y)
        positions[ref] = {"x": mod_x, "y": mod_y, "rotation": mod_rot, "pads": pads}
    return positions


# ---- assembly into the shared Schematic model ----


def build_schematic(
    net_text: str, pcb_text: str | None, source_name: str
) -> Schematic:
    components_raw, nets_raw = parse_netlist(net_text)
    positions = parse_pcb_positions(pcb_text) if pcb_text else {}

    # coordinate normalization: mm -> canvas units. Page dimensions follow
    # the BOARD's aspect ratio -- a fixed landscape page overflowed on a
    # portrait board (caught by the coordinate-bounds test).
    if positions:
        xs = [p["x"] for p in positions.values()]
        ys = [p["y"] for p in positions.values()]
        min_x, max_x = min(xs), max(xs)
        min_y, max_y = min(ys), max(ys)
        span = max(max_x - min_x, max_y - min_y, 1.0)
        scale = (CANVAS_WIDTH - 2 * CANVAS_MARGIN) / span
        page_width = 2 * CANVAS_MARGIN + (max_x - min_x) * scale
        page_height = 2 * CANVAS_MARGIN + (max_y - min_y) * scale

        def to_canvas(x: float, y: float) -> tuple[float, float]:
            return (
                CANVAS_MARGIN + (x - min_x) * scale,
                CANVAS_MARGIN + (y - min_y) * scale,
            )
    else:
        page_width = CANVAS_WIDTH
        page_height = CANVAS_WIDTH * 0.75

        def to_canvas(x: float, y: float) -> tuple[float, float]:
            return x, y

    components: list[Component] = []
    pin_tokens: dict[tuple[str, str], str] = {}
    for index, raw in enumerate(components_raw):
        ref = raw["ref"]
        placement = positions.get(ref)
        if placement:
            cx, cy = to_canvas(placement["x"], placement["y"])
        else:
            # synthetic grid for unplaced components (or no PCB file at all)
            cx = CANVAS_MARGIN + (index % 14) * 58.0
            cy = CANVAS_MARGIN + (index // 14) * 44.0

        pin_numbers = sorted(
            {p for net in nets_raw for r, p in net["nodes"] if r == ref},
            key=lambda s: (len(s), s),
        )
        pins = []
        for pin_index, pin_number in enumerate(pin_numbers):
            token = f"PI{ref}#{pin_number}"
            pin_tokens[(ref, pin_number)] = token
            pad = (placement or {}).get("pads", {}).get(pin_number)
            if pad:
                px, py = to_canvas(*pad)
            else:  # fan pins in rows beside the body
                px = cx - 6 + 4.0 * (pin_index % 4)
                py = cy + 4.0 * (pin_index // 4)
            pins.append(Pin(ref_token=token, pin_id=pin_number, x=px, y=py))

        value = raw.get("value") or ""
        component = Component(
            ref_token=f"CO{ref}",
            encoded_refdes=ref,
            display_label=ref,
            x=cx,
            y=cy,
            pins=pins,
            region=None,  # KiCad netlists carry no drawn section titles
            nearby_text=[t for t in (value, raw.get("footprint")) if t][:12],
        )
        component.nearest_value = value if value else None
        components.append(component)

    nets: list[Net] = []
    for raw_net in nets_raw:
        tokens = [
            pin_tokens[(r, p)] for r, p in raw_net["nodes"] if (r, p) in pin_tokens
        ]
        if not tokens:
            continue
        # KiCad net names like "/I2C_SDA" or "Net-(C1-Pad1)" -- strip the
        # leading slash; auto-named nets keep their generated name
        name = raw_net["name"].lstrip("/") or "unnamed"
        nets.append(Net(names=[name], pin_tokens=tokens))

    return Schematic(
        source_file=source_name,
        page_width=page_width,
        page_height=page_height,
        components=components,
        nets=nets,
    )


def load_kicad_project(paths: list[Path], source_name: str) -> Schematic:
    """paths: files from an upload (zip contents or single .net). Picks the
    .net (required) and .kicad_pcb (optional)."""
    net_path = next((p for p in paths if p.suffix.lower() == ".net"), None)
    pcb_path = next((p for p in paths if p.suffix.lower() == ".kicad_pcb"), None)
    if net_path is None:
        raise ValueError(
            "no .net file found -- export one from KiCad (File > Export > Netlist) "
            "and include it; add the .kicad_pcb too for real board placement"
        )
    net_text = net_path.read_text(encoding="utf-8", errors="replace")
    pcb_text = pcb_path.read_text(encoding="utf-8", errors="replace") if pcb_path else None
    return build_schematic(net_text, pcb_text, source_name)
