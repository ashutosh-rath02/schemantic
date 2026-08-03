"""Schemantic web UI -- an interactive canvas over parsed + AI-enriched
schematics, organized into projects (isolated containers of boards, mate
links, and chat history -- create/switch only in this phase, no accounts
yet; see schemantic/projects.py for why the default project id is fixed).

    uvicorn schemantic.web.app:app --reload

Any Altium-exported schematic PDF works: upload one through the UI, or set
SCHEMANTIC_PDF to change the default board (only applies to the default
project -- an explicitly-created project always starts empty). Results are
cached by content hash, so re-analyzing a previously seen PDF is instant
even across different projects. Board-to-board links are AI-proposed and
human-confirmed (see schemantic/workspace.py for why they can't be parsed).
"""

from __future__ import annotations

import hashlib
import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from openai import OpenAI
from pydantic import BaseModel
from starlette.middleware.sessions import SessionMiddleware

load_dotenv()

from schemantic import auth  # noqa: E402
from schemantic import projects as proj_store  # noqa: E402
from schemantic import workspace as ws_store  # noqa: E402
from schemantic.agents.chat import ChatSession, chat_turn  # noqa: E402
from schemantic.agents.mate_proposer import propose_mates  # noqa: E402
from schemantic.chat_memory import ChatMemoryStore  # noqa: E402
from schemantic.design_checks import run_all_checks  # noqa: E402
from schemantic.graph_api import export_knowledge_graph  # noqa: E402
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

# https_only defaults on -- the live site is HTTPS-only. Overridable so the
# gate (_require_admin behavior) stays testable against plain-http local
# dev; the actual GitHub OAuth round-trip only makes sense against the live
# site anyway, since the registered callback URL is fixed to it.
app.add_middleware(
    SessionMiddleware,
    secret_key=os.getenv("SCHEMANTIC_SESSION_SECRET", ""),
    https_only=os.getenv("SCHEMANTIC_SESSION_HTTPS_ONLY", "1") == "1",
    same_site="lax",
)


@dataclass
class ProjectState:
    """Everything that used to be flat module globals, one level deeper --
    an in-memory cache of one project's on-disk truth. Lost on restart,
    same accepted tradeoff as before; rehydrated lazily by _get_project_state."""

    boards: dict[str, dict] = field(default_factory=dict)  # board_id -> {"path","payload"}
    active_board_id: str | None = None
    workspace: dict = field(default_factory=lambda: {"boards": [], "mates": []})
    chat_sessions: dict[str, ChatSession] = field(default_factory=dict)


_registry = proj_store.load_registry()
_project_states: dict[str, ProjectState] = {}


def _migrate_legacy_state() -> None:
    """One-time, idempotent, runs at import time (replacing what used to be
    a bare `_workspace = ws_store.load_workspace()` line). Bootstraps
    projects.json on a fresh install; folds a pre-multi-project
    workspace.json into the fixed default project if one exists and hasn't
    been migrated yet. Safe to run on every startup -- each step is a no-op
    once it's already happened."""
    proj_store.ensure_default_project(_registry)
    default_ws_path = ws_store.workspace_path_for(proj_store.DEFAULT_PROJECT_ID)
    if not default_ws_path.exists():
        legacy = ws_store.load_legacy_workspace()
        if legacy is not None:
            ws_store.save_workspace(legacy, proj_store.DEFAULT_PROJECT_ID)
    proj_store.save_registry(_registry)


_migrate_legacy_state()


def _get_project_state(project_id: str) -> ProjectState:
    if project_id not in _project_states:
        state = ProjectState()
        state.workspace = ws_store.load_workspace(project_id)
        _project_states[project_id] = state
    return _project_states[project_id]


def _resolve_project(project_id: str) -> ProjectState:
    """FastAPI dependency: every /p/{project_id}/... route resolves through
    this. Centralizes the unknown-project 404 instead of duplicating the
    check in ~13 route bodies."""
    if proj_store.get_project(_registry, project_id) is None:
        raise HTTPException(status_code=404, detail=f"unknown project {project_id!r}")
    return _get_project_state(project_id)


def _register_board(state: ProjectState, project_id: str, pdf_path: str) -> str:
    """Analyze (cache-hit if seen) + add to this project's workspace.
    Returns board id."""
    payload = build_enriched_schematic(pdf_path, use_cache=True)
    board_id = ws_store.board_id_for(pdf_path)
    state.boards[board_id] = {"path": pdf_path, "payload": payload}
    state.workspace = ws_store.add_board(state.workspace, pdf_path)
    ws_store.save_workspace(state.workspace, project_id)
    return board_id


