"""Hardware Map export: the schematic's verified facts, packaged for a
coding agent.

The output is what an embedded engineer (or their AI coding tool) needs to
start firmware: every controller pin -> net -> what's on the other end,
bus inventories, power rails, connector pinouts, confirmed board-to-board
links, and datasheet links with verified facts. Assembled 100% from the
enriched payload -- no model call happens here, so the map inherits the
netlist's trust level. Fields that ARE AI-derived upstream (part
identities) carry their confidence, and name-derived guesses (rail
voltages) are labeled as such.

Two renderings of the same data: .json (machine) and .md (human/agent
prompt context).
"""

from __future__ import annotations

import re
import time
from pathlib import Path

_BUS_PATTERNS = [
    ("I2C", re.compile(r"(sda|scl|i2c|iic)", re.IGNORECASE)),
    ("SPI", re.compile(r"(spi|miso|mosi|sck|\bcs\b|sd0|sd1|clk|cmd)", re.IGNORECASE)),
    ("UART", re.compile(r"(txd?|rxd?|uart|u0tx|u0rx|dtr|rts|cts)", re.IGNORECASE)),
    ("USB", re.compile(r"(usb|d\+|d-|dp\b|dn\b|vbus)", re.IGNORECASE)),
]

_RAIL_VOLTAGE = re.compile(r"(\d+)V(\d*)", re.IGNORECASE)

# A controller is what firmware runs on / talks through. Prefix gate first:
# only IC-type refdes prefixes qualify -- connector identities routinely say
# "module" ("LIDAR module interface"), which put 6-pin headers in the
# controller section until the gate existed (caught in live output review).
_CONTROLLER_PREFIXES = ("U", "M", "TB")
_CONTROLLER_HINT = re.compile(
    r"(microcontroller|mcu|\bsoc\b|system.on.chip|esp32|stm32|processor)", re.IGNORECASE
)


def _label(component: dict) -> str:
    return component["display_label"] or component["encoded_refdes"]


def _is_no_connect(net_names: list[str]) -> bool:
    # a shared "NC" net has enough pins to look like a power rail by fanout;
    # it is neither power nor signal -- it's the absence of a connection
    return all(n.upper() == "NC" for n in net_names)


def _bus_of(net_names: list[str]) -> str | None:
    joined = " ".join(net_names)
    for bus, pattern in _BUS_PATTERNS:
        if pattern.search(joined):
            return bus
    return None


def _voltage_of(net_names: list[str]) -> str | None:
    for name in net_names:
        match = _RAIL_VOLTAGE.search(name)
        if match:
            whole, frac = match.groups()
            return f"{whole}.{frac}V" if frac else f"{whole}V"
        if name.upper().startswith("VDD") or name.upper().startswith("VCC"):
            return None  # rail known, voltage not encoded in the name
    return None


def _is_controller(component: dict) -> bool:
    prefix = "".join(ch for ch in component["encoded_refdes"] if ch.isalpha()).upper()
    if prefix not in _CONTROLLER_PREFIXES:
        return False
    identity = component.get("identity") or {}
    text = " ".join(
        str(identity.get(k) or "") for k in ("function", "likely_part_number", "package_type")
    )
    return bool(_CONTROLLER_HINT.search(text)) or component["pin_count"] >= 24


def build_hardware_map(payload: dict, board_name: str, confirmed_mates: list[dict]) -> dict:
    components = payload["components"]
    nets = payload["nets"]
    by_token = {c["ref_token"]: c for c in components}

    def peers_on_net(net_key: str, exclude_token: str) -> list[dict]:
        out = []
        for member in nets[net_key]["members"]:
            if member == exclude_token:
                continue
            peer = by_token.get(member)
            if peer is None:
                continue
            identity = peer.get("identity") or {}
            out.append(
                {
                    "ref": _label(peer),
                    "part": identity.get("likely_part_number"),
                    "function": identity.get("function"),
                }
            )
        return out

    controllers = []
    for c in components:
        if not _is_controller(c):
            continue
        identity = c.get("identity") or {}
        pins = []
        for pin in c["pins"]:
            if not pin["net"]:
                continue
            net = nets.get(pin["net"])
            if net is None:
                continue
            no_connect = _is_no_connect(net["names"])
            is_power = net["is_power"] and not no_connect
            pins.append(
                {
                    "pin": pin["pin_id"],
                    "net": pin["net"],
                    "net_aliases": net["names"],
                    "is_power": is_power,
                    "is_no_connect": no_connect,
                    "bus": _bus_of(net["names"]) if not (is_power or no_connect) else None,
                    "connected_to": []
                    if (is_power or no_connect)
                    else peers_on_net(pin["net"], c["ref_token"]),
                }
            )
        controllers.append(
            {
                "ref": _label(c),
                "part": identity.get("likely_part_number"),
                "identity_confidence": identity.get("confidence"),
                "function": identity.get("function"),
                "datasheet_url": (c.get("datasheet") or {}).get("url"),
                "region": c["region"],
                "pins": pins,
            }
        )

    buses: dict[str, dict] = {}
    for key, net in nets.items():
        if net["is_power"]:
            continue
        bus = _bus_of(net["names"])
        if bus is None:
            continue
        buses.setdefault(bus, {"nets": []})
        buses[bus]["nets"].append(
            {
                "net": key,
                "aliases": net["names"],
                "members": [
                    _label(by_token[m]) for m in net["members"] if m in by_token
                ],
            }
        )

    rails = []
    for key, net in nets.items():
        if not net["is_power"]:
            continue
        rails.append(
            {
                "net": key,
                "aliases": net["names"],
                "voltage_from_name": _voltage_of(net["names"]),  # name-derived guess
                "member_count": len(net["members"]),
            }
        )
    rails.sort(key=lambda r: -r["member_count"])

    connectors = []
    for c in components:
        prefix = "".join(ch for ch in c["encoded_refdes"] if ch.isalpha()).upper()
        if prefix not in ("P", "H", "J", "TYPE", "DC"):
            continue
        connectors.append(
            {
                "ref": _label(c),
                "region": c["region"],
                "pins": [
                    {"pin": p["pin_id"], "net": p["net"]}
                    for p in c["pins"]
                    if p["net"]
                ],
            }
        )

    parts = []
    for c in components:
        identity = c.get("identity") or {}
        if not identity.get("likely_part_number"):
            continue
        sheet = c.get("datasheet") or {}
        parts.append(
            {
                "ref": _label(c),
                "part": identity["likely_part_number"],
                "confidence": identity.get("confidence"),
                "function": identity.get("function"),
                "datasheet_url": sheet.get("url"),
                "verified_facts": [
                    {"fact": f["fact"], "page": f["page"]} for f in sheet.get("facts", [])
                ],
            }
        )

    return {
        "generator": "schemantic",
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "board": board_name,
        "source_file": Path(payload["source_file"]).name,
        "trust_notes": {
            "connectivity": "parsed mechanically from the schematic netlist -- verified fact",
            "part_identities": "AI-identified from schematic text; confidence attached",
            "rail_voltages": "derived from net NAMES only -- confirm before applying power",
            "datasheet_facts": "verbatim-quoted and mechanically checked against the PDF page",
        },
        "controllers": controllers,
        "buses": buses,
        "power_rails": rails,
        "connectors": connectors,
        "identified_parts": parts,
        "board_links_confirmed": [
            {
                "link": f"{m['board_a_connector']} <-> {m['board_b_connector']}",
                "pin_matches": m["pin_matches"],
            }
            for m in confirmed_mates
        ],
    }


