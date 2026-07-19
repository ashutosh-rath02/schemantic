"""Workspace + cross-board graph tests -- synthetic two-board fixtures, no
network, no AI. The mechanical proposal validator and the confirmed-mates-
only traversal rule are the safety properties; both are pinned here.
"""

from schemantic import graph_api
from schemantic.workspace import set_mate_status, validate_proposal


def _board(prefix: str) -> dict:
    """Tiny board: MCU (U1) -- UART net -- connector (J1), plus GND."""
    return {
        "source_file": f"{prefix}.pdf",
        "components": [
            {
                "ref_token": f"CO{prefix}U1",
                "encoded_refdes": "U1",
                "display_label": "U1",
                "region": "MCU",
                "pin_count": 2,
                "identity": None,
                "pins": [
                    {"pin_id": "01", "net": f"{prefix}_TX", "x": 0, "y": 0},
                    {"pin_id": "02", "net": "GND", "x": 0, "y": 1},
                ],
                "nets": [
                    {"key": f"{prefix}_TX", "names": [f"{prefix}_TX"], "peer_count": 1, "is_power": False},
                    {"key": "GND", "names": ["GND"], "peer_count": 1, "is_power": True},
                ],
            },
            {
                "ref_token": f"CO{prefix}J1",
                "encoded_refdes": "J1",
                "display_label": "J1",
                "region": "IO",
                "pin_count": 2,
                "identity": None,
                "pins": [
                    {"pin_id": "01", "net": f"{prefix}_TX", "x": 1, "y": 0},
                    {"pin_id": "02", "net": "GND", "x": 1, "y": 1},
                ],
                "nets": [
                    {"key": f"{prefix}_TX", "names": [f"{prefix}_TX"], "peer_count": 1, "is_power": False},
                    {"key": "GND", "names": ["GND"], "peer_count": 1, "is_power": True},
                ],
            },
        ],
        "nets": {
            f"{prefix}_TX": {
                "names": [f"{prefix}_TX"],
                "members": [f"CO{prefix}U1", f"CO{prefix}J1"],
                "pins": [],
                "segments": [],
                "is_power": False,
            },
            "GND": {
                "names": ["GND"],
                "members": [f"CO{prefix}U1", f"CO{prefix}J1"],
                "pins": [],
                "segments": [],
                "is_power": True,
            },
        },
        "regions": {},
    }


PAYLOADS = {"boardA": _board("A"), "boardB": _board("B")}
NAMES = {"boardA": "Master", "boardB": "Slave"}

GOOD_PROPOSAL = {
    "reasoning": "UART out to UART in",
    "board_a_connector": "J1",
    "board_b_connector": "J1",
    "pin_matches": [
        {"a_pin": "01", "a_net": "A_TX", "b_pin": "01", "b_net": "B_TX"},
    ],
    "confidence": 0.8,
}

MATE = {
    "id": "m1",
    "board_a": "boardA",
    "board_b": "boardB",
    "board_a_connector": "J1",
    "board_b_connector": "J1",
    "pin_matches": GOOD_PROPOSAL["pin_matches"],
    "rationale": "UART link",
    "confidence": 0.8,
    "status": "confirmed",
}


def test_validator_accepts_real_pins_and_nets():
    ok, reason = validate_proposal(GOOD_PROPOSAL, PAYLOADS["boardA"], PAYLOADS["boardB"])
    assert ok, reason


def test_validator_rejects_wrong_net_claim():
    bad = {**GOOD_PROPOSAL, "pin_matches": [
        {"a_pin": "01", "a_net": "NOT_THE_NET", "b_pin": "01", "b_net": "B_TX"}
    ]}
    ok, reason = validate_proposal(bad, PAYLOADS["boardA"], PAYLOADS["boardB"])
    assert not ok
    assert "NOT_THE_NET" in reason


def test_validator_rejects_phantom_connector():
    bad = {**GOOD_PROPOSAL, "board_b_connector": "J99"}
    ok, _ = validate_proposal(bad, PAYLOADS["boardA"], PAYLOADS["boardB"])
    assert not ok


def test_cross_board_path_via_confirmed_mate():
    result = graph_api.cross_board_path(
        PAYLOADS, NAMES, [MATE], "U1", "Master", "U1", "Slave"
    )
    assert result["found"] is True
    joined = " > ".join(result["path"])
    assert "U1@Master" in result["path"][0]
    assert "link]" in joined  # the mate bridge is named in the path
    assert "U1@Slave" in result["path"][-1]


def test_cross_board_path_refuses_unconfirmed_mates():
    proposed = {**MATE, "status": "proposed"}
    # caller passes confirmed mates only; simulate the endpoint's filter
    confirmed = [m for m in [proposed] if m["status"] == "confirmed"]
    result = graph_api.cross_board_path(
        PAYLOADS, NAMES, confirmed, "U1", "Master", "U1", "Slave"
    )
    assert result["found"] is False
    assert "no confirmed links" in result["note"]


def test_set_mate_status_rejects_unknown_status():
    ws = {"boards": [], "mates": [dict(MATE)]}
    assert not set_mate_status(ws, "m1", "definitely")
    assert set_mate_status(ws, "m1", "rejected")
