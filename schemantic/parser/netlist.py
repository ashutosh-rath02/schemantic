"""Deterministic netlist extraction from an Altium-exported schematic PDF.

Altium Designer's PDF export (confirmed via PDF creator metadata on every
token-carrying sample; originally misattributed to EasyEDA before checking)
embeds invisible text objects at every pin and component location,
positioned exactly on top of the visible drawing, for interactive
net-highlighting in a PDF viewer. Three token families, in a single
reading-order pass:

  PI<refdes><pin>   -- a specific pin instance, e.g. "PIC101" = C1 pin 01
  CO<refdes>        -- a component reference, closes a run of PI tokens
                        into "these pins belong to this component"
  NL<netname>       -- a net name, closes a run of PI tokens into "these
                        pins are electrically connected". A net can have
                        multiple consecutive NL tokens (aliases on the same
                        wire, e.g. IO14 / SPI_CK).

Nothing here is inferred -- every component/pin/net relationship is read
directly from tokens embedded by the EDA tool. Verified unsupported (no
tokens present): KiCad's Eeschema PDF export, and image-based PDFs (e.g. a
schematic screenshot pasted into a document). Those are rejected with an
explicit error naming the PDF's creator tool.
"""

from __future__ import annotations

import re

import fitz

from schemantic.parser.models import Component, Net, Pin, Schematic
from schemantic.parser.regions import assign_regions, extract_region_titles, extract_walls
from schemantic.parser.values import looks_like_value

_LABEL_MAX_LEN = 20  # visible spans longer than this are prose, not a refdes

# Real tokens are alnum plus '#' (NL#EN, an active-low enable net) and "'"
# (NLMA1', motor-output primes) -- full charset enumerated across both
# reference boards. Two things this pattern fixes at once:
#   1. A visible KiCad signal label like "COL/CRS_DV/MODE2" starts with "CO"
#      but contains '/', and must not be mistaken for a component token (it
#      was: garbage one-component parse slipping past the rejection check).
#   2. The OLD check ("third char isalnum") silently REJECTED NL#EN, so that
#      net's pins were merged into whichever net came next in reading order
#      -- a real, latent mis-grouping present since the first version, found
#      only by diffing token classifications while fixing (1).
_TOKEN_PATTERN = re.compile(r"^(PI|CO|NL)[A-Za-z0-9#'][A-Za-z0-9#'\-]*$")


def _is_netlist_token(text: str) -> bool:
    return bool(_TOKEN_PATTERN.match(text))


def parse_schematic_pdf(path: str) -> Schematic:
    doc = fitz.open(path)
    if doc.page_count != 1:
        raise ValueError(f"expected a single-page schematic export, got {doc.page_count} pages")
    page = doc[0]
    words = page.get_text("words")  # (x0, y0, x1, y1, text, block_no, line_no, word_no)

    netlist_tokens: list[tuple[str, float, float]] = []
    visible_labels: list[tuple[str, float, float]] = []
    for x0, y0, x1, y1, text, *_ in words:
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        if _is_netlist_token(text):
            netlist_tokens.append((text, cx, cy))
        elif len(text) <= _LABEL_MAX_LEN:
            visible_labels.append((text, cx, cy))

    components, nets = _parse_tokens(netlist_tokens)
    # Structural floor, not just non-emptiness: a stray label that survives
    # token filtering must not be accepted as a one-component "schematic".
    total_pins = sum(len(c.pins) for c in components)
    if len(components) < 3 or not nets or total_pins < 6:
        creator = (doc.metadata or {}).get("creator") or "unknown tool"
        raise ValueError(
            "no embedded netlist tokens found -- this PDF (created by "
            f"{creator!r}) doesn't carry Altium Designer's interactive-PDF "
            "netlist layer. KiCad/Eagle exports and image-based PDFs aren't "
            "parseable this way."
        )
    _attach_display_labels(components, visible_labels)
    _attach_nearby_text(components, visible_labels)
    assign_regions(components, extract_region_titles(path), extract_walls(path))

    return Schematic(
        source_file=path,
        page_width=page.rect.width,
        page_height=page.rect.height,
        components=components,
        nets=nets,
    )


def _parse_tokens(
    tokens: list[tuple[str, float, float]],
) -> tuple[list[Component], list[Net]]:
    components: list[Component] = []
    nets: list[Net] = []
    pending_pins: list[Pin] = []

    i = 0
    n = len(tokens)
    while i < n:
        text, x, y = tokens[i]
        if text.startswith("PI"):
            pending_pins.append(Pin(ref_token=text, pin_id="", x=x, y=y))
            i += 1
        elif text.startswith("CO"):
            encoded_refdes = text[2:]
            for pin in pending_pins:
                pin.pin_id = _strip_pin_id(pin.ref_token, encoded_refdes)
            components.append(
                Component(
                    ref_token=text,
                    encoded_refdes=encoded_refdes,
                    x=x,
                    y=y,
                    pins=pending_pins,
                )
            )
            pending_pins = []
            i += 1
        elif text.startswith("NL"):
            names = []
            while i < n and tokens[i][0].startswith("NL"):
                names.append(tokens[i][0][2:])
                i += 1
            nets.append(Net(names=names, pin_tokens=[p.ref_token for p in pending_pins]))
            pending_pins = []
        else:
            i += 1

    return components, nets


