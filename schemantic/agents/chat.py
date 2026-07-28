"""Chat agent over the schematic knowledge graph.

Grounding contract: the model may only make connectivity/identity claims it
obtained through the graph tools in this turn's loop -- the system prompt
enforces it and the UI shows which tools ran under every answer, so an
ungrounded claim is visible as one with no provenance. Canvas actions come
back as structured fields (highlight/trace/fly-to), never parsed out of
prose.

Memory: each ChatSession keeps the running conversation (user turns, tool
calls, tool results, replies) so follow-ups like "what is IT connected to?"
resolve naturally. History is capped by turn count; the cap trims oldest
turns first and is a constant below, not a magic number inline.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from openai import OpenAI
from pydantic import BaseModel, Field

from schemantic import graph_api
from schemantic.design_checks import run_all_checks
from schemantic.supervisor.core import Supervisor

MODEL = os.getenv("SCHEMANTIC_CHAT_MODEL", "gpt-4.1")
MAX_TOOL_ITERATIONS = 8      # hard stop on a single turn's tool loop
MAX_REMEMBERED_ITEMS = 60    # ~15-20 turns incl. tool traffic; oldest trimmed
EST_COST_PER_CALL_USD = 0.01
BUDGET_PER_MESSAGE_USD = 0.15

_SYSTEM_PROMPT = """You are Schemantic's board assistant. You answer questions about ONE
specific circuit board using ONLY facts returned by your tools in this conversation --
connectivity, part identity, regions all come from a netlist mechanically parsed from the
board's schematic PDF plus AI part identification that is clearly marked as a guess.

Rules:
- Never state a connection, part number, or location you did not read from a tool result.
  If tools return nothing, say you couldn't find it -- do not improvise.
- Search persistently BEFORE concluding not-found: try synonyms and related terms
  (IMU -> inertial, accelerometer, gyroscope, motion; regulator -> LDO, buck, converter;
  USB chip -> UART, bridge, CH343, CP2102), and check list_regions for a matching section.
  Only give up after 2-3 distinct attempts.
- Part identities are AI guesses grounded in schematic text; when one matters to your answer,
  say so briefly (e.g. "identified as INA219, 95% confidence").
- For spec questions (voltages, interfaces, ratings, package details) use get_datasheet first;
  if the parameter isn't among its verified facts, use search_datasheet to find it in the full
  document and answer by QUOTING the passage with its page ("per datasheet p.23: '...'").
  If neither finds it, say so and point to the datasheet URL; never fill the gap from memory.
- Keep answers short and concrete. Lists over prose. No filler.
- The transcript contains system markers for active-board switches. When recalling earlier
  conversation, attribute earlier statements and tool results to the board that was active at
  that point in the transcript -- ref designators repeat across boards (both boards may have a
  "U6"), so board attribution matters.
- Whenever your answer refers to specific components or a net, populate highlight_refs /
  trace_net so the canvas shows what you mean. fly_to should name the single most relevant
  component when the user asked "where is X".
