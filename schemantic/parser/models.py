"""Data model for a parsed schematic. Mirrors what's mechanically extractable
from an EDA-exported PDF's embedded netlist text -- nothing here is inferred
by a model, only what the parser can prove from the document itself.
"""

from __future__ import annotations

from pydantic import BaseModel


class Pin(BaseModel):
    ref_token: str  # raw encoded token, e.g. "PIC101" -- internal join key
    pin_id: str  # e.g. "01"
    x: float
    y: float


class Component(BaseModel):
    ref_token: str  # raw encoded token, e.g. "COC1" -- internal join key
    encoded_refdes: str  # e.g. "C1", "AMS01" -- decoded from the CO token, lossy
    display_label: str | None = None  # best-effort human-readable label, spatial match
    x: float
    y: float
    pins: list[Pin] = []
    region: str | None = None  # nearest section title drawn on the schematic itself
    nearby_text: list[str] = []  # visible labels/values near the symbol, e.g. part numbers
    nearest_value: str | None = None  # closest value-shaped label by pure distance


class Net(BaseModel):
    names: list[str]  # a net can have multiple aliases, e.g. ["IO14", "SPI_CK"]
    pin_tokens: list[str]  # raw PI tokens on this net


class Schematic(BaseModel):
    source_file: str
    page_width: float
    page_height: float
    components: list[Component]
    nets: list[Net]

    def component_by_token(self, ref_token: str) -> Component | None:
        return next((c for c in self.components if c.ref_token == ref_token), None)

    def nets_for_component(self, ref_token: str) -> list[Net]:
        component = self.component_by_token(ref_token)
        if component is None:
            return []
        pin_tokens = {p.ref_token for p in component.pins}
        return [n for n in self.nets if pin_tokens & set(n.pin_tokens)]