def _strip_pin_id(pin_token: str, encoded_refdes: str) -> str:
    # "PIC101" with encoded_refdes="C1" -> strip "PI"+"C1" -> "01"
    remainder = pin_token[2:]
    if remainder.startswith(encoded_refdes):
        return remainder[len(encoded_refdes) :]
    return remainder  # fallback: couldn't confidently strip, keep raw for inspection


_REFDES_PATTERN = re.compile(r"^[A-Za-z]{1,5}[-_]?\d{1,3}$")


def _alpha_prefix(text: str) -> str:
    match = re.match(r"[A-Za-z]+", text)
    return match.group(0).upper() if match else ""


def _attach_display_labels(
    components: list[Component], visible_labels: list[tuple[str, float, float]]
) -> None:
    """Best-effort: recover the human-readable refdes (the encoded token is
    lossy -- "AMS01" for a silkscreen "AMS-1"). Among nearby text spans,
    prefer one that actually LOOKS like this component's reference designator
    (refdes shape + same alpha prefix); only fall back to raw nearest text.
    Raw-nearest alone picked R10's value label "3.3R" over "R10" -- a real
    bug caught by seeing "3.3R" listed as a component name in the UI.
    """
    max_distance = 25.0
    for component in components:
        target_prefix = _alpha_prefix(component.encoded_refdes)
        best_any, best_any_dist = None, max_distance
        best_refdes, best_refdes_dist = None, max_distance
        for text, lx, ly in visible_labels:
            dist = ((lx - component.x) ** 2 + (ly - component.y) ** 2) ** 0.5
            if dist >= max_distance:
                continue
            if dist < best_any_dist:
                best_any, best_any_dist = text, dist
            if (
                _REFDES_PATTERN.match(text)
                and _alpha_prefix(text) == target_prefix
                and dist < best_refdes_dist
            ):
                best_refdes, best_refdes_dist = text, dist

        if best_refdes:
            component.display_label = best_refdes
        elif best_any and not looks_like_value(best_any) and _alpha_prefix(best_any):
            # plausible name (has letters, isn't a part value like "3.3R")
            component.display_label = best_any
        else:
            # no refdes text printed near this symbol at all (observed: U5's
            # designer only wrote the part number) -- the encoded refdes is
            # always a valid identifier, just possibly missing punctuation
            component.display_label = component.encoded_refdes


def _rect_distance(px: float, py: float, x0: float, y0: float, x1: float, y1: float) -> float:
    dx = max(x0 - px, 0.0, px - x1)
    dy = max(y0 - py, 0.0, py - y1)
    return (dx * dx + dy * dy) ** 0.5


def _attach_nearby_text(
    components: list[Component], visible_labels: list[tuple[str, float, float]]
) -> None:
    """Every visible text span near a component's BODY (its pin bounding
    box), as context for the part-identity agent. Distance is measured from
    the box edge, not the label anchor point -- anchor-radius collection
    missed "ESP32-WROOM-32UE" printed right under the ESP32 module because
    the module is physically large and its anchor sits far from that text,
    leaving the model to guess "unknown 39-pin MCU" at 60% confidence when
    the answer was literally on the page. Deliberately broad (the agent does
    the filtering); this step stays purely mechanical.
    """
    radius = 55.0
    for component in components:
        xs = [p.x for p in component.pins] or [component.x]
        ys = [p.y for p in component.pins] or [component.y]
        x0, y0, x1, y1 = min(xs), min(ys), max(xs), max(ys)
        nearby = []
        for text, lx, ly in visible_labels:
            if text == component.display_label:
                continue
            dist = _rect_distance(lx, ly, x0, y0, x1, y1)
            if dist < radius:
                nearby.append((dist, text))

        # nearest_value: pure nearest-by-distance among value-shaped tokens.
        # A component's own printed value sits closer to it than a packed-in
        # neighbor's value does -- verified this matters: on this board,
        # about half of all passives have more than one value-shaped token
        # within radius, and picking the wrong one is a real, common error,
        # not a rare edge case. Computed here, before nearby_text's
        # informativeness reordering below discards distance ordering.
        value_candidates = [(d, t) for d, t in nearby if looks_like_value(t)]
        value_candidates.sort(key=lambda t: t[0])
        component.nearest_value = value_candidates[0][1] if value_candidates else None

        # nearby_text (broad context for the AI agent): longer tokens (part
        # numbers) carry more signal than short pin-name/number noise ("1",
        # "NC", "A0") clustered right at the pins -- rank informativeness
        # first, distance second, so the real identifier isn't crowded out.
        nearby.sort(key=lambda t: (-len(t[1]), t[0]))
        component.nearby_text = [t for _, t in nearby[:12]]
