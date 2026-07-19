"""Extracts a small set of quoted, page-cited facts from a fetched
datasheet's text. Evidence-first field order (quote and page before the
fact they support), same reasoning as every schema in this project.

Every fact is mechanically verified afterwards -- the quote must literally
appear on the claimed page (schemantic/datasheets.py::verify_quote) or the
fact is dropped and counted, never displayed.
"""

from __future__ import annotations

import os

from openai import OpenAI
from pydantic import BaseModel, Field

from schemantic.datasheets import FetchedDatasheet, digest_text, verify_quote
from schemantic.supervisor.core import Supervisor

MODEL = os.getenv("SCHEMANTIC_DIGEST_MODEL", "gpt-4.1-mini")
_EST_COST_USD = 0.012  # ~30k input tokens of datasheet text on the mini tier


class DatasheetFact(BaseModel):
    quote: str = Field(
        description="VERBATIM text copied from the provided pages -- exact characters, "
        "no paraphrase, no ellipsis. This is checked mechanically against the page."
    )
    page: int = Field(description="1-indexed page the quote appears on, per the === PAGE N === markers.")
    fact: str = Field(description="The spec/behavior this quote establishes, one short sentence.")


class DatasheetDigest(BaseModel):
    summary: str = Field(
        description="One or two sentences describing the part, grounded in the provided text."
    )
    facts: list[DatasheetFact] = Field(
        description="Up to 8 of the most useful facts for someone reading a schematic: "
        "supply voltage range, interface, key ratings, package. Fewer is fine."
    )


def digest_datasheet(
    fetched: FetchedDatasheet, client: OpenAI, supervisor: Supervisor
) -> dict:
    """Returns the payload-ready dict: verified facts only, dropped count
    disclosed."""
    with supervisor.stage("datasheets"):
        response = client.responses.parse(
            model=MODEL,
            input=[
                {
                    "role": "system",
                    "content": "You extract key facts from electronic component datasheets. "
                    "Quotes must be copied verbatim from the provided text -- every quote is "
                    "mechanically checked against the page it cites, and facts with quotes "
                    "that don't match are discarded.",
                },
                {
                    "role": "user",
                    "content": f"Datasheet for {fetched.mpn}:\n{digest_text(fetched.page_texts)}",
                },
            ],
            text_format=DatasheetDigest,
        )
        supervisor.spend("datasheets", _EST_COST_USD)

    digest = response.output_parsed
    verified, dropped = [], 0
    for fact in digest.facts:
        if verify_quote(fact.quote, fetched.page_texts, fact.page):
            verified.append(
                {"fact": fact.fact, "quote": fact.quote, "page": fact.page, "verified": True}
            )
        else:
            dropped += 1

    return {
        "mpn": fetched.mpn,
        "url": fetched.url,
        "source": fetched.source,
        "summary": digest.summary,
        "facts": verified,
        "facts_dropped_unverified": dropped,
    }
