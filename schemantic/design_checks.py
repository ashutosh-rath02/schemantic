"""Pre-fab design checks: catches worth a look before ordering a board.

Deliberately split into two trust tiers, each labeled:

  MECHANICAL checks are graph-derived facts, not AI opinion -- a signal net
  with no pull-up resistor visible in its own membership either has one or
  it doesn't, checkable the same way GND-vs-signal classification is. These
  never false-positive on the connectivity question itself (they can still
  be irrelevant -- an I2C bus with an external pull-up elsewhere in the
  harness looks unpulled to us, which is exactly why every finding says so).

  HEURISTIC checks use AI part identities and datasheet facts, which are
  themselves confidence-scored guesses -- a check built on top inherits that
  uncertainty and is labeled "heuristic," not "verified."

This is NOT an ERC/DRC replacement (that needs the CAD project; we don't
have it) and not a Traceformer-style datasheet-compliance checker (same
reason). It's the narrower thing possible from a schematic PDF alone:
whatever the verified graph and already-fetched datasheets can support.
"""

from __future__ import annotations

import re

_PULLUP_VALUE = re.compile(r"^(\d+(\.\d+)?)(k)?$", re.IGNORECASE)
_ENABLE_NAME = re.compile(r"(^|_)(en|nrst|reset|boot|mode)(_|$)", re.IGNORECASE)


def _label(component: dict) -> str:
    return component["display_label"] or component["encoded_refdes"]


def _is_pullup_resistor(component: dict, net_key: str) -> bool:
    if component["encoded_refdes"][0] != "R":
        return False
    if len(component["pins"]) != 2:
        return False
    nets_on_component = {n["key"] for n in component["nets"]}
    if net_key not in nets_on_component:
        return False
    # the OTHER pin must land on a POSITIVE supply rail specifically -- a
    # resistor tied to GND is a pull-DOWN, which does nothing for an
    # open-drain I2C bus. Checked against real output: on the reference
    # board, R2 sits on GND (some other function, not I2C) while R5-R8 sit
    # on 3V3 (the real pull-ups) -- treating "any power rail" as sufficient
    # would have silently accepted R2-style GND resistors as satisfying a
    # bus that actually had no pull-up, on a board where the real pull-ups
    # didn't happen to also exist.
    other_nets = [n for n in component["nets"] if n["key"] != net_key]
    return any(
        n["is_power"] and "GND" not in " ".join(n["names"]).upper() for n in other_nets
    )


def check_missing_pullups(payload: dict) -> list[dict]:
    """I2C is open-drain: SDA/SCL need a pull-up somewhere on the bus. This
    checks whether ANY resistor tied to that net also ties to a power rail
    -- if none do, either the pull-up lives off-board (common on a
    connector-exposed bus) or it's genuinely missing. Both are worth a
    human look, so the finding says which shape it found."""
    findings = []
    by_token = {c["ref_token"]: c for c in payload["components"]}
    seen_nets = set()
    for c in payload["components"]:
        for n in c["nets"]:
            key = n["key"]
            if n["is_power"] or key in seen_nets:
                continue
            names_joined = " ".join(n["names"]).upper()
            if not re.search(r"(SDA|SCL|I2C|IIC)", names_joined):
                continue
            seen_nets.add(key)
            net = payload["nets"][key]
            members = [by_token[m] for m in net["members"] if m in by_token]
            pullups = [m for m in members if _is_pullup_resistor(m, key)]
            has_connector = any(
                "".join(ch for ch in m["encoded_refdes"] if ch.isalpha()).upper()
                in ("P", "H", "J", "TYPE")
                for m in members
            )
            if pullups:
                continue  # pull-up present on this net -- nothing to flag
            findings.append(
                {
                    "check": "missing_pullup",
                    "severity": "info" if has_connector else "warn",
                    "net": key,
                    "net_names": n["names"],
                    "members": [_label(m) for m in members],
                    "message": (
                        f"No pull-up resistor found on I2C-looking net {'/'.join(n['names'])}. "
                        + (
                            "The net reaches a connector, so the pull-up may live off-board "
                            "on the mating harness -- verify before assuming it's missing."
                            if has_connector
                            else "No connector on this net either, so if this bus needs a "
                            "pull-up it likely IS missing from this board."
                        )
                    ),
                    "tier": "mechanical",
                }
            )
    return findings


