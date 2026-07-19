# Schemantic

Turns an EDA-exported schematic PDF into something actually understood, not just redrawn: real
component connections, real part identities, and plain-English explanations of what each
subcircuit does — rendered as its own interactive infinite canvas (pan/zoom) built from the
connectivity graph. Click a component and everything it's wired to lights up, with the
connections traced; click a wire and every component on that net highlights.

## What problem this solves

A schematic PDF is a wall of reference designators and wires. Someone unfamiliar with the board
can see that `U6` connects to `IIC_SDA`, but not *what U6 is*, *what package it's in*, or *what
this whole corner of the board is for*. Schemantic answers those questions: click any component
and see what it actually is (with links to check the real part), and click any net to trace exactly
what it's wired to on the canvas.

## How it works — deterministic core, AI layer on top

The most important design decision here: **never let the model guess at something that can be
proven mechanically.**

1. **Netlist extraction (deterministic, zero AI).** Altium Designer's PDF exports embed invisible text
   objects at every pin and component location — `PI<refdes><pin>`, `CO<refdes>`, `NL<netname>` —
   for interactive net-highlighting in a PDF viewer. `schemantic/parser/netlist.py` parses these
   directly from the PDF's text layer. Every component, pin, and net connection is read straight
   from the document, not inferred — verified by hand against the real schematic (see
   `tests/test_netlist_parser.py`, e.g. U6's SDA pin lands on the IIC_SDA bus exactly as drawn, and
   the IMU has exactly the 24 pins its real QFN-24 package has).
2. **Part identification (AI, narrow scope).** `schemantic/agents/part_identity.py` identifies what
   each component actually is — but generic passives (resistors, capacitors, inductors) never hit
   the model at all; there's nothing to identify, a 0402 resistor looks like every other 0402
   resistor. Only ICs, connectors, and named transistors go through the agent (54 of 146 components
   on the reference board). Every identification ships with its reasoning, a confidence score, and
   deterministic search links (Octopart/LCSC/Digi-Key/datasheet) so a guess is always one click from
   being checked against the real part — this is not a verified fact the way the netlist is.
