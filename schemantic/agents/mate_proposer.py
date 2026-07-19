"""Proposes which connectors on two boards might physically mate.

This is the one place in the pipeline where the answer CANNOT be derived
from the documents -- mating is a harness decision that exists in neither
schematic. So the output is explicitly a proposal: reasoning-first schema,
pin-level evidence for every claim, mechanical validation against the real
connectors before storage (schemantic/workspace.py::validate_proposal), and
nothing becomes a traversable graph edge until a human confirms it in the
UI. The agent is told that proposing nothing is a valid answer.
"""

from __future__ import annotations

import os

from openai import OpenAI
from pydantic import BaseModel, Field

from schemantic.supervisor.core import Supervisor

MODEL = os.getenv("SCHEMANTIC_MATE_MODEL", "gpt-4.1")
_EST_COST_USD = 0.03
_MAX_CONNECTOR_PINS = 40


class PinMatch(BaseModel):
    a_pin: str = Field(description="pin_id on board A's connector, exactly as listed")
    a_net: str = Field(description="that pin's net key on board A, exactly as listed")
    b_pin: str = Field(description="pin_id on board B's connector, exactly as listed")
    b_net: str = Field(description="that pin's net key on board B, exactly as listed")


class MateProposal(BaseModel):
    reasoning: str = Field(
        description="Why these two connectors plausibly mate: pin-count compatibility, "
        "matching connector families in the hints, and signal-role alignment "
        "(power to power, TX to RX crossover, SDA to SDA). Name the evidence."
    )
    board_a_connector: str = Field(description="reference designator on board A, e.g. 'H4'")
    board_b_connector: str = Field(description="reference designator on board B, e.g. 'J1'")
    pin_matches: list[PinMatch] = Field(
        description="Per-pin mapping. Every entry is checked mechanically against the real "
        "connectors; a single wrong pin/net drops the whole proposal."
    )
    confidence: float = Field(ge=0.0, le=1.0)


class MateProposals(BaseModel):
    analysis: str = Field(
        description="Overall reasoning across both boards' connector inventories, written "
        "BEFORE committing to any proposals."
    )
    proposals: list[MateProposal] = Field(
        description="Zero or more plausible matings. An empty list is a legitimate answer -- "
        "do not force pairings between connectors that don't align."
    )


def _connector_inventory(payload: dict) -> list[dict]:
    inventory = []
    for c in payload["components"]:
        prefix = "".join(ch for ch in c["encoded_refdes"] if ch.isalpha()).upper()
        if prefix not in ("P", "H", "J", "TYPE", "DC"):
            continue
        if len(c["pins"]) > _MAX_CONNECTOR_PINS:
            continue
        identity = c.get("identity") or {}
        inventory.append(
            {
                "ref": c["display_label"] or c["encoded_refdes"],
                "pin_count": c["pin_count"],
                "pins": [{"pin_id": p["pin_id"], "net": p["net"]} for p in c["pins"]],
                "hint": identity.get("function"),
                "region": c["region"],
            }
        )
    return inventory


def propose_mates(
    payload_a: dict,
    name_a: str,
    payload_b: dict,
    name_b: str,
    client: OpenAI,
    supervisor: Supervisor,
) -> MateProposals:
    import json

    with supervisor.stage("mate_proposer"):
        response = client.responses.parse(
            model=MODEL,
            input=[
                {
                    "role": "system",
                    "content": "You analyze two circuit boards' connector inventories and propose "
                    "which connectors could physically mate via a cable/harness. Mating is NOT "
                    "recorded in either schematic -- you are proposing hypotheses a human will "
                    "confirm or reject. Only propose pairings with genuine evidence: compatible "
                    "pin counts, aligned signal roles (GND to GND, VCC to VCC at the same "
                    "voltage, TX to RX and RX to TX for UARTs, SDA to SDA for I2C). Copy pin_id "
                    "and net values EXACTLY as given -- every pin match is machine-checked and "
                    "one mismatch discards the proposal. Proposing nothing is acceptable.",
                },
                {
                    "role": "user",
                    "content": (
                        f"Board A ({name_a}) connectors:\n"
                        f"{json.dumps(_connector_inventory(payload_a), indent=1)}\n\n"
                        f"Board B ({name_b}) connectors:\n"
                        f"{json.dumps(_connector_inventory(payload_b), indent=1)}"
                    ),
                },
            ],
            text_format=MateProposals,
        )
        supervisor.spend("mate_proposer", _EST_COST_USD)
    return response.output_parsed
