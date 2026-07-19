"""Hardware map assembly against the real enriched board -- pure
deterministic code, no AI, skipped if the enrichment cache isn't built.
"""

import json
from pathlib import Path

import pytest

from schemantic.hardwaremap import build_hardware_map, render_markdown
from schemantic.pipeline import CACHE_DIR, SCHEMA_VERSION, _pdf_hash

REFERENCE_PDF = Path(__file__).parent.parent / "ROS_Driver_for_Robots.pdf"


@pytest.fixture(scope="module")
def hw_map():
    cache = CACHE_DIR / f"{_pdf_hash(str(REFERENCE_PDF))}_v{SCHEMA_VERSION}.json"
    if not cache.exists():
        pytest.skip("enriched cache not built yet")
    payload = json.loads(cache.read_text(encoding="utf-8"))
    return build_hardware_map(payload, "ROS_Driver_for_Robots", confirmed_mates=[])


def test_esp32_is_a_controller_with_pin_table(hw_map):
    esp32 = next((c for c in hw_map["controllers"] if c["part"] == "ESP32-WROOM-32UE"), None)
    assert esp32 is not None
    assert len(esp32["pins"]) > 20


def test_controller_pin_rows_carry_real_connections(hw_map):
    esp32 = next(c for c in hw_map["controllers"] if c["part"] == "ESP32-WROOM-32UE")
    scl_pin = next(
        (p for p in esp32["pins"] if any("IIC0SCL" in a for a in p["net_aliases"])), None
    )
    assert scl_pin is not None
    assert scl_pin["bus"] == "I2C"
    peer_refs = {p["ref"] for p in scl_pin["connected_to"]}
    assert "U6" in peer_refs  # the INA219 shares the SCL bus -- hand-verified


def test_bus_inventory_finds_i2c(hw_map):
    assert "I2C" in hw_map["buses"]
    i2c_members = {m for net in hw_map["buses"]["I2C"]["nets"] for m in net["members"]}
    assert "U6" in i2c_members


def test_rail_voltage_is_name_derived_and_flagged(hw_map):
    rail_3v3 = next(
        (r for r in hw_map["power_rails"] if any("3V3" in a for a in r["aliases"])), None
    )
    assert rail_3v3 is not None
    assert rail_3v3["voltage_from_name"] == "3.3V"
    assert "name" in hw_map["trust_notes"]["rail_voltages"].lower()


def test_connectors_are_never_controllers(hw_map):
    # a 6-pin header whose identity says "module" put connectors in the
    # controller section until the prefix gate existed
    refs = {c["ref"] for c in hw_map["controllers"]}
    assert not any(r.startswith(("H", "P", "J")) for r in refs), refs


def test_nc_nets_are_no_connect_not_power(hw_map):
    esp32 = next(c for c in hw_map["controllers"] if c["part"] == "ESP32-WROOM-32UE")
    nc_pins = [p for p in esp32["pins"] if p.get("is_no_connect")]
    if nc_pins:  # ESP32 has NC pins on this board
        assert all(not p["is_power"] for p in nc_pins)


def test_markdown_renders_all_sections(hw_map):
    md = render_markdown(hw_map)
    for heading in ("# Hardware Map", "## Trust levels", "## Controller:",
                    "## Buses", "## Power rails", "## Connectors", "## Identified parts"):
        assert heading in md
    assert "confirm before applying power" in md