3. **Region explanation (AI, one call per region).** `schemantic/agents/region_explainer.py` explains
   what each labeled subcircuit (the designer's own "POWER," "LIDAR," "Motor" boxes) actually does,
   grounded only in the parts identified within it.
3.5. **Graph + geometry (deterministic).** `schemantic/graph.py` turns the parse into the drawable
   connectivity graph: component bodies sized from their real pin bounds, and per-net wires as a
   minimum spanning tree over the net's actual pin coordinates. The honest contract: topology is
   exact (every drawn segment connects electrically-connected pins, every pin is reachable —
   enforced by `tests/test_graph.py`), while the specific line paths are synthetic, not the
   original drawn routes. Power rails (GND touches all 146 components) are hidden by default and
   render only when explicitly selected, so signal wiring stays readable.
4. **Supervisor.** Every model call runs through `schemantic/supervisor/core.py` — cost budget,
   per-stage spend tracking, thread-safe (identification runs concurrently across a
   `ThreadPoolExecutor`). Full run against the 146-component reference board: **54 model calls,
   $0.07, ~55 seconds.**
5. **Datasheet grounding (AI extracts, mechanics verify).** For every confidently-identified
   part, `schemantic/datasheets.py` locates and fetches the real datasheet (TI's documented URL
   pattern first, then a web search that in testing surfaced manufacturers' own PDFs; LCSC's and
   EasyEDA's part APIs were probed and are 403-blocked — absent deliberately, not overlooked).
   `schemantic/agents/datasheet_digest.py` then extracts up to 8 facts, each carrying a VERBATIM
   quote and page number — and every quote is mechanically re-checked against the cited page of
   the fetched PDF. Facts that fail verification are dropped and counted, never displayed. This is
   the structural fix for the "9-axis that was actually 6-axis" class of error: spec numbers now
   come from the document, with the quote and page shown, or they don't come at all. Datasheets
   cache globally by part number, so a board with three of the same chip fetches once.
6. **Multi-board workspaces (AI proposes, human confirms, graph traverses).** Board-to-board
   connections exist in *neither* schematic — which connector mates with which is a physical
   harness decision. So `schemantic/agents/mate_proposer.py` proposes candidate matings with
   pin-level evidence (GND↔GND, SDA↔SDA, TX↔RX), every proposal is mechanically validated
   against the real connectors before storage (`schemantic/workspace.py` — one wrong pin/net
   reference discards the whole proposal), and **only human-confirmed links become traversable
   edges**. Verified live across the two reference boards: 9 proposals stored with 0 validation
   failures; after confirming the I2C expansion link (P3↔P1), the chat correctly traced
   "U6 on board A → I2C bus → [P3↔P1 link] → board B's I2C bus → through the level shifter →
   the IMU" — a real cross-board electrical path through the confirmed connector only.
7. **"Ask the board" chat (AI, grounded by construction).** `schemantic/agents/chat.py` +
   `schemantic/graph_api.py`: a tool-calling agent that can only answer through deterministic
   queries over the parsed graph (`search_components`, `get_connections`, `path_between`, …), so
   every connectivity claim it makes is one the netlist computed — never model recall. Every
   answer shows its provenance ("checked: get_connections(U6)") in the UI, carries structured
   canvas commands (highlight/trace/fly-to) instead of prose parsing, keeps per-session
   conversation memory so follow-ups like "what is *it* connected to?" resolve, and is capped by
   per-message tool-loop limits plus a server-level spend budget. When the graph has no answer it
   says so — verified: asked "where is the IMU?", it searched "IMU", found nothing, retried
   "inertial", found U1, and disclosed the identification as an AI guess with its confidence.
   Conversations persist in **semantic memory** (`schemantic/chat_memory.py`): every exchange is
   stored in SQLite with an embedding, and each new question is cosine-matched against everything
   stored — so a reworded question weeks later ("up to how many volts can the current sensor
   monitor?") recalls the original answer ("what bus voltage range can U6 measure?") across
   server restarts and sessions. Recalled exchanges are injected as clearly-marked, possibly-stale
   context — the agent still re-verifies with tools, because board state may have changed since.
   Deliberate right-sizing: SQLite + exact cosine, not a vector database — at chat scale
   (thousands of exchanges), exact search is milliseconds; the semantic power is the embeddings,
   not the storage engine. The browser's localStorage is only a display cache; the system of
   record is the server store, and an empty cache rebuilds the transcript from it.

## Honesty notes — real limitations found and either fixed or documented

- **Region assignment is a heuristic, not a fact.** Components are assigned to the nearest labeled
  section title by on-page distance. Tried making this more "sophisticated" with divider-line
  blocking — it fixed one boundary error and broke several others (a divider near a large
  component's own edge wrongly blocked it from its own section). Reverted to plain nearest-neighbor,
  which measured 145/146 correct by hand. Surfaced as inferred in the UI, not asserted as fact.
- **Two real bugs found during verification, both fixed:** a loose substring check for component
  values matched a nearby section title ("10-DOF-IMU-Sensor-D") as a capacitor value because "M" is
  a substring of "IMU"; and about half of all passives on this board have more than one
  value-shaped label within radius, so naive nearest-text picked up a neighboring component's value
  around 50% of the time. Both are covered by regression tests now.
- **The model is sometimes wrong, and says so.** One identification (`M1`) reasoned through a real
  pin-count contradiction — it assumed a mentioned part number implied a 3-pin package, when the
  actual pin count was 8 — and correctly landed on lower confidence (60%) instead of asserting a
  wrong answer confidently. That's the system working as designed: every guess ships with its
  reasoning and a way to check it, not just a bare answer.

## Run it

```bash
# .env with OPENAI_API_KEY must exist in the project root
python -m venv .venv
./.venv/Scripts/pip install -e ".[dev]"
./.venv/Scripts/pytest -q                              # parser regression suite
./.venv/Scripts/python -m uvicorn schemantic.web.app:app --reload
```

Open http://127.0.0.1:8000/. First load runs the full pipeline (~55s, ~$0.07 against the reference
board) and caches the result to `.cache/`; subsequent loads are instant. Delete `.cache/` to force a
fresh run.

## Generalization — tested on a second board

Any Altium-exported schematic PDF can be uploaded through the UI ("Analyze a PDF"); results cache by
content hash. Verified against a second, independently downloaded board (Waveshare General Driver
for Robots): the parser needed **zero code changes** (149 components / 73 nets extracted), and the
part-identity agent correctly identified that board's different parts — QMI8658C IMU vs the first
board's ICM-20948, CP2102N USB-UART vs CH343P — at 95% confidence. Two imperfections were found
and both led to real fixes:

- The model described the QMI8658C as "9-axis" (it's 6-axis) — part number grounded in schematic
  text, but the spec number recalled from parametric memory, which is exactly where LLMs
  confabulate. The identity prompt now forbids numeric specs unless the number appears in the
  provided context, and a **mechanical** cross-check (no AI) flags any claimed package whose pin
  count contradicts the netlist-parsed pin count.
- Region grouping initially forced every component into *some* section. Diagnosis showed the
  second board genuinely draws no title over parts of its area (its captions there are the same
  font size as part numbers, so no font threshold can separate them). Components whose area has no
  reachable title are now honestly reported as "unlabeled" rather than assigned the nearest wrong
  section — 39 of 149 on that board, and, correctly, the two title-less RPi/Jetson headers on the
  reference board.

Uploading an unsupported PDF fails with an explicit message naming the PDF's creator tool, not a
crash — verified against a real KiCad (Eeschema) export and a schematic image pasted into a word
processor, both downloaded from their vendors, both in `tests/test_unsupported_pdfs.py`.

**Format provenance, corrected by evidence:** the token scheme was originally attributed to
EasyEDA by guess. Checking PDF creator metadata across all samples settled it — every
token-carrying PDF says `creator='Altium Designer'`; the KiCad export has no tokens. The docs, the
error messages, and this README were corrected accordingly. Bonus find from the same
investigation: tightening the token charset exposed a latent day-one bug where the `#EN` net's
token was silently rejected and its five pins merged into a neighboring net. Both reference
boards re-verified after the fix (72 and 74 nets), with regression tests locking the corrected
grouping.

## Not yet built

- KiCad/Eagle PDF export support — those tools don't embed a netlist text layer at all (verified
  against a real Eeschema export), so supporting them means the computer-vision path, not a parser
  tweak. Unsupported PDFs are rejected with a clear message naming the creator tool.
- Scanned/rasterized schematics with no embedded text layer — that needs real computer-vision
  symbol recognition, a fundamentally different (and much harder) problem than parsing structured
  text, and out of scope for now.
- Multi-page schematics — the parser currently expects a single-page export.
