"""Rejection-path tests against real unsupported PDFs, downloaded from their
vendors (kept in test_boards/, not fabricated): a genuine KiCad Eeschema
export (Olimex ESP32-PoE) and a schematic image pasted into a word processor
(a Waveshare bus-servo doc created in WPS Writer). Skipped if the files
aren't present.
"""

from pathlib import Path

import pytest

from schemantic.parser.netlist import _is_netlist_token, parse_schematic_pdf

BOARDS_DIR = Path(__file__).parent.parent / "test_boards"


def test_kicad_signal_label_is_not_a_component_token():
    # Real label from the Olimex KiCad export: starts with "CO" but is a
    # visible RMII signal name. Mistaking it for a token produced a garbage
    # one-component parse that slipped past the no-components check.
    assert not _is_netlist_token("COL/CRS_DV/MODE2")
    # while the real token families still pass
    assert _is_netlist_token("PIC101")
    assert _is_netlist_token("COAMS01")
    assert _is_netlist_token("NL#EN")
    assert _is_netlist_token("NLMA1'")


@pytest.mark.parametrize(
    "filename",
    ["Olimex_KiCad.pdf", "Waveshare_Bus_Servo.pdf"],
)
def test_unsupported_pdfs_reject_with_clear_error(filename):
    path = BOARDS_DIR / filename
    if not path.exists():
        pytest.skip(f"{filename} not downloaded")
    with pytest.raises(ValueError, match="netlist tokens"):
        parse_schematic_pdf(str(path))
