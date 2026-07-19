"""Regression tests locking in manually-verified parser output against the
real reference schematic. Every assertion here was cross-checked by hand
against the schematic's visible text/pinout before being written -- see the
build notes. If one of these breaks, the parser regressed; don't loosen the
assertion to make it pass without re-verifying against the PDF.
"""

from pathlib import Path

import pytest

from schemantic.parser.netlist import parse_schematic_pdf

REFERENCE_PDF = Path(__file__).parent.parent / "ROS_Driver_for_Robots.pdf"


@pytest.fixture(scope="module")
def schematic():
    return parse_schematic_pdf(str(REFERENCE_PDF))


def _component(schematic, label):
    return next(c for c in schematic.components if c.display_label == label)


def test_component_and_net_counts(schematic):
    # 72, not 71: the 72nd is #EN, whose NL token the original "third char
    # isalnum" check silently rejected, merging its pins into the next net
    # in reading order. Found by diffing token classifications.
    assert len(schematic.components) == 146
    assert len(schematic.nets) == 72


def test_enable_net_is_its_own_net_not_merged(schematic):
    en = next(n for n in schematic.nets if "#EN" in n.names)
    assert len(en.pin_tokens) == 5


def test_apostrophe_net_names_survive(schematic):
    # MA1'/MA2'/MB1'/MB2' -- the motor-output primes -- are real net names
    # containing an apostrophe; the tightened token charset must keep them.
    primes = {n.names[0] for n in schematic.nets if "'" in n.names[0]}
    assert primes == {"MA1'", "MA2'", "MB1'", "MB2'"}


def test_display_label_recovers_hyphenated_refdes(schematic):
    # encoded token is "AMS01" (hyphen dropped by the EDA export); the real
    # silkscreen label is "AMS-1" -- verified by eye against the schematic.
    ams1 = next(c for c in schematic.components if c.encoded_refdes == "AMS01")
    assert ams1.display_label == "AMS-1"


def test_capacitor_c1_has_two_pins(schematic):
    c1 = _component(schematic, "C1")
    assert {p.pin_id for p in c1.pins} == {"01", "02"}


def test_ina219_pinout_matches_datasheet(schematic):
    # U6 = INA219BIDR (SOP-8). Schematic lists: A1=1 A0=2 SDA=3 SCL=4 Vs=5
    # GND=6 IN-=7 IN+=8. Verified pin 3 lands on the IIC_SDA bus, pin 4 on
    # IIC_SCL, matching the drawn I2C connections.
    u6 = _component(schematic, "U6")
    assert len(u6.pins) == 8
    pin3 = next(p for p in u6.pins if p.pin_id == "03")
    pin4 = next(p for p in u6.pins if p.pin_id == "04")
    net3_names = next(n.names for n in schematic.nets if pin3.ref_token in n.pin_tokens)
    net4_names = next(n.names for n in schematic.nets if pin4.ref_token in n.pin_tokens)
    assert "IIC0SDA" in net3_names
    assert "IIC0SCL" in net4_names


def test_imu_has_qfn24_pin_count(schematic):
    # U1 = ICM-20948, a QFN-24 package -- 24 physical pins.
    u1 = _component(schematic, "U1")
    assert len(u1.pins) == 24


def test_ground_net_touches_every_component(schematic):
    # Every component on a board like this needs a ground reference.
    gnd = next(n for n in schematic.nets if "GND" in n.names)
    assert len(gnd.pin_tokens) == 146


def test_unlabeled_regions_are_honest_not_forced(schematic):
    # A component whose area has no reachable section title gets region=None
    # ("unlabeled") instead of a forced wrong label. On the reference board
    # exactly two components sit in an untitled corridor: the P1/P2 40-pin
    # RPi/Jetson headers. Everything else must have a real label.
    unlabeled = {c.encoded_refdes for c in schematic.components if c.region is None}
    assert unlabeled == {"P1", "P2"}


def test_nearest_value_picks_own_value_not_a_neighbors(schematic):
    # R10's real marked value is "3.3R" (verified against the schematic).
    # A naive "any value-shaped token within radius" pick previously
    # returned a nearby capacitor's value instead -- about half of all
    # passives on this board have more than one value-shaped token in
    # range, so this is a common failure mode, not an edge case.
    r10 = _component(schematic, "R10")
    assert r10.nearest_value == "3.3R"


def test_nearest_value_does_not_match_section_titles(schematic):
    # A loose substring check ("M" in "IMU") previously matched the nearby
    # "10-DOF-IMU-Sensor-D" section title as a capacitor value.
    c1 = _component(schematic, "C1")
    assert c1.nearest_value is not None
    assert "IMU" not in c1.nearest_value
    assert "DOF" not in c1.nearest_value


def test_nearby_text_surfaces_real_part_numbers(schematic):
    # The part-identity agent depends on the real part number actually being
    # present in nearby_text, not crowded out by pin-name noise.
    u6 = _component(schematic, "U6")
    u1 = _component(schematic, "U1")
    tb1 = _component(schematic, "TB1")
    assert "INA219BIDR(SOP-8)" in u6.nearby_text
    assert "ICM-20948" in u1.nearby_text
    assert "TB6612FNG" in tb1.nearby_text


def test_nearby_text_measured_from_body_not_anchor(schematic):
    # The ESP32 module is physically large; its identifying text sits far
    # from the label anchor but right under the body. Anchor-radius
    # collection missed it entirely, leaving the AI to guess "unknown
    # 39-pin MCU" when the answer was printed on the page.
    m3 = next(c for c in schematic.components if c.encoded_refdes == "M3")
    assert "ESP32-WROOM-32UE" in m3.nearby_text


def test_display_label_prefers_refdes_over_value_text(schematic):
    # R10's value "3.3R" sits closer to its anchor than the text "R10";
    # raw-nearest picked the value as the component's display name.
    r10 = next(c for c in schematic.components if c.encoded_refdes == "R10")
    assert r10.display_label == "R10"


def test_display_label_falls_back_to_encoded_refdes(schematic):
    # U5 (the MP8759GD buck converter) has no refdes text printed near it
    # at all -- fallback must be the encoded refdes, never a neighbor's
    # part value.
    u5 = next(c for c in schematic.components if c.encoded_refdes == "U5")
    assert u5.display_label == "U5"


def test_region_assignment_respects_dividers(schematic):
    # Section titles are placed inconsistently (headers above vs captions
    # below), so nearest-distance alone misassigns near boundaries; the
    # drawn divider lines resolve it. These four were all wrong under at
    # least one earlier, simpler approach.
    m3 = next(c for c in schematic.components if c.encoded_refdes == "M3")
    assert m3.region == "ESP-32UE"
    assert _component(schematic, "U1").region == "10DOF"
    assert _component(schematic, "TB1").region == "Motor"
    assert _component(schematic, "TB2").region == "Motor"