def check_floating_enable_pins(payload: dict) -> list[dict]:
    """An EN/RESET/BOOT/MODE-named net with only one member (the pin
    itself, no resistor, no connector, nothing) is either floating in the
    schematic or the parser genuinely found no other membership -- either
    way, worth a human glance before fab, since these pins commonly need a
    defined idle state."""
    findings = []
    seen_nets = set()
    for c in payload["components"]:
        for pin in c["pins"]:
            if not pin["net"] or pin["net"] in seen_nets:
                continue
            net = payload["nets"].get(pin["net"])
            if net is None or net["is_power"]:
                continue
            if not any(_ENABLE_NAME.search(n) for n in net["names"]):
                continue
            seen_nets.add(pin["net"])
            if len(net["members"]) > 1:
                continue  # has other membership -- not floating
            findings.append(
                {
                    "check": "floating_enable",
                    "severity": "warn",
                    "net": pin["net"],
                    "net_names": net["names"],
                    "members": [_label(c)],
                    "message": (
                        f"{_label(c)}'s {'/'.join(net['names'])} pin has no other net "
                        "membership -- no resistor, no connector, nothing else on this net. "
                        "Control/enable pins usually need a defined pull to avoid an "
                        "indeterminate power-up state."
                    ),
                    "tier": "mechanical",
                }
            )
    return findings


def check_unidentified_controllers(payload: dict) -> list[dict]:
    """A high-pin-count IC the identification agent couldn't confidently
    name is a real gap for pre-fab review -- you can't check supply
    compatibility or pin function against a datasheet you don't have."""
    findings = []
    for c in payload["components"]:
        if c["pin_count"] < 8:
            continue
        identity = c.get("identity") or {}
        if identity.get("likely_part_number"):
            continue
        prefix = "".join(ch for ch in c["encoded_refdes"] if ch.isalpha()).upper()
        if prefix not in ("U", "M", "TB"):
            continue
        findings.append(
            {
                "check": "unidentified_controller",
                "severity": "info",
                "net": None,
                "net_names": [],
                "members": [_label(c)],
                "message": (
                    f"{_label(c)} ({c['pin_count']} pins) could not be confidently identified "
                    f"({identity.get('confidence', 0):.0%} confidence, no part number). No "
                    "datasheet was fetched, so its ratings can't be cross-checked."
                ),
                "tier": "mechanical",
            }
        )
    return findings


# "3.0 to 3.6 V" or "3.0V to 3.6V" -- a real range, not two independent
# numbers. Tried single-number extraction first; it caught only the upper
# bound of ranges like this, which then failed to contain the board's
# actual 3.3V rail -- a false positive on the ESP32 itself, caught by
# reading live output rather than trusting the check unverified.
#
# The separator alternation includes an EN DASH (U+2013) deliberately, not
# as a stray character -- professionally typeset datasheet PDFs commonly
# use one for a numeric range (e.g. "3.0" + EN DASH + "3.6 V") instead of a
# plain hyphen. Spelled out here instead of pasting the literal character a
# second time, which is exactly the ambiguous-glyph situation this comment
# is explaining.
_VOLTAGE_RANGE = re.compile(
    r"(\d+(?:\.\d+)?)\s*V?\s*(?:to|-|–)\s*(\d+(?:\.\d+)?)\s*V",  # noqa: RUF001
    re.IGNORECASE,
)
_VOLTAGE_SINGLE = re.compile(r"(\d+(?:\.\d+)?)\s*V\b")
_RAIL_NAME_VOLTAGE = re.compile(r"(\d+)V(\d*)", re.IGNORECASE)


