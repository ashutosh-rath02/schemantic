"""Schemantic web UI -- an interactive canvas over parsed + AI-enriched
schematics, with a multi-board workspace.

    uvicorn schemantic.web.app:app --reload

Any Altium-exported schematic PDF works: upload one through the UI, or set
SCHEMANTIC_PDF to change the default board. Results are cached by content
hash, so re-analyzing a previously seen PDF is instant. Every analyzed
board joins the workspace; board-to-board links are AI-proposed and
human-confirmed (see schemantic/workspace.py for why they can't be parsed).
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from openai import OpenAI
from pydantic import BaseModel

load_dotenv()

from schemantic import workspace as ws_store  # noqa: E402
from schemantic.agents.chat import ChatSession, chat_turn  # noqa: E402
from schemantic.agents.mate_proposer import propose_mates  # noqa: E402
from schemantic.chat_memory import ChatMemoryStore  # noqa: E402
from schemantic.design_checks import run_all_checks  # noqa: E402
from schemantic.hardwaremap import build_hardware_map, render_markdown  # noqa: E402
from schemantic.kicad import load_kicad_project  # noqa: E402
from schemantic.pipeline import (  # noqa: E402
    CACHE_DIR,
    build_enriched_schematic,
    enrich_schematic,
    render_background_image,
)
from schemantic.supervisor.core import BudgetExceeded, Supervisor  # noqa: E402

EMBED_MODEL = os.getenv("SCHEMANTIC_EMBED_MODEL", "text-embedding-3-small")

BASE_DIR = Path(__file__).parent
REPO_ROOT = BASE_DIR.parent.parent
DEFAULT_PDF = Path(os.getenv("SCHEMANTIC_PDF", REPO_ROOT / "ROS_Driver_for_Robots.pdf"))
UPLOAD_DIR = CACHE_DIR / "uploads"


class NoCacheStaticFiles(StaticFiles):
    """Static assets change on redeploy but keep the same URLs -- a browser
    that cached the old CSS/JS renders a broken hybrid of old and new UI
    (observed). No-cache forces revalidation; fine at this scale."""

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


app = FastAPI(title="Schemantic")
app.mount("/static", NoCacheStaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")

# board_id -> {"path": str, "payload": dict}; the workspace file persists
# board identity across restarts, payloads reload lazily from cache.
_boards: dict[str, dict] = {}
_active_board_id: str | None = None
_workspace = ws_store.load_workspace()


def _register_board(pdf_path: str) -> str:
    """Analyze (cache-hit if seen) + add to workspace. Returns board id."""
    global _workspace
    payload = build_enriched_schematic(pdf_path, use_cache=True)
    board_id = ws_store.board_id_for(pdf_path)
    _boards[board_id] = {"path": pdf_path, "payload": payload}
    _workspace = ws_store.add_board(_workspace, pdf_path)
    ws_store.save_workspace(_workspace)
    return board_id


def _ensure_default_board() -> str:
    global _active_board_id
    if _active_board_id is None:
        _active_board_id = _register_board(str(DEFAULT_PDF))
        # boards from previous sessions rejoin silently if their cache exists
        for bid, payload in ws_store.load_board_payloads(_workspace).items():
            if bid not in _boards:
                board = next(b for b in _workspace["boards"] if b["id"] == bid)
                _boards[bid] = {"path": board["pdf_path"], "payload": payload}
    return _active_board_id


def _active_payload() -> dict:
    return _boards[_ensure_default_board() if _active_board_id is None else _active_board_id]["payload"]


def _board_names() -> dict[str, str]:
    return {b["id"]: b["name"] for b in _workspace["boards"] if b["id"] in _boards}


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
def index(request: Request, error: str | None = None):
    _ensure_default_board()
    result = _active_payload()
    totals = {
        "components": len(result["components"]),
        "nets": len(result["nets"]),
        "regions": len(result["regions"]),
        "spend_usd": result["manifest"]["total_spend_usd"],
    }
    return templates.TemplateResponse(
        request,
        "schematic.html",
        {
            "totals": totals,
            "source_name": Path(result["source_file"]).name,
            "error": error,
        },
    )


def _register_kicad_board(saved: Path, source_name: str) -> str:
    """KiCad path: .net (+ optional .kicad_pcb) -> same payload shape,
    same enrichment, same everything downstream."""
    global _workspace
    import zipfile

    if saved.suffix.lower() == ".zip":
        extract_dir = saved.with_suffix("")
        extract_dir.mkdir(exist_ok=True)
        with zipfile.ZipFile(saved) as zf:
            for name in zf.namelist():
                base = Path(name).name  # flatten: no traversal, no dirs
                if base and Path(base).suffix.lower() in (".net", ".kicad_pcb"):
                    (extract_dir / base).write_bytes(zf.read(name))
        paths = list(extract_dir.iterdir())
    else:
        paths = [saved]

    schematic = load_kicad_project(paths, source_name)
    payload = enrich_schematic(schematic, ws_store.board_id_for(str(saved)))
    board_id = ws_store.board_id_for(str(saved))
    _boards[board_id] = {"path": str(saved), "payload": payload}
    _workspace = ws_store.add_board(_workspace, str(saved))
    ws_store.save_workspace(_workspace)
    return board_id


@app.post("/upload")
async def upload(file: UploadFile):
    global _active_board_id
    _ensure_default_board()
    filename = file.filename or ""
    suffix = Path(filename).suffix.lower()
    if suffix not in (".pdf", ".zip", ".net"):
        return RedirectResponse(
            url="/?error=Supported+uploads:+Altium+PDF,+KiCad+.net,+or+.zip+with+.net+%2B+.kicad_pcb",
            status_code=303,
        )
    content = await file.read()
    digest = hashlib.sha256(content).hexdigest()[:16]
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    saved = UPLOAD_DIR / f"{digest}_{Path(filename).name}"
    saved.write_bytes(content)
    try:
        if suffix == ".pdf":
            _active_board_id = _register_board(str(saved))
        else:
            _active_board_id = _register_kicad_board(saved, Path(filename).stem)
    except ValueError as exc:  # unsupported format -- honest, actionable message
        return RedirectResponse(url=f"/?error={str(exc)[:300]}", status_code=303)
    return RedirectResponse(url="/", status_code=303)


@app.get("/api/schematic")
def api_schematic() -> JSONResponse:
    _ensure_default_board()
    return JSONResponse(_active_payload())


@app.get("/api/hardwaremap")
def api_hardwaremap(format: str = "md"):
    """The board's verified facts packaged for a coding agent: controller
    pin tables, buses, rails, connectors, datasheet links. Deterministic
    assembly -- no model call. ?format=json for the structured form."""
    _ensure_default_board()
    names = _board_names()
    board_name = names.get(_active_board_id, "board")
    mates = [
        m for m in ws_store.confirmed_mates(_workspace)
        if _active_board_id in (m["board_a"], m["board_b"])
    ]
    hw_map = build_hardware_map(_active_payload(), board_name, mates)
    if format == "json":
        return JSONResponse(hw_map)
    markdown = render_markdown(hw_map)
    return HTMLResponse(
        markdown,
        media_type="text/markdown",
        headers={"Content-Disposition": f'attachment; filename="hardwaremap_{board_name}.md"'},
    )


@app.get("/api/firmware-starter")
def api_firmware_starter():
    """Starter firmware zip generated from the hardware map -- grounded in
    verified connectivity, TODO-marked where the map can't know, and
    labeled a starting point in every file."""
    import io
    import zipfile

    from schemantic.agents.firmware_starter import generate_starter

    _ensure_default_board()
    names = _board_names()
    board_name = names.get(_active_board_id, "board")
    mates = [
        m for m in ws_store.confirmed_mates(_workspace)
        if _active_board_id in (m["board_a"], m["board_b"])
    ]
    hw_map = build_hardware_map(_active_payload(), board_name, mates)
    if not hw_map["controllers"]:
        return JSONResponse(
            {"error": "no controller identified on this board -- nothing to target"},
            status_code=400,
        )
    try:
        starter = generate_starter(hw_map, _client(), _agent_supervisor)
    except BudgetExceeded:
        return JSONResponse({"error": "agent budget exhausted"}, status_code=429)

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in starter.files:
            safe_path = f.path.replace("..", "").lstrip("/\\")
            zf.writestr(safe_path, f.content)
        zf.writestr(
            "GENERATED_README.md",
            f"# Starter firmware for {board_name}\n\nFramework: {starter.framework}\n\n"
            f"{starter.reasoning}\n\nGenerated by Schemantic from the board's hardware map. "
            "A starting point -- verify pins against the schematic and every TODO against "
            "the datasheets before flashing.\n",
        )
    buffer.seek(0)
    from fastapi.responses import Response

    return Response(
        buffer.read(),
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="starter_{board_name}.zip"'
        },
    )


@app.get("/api/design-checks")
def api_design_checks() -> JSONResponse:
    """Pre-fab findings: mechanical (verified-connectivity-derived) and
    heuristic (AI-identity-derived) tiers, each labeled. Not an ERC/DRC
    replacement -- see schemantic/design_checks.py for the exact scope."""
    _ensure_default_board()
    return JSONResponse(run_all_checks(_active_payload()))


@app.get("/schematic-image")
def schematic_image():
    _ensure_default_board()
    source = _boards[_active_board_id]["path"]
    if not source.lower().endswith(".pdf"):
        return JSONResponse(
            {"error": "no original PDF for this board (KiCad source ingestion)"},
            status_code=404,
        )
    path = render_background_image(source)
    return FileResponse(path, media_type="image/png")


# ---- workspace / multi-board ----

_agent_client: OpenAI | None = None
_agent_supervisor = Supervisor(max_spend_usd=5.0)


def _client() -> OpenAI:
    global _agent_client
    if _agent_client is None:
        _agent_client = OpenAI()
    return _agent_client


@app.get("/api/workspace")
def api_workspace() -> JSONResponse:
    _ensure_default_board()
    names = _board_names()
    return JSONResponse(
        {
            "boards": [
                {"id": bid, "name": names.get(bid, bid), "active": bid == _active_board_id}
                for bid in _boards
            ],
            "mates": [
                {**m, "board_a_name": names.get(m["board_a"], m["board_a"]),
                 "board_b_name": names.get(m["board_b"], m["board_b"])}
                for m in _workspace["mates"]
                if m["board_a"] in _boards and m["board_b"] in _boards
            ],
        }
    )


class SwitchRequest(BaseModel):
    board_id: str


@app.post("/api/workspace/switch")
def api_switch(req: SwitchRequest) -> JSONResponse:
    global _active_board_id
    _ensure_default_board()
    if req.board_id not in _boards:
        return JSONResponse({"error": "unknown board"}, status_code=404)
    _active_board_id = req.board_id
    return JSONResponse({"ok": True})


@app.post("/api/workspace/propose-mates")
def api_propose_mates() -> JSONResponse:
    """Runs the proposer across the two most relevant boards (v1: exactly
    two boards in workspace). Proposals are mechanically validated before
    storage; invalid ones are dropped and counted."""
    # no `global _workspace` needed here: store_proposals mutates the dict
    # in place (appends to ws["mates"]), it doesn't return a new object
    _ensure_default_board()
    board_ids = list(_boards.keys())
    if len(board_ids) < 2:
        return JSONResponse(
            {"error": "need at least two analyzed boards to propose links"}, status_code=400
        )
    bid_a, bid_b = board_ids[0], board_ids[1]
    names = _board_names()
    try:
        result = propose_mates(
            _boards[bid_a]["payload"], names[bid_a],
            _boards[bid_b]["payload"], names[bid_b],
            _client(), _agent_supervisor,
        )
    except BudgetExceeded:
        return JSONResponse({"error": "agent budget exhausted"}, status_code=429)

    # drop re-proposals of connector pairs that already have a mate record
    existing = {
        (m["board_a_connector"], m["board_b_connector"])
        for m in _workspace["mates"]
        if {m["board_a"], m["board_b"]} == {bid_a, bid_b}
    }
    fresh = [
        p.model_dump()
        for p in result.proposals
        if (p.board_a_connector, p.board_b_connector) not in existing
    ]
    stored, dropped = ws_store.store_proposals(
        _workspace, bid_a, bid_b, fresh,
        {bid_a: _boards[bid_a]["payload"], bid_b: _boards[bid_b]["payload"]},
    )
    ws_store.save_workspace(_workspace)
    return JSONResponse(
        {
            "analysis": result.analysis,
            "stored": stored,
            "dropped_invalid": dropped,
            "skipped_existing": len(result.proposals) - len(fresh),
        }
    )


class MateDecision(BaseModel):
    status: str  # "confirmed" | "rejected"


@app.post("/api/mates/{mate_id}")
def api_mate_decision(mate_id: str, req: MateDecision) -> JSONResponse:
    # no `global _workspace` needed here either -- same reason: in-place mutation
    if req.status not in ("confirmed", "rejected"):
        return JSONResponse({"error": "status must be confirmed or rejected"}, status_code=400)
    if not ws_store.set_mate_status(_workspace, mate_id, req.status):
        return JSONResponse({"error": "unknown mate id"}, status_code=404)
    ws_store.save_workspace(_workspace)
    return JSONResponse({"ok": True})


# ---- chat ----

_chat_sessions: dict[str, ChatSession] = {}
_chat_memory: ChatMemoryStore | None = None


def _memory() -> ChatMemoryStore:
    """Semantic chat memory, persisted to SQLite -- survives restarts. The
    embedder is budget-tracked like every other model call."""
    global _chat_memory
    if _chat_memory is None:

        def embed(text: str) -> list[float]:
            response = _client().embeddings.create(model=EMBED_MODEL, input=text[:8000])
            _agent_supervisor.spend("chat_memory", 0.0001)
            return response.data[0].embedding

        _chat_memory = ChatMemoryStore(CACHE_DIR / "chat_memory.db", embed)
    return _chat_memory


class ChatRequest(BaseModel):
    message: str
    session_id: str | None = None


@app.get("/api/chat/history")
def api_chat_history(session_id: str) -> JSONResponse:
    """Transcript from persistent memory -- what makes localStorage a mere
    display cache rather than the system of record."""
    return JSONResponse({"exchanges": _memory().history(session_id)})


@app.post("/api/chat")
def api_chat(req: ChatRequest) -> JSONResponse:
    _ensure_default_board()
    session = _chat_sessions.get(req.session_id or "")
    if session is None:
        session = ChatSession()
        _chat_sessions[session.session_id] = session
    payloads = {bid: b["payload"] for bid, b in _boards.items()}
    try:
        reply, tool_trace = chat_turn(
            session,
            req.message[:2000],
            _active_payload(),
            _client(),
            _agent_supervisor,
            workspace_payloads=payloads,
            board_names=_board_names(),
            confirmed_mates=ws_store.confirmed_mates(_workspace),
            all_mates=_workspace["mates"],
            memory=_memory(),
        )
    except BudgetExceeded:
        return JSONResponse(
            {"error": "chat budget for this server session is exhausted"}, status_code=429
        )
    return JSONResponse(
        {
            "session_id": session.session_id,
            "answer": reply.answer,
            "highlight_refs": reply.highlight_refs,
            "trace_net": reply.trace_net,
            "fly_to": reply.fly_to,
            "tool_trace": tool_trace,
        }
    )
