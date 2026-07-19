"""Resolves a component to a real-world part identity.

Deliberately narrow scope: generic passives (resistors, capacitors,
inductors) never hit the model at all -- there's nothing to identify, a
0402 resistor looks like every other 0402 resistor, and spending a model
call on it would just be paying to be told that. Only components with real,
distinguishing identity (ICs, connectors, named transistors) go through the
agent. This keeps cost down and keeps the model's job scoped to things it
can actually add value on.

The agent's output is a guess, not a fact -- every PartIdentity ships next
to deterministic search links (schemantic/lookup/links.py) so a low-
confidence or wrong guess is always one click away from being checked
against the genuine article, the same "don't trust the model where you can
check mechanically" principle as everywhere else in this pipeline.
"""

from __future__ import annotations

import os

from openai import OpenAI

from schemantic.agents.schema import PartIdentity
from schemantic.parser.models import Component
from schemantic.supervisor.core import Supervisor

MODEL = os.getenv("SCHEMANTIC_IDENTITY_MODEL", "gpt-4.1-mini")
_EST_COST_USD = 0.001  # rough per-call ceiling for a small structured-output call

_GENERIC_PASSIVE_PREFIXES = ("C", "R", "L")
_GENERIC_DESCRIPTIONS = {
    "C": "Generic SMD capacitor -- ceramic unless otherwise marked.",
    "R": "Generic SMD resistor.",
    "L": "Generic SMD inductor.",
}


def is_generic_passive(component: Component) -> bool:
    refdes = component.encoded_refdes
    return bool(refdes) and refdes[0] in _GENERIC_PASSIVE_PREFIXES and refdes[1:].isdigit()


def generic_passive_identity(component: Component) -> PartIdentity:
    prefix = component.encoded_refdes[0]
    function = _GENERIC_DESCRIPTIONS[prefix]
    if component.nearest_value:
        function += f" Marked value: {component.nearest_value}."
    return PartIdentity(
        reasoning="Reference designator prefix identifies this as a generic passive; "
        "no model call needed -- passives of a given type are visually interchangeable. "
        "Value label picked by nearest on-page distance, not the model.",
        likely_part_number=None,
        manufacturer=None,
        package_type=None,
        function=function,
        confidence=1.0,
    )


def identify_part(
    component: Component, client: OpenAI, supervisor: Supervisor
) -> PartIdentity:
    if is_generic_passive(component):
        return generic_passive_identity(component)

    with supervisor.stage("part_identity"):
        response = client.responses.parse(
            model=MODEL,
            input=[
                {
                    "role": "system",
                    "content": "You identify electronic components from schematic context. "
                    "Only claim a specific manufacturer part number if the evidence "
                    "genuinely supports it -- otherwise say so and lower confidence. "
                    "In the function description, state the part's role generically; "
                    "do NOT assert numeric specifications (axis counts, channel counts, "
                    "bit widths, voltages, frequencies) unless that exact number appears "
                    "in the provided context -- recalled spec numbers are frequently "
                    "wrong even when the part number is right.",
                },
                {
                    "role": "user",
                    "content": (
                        f"Component reference: {component.display_label or component.encoded_refdes}\n"
                        f"Pin count: {len(component.pins)}\n"
                        f"Text visible near this symbol on the schematic: "
                        f"{', '.join(component.nearby_text) or '(none)'}"
                    ),
                },
            ],
            text_format=PartIdentity,
        )
        supervisor.spend("part_identity", _EST_COST_USD)
    return response.output_parsed