def _extract_voltage_ranges(text: str) -> list[tuple[float, float]]:
    ranges = []
    consumed = set()
    for m in _VOLTAGE_RANGE.finditer(text):
        lo, hi = float(m.group(1)), float(m.group(2))
        ranges.append((min(lo, hi), max(lo, hi)))
        consumed.add(m.span())
    for m in _VOLTAGE_SINGLE.finditer(text):
        if any(m.start() >= s and m.end() <= e for s, e in consumed):
            continue  # already part of a captured range
        v = float(m.group(1))
        ranges.append((v, v))
    return ranges


def _rail_voltage_from_names(names: list[str]) -> float | None:
    for name in names:
        m = _RAIL_NAME_VOLTAGE.search(name)
        if m:
            whole, frac = m.groups()
            return float(f"{whole}.{frac}") if frac else float(whole)
    return None


def check_rail_compatibility(payload: dict) -> list[dict]:
    """Heuristic tier: for components with a verified datasheet fact
    mentioning a supply-voltage range, flag if the board's power-rail
    voltage (parsed from the net name, e.g. "3V3" -> 3.3) falls OUTSIDE
    that range -- numeric containment, not substring matching (substring
    matching against a range's upper bound alone false-flagged the ESP32's
    own supply rail). A rail whose voltage isn't encoded in its name at all
    is silently skipped, not flagged -- absence of evidence isn't evidence
    of a mismatch. Built on AI-identified parts and AI-extracted (but
    quote-verified) facts -- inherits their uncertainty; advisory only."""
    findings = []
    for c in payload["components"]:
        sheet = c.get("datasheet")
        if not sheet:
            continue
        ranges: list[tuple[float, float]] = []
        source_facts = []
        for fact in sheet.get("facts", []):
            if re.search(r"suppl|operating voltage|v(cc|dd)", fact["fact"], re.IGNORECASE):
                found = _extract_voltage_ranges(fact["fact"])
                if found:
                    ranges.extend(found)
                    source_facts.append(fact["fact"])
        if not ranges:
            continue

        power_nets = [n for n in c["nets"] if n["is_power"]]
        rail_voltages = [
            (n, v)
            for n in power_nets
            if (v := _rail_voltage_from_names(n["names"])) is not None
        ]
        if not rail_voltages:
            continue  # no rail here encodes a voltage in its name -- nothing to compare

        tolerance = 0.05
        for net, voltage in rail_voltages:
            if any(lo - tolerance <= voltage <= hi + tolerance for lo, hi in ranges):
                continue
            findings.append(
                {
                    "check": "rail_compatibility",
                    "severity": "info",
                    "net": net["key"],
                    "net_names": net["names"],
                    "members": [_label(c)],
                    "message": (
                        f"{_label(c)} is on rail {'/'.join(net['names'])} (~{voltage}V), but its "
                        f"datasheet's stated range is {ranges} V. Datasheet says: "
                        f"\"{source_facts[0]}\". Could be a naming mismatch or a genuine "
                        "compatibility gap -- worth a manual check, not a confirmed violation."
                    ),
                    "tier": "heuristic",
                }
            )
    return findings


ALL_CHECKS = [
    check_missing_pullups,
    check_floating_enable_pins,
    check_unidentified_controllers,
    check_rail_compatibility,
]


def run_all_checks(payload: dict) -> dict:
    findings: list[dict] = []
    for check in ALL_CHECKS:
        findings.extend(check(payload))
    by_severity = {"warn": 0, "info": 0}
    for f in findings:
        by_severity[f["severity"]] = by_severity.get(f["severity"], 0) + 1
    return {
        "findings": findings,
        "counts": by_severity,
        "note": "Mechanical findings come from verified connectivity; heuristic findings lean on "
        "AI part identification and are advisory. This is not an ERC/DRC replacement and not a "
        "substitute for review in your actual CAD tool -- it's what's checkable from a schematic "
        "PDF alone.",
    }