def _register_kicad_board(
    state: ProjectState, project_id: str, saved: Path, source_name: str
) -> str:
    """KiCad path: .net (+ optional .kicad_pcb) -> same payload shape,
    same enrichment, same everything downstream."""
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
    board_id = ws_store.board_id_for(str(saved))
    payload = enrich_schematic(schematic, board_id)
    state.boards[board_id] = {"path": str(saved), "payload": payload}
    state.workspace = ws_store.add_board(state.workspace, str(saved))
    ws_store.save_workspace(state.workspace, project_id)
    return board_id


def _ensure_board(state: ProjectState, project_id: str) -> str | None:
    """Bootstraps/rejoins boards for this project, returns the active board
    id -- or None for a genuinely empty, explicitly-created project (the
    DEFAULT_PDF auto-bootstrap only applies to the fixed default project;
    a project a user just created should start empty, not silently contain
    the demo robot board)."""
    if state.active_board_id is not None:
        return state.active_board_id
    if project_id == proj_store.DEFAULT_PROJECT_ID:
        state.active_board_id = _register_board(state, project_id, str(DEFAULT_PDF))
    # boards already known to this project's workspace rejoin silently if their cache exists
    for bid, payload in ws_store.load_board_payloads(state.workspace).items():
        if bid not in state.boards:
            board = next(b for b in state.workspace["boards"] if b["id"] == bid)
            state.boards[bid] = {"path": board["pdf_path"], "payload": payload}
    if state.active_board_id is None and state.boards:
        state.active_board_id = next(iter(state.boards))
    return state.active_board_id


def _active_payload(state: ProjectState, project_id: str) -> dict | None:
    board_id = _ensure_board(state, project_id)
    return state.boards[board_id]["payload"] if board_id else None


def _board_names(state: ProjectState) -> dict[str, str]:
    return {b["id"]: b["name"] for b in state.workspace["boards"] if b["id"] in state.boards}


# ---- auth: admin-only GitHub login gate, not a user-accounts system ----
# Deliberately named around "admin", not "session" -- session_id already
# means something unrelated (chat-conversation identity) throughout this
# file; reusing that word for login state would read as the same concept.


def _require_admin(request: Request) -> None:
    if not request.session.get("admin"):
        raise HTTPException(status_code=401, detail="log in required")


@app.get("/auth/login")
def auth_login(request: Request):
    state = secrets.token_urlsafe(16)
    request.session["oauth_state"] = state
    redirect_uri = str(request.url_for("auth_callback"))
    return RedirectResponse(auth.authorize_url(redirect_uri, state))


@app.get("/auth/github/callback")
def auth_callback(request: Request, code: str, state: str):
    if state != request.session.pop("oauth_state", None):
        return RedirectResponse("/?error=Login+failed+-+try+again")
    redirect_uri = str(request.url_for("auth_callback"))
    login = auth.exchange_code_for_login(code, redirect_uri)
    if auth.is_admin_login(login):
        request.session["admin"] = True
        return RedirectResponse("/")
    request.session.pop("admin", None)
    return RedirectResponse("/?error=That+GitHub+account+isn%27t+authorized+for+this+site")


@app.get("/auth/logout")
def auth_logout(request: Request):
    request.session.clear()
    return RedirectResponse("/")


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.get("/")
def root_redirect(request: Request):
    """Auto-resolves to the last-active project (creating the default one
    on a truly fresh install) -- zero extra clicks for the common case,
    including the live resume-demo link. 307, not 301: the target
    legitimately changes as projects switch, and a permanent redirect
    risks a browser/CDN caching today's target forever."""
    target_id = _registry.get("last_active_project_id")
    if target_id is None or proj_store.get_project(_registry, target_id) is None:
        target_id = _registry["projects"][0]["id"]
    qs = request.url.query
    return RedirectResponse(url=f"/p/{target_id}/" + (f"?{qs}" if qs else ""), status_code=307)


@app.get("/p/{project_id}/", response_class=HTMLResponse)
def index(
    request: Request,
    project_id: str,
    state: ProjectState = Depends(_resolve_project),
    error: str | None = None,
):
    proj_store.set_last_active(_registry, project_id)
    proj_store.save_registry(_registry)
    board_id = _ensure_board(state, project_id)
    project = proj_store.get_project(_registry, project_id)
    is_admin = bool(request.session.get("admin"))
    if board_id is None:
        return templates.TemplateResponse(
            request,
            "schematic.html",
            {
                "empty": True,
                "project_id": project_id,
                "project_name": project["name"],
                "error": error,
                "is_admin": is_admin,
            },
        )
    result = state.boards[board_id]["payload"]
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
            "empty": False,
            "totals": totals,
            "source_name": Path(result["source_file"]).name,
            "error": error,
            "project_id": project_id,
            "project_name": project["name"],
            "is_admin": is_admin,
        },
    )