"""


class ChatReply(BaseModel):
    answer: str = Field(description="The reply shown to the user. Short, concrete, grounded.")
    highlight_refs: list[str] = Field(
        default_factory=list,
        description="Reference designators (e.g. 'U6') this answer is about; canvas highlights them.",
    )
    trace_net: str | None = Field(
        default=None, description="A net name/key to trace on the canvas, if the answer is about one."
    )
    fly_to: str | None = Field(
        default=None, description="Single component ref to zoom the canvas to, for where-is questions."
    )


# tool name -> (description, {param: (type, description)}, callable)
def _tool_table(
    payload: dict,
    workspace_payloads: dict[str, dict] | None = None,
    board_names: dict[str, str] | None = None,
    confirmed_mates: list[dict] | None = None,
    all_mates: list[dict] | None = None,
) -> dict[str, tuple[str, dict, Callable[..., Any]]]:
    table = {
        "search_components": (
            "Substring search over component labels, part numbers, and functions.",
            {"query": ("string", "text to search for")},
            lambda query: graph_api.search_components(payload, query),
        ),
        "get_component": (
            "Full detail for one component: identity, region, pin count, nets.",
            {"ref": ("string", "reference designator, e.g. 'U6'")},
            lambda ref: graph_api.get_component(payload, ref),
        ),
        "get_connections": (
            "Everything electrically connected to a component, grouped by net.",
            {"ref": ("string", "reference designator, e.g. 'U6'")},
            lambda ref: graph_api.get_connections(payload, ref),
        ),
        "get_net": (
            "A net's aliases, members, pin count, and whether it's a power rail.",
            {"name": ("string", "net name or alias, e.g. 'IIC0SDA'")},
            lambda name: graph_api.get_net(payload, name),
        ),
        "path_between": (
            ("Shortest electrical path between two components via signal nets "
             "(power rails excluded unless include_power)."),
            {
                "ref_a": ("string", "first component ref"),
                "ref_b": ("string", "second component ref"),
            },
            lambda ref_a, ref_b: graph_api.path_between(payload, ref_a, ref_b),
        ),
        "get_datasheet": (
            ("Verified, page-cited facts from the part's fetched datasheet, plus its URL. "
             "Use FIRST for spec questions."),
            {"ref": ("string", "reference designator, e.g. 'U6'")},
            lambda ref: graph_api.get_datasheet(payload, ref),
        ),
        "search_datasheet": (
            ("Full-document search of the part's datasheet PDF: verbatim passages with page "
             "numbers. Use when the parameter isn't among get_datasheet's digest facts "
             "(e.g. I2C address, timing, register details). Quote the passage and cite the page."),
            {
                "ref": ("string", "reference designator, e.g. 'U7'"),
                "query": ("string", "parameter terms, e.g. 'I2C slave address'"),
            },
            lambda ref, query: graph_api.search_datasheet(payload, ref, query),
        ),
        "list_regions": (
            "All board sections with AI explanations and component counts.",
            {},
            lambda: graph_api.list_regions(payload),
        ),
        "board_summary": (
            "Board-level totals: source file, component/net/region counts.",
            {},
            lambda: graph_api.board_summary(payload),
        ),
        "run_design_checks": (
            ("Pre-fab findings on this board: missing I2C pull-ups, floating enable/reset pins, "
             "unidentified controllers, and supply-rail mismatches. Each finding is tagged "
             "'mechanical' (from verified connectivity) or 'heuristic' (from AI part identity) "
             "-- state which tier a finding is when you mention it. Not an ERC/DRC replacement."),
            {},
            lambda: run_all_checks(payload),
        ),
    }

    if workspace_payloads and len(workspace_payloads) > 1:
        names = board_names or {}
        table["search_all_boards"] = (
            "Search components across EVERY board in the workspace, not just the active one.",
            {"query": ("string", "text to search for")},
            lambda query: graph_api.search_all_boards(workspace_payloads, names, query),
        )
        table["list_board_links"] = (
            ("Board-to-board connector links: confirmed ones are traversable facts; "
             "proposed ones are AI hypotheses awaiting human confirmation -- say which is which."),
            {},
            lambda: graph_api.list_board_links(all_mates or [], names),
        )
        table["cross_board_path"] = (
            ("Electrical path between components on DIFFERENT boards, via human-confirmed "
             "connector links only."),
            {
                "ref_a": ("string", "component ref on the first board"),
                "board_a": ("string", "first board's name"),
                "ref_b": ("string", "component ref on the second board"),
                "board_b": ("string", "second board's name"),
            },
            lambda ref_a, board_a, ref_b, board_b: graph_api.cross_board_path(
                workspace_payloads, names, confirmed_mates or [],
                ref_a, board_a, ref_b, board_b,
            ),
        )
    return table


def _tool_schemas(table: dict) -> list[dict]:
    schemas = []
    for name, (description, params, _fn) in table.items():
        schemas.append(
            {
                "type": "function",
                "name": name,
                "description": description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        p: {"type": t, "description": d} for p, (t, d) in params.items()
                    },
                    "required": list(params.keys()),
                    "additionalProperties": False,
                },
                "strict": True,
            }
        )
    return schemas


def _sync_active_board(session: ChatSession, board_name: str) -> None:
    """Board switches must NOT wipe conversation memory (they did, back when
    chat was single-board -- observed as 'chat is lost' the moment the
    workspace made switching routine). Every board context gets an explicit
    transcript marker -- including the first -- so recalling 'which board
    was active when the user said X' is a matter of reading the transcript,
    not inferring from a switch notice (tested: inference alone
    misattributed; refs like 'U6' exist on both boards)."""
    if session.board_file == board_name:
        return
    if session.board_file:
        marker = (
            f"(active board switched from {session.board_file} to {board_name}; "
            "messages and tool results above this line were in the context of "
            f"{session.board_file})"
        )
    else:
        marker = f"(active board is {board_name})"
    session.remember({"role": "system", "content": marker})
    session.board_file = board_name


@dataclass
class ChatSession:
    session_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    board_file: str = ""
    items: list[dict] = field(default_factory=list)  # conversation memory

    def remember(self, item: dict) -> None:
        self.items.append(item)
        if len(self.items) > MAX_REMEMBERED_ITEMS:
            del self.items[: len(self.items) - MAX_REMEMBERED_ITEMS]


def chat_turn(
    session: ChatSession,
    user_message: str,
    payload: dict,
    client: OpenAI,
    supervisor: Supervisor,
    workspace_payloads: dict[str, dict] | None = None,
    board_names: dict[str, str] | None = None,
    confirmed_mates: list[dict] | None = None,
    all_mates: list[dict] | None = None,
    memory=None,  # ChatMemoryStore | None -- semantic recall + persistence
) -> tuple[ChatReply, list[str]]:
    """One user turn: tool loop until the model answers, memory updated.
    Returns (reply, tool_trace) -- tool_trace is the provenance line the UI
    shows under the answer ("checked: get_connections(U6), ...")."""
    board_name = graph_api.board_summary(payload)["source_file"]
    _sync_active_board(session, board_name)

    table = _tool_table(payload, workspace_payloads, board_names, confirmed_mates, all_mates)
    tools = _tool_schemas(table)
    session.remember({"role": "user", "content": user_message})

    workspace_note = ""
    if workspace_payloads and len(workspace_payloads) > 1:
        names = ", ".join((board_names or {}).values())
        workspace_note = (
            f"\nWorkspace boards: {names}. Single-board tools act on the ACTIVE board; "
            "use search_all_boards / cross_board_path for questions spanning boards. "
            "Cross-board paths exist only through human-confirmed connector links."
        )

    # semantic recall: past exchanges (any session, any restart) similar to
    # this question, injected this-turn-only as clearly-stale-able context.
    # Never remembered into session items -- each turn re-recalls fresh, and
    # recalled text must not masquerade as this conversation's history.
    recall_note = ""
    recalled_count = 0
    if memory is not None:
        hits = memory.recall(user_message)
        recalled_count = len(hits)
        if hits:
            lines = [
                f"- [{h['board']}] Q: {h['question']} -> A: {h['answer']}"
                for h in hits
            ]
            recall_note = (
                "\nSemantically similar past exchanges (possibly stale -- board state "
                "such as confirmed links may have changed; re-verify with tools when "
                "it matters):\n" + "\n".join(lines)
            )

    input_items: list[dict] = [
        {
            "role": "system",
            "content": _SYSTEM_PROMPT
            + f"\nActive board: {board_name}"
            + workspace_note
            + recall_note,
        },
        *session.items,
    ]
    tool_trace: list[str] = []
    if recalled_count:
        tool_trace.append(f"semantic_recall({recalled_count} past)")

    # Observed live, reproducibly: the model sometimes emits a text-only
    # first response that ANNOUNCES an intent to search ("I will retry with
    # related terms...") without calling any tool at all -- an ungrounded
    # reply slipping past a purely prompt-based instruction, which the
    # architecture is supposed to make structurally impossible. Worse: that
    # reply was being stored in semantic memory, then recalled on the next
    # attempt, so the model saw its own prior non-answer as "similar past
    # context" and repeated it -- a self-reinforcing failure compounding
    # across calls, confirmed by three consecutive identical repro attempts
    # against the deployed server. Two structural fixes, not prompt tweaks:
    #   1. a zero-tool-call first response gets ONE forced retry with an
    #      explicit corrective instruction, not accepted immediately.
    #   2. memory only stores a reply if a real tool actually ran this turn
    #      -- an ungrounded reply can never be memorized and re-surfaced.
    tools_ever_called = False
    nudged = False

    with supervisor.stage("chat"):
        for _ in range(MAX_TOOL_ITERATIONS):
            response = client.responses.parse(
                model=MODEL,
                input=input_items,
                tools=tools,
                text_format=ChatReply,
            )
            supervisor.spend("chat", EST_COST_PER_CALL_USD)

            calls = [item for item in response.output if item.type == "function_call"]
            if not calls:
                if not tools_ever_called and not nudged:
                    nudged = True
                    input_items.append(
                        {
                            "role": "system",
                            "content": "You did not call any tool. Do not describe what you "
                            "would search for -- actually call search_components or another "
                            "tool now, before answering.",
                        }
                    )
                    continue
                reply = response.output_parsed
                if reply is None:  # model emitted neither tools nor parseable text
                    reply = ChatReply(answer="I couldn't produce an answer for that -- try rephrasing.")
                session.remember({"role": "assistant", "content": reply.answer})
                if memory is not None and reply.answer and tools_ever_called:
                    memory.store(
                        session.session_id, board_name, user_message, reply.answer, tool_trace
                    )
                return reply, tool_trace
            tools_ever_called = True

            for call in calls:
                args = json.loads(call.arguments)
                _desc, _params, fn = table[call.name]
                try:
                    result = fn(**args)
                except Exception as exc:
                    result = {"error": f"tool failed: {exc}"}
                arg_str = ", ".join(str(v) for v in args.values())
                tool_trace.append(f"{call.name}({arg_str})")
                for item in (
                    {
                        "type": "function_call",
                        "call_id": call.call_id,
                        "name": call.name,
                        "arguments": call.arguments,
                    },
                    {
                        "type": "function_call_output",
                        "call_id": call.call_id,
                        "output": json.dumps(result),
                    },
                ):
                    session.remember(item)
                    input_items.append(item)

    reply = ChatReply(
        answer="I hit the tool-call limit for one question without reaching an answer -- "
        "try something narrower."
    )
    session.remember({"role": "assistant", "content": reply.answer})
    return reply, tool_trace
