"""KiCad ingestion against the real Olimex ESP32-PoE project (netlist +
PCB, both committed under test_boards/kicad). No AI, no network.
"""

from pathlib import Path

import pytest

from schemantic.kicad import build_schematic, parse_netlist, parse_pcb_positions, parse_sexpr

KICAD_DIR = Path(__file__).parent.parent / "test_boards" / "kicad"
NET = KICAD_DIR / "ESP32-PoE_Rev_J.net"
PCB = KICAD_DIR / "ESP32-PoE_Rev_J.kicad_pcb"

pytestmark = pytest.mark.skipif(not NET.exists(), reason="Olimex fixtures not downloaded")


@pytest.fixture(scope="module")
def schematic():
    return build_schematic(
        NET.read_text(encoding="utf-8", errors="replace"),
        PCB.read_text(encoding="utf-8", errors="replace") if PCB.exists() else None,
        "ESP32-PoE_Rev_J",
    )


def test_sexpr_parser_roundtrips_quotes_and_nesting():
    parsed = parse_sexpr('(a (b "hello world") (c 1 2))')
    assert parsed == ["a", ["b", "hello world"], ["c", "1", "2"]]


def test_netlist_finds_real_components(schematic):
    refs = {c.encoded_refdes for c in schematic.components}
    assert "U2" in refs  # the ESP32 module on this board
    esp32 = next(
        (c for c in schematic.components if "ESP32" in " ".join(c.nearby_text)), None
    )
    assert esp32 is not None, "some component should carry an ESP32 value"


def test_gnd_net_is_large_and_named(schematic):
    gnd = next((n for n in schematic.nets if "GND" in n.names), None)
    assert gnd is not None
    assert len(gnd.pin_tokens) > 40  # ground touches much of a real board


def test_component_values_feed_identity_context(schematic):
    # KiCad values ("10k", "ESP32-WROOM-32E") land in nearby_text so the
    # identity agent gets the same kind of signal the PDF path provides
    with_values = [c for c in schematic.components if c.nearby_text]
    assert len(with_values) > len(schematic.components) * 0.8


def test_pcb_positions_give_distinct_pin_coordinates(schematic):
    if not PCB.exists():
        pytest.skip("no PCB fixture")
    esp32 = next(c for c in schematic.components if c.encoded_refdes == "U2")
    coords = {(round(p.x, 2), round(p.y, 2)) for p in esp32.pins}
    assert len(coords) > len(esp32.pins) * 0.8  # real pads, not one stacked point


def test_canvas_coordinates_fit_the_derived_page(schematic):
    xs = [p.x for c in schematic.components for p in c.pins]
    ys = [p.y for c in schematic.components for p in c.pins]
    assert min(xs) >= 0 and max(xs) <= schematic.page_width + 20
    assert min(ys) >= 0 and max(ys) <= schematic.page_height + 20
    # page follows the board's aspect (this board is portrait-ish)
    assert schematic.page_height > 0


def test_netlist_without_export_root_rejected():
    with pytest.raises(ValueError, match="netlist"):
        parse_netlist("(kicad_pcb (version 4))")


def test_pcb_parse_survives_modules_without_reference():
    positions = parse_pcb_positions('(kicad_pcb (module X (at 1 2)))')
    assert positions == {}
