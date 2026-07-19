"""Orchestrates the full pipeline: deterministic parse -> concurrent part
identification -> region explanation -> cached enriched output.

Concurrency: part identification runs across a ThreadPoolExecutor since
each call is independent I/O (matches the pattern that made the pipeline
stages in the prior project fast). Region explanation runs after, since it
depends on the identified parts within each region.

Caching: results are cached to a JSON file keyed by the source PDF's content
hash, so re-running the web UI doesn't re-spend API budget on every reload.
Delete the cache file to force a fresh run.
"""

from __future__ import annotations

import hashlib
import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import fitz
from openai import OpenAI

from schemantic.agents.datasheet_digest import digest_datasheet
from schemantic.agents.part_identity import identify_part
from schemantic.agents.region_explainer import explain_region
from schemantic.agents.schema import PartIdentity, RegionExplanation
from schemantic.datasheets import fetch_datasheet
from schemantic.graph import build_graph, component_box
from schemantic.lookup.links import lookup_links
from schemantic.parser.models import Schematic
from schemantic.parser.netlist import parse_schematic_pdf
from schemantic.supervisor.core import Supervisor

CACHE_DIR = Path(__file__).parent.parent / ".cache"
SCHEMA_VERSION = 6  # bump when payload shape, prompts, OR parse semantics change -- invalidates cache

# Only confidently-identified parts get a datasheet lookup -- fetching a
# datasheet for a wrong part number would ground facts in the wrong document.
DATASHEET_MIN_CONFIDENCE = 0.75


def _pdf_hash(path: str) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()[:16]


def render_background_image(pdf_path: str, scale: float = 2.0) -> Path:
    """The real schematic, rasterized, as the canvas background -- the
    interactive layer overlays exactly on top of it at the same coordinates
    the parser already extracted. Showing the actual drawing beats trying to
    redraw it: real symbols, real wires, real layout, for free.
    """
    image_path = CACHE_DIR / f"{_pdf_hash(pdf_path)}_bg.png"
    if image_path.exists():
        return image_path
    CACHE_DIR.mkdir(exist_ok=True)
    doc = fitz.open(pdf_path)
    pix = doc[0].get_pixmap(matrix=fitz.Matrix(scale, scale))
    pix.save(str(image_path))
    return image_path


def enrich_schematic(
    schematic: Schematic,
    cache_key: str,
    max_spend_usd: float = 2.0,
    workers: int = 8,
    use_cache: bool = True,
) -> dict:
    """Format-agnostic enrichment core: identities, regions, datasheets,
    assembly, caching. The PDF and KiCad ingestion paths both end here."""
    cache_path = CACHE_DIR / f"{cache_key}_v{SCHEMA_VERSION}.json"
    if use_cache and cache_path.exists():
        return json.loads(cache_path.read_text(encoding="utf-8"))

    client = OpenAI()
    supervisor = Supervisor(max_spend_usd=max_spend_usd)

    identities = _identify_all_parts(schematic, client, supervisor, workers)
    region_explanations = _explain_all_regions(schematic, identities, client, supervisor)
    datasheets = _fetch_all_datasheets(identities, client, supervisor)

    result = _assemble(schematic, identities, region_explanations, datasheets, supervisor)

    CACHE_DIR.mkdir(exist_ok=True)
    cache_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


def build_enriched_schematic(
    pdf_path: str,
    max_spend_usd: float = 2.0,
    workers: int = 8,
    use_cache: bool = True,
) -> dict:
    cache_path = CACHE_DIR / f"{_pdf_hash(pdf_path)}_v{SCHEMA_VERSION}.json"
    if use_cache and cache_path.exists():
        return json.loads(cache_path.read_text(encoding="utf-8"))

    schematic = parse_schematic_pdf(pdf_path)
    return enrich_schematic(
        schematic, _pdf_hash(pdf_path), max_spend_usd, workers, use_cache
    )


def _identify_all_parts(
    schematic: Schematic, client: OpenAI, supervisor: Supervisor, workers: int
) -> dict[str, PartIdentity]:
    identities: dict[str, PartIdentity] = {}

    def _one(component):
        return component.ref_token, identify_part(component, client, supervisor)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_one, c) for c in schematic.components]
        for future in as_completed(futures):
            try:
                ref_token, identity = future.result()
                identities[ref_token] = identity
            except Exception as exc:  # noqa: BLE001 -- one failed unit shouldn't sink the batch
                print(f"part identification failed for one component: {exc}")

    return identities