@app.post("/p/{project_id}/upload")
async def upload(
    project_id: str,
    file: UploadFile,
    _admin: None = Depends(_require_admin),
    state: ProjectState = Depends(_resolve_project),
):
    filename = file.filename or ""
    suffix = Path(filename).suffix.lower()
    if suffix not in (".pdf", ".zip", ".net"):
        return RedirectResponse(
            url=f"/p/{project_id}/?error=Supported+uploads:+Altium+PDF,+KiCad+.net,"
            "+or+.zip+with+.net+%2B+.kicad_pcb",
            status_code=303,
        )
    content = await file.read()
    digest = hashlib.sha256(content).hexdigest()[:16]
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    saved = UPLOAD_DIR / f"{digest}_{Path(filename).name}"
    saved.write_bytes(content)
    try:
        if suffix == ".pdf":
            state.active_board_id = _register_board(state, project_id, str(saved))
        else:
            state.active_board_id = _register_kicad_board(
                state, project_id, saved, Path(filename).stem
            )
    except ValueError as exc:  # unsupported format -- honest, actionable message
        return RedirectResponse(url=f"/p/{project_id}/?error={str(exc)[:300]}", status_code=303)
    return RedirectResponse(url=f"/p/{project_id}/", status_code=303)


@app.get("/p/{project_id}/api/schematic")
def api_schematic(project_id: str, state: ProjectState = Depends(_resolve_project)) -> JSONResponse:
    payload = _active_payload(state, project_id)
    if payload is None:
        return JSONResponse({"error": "no boards in this project yet"}, status_code=404)
    return JSONResponse(payload)


@app.get("/p/{project_id}/api/hardwaremap")
def api_hardwaremap(
    project_id: str, format: str = "md", state: ProjectState = Depends(_resolve_project)
):
    """The board's verified facts packaged for a coding agent: controller
    pin tables, buses, rails, connectors, datasheet links. Deterministic
    assembly -- no model call. ?format=json for the structured form."""
    payload = _active_payload(state, project_id)
    if payload is None:
        return JSONResponse({"error": "no boards in this project yet"}, status_code=404)
    names = _board_names(state)
    board_name = names.get(state.active_board_id, "board")
    mates = [
        m for m in ws_store.confirmed_mates(state.workspace)
        if state.active_board_id in (m["board_a"], m["board_b"])
    ]
    hw_map = build_hardware_map(payload, board_name, mates)
    if format == "json":
        return JSONResponse(hw_map)
    markdown = render_markdown(hw_map)
    return HTMLResponse(
        markdown,
        media_type="text/markdown",
        headers={"Content-Disposition": f'attachment; filename="hardwaremap_{board_name}.md"'},
    )


@app.get("/p/{project_id}/api/firmware-starter")
def api_firmware_starter(
    project_id: str,
    _admin: None = Depends(_require_admin),
    state: ProjectState = Depends(_resolve_project),
):
    """Starter firmware zip generated from the hardware map -- grounded in
    verified connectivity, TODO-marked where the map can't know, and
    labeled a starting point in every file. Login-gated: a real paid AI
    generation call, same reasoning as propose-mates."""
    import io
    import zipfile

    from schemantic.agents.firmware_starter import generate_starter

    payload = _active_payload(state, project_id)
    if payload is None:
        return JSONResponse({"error": "no boards in this project yet"}, status_code=404)
    names = _board_names(state)
    board_name = names.get(state.active_board_id, "board")
    mates = [
        m for m in ws_store.confirmed_mates(state.workspace)
        if state.active_board_id in (m["board_a"], m["board_b"])
    ]
    hw_map = build_hardware_map(payload, board_name, mates)
    if not hw_map["controllers"]:
        return JSONResponse(
            {"error": "no controller identified on this board -- nothing to target"},
            status_code=400,
        )
    try:
        starter = generate_starter(hw_map, _client(), _get_supervisor("admin"))
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


