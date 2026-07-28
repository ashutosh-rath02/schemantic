"""Pre-fab design checks against the real reference board. Skipped if the
enrichment cache isn't built (no network/AI calls happen in this test file
itself -- it reads the already-cached payload)."""

import json
from pathlib import Path

import pytest

from schemantic.design_checks import (
    check_floating_enable_pins,
    check_missing_pullups,
    check_rail_compatibility,
    check_unidentified_controllers,
    run_all_checks,
)
from schemantic.pipeline import CACHE_DIR, SCHEMA_VERSION, _pdf_hash

REFERENCE_PDF = Path(__file__).parent.parent / "ROS_Driver_for_Robots.pdf"


@pytest.fixture(scope="module")
def payload():
    cache = CACHE_DIR / f"{_pdf_hash(str(REFERENCE_PDF))}_v{SCHEMA_VERSION}.json"
    if not cache.exists():
        pytest.skip("enriched cache not built yet")
    return json.loads(cache.read_text(encoding="utf-8"))


def test_gnd_tied_resistor_does_not_count_as_pullup(payload):
    # R2 sits on GND, not a supply rail -- it's a pull-DOWN and does
    # nothing for an open-drain I2C bus. Treating any power-rail connection
    # (GND included) as satisfying the pull-up check accepted this before
    # the fix; the real pull-ups (R5-R8, tied to 3V3) must be what actually
    # clears the SCL net, not R2.
    from schemantic.design_checks import _is_pullup_resistor

    r2 = next(c for c in payload["components"] if c["display_label"] == "R2")
    scl_key = next(n["key"] for n in r2["nets"] if not n["is_power"])
    assert not _is_pullup_resistor(r2, scl_key)

    r5 = next(c for c in payload["components"] if c["display_label"] == "R5")
    scl_key_r5 = next(n["key"] for n in r5["nets"] if not n["is_power"])
    assert _is_pullup_resistor(r5, scl_key_r5)


def test_rail_compatibility_uses_range_containment_not_substring(payload):
    # The ESP32's own datasheet fact is "3.0 to 3.6 V" -- naive digit-
    # substring matching against the rail name only caught "3.6" and
    # missed that 3.3V (the board's actual rail) falls inside that range,
    # producing a false positive on the board's own MCU. Must not recur.
    findings = check_rail_compatibility(payload)
    flagged = {f["members"][0] for f in findings}
    assert "M3" not in flagged


def test_i2c_bus_with_no_local_pullup_flagged_or_explained(payload):
    # The board's I2C bus is exposed on connectors (P1-P4) as well as used
    # on-board -- whatever the finding, it must correctly describe which
    # shape it found, not just fire blindly.
    findings = check_missing_pullups(payload)
    for f in findings:
        if f["severity"] == "warn":
            assert "off-board" not in f["message"]  # warn = no connector escape hatch
        else:
            assert "connector" in f["message"]


def test_missing_pullup_finding_shape(payload):
    findings = check_missing_pullups(payload)
    for f in findings:
        assert f["tier"] == "mechanical"
        assert f["net"] in payload["nets"]
        assert f["severity"] in ("warn", "info")


def test_floating_enable_findings_have_no_other_membership(payload):
    findings = check_floating_enable_pins(payload)
    for f in findings:
        net = payload["nets"][f["net"]]
        assert len(net["members"]) == 1


def test_unidentified_controller_findings_have_no_part_number(payload):
    findings = check_unidentified_controllers(payload)
    by_token = {c["ref_token"]: c for c in payload["components"]}
    label_to_token = {
        (c["display_label"] or c["encoded_refdes"]): c["ref_token"] for c in payload["components"]
    }
    for f in findings:
        token = label_to_token[f["members"][0]]
        identity = by_token[token].get("identity") or {}
        assert not identity.get("likely_part_number")


def test_known_identified_controller_not_flagged(payload):
    # M3 (ESP32-WROOM-32UE) is confidently identified -- must never appear
    # in the unidentified-controller findings.
    findings = check_unidentified_controllers(payload)
    flagged = {f["members"][0] for f in findings}
    assert "M3" not in flagged


def test_rail_compatibility_findings_are_heuristic_tier(payload):
    for f in check_rail_compatibility(payload):
        assert f["tier"] == "heuristic"


def test_run_all_checks_aggregates_with_counts_and_note(payload):
    result = run_all_checks(payload)
    assert result["counts"]["warn"] + result["counts"]["info"] == len(result["findings"])
    assert "not a substitute" in result["note"]