def render_markdown(hw_map: dict) -> str:
    lines = [
        f"# Hardware Map -- {hw_map['board']}",
        "",
        f"Generated by Schemantic from `{hw_map['source_file']}` at {hw_map['generated_at']}.",
        "",
        "## Trust levels",
        "",
    ]
    for key, note in hw_map["trust_notes"].items():
        lines.append(f"- **{key.replace('_', ' ')}**: {note}")

    for controller in hw_map["controllers"]:
        conf = controller.get("identity_confidence")
        conf_txt = f", {conf:.0%} confidence" if conf else ""
        lines += [
            "",
            f"## Controller: {controller['ref']} -- {controller['part'] or 'unidentified'}{conf_txt}",
            "",
        ]
        if controller.get("function"):
            lines.append(f"{controller['function']}")
        if controller.get("datasheet_url"):
            lines.append(f"Datasheet: {controller['datasheet_url']}")
        lines += ["", "| Pin | Net | Bus | Connected to |", "|---|---|---|---|"]
        for pin in controller["pins"]:
            if pin.get("is_no_connect"):
                connected = "(no connect)"
            elif pin["is_power"]:
                connected = "(power rail)"
            else:
                connected = ", ".join(
                    f"{p['ref']}{' (' + p['part'] + ')' if p['part'] else ''}"
                    for p in pin["connected_to"]
                ) or "--"
            lines.append(
                f"| {pin['pin']} | {'/'.join(pin['net_aliases'])} | {pin['bus'] or ''} | {connected} |"
            )

    if hw_map["buses"]:
        lines += ["", "## Buses", ""]
        for bus, data in hw_map["buses"].items():
            lines.append(f"### {bus}")
            for net in data["nets"]:
                lines.append(
                    f"- `{'/'.join(net['aliases'])}` -- {', '.join(net['members'])}"
                )

    lines += ["", "## Power rails (voltages are name-derived -- confirm before applying power)", ""]
    for rail in hw_map["power_rails"]:
        voltage = rail["voltage_from_name"] or "?"
        lines.append(
            f"- `{'/'.join(rail['aliases'])}` -- {voltage}, {rail['member_count']} components"
        )

    if hw_map["connectors"]:
        lines += ["", "## Connectors", ""]
        for connector in hw_map["connectors"]:
            pin_txt = ", ".join(f"{p['pin']}:{p['net']}" for p in connector["pins"])
            lines.append(f"- **{connector['ref']}** ({connector['region'] or 'unlabeled'}): {pin_txt}")

    if hw_map["board_links_confirmed"]:
        lines += ["", "## Confirmed board-to-board links", ""]
        for link in hw_map["board_links_confirmed"]:
            lines.append(f"- {link['link']}")
            for pm in link["pin_matches"]:
                lines.append(
                    f"  - pin {pm['a_pin']} ({pm['a_net']}) <-> pin {pm['b_pin']} ({pm['b_net']})"
                )

    lines += ["", "## Identified parts", ""]
    for part in hw_map["identified_parts"]:
        lines.append(
            f"- **{part['ref']}** -- {part['part']} ({part['confidence']:.0%})"
            + (f" -- {part['datasheet_url']}" if part["datasheet_url"] else "")
        )
        for fact in part["verified_facts"]:
            lines.append(f"  - {fact['fact']} (datasheet p.{fact['page']})")

    return "\n".join(lines) + "\n"