@app.get("/p/{project_id}/api/design-checks")
def api_design_checks(
    project_id: str, state: ProjectState = Depends(_resolve_project)
) -> JSONResponse:
    """Pre-fab findings: mechanical (verified-connectivity-derived) and
    heuristic (AI-identity-derived) tiers, each labeled. Not an ERC/DRC
    replacement -- see schemantic/design_checks.py for the exact scope."""
    payload = _active_payload(state, project_id)
    if payload is None:
        return JSONResponse({"error": "no boards in this project yet"}, status_code=404)
    return JSONResponse(run_all_checks(payload))


@app.get("/p/{project_id}/api/knowledge-graph")
def api_knowledge_graph(
    project_id: str, state: ProjectState = Depends(_resolve_project)
) -> JSONResponse:
    """The board's knowledge graph as typed nodes/edges for the graph-view
    UI -- same data the chat agent queries, laid out visually. Zero AI:
    a pure reshaping of the already-verified payload."""
    payload = _active_payload(state, project_id)
    if payload is None:
        return JSONResponse({"error": "no boards in this project yet"}, status_code=404)
    return JSONResponse(export_knowledge_graph(payload))


@app.get("/p/{project_id}/schematic-image")
def schematic_image(project_id: str, state: ProjectState = Depends(_resolve_project)):
    board_id = _ensure_board(state, project_id)
    if board_id is None:
        return JSONResponse({"error": "no boards in this project yet"}, status_code=404)
    source = state.boards[board_id]["path"]
    if not source.lower().endswith(".pdf"):
        return JSONResponse(
            {"error": "no original PDF for this board (KiCad source ingestion)"},
            status_code=404,
        )
    path = render_background_image(source)
    return FileResponse(path, media_type="image/png")


# ---- projects ----


class CreateProjectRequest(BaseModel):
    name: str | None = None


@app.get("/api/projects")
def api_list_projects() -> JSONResponse:
    return JSONResponse({"projects": proj_store.list_projects(_registry)})


@app.post("/api/projects")
def api_create_project(
    req: CreateProjectRequest, _admin: None = Depends(_require_admin)
) -> JSONResponse:
    project = proj_store.create_project(_registry, req.name)
    proj_store.set_last_active(_registry, project["id"])
    proj_store.save_registry(_registry)
    return JSONResponse(project)


# ---- workspace / multi-board ----

_agent_client: OpenAI | None = None
_supervisors: dict[str, Supervisor] = {}


def _client() -> OpenAI:
    global _agent_client
    if _agent_client is None:
        _agent_client = OpenAI()
    return _agent_client


def _get_supervisor(scope: str) -> Supervisor:
    """Two independent budget pools, not one shared cap: "public" covers
    chat (the one thing anonymous visitors can trigger repeatedly and for
    free), "admin" covers everything login-gated. Public chat traffic can
    no longer exhaust the budget the admin needs for uploads/proposals/
    firmware generation, and vice versa."""
    if scope not in _supervisors:
        cap = float(os.getenv(f"SCHEMANTIC_{scope.upper()}_BUDGET_USD", "5.0"))
        _supervisors[scope] = Supervisor(max_spend_usd=cap)
    return _supervisors[scope]


@app.get("/p/{project_id}/api/workspace")
def api_workspace(project_id: str, state: ProjectState = Depends(_resolve_project)) -> JSONResponse:
    _ensure_board(state, project_id)
    names = _board_names(state)
    return JSONResponse(
        {
            "boards": [
                {"id": bid, "name": names.get(bid, bid), "active": bid == state.active_board_id}
                for bid in state.boards
            ],
            "mates": [
                {**m, "board_a_name": names.get(m["board_a"], m["board_a"]),
                 "board_b_name": names.get(m["board_b"], m["board_b"])}
                for m in state.workspace["mates"]
                if m["board_a"] in state.boards and m["board_b"] in state.boards
            ],
        }
    )


class SwitchRequest(BaseModel):
    board_id: str


@app.post("/p/{project_id}/api/workspace/switch")
def api_switch(
    project_id: str, req: SwitchRequest, state: ProjectState = Depends(_resolve_project)
) -> JSONResponse:
    if req.board_id not in state.boards:
        return JSONResponse({"error": "unknown board"}, status_code=404)
    state.active_board_id = req.board_id
    return JSONResponse({"ok": True})


