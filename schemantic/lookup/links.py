"""Deterministic part-photo/datasheet lookup links.

Not an API call, not a model guess -- just URL construction from a part
number string. Always correct, always works, no rate limit, no API key.
This is the "go see the real thing yourself" link the AI-generated part
identity card (schemantic/agents/part_identity.py) links out to, so a wrong
or uncertain AI guess is always one click away from being checked against
the genuine article.
"""

from __future__ import annotations

from urllib.parse import quote_plus


def lookup_links(query: str) -> dict[str, str]:
    q = quote_plus(query)
    return {
        "octopart": f"https://octopart.com/search?q={q}",
        "lcsc": f"https://www.lcsc.com/search?q={q}",
        "digikey": f"https://www.digikey.com/en/products/result?keywords={q}",
        "google_images": f"https://www.google.com/search?tbm=isch&q={q}",
        "datasheet_search": f"https://www.google.com/search?q={q}+datasheet+pdf",
    }
