"""Explains what a functional block (region) on the schematic actually does,
given the identified parts within it. One call per region, not per
component -- region count is small and bounded (this board has 16), so cost
stays predictable regardless of component count.
"""

from __future__ import annotations

import os

from openai import OpenAI

from schemantic.agents.schema import PartIdentity, RegionExplanation
from schemantic.supervisor.core import Supervisor

MODEL = os.getenv("SCHEMANTIC_EXPLAINER_MODEL", "gpt-4.1-mini")
_EST_COST_USD = 0.001


def explain_region(
    region_name: str,
    component_identities: list[tuple[str, PartIdentity]],
    client: OpenAI,
    supervisor: Supervisor,
) -> RegionExplanation:
    parts_summary = "\n".join(
        f"- {label}: {identity.function}"
        + (f" ({identity.likely_part_number})" if identity.likely_part_number else "")
        for label, identity in component_identities
    )
    with supervisor.stage("region_explainer"):
        response = client.responses.parse(
            model=MODEL,
            input=[
                {
                    "role": "system",
                    "content": "You explain what a labeled subcircuit on a schematic does, "
                    "for someone unfamiliar with electronics reading it. Ground your "
                    "explanation only in the parts actually listed -- don't invent "
                    "capabilities the listed parts don't support.",
                },
                {
                    "role": "user",
                    "content": (
                        f"Region label on the schematic: {region_name!r}\n"
                        f"Identified parts in this region:\n{parts_summary or '(none identified)'}"
                    ),
                },
            ],
            text_format=RegionExplanation,
        )
        supervisor.spend("region_explainer", _EST_COST_USD)
    return response.output_parsed