def _explain_all_regions(
    schematic: Schematic,
    identities: dict[str, PartIdentity],
    client: OpenAI,
    supervisor: Supervisor,
) -> dict[str, RegionExplanation]:
    by_region: dict[str, list[tuple[str, PartIdentity]]] = {}
    for component in schematic.components:
        identity = identities.get(component.ref_token)
        if identity is None or component.region is None:
            # unlabeled areas get no AI narrative -- there's no designer
            # intent to explain, and inventing one would be exactly the
            # kind of forced guess the None region exists to avoid
            continue
        label = component.display_label or component.encoded_refdes
        by_region.setdefault(component.region, []).append((label, identity))

    explanations: dict[str, RegionExplanation] = {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {
            pool.submit(explain_region, region, parts, client, supervisor): region
            for region, parts in by_region.items()
        }
        for future in as_completed(futures):
            region = futures[future]
            try:
                explanations[region] = future.result()
            except Exception as exc:  # noqa: BLE001
                print(f"region explanation failed for {region!r}: {exc}")

    return explanations


_PIN_COUNT_RANGE = (2, 64)  # plausible package pin counts; excludes e.g. "0402"


def _package_pin_mismatch(identity: PartIdentity | None, parsed_pin_count: int) -> bool:
    """Mechanical cross-check, no AI: if the claimed package names a pin
    count (SOP-8, QFN-24, "39 pins") that contradicts the pin count parsed
    from the netlist, flag it for the UI. The M1 case showed the model can
    reason itself into a wrong package under a pin-count contradiction --
    this makes the contradiction visible instead of trusting the prose.
    """
    if identity is None or not identity.package_type:
        return False
    claimed = [
        int(n)
        for n in re.findall(r"\d+", identity.package_type)
        if _PIN_COUNT_RANGE[0] <= int(n) <= _PIN_COUNT_RANGE[1]
    ]
    return bool(claimed) and parsed_pin_count not in claimed


def _fetch_all_datasheets(
    identities: dict[str, PartIdentity], client: OpenAI, supervisor: Supervisor
) -> dict[str, dict]:
    """MPN -> digest dict. Deduped: a board with three CH343P chips fetches
    and digests that datasheet once. Sequential on purpose -- the web-search
    fallback rate-limits burst traffic, and the per-MPN cache makes reruns
    free anyway."""
    unique_mpns: dict[str, None] = {}
    for identity in identities.values():
        mpn = identity.likely_part_number
        if mpn and identity.confidence >= DATASHEET_MIN_CONFIDENCE:
            unique_mpns.setdefault(mpn.strip(), None)

    digests: dict[str, dict] = {}
    for mpn in unique_mpns:
        try:
            fetched = fetch_datasheet(mpn)
            if fetched is None:
                continue
            digests[mpn] = digest_datasheet(fetched, client, supervisor)
        except Exception as exc:  # noqa: BLE001 -- one bad PDF shouldn't sink the batch
            print(f"datasheet step failed for {mpn}: {exc}")
    return digests


def _assemble(
    schematic: Schematic,
    identities: dict[str, PartIdentity],
    region_explanations: dict[str, RegionExplanation],
    datasheets: dict[str, dict],
    supervisor: Supervisor,
) -> dict:
    nets_out, pin_to_net = build_graph(schematic)

    components_out = []
    for c in schematic.components:
        identity = identities.get(c.ref_token)
        query = (identity.likely_part_number if identity else None) or c.display_label or c.encoded_refdes
        own_nets = [
            {
                "key": key,
                "names": payload["names"],
                "peer_count": len(payload["members"]) - 1,
                "is_power": payload["is_power"],
            }
            for key, payload in nets_out.items()
            if c.ref_token in payload["members"]
        ]
        # signal nets first (the interesting connections), power rails last
        own_nets.sort(key=lambda n: (n["is_power"], -n["peer_count"]))
        components_out.append(
            {
                "ref_token": c.ref_token,
                "encoded_refdes": c.encoded_refdes,
                "display_label": c.display_label,
                "x": c.x,
                "y": c.y,
                "box": component_box(c),
                "region": c.region,
                "pin_count": len(c.pins),
                "pins": [
                    {
                        "pin_id": p.pin_id,
                        "x": p.x,
                        "y": p.y,
                        "net": pin_to_net.get(p.ref_token),
                    }
                    for p in c.pins
                ],
                "identity": identity.model_dump() if identity else None,
                "pin_count_mismatch": _package_pin_mismatch(identity, len(c.pins)),
                "datasheet": datasheets.get(
                    (identity.likely_part_number or "").strip() if identity else ""
                ),
                "lookup_links": lookup_links(query),
                "nets": own_nets,
            }
        )

    region_bounds: dict[str, dict] = {}
    for c in schematic.components:
        if c.region is None:
            continue
        b = region_bounds.setdefault(
            c.region, {"x0": c.x, "y0": c.y, "x1": c.x, "y1": c.y}
        )
        b["x0"], b["x1"] = min(b["x0"], c.x), max(b["x1"], c.x)
        b["y0"], b["y1"] = min(b["y0"], c.y), max(b["y1"], c.y)

    regions_out = {
        region: {
            **explanation.model_dump(),
            "bounds": region_bounds.get(region, {}),
        }
        for region, explanation in region_explanations.items()
    }

    return {
        "source_file": schematic.source_file,
        "page_width": schematic.page_width,
        "page_height": schematic.page_height,
        "components": components_out,
        "nets": nets_out,
        "regions": regions_out,
        "manifest": supervisor.report(),
    }