@app.post("/p/{project_id}/api/workspace/propose-mates")
def api_propose_mates(
    project_id: str,
    _admin: None = Depends(_require_admin),
    state: ProjectState = Depends(_resolve_project),
) -> JSONResponse:
    """Runs the proposer across the two most relevant boards (v1: exactly
    two boards in the project). Proposals are mechanically validated before
    storage; invalid ones are dropped and counted."""
    board_ids = list(state.boards.keys())
    if len(board_ids) < 2:
        return JSONResponse(
            {"error": "need at least two analyzed boards to propose links"}, status_code=400
        )
    bid_a, bid_b = board_ids[0], board_ids[1]
    names = _board_names(state)
    try:
        result = propose_mates(
            state.boards[bid_a]["payload"], names[bid_a],
            state.boards[bid_b]["payload"], names[bid_b],
            _client(), _get_supervisor("admin"),
        )
    except BudgetExceeded:
        return JSONResponse({"error": "agent budget exhausted"}, status_code=429)

    # drop re-proposals of connector pairs that already have a mate record
    existing = {
        (m["board_a_connector"], m["board_b_connector"])
        for m in state.workspace["mates"]
        if {m["board_a"], m["board_b"]} == {bid_a, bid_b}
    }
    fresh = [
        p.model_dump()
        for p in result.proposals
        if (p.board_a_connector, p.board_b_connector) not in existing
    ]
    stored, dropped = ws_store.store_proposals(
        state.workspace, bid_a, bid_b, fresh,
        {bid_a: state.boards[bid_a]["payload"], bid_b: state.boards[bid_b]["payload"]},
    )
    ws_store.save_workspace(state.workspace, project_id)
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


@app.post("/p/{project_id}/api/mates/{mate_id}")
def api_mate_decision(
    project_id: str,
    mate_id: str,
    req: MateDecision,
    _admin: None = Depends(_require_admin),
    state: ProjectState = Depends(_resolve_project),
) -> JSONResponse:
    if req.status not in ("confirmed", "rejected"):
        return JSONResponse({"error": "status must be confirmed or rejected"}, status_code=400)
    if not ws_store.set_mate_status(state.workspace, mate_id, req.status):
        return JSONResponse({"error": "unknown mate id"}, status_code=404)
    ws_store.save_workspace(state.workspace, project_id)
    return JSONResponse({"ok": True})


# ---- chat ----

_chat_memory: ChatMemoryStore | None = None


def _memory() -> ChatMemoryStore:
    """Semantic chat memory, persisted to SQLite -- survives restarts, one
    file for every project (queries are project-scoped, not the storage
    itself). The embedder is budget-tracked like every other model call."""
    global _chat_memory
    if _chat_memory is None:

        def embed(text: str) -> list[float]:
            response = _client().embeddings.create(model=EMBED_MODEL, input=text[:8000])
            _get_supervisor("public").spend("chat_memory", 0.0001)
            return response.data[0].embedding

        _chat_memory = ChatMemoryStore(CACHE_DIR / "chat_memory.db", embed)
    return _chat_memory


class ChatRequest(BaseModel):
    message: str
    session_id: str | None = None


@app.get("/p/{project_id}/api/chat/history")
def api_chat_history(
    project_id: str, session_id: str, _state: ProjectState = Depends(_resolve_project)
) -> JSONResponse:
    """Transcript from persistent memory -- what makes localStorage a mere
    display cache rather than the system of record."""
    return JSONResponse({"exchanges": _memory().history(project_id, session_id)})


@app.get("/p/{project_id}/api/chat/sessions")
def api_chat_sessions(
    project_id: str, _state: ProjectState = Depends(_resolve_project)
) -> JSONResponse:
    """Past chats for this project, most recently active first -- what the
    chat sidebar renders. Titles are AI-generated after each session's
    first grounded exchange; a session that hasn't gotten one yet (title
    generation failed, or the first turn is still in flight) shows empty."""
    return JSONResponse({"sessions": _memory().list_sessions(project_id)})


@app.post("/p/{project_id}/api/chat")
def api_chat(
    project_id: str, req: ChatRequest, state: ProjectState = Depends(_resolve_project)
) -> JSONResponse:
    payload = _active_payload(state, project_id)
    if payload is None:
        return JSONResponse({"error": "no boards in this project yet"}, status_code=404)
    session = state.chat_sessions.get(req.session_id or "")
    if session is None:
        session = ChatSession(project_id=project_id)
        state.chat_sessions[session.session_id] = session
    payloads = {bid: b["payload"] for bid, b in state.boards.items()}
    try:
        reply, tool_trace = chat_turn(
            session,
            req.message[:2000],
            payload,
            _client(),
            _get_supervisor("public"),
            project_id=project_id,
            workspace_payloads=payloads,
            board_names=_board_names(state),
            confirmed_mates=ws_store.confirmed_mates(state.workspace),
            all_mates=state.workspace["mates"],
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
