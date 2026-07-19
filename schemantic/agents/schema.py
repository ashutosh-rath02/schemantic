"""Structured-output schemas for the agent layer.

Field order is reasoning-first, conclusion-last on every model, deliberately.
OpenAI structured outputs fill JSON fields in schema declaration order --
putting a verdict before the reasoning meant to justify it lets a model
commit to an answer before it's actually thought about it, which produced
real, observed self-contradictory output in a prior project. Don't reorder
these without a reason.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class PartIdentity(BaseModel):
    reasoning: str = Field(
        description="What in the provided context (nearby text, pin count, package "
        "hints) points to this identification. Say explicitly if the evidence is weak."
    )
    likely_part_number: str | None = Field(
        description="Best-guess manufacturer part number, or null if not determinable."
    )
    manufacturer: str | None = None
    package_type: str | None = Field(
        description="Physical package, e.g. 'SOP-8', 'QFN-24', '0402 SMD'."
    )
    function: str = Field(description="One sentence: what this part does in the circuit.")
    confidence: float = Field(ge=0.0, le=1.0)


class RegionExplanation(BaseModel):
    reasoning: str = Field(
        description="What the components and their identified functions in this region "
        "suggest about its purpose. Note if the evidence is thin or contradictory."
    )
    explanation: str = Field(
        description="Two to three sentences, plain English, explaining what this "
        "subcircuit does and why it's needed on this board."
    )
    confidence: float = Field(ge=0.0, le=1.0)
