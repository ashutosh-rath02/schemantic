"""Datasheet location, fetch, and mechanical quote verification.

No AI in this module. The LLM digest layer sits in
schemantic/agents/datasheet_digest.py; every fact it produces comes back
here to be verified -- the quote must literally appear on the claimed page
of the fetched PDF (whitespace/case-normalized), or the fact is dropped.
That is the same citation-faithfulness mechanism that made claim extraction
trustworthy in the predecessor project: specs are quoted from the document,
never recalled from model weights (the "9-axis that was actually 6" lesson).

Provider chain, ordered by what probing showed actually works server-side:
  1. TI's documented-stable symlink pattern (ti.com/lit/ds/symlink/<slug>.pdf)
  2. DuckDuckGo HTML search, first .pdf result (found the manufacturer's own
     PDF for the hardest test part). Fetched sequentially with a delay --
     burst-parallel search requests are how you get rate-limited.
LCSC's and EasyEDA's part APIs were probed and are 403-blocked; they are
deliberately absent, not overlooked.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote

import fitz
import httpx

CACHE_DIR = Path(__file__).parent.parent / ".cache" / "datasheets"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Schemantic/0.1"
FETCH_TIMEOUT_S = 20.0
SEARCH_DELAY_S = 1.5          # politeness gap between DDG searches
MAX_PDF_BYTES = 30_000_000
MAX_PAGES_FOR_DIGEST = 10     # title/features/description/pinout live up front
MAX_CHARS_FOR_DIGEST = 40_000


@dataclass
class FetchedDatasheet:
    mpn: str
    url: str
    source: str                # "ti-pattern" | "web-search:<domain>"
    local_path: Path
    page_texts: list[str]      # first MAX_PAGES_FOR_DIGEST pages


def _slug(mpn: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "_", mpn.strip())


def _ti_candidate_slugs(mpn: str) -> list[str]:
    """INA219BIDR -> [ina219bidr, ina219bid, ..., ina219] -- TI's symlink
    accepts the family name; order tries most-specific first."""
    base = mpn.strip().lower()
    candidates = [base]
    trimmed = base
    while len(trimmed) > 4 and trimmed[-1].isalpha():
        trimmed = trimmed[:-1]
        candidates.append(trimmed)
    return candidates[:6]


def _try_ti_pattern(client: httpx.Client, mpn: str) -> tuple[str, str] | None:
    for slug in _ti_candidate_slugs(mpn):
        url = f"https://www.ti.com/lit/ds/symlink/{slug}.pdf"
        try:
            head = client.head(url, timeout=FETCH_TIMEOUT_S)
        except httpx.HTTPError:
            return None  # network trouble: don't hammer remaining slugs
        if head.status_code == 200 and "pdf" in head.headers.get("content-type", ""):
            return url, "ti-pattern"
    return None


_DDG_PDF_LINK = re.compile(r'uddg=([^&"]+)')


def _try_web_search(client: httpx.Client, mpn: str) -> tuple[str, str] | None:
    try:
        response = client.get(
            "https://html.duckduckgo.com/html/",
            params={"q": f"{mpn} datasheet pdf"},
            timeout=FETCH_TIMEOUT_S,
        )
    except httpx.HTTPError:
        return None
    if response.status_code != 200:
        return None
    for match in _DDG_PDF_LINK.finditer(response.text):
        url = unquote(match.group(1))
        if url.lower().split("?")[0].endswith(".pdf"):
            domain = re.sub(r"^https?://([^/]+)/.*", r"\1", url)
            return url, f"web-search:{domain}"
    return None


def _download_pdf(client: httpx.Client, url: str, dest: Path) -> bool:
    try:
        response = client.get(url, timeout=FETCH_TIMEOUT_S)
    except httpx.HTTPError:
        return False
    body = response.content
    if response.status_code != 200 or len(body) > MAX_PDF_BYTES or not body.startswith(b"%PDF"):
        return False
    dest.write_bytes(body)
    return True


def _page_texts(pdf_path: Path) -> list[str]:
    doc = fitz.open(str(pdf_path))
    texts = []
    for page in doc[:MAX_PAGES_FOR_DIGEST]:
        texts.append(page.get_text())
    return texts


def fetch_datasheet(mpn: str) -> FetchedDatasheet | None:
    """Locate + download + text-extract, cached by MPN. Returns None (never
    a fabricated result) when nothing verifiable was found."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    slug = _slug(mpn)
    pdf_path = CACHE_DIR / f"{slug}.pdf"
    meta_path = CACHE_DIR / f"{slug}.meta.json"

    if meta_path.exists():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        if meta.get("not_found"):
            return None
        if pdf_path.exists():
            return FetchedDatasheet(
                mpn=mpn,
                url=meta["url"],
                source=meta["source"],
                local_path=pdf_path,
                page_texts=_page_texts(pdf_path),
            )

    with httpx.Client(headers={"User-Agent": USER_AGENT}, follow_redirects=True) as client:
        located = _try_ti_pattern(client, mpn)
        if located is None:
            time.sleep(SEARCH_DELAY_S)
            located = _try_web_search(client, mpn)
        if located is None or not _download_pdf(client, located[0], pdf_path):
            meta_path.write_text(json.dumps({"not_found": True}), encoding="utf-8")
            return None

    url, source = located
    try:
        page_texts = _page_texts(pdf_path)
    except Exception:  # noqa: BLE001 -- corrupt/odd PDF: treat as not found
        pdf_path.unlink(missing_ok=True)
        meta_path.write_text(json.dumps({"not_found": True}), encoding="utf-8")
        return None

    meta_path.write_text(json.dumps({"url": url, "source": source}), encoding="utf-8")
    return FetchedDatasheet(mpn=mpn, url=url, source=source, local_path=pdf_path, page_texts=page_texts)


_NORMALIZE = re.compile(r"[^a-z0-9.%±+/-]+")


def _normalize(text: str) -> str:
    return _NORMALIZE.sub(" ", text.lower()).strip()


def verify_quote(quote: str, page_texts: list[str], page_number: int) -> bool:
    """Mechanical check: the (normalized) quote must appear on the claimed
    1-indexed page. A fact that fails this is dropped, not shown."""
    if not 1 <= page_number <= len(page_texts):
        return False
    return _normalize(quote) in _normalize(page_texts[page_number - 1])


def digest_text(page_texts: list[str]) -> str:
    """Page-tagged text handed to the digest model, capped."""
    parts = []
    total = 0
    for i, text in enumerate(page_texts, start=1):
        chunk = f"\n=== PAGE {i} ===\n{text.strip()}\n"
        if total + len(chunk) > MAX_CHARS_FOR_DIGEST:
            break
        parts.append(chunk)
        total += len(chunk)
    return "".join(parts)
