"""Functional-block grouping via the schematic's own section titles.

Title placement on this schematic is inconsistent -- some sections put the
title at the top ("10DOF", "PWR-IN"), others as a caption at the bottom
("ESP-32UE", "IIC") -- so pure nearest-distance matching is fundamentally
ambiguous near section boundaries: the physically large ESP32 module sat
geometrically closer to the section title ABOVE its divider than to its own
caption below it.

Current approach, after two failed simpler ones (see git history: plain
anchor-nearest misassigned the ESP32 and a motor driver; anchor-based
line-of-sight over-blocked large components):

1. distance measured title -> nearest point on the component's pin bounding
   box (not its label anchor), and
2. a candidate title is disqualified if the straight line from the box edge
   to it crosses a drawn divider line ("wall") -- the dividers the designer
   actually drew between sections, extracted from the PDF's vector layer.

Still a heuristic, not a verified fact the way netlist connections are --
surface region membership in the UI as inferred, not guaranteed.
"""

from __future__ import annotations

import fitz

from schemantic.parser.models import Component

_TITLE_MIN_SIZE = 10.5  # separates section titles from component labels/values
_MIN_WALL_LENGTH = 80.0  # long enough to be a section divider, not symbol art

# (orientation, fixed_coord, span_start, span_end); orientation "h" or "v"
Wall = tuple[str, float, float, float]


def extract_region_titles(path: str) -> list[tuple[str, float, float]]:
    doc = fitz.open(path)
    page = doc[0]
    text_dict = page.get_text("dict")
    titles = []
    for block in text_dict["blocks"]:
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                if span["size"] >= _TITLE_MIN_SIZE:
                    x0, y0, x1, y1 = span["bbox"]
                    titles.append((span["text"].strip(), (x0 + x1) / 2, (y0 + y1) / 2))
    return titles


def extract_walls(path: str) -> list[Wall]:
    """Long horizontal/vertical strokes from the vector layer -- the drawn
    section dividers. Collected from the drawing bounding rects (a stroked
    divider shows up as a very thin, very long rect) so both plain lines and
    thin filled bars are caught."""
    doc = fitz.open(path)
    page = doc[0]
    walls: list[Wall] = []
    seen: set[tuple] = set()
    for drawing in page.get_drawings():
        r = drawing["rect"]
        wall: Wall | None = None
        if r.width >= _MIN_WALL_LENGTH and r.height < 2:
            wall = ("h", (r.y0 + r.y1) / 2, r.x0, r.x1)
        elif r.height >= _MIN_WALL_LENGTH and r.width < 2:
            wall = ("v", (r.x0 + r.x1) / 2, r.y0, r.y1)
        if wall:
            key = (wall[0], round(wall[1]), round(wall[2]), round(wall[3]))
            if key not in seen:
                seen.add(key)
                walls.append(wall)
    return walls


def _crosses_wall(x1: float, y1: float, x2: float, y2: float, wall: Wall) -> bool:
    orientation, fixed, span_start, span_end = wall
    if orientation == "h":
        if (y1 - fixed) * (y2 - fixed) >= 0:  # both endpoints on same side
            return False
        t = (fixed - y1) / (y2 - y1)
        crossing_x = x1 + t * (x2 - x1)
        return span_start <= crossing_x <= span_end
    else:
        if (x1 - fixed) * (x2 - fixed) >= 0:
            return False
        t = (fixed - x1) / (x2 - x1)
        crossing_y = y1 + t * (y2 - y1)
        return span_start <= crossing_y <= span_end


def assign_regions(
    components: list[Component],
    titles: list[tuple[str, float, float]],
    walls: list[Wall] | None = None,
) -> None:
    if not titles:
        return
    walls = walls or []
    for component in components:
        xs = [p.x for p in component.pins] or [component.x]
        ys = [p.y for p in component.pins] or [component.y]
        x0, y0, x1, y1 = min(xs), min(ys), max(xs), max(ys)

        candidates = []
        for name, tx, ty in titles:
            # nearest point on the box to the title
            nx = min(max(tx, x0), x1)
            ny = min(max(ty, y0), y1)
            dist_sq = (tx - nx) ** 2 + (ty - ny) ** 2
            blocked = any(_crosses_wall(nx, ny, tx, ty, w) for w in walls)
            candidates.append((blocked, dist_sq, name))

        candidates.sort()
        blocked, _, name = candidates[0]
        # If every title is walled off from this component, its area has no
        # title at all (verified on a second board: some sections simply
        # aren't captioned). Say so, instead of forcing the nearest wrong
        # label -- an "unlabeled" region is honest; "PWR-IN" for a component
        # three sections away is not.
        component.region = None if blocked else name
