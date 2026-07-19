"""ChatSession memory contract -- offline, no LLM (tests never make live
model calls; the live loop is verified manually against the running app).
"""

from schemantic.agents.chat import MAX_REMEMBERED_ITEMS, ChatSession


def test_memory_keeps_recent_items_and_trims_oldest():
    session = ChatSession()
    for i in range(MAX_REMEMBERED_ITEMS + 25):
        session.remember({"role": "user", "content": f"msg-{i}"})
    assert len(session.items) == MAX_REMEMBERED_ITEMS
    assert session.items[0]["content"] == "msg-25"   # oldest trimmed
    assert session.items[-1]["content"] == f"msg-{MAX_REMEMBERED_ITEMS + 24}"


def test_sessions_have_distinct_ids():
    assert ChatSession().session_id != ChatSession().session_id


def test_board_switch_keeps_memory_and_adds_marker():
    # Switching the active board must NOT wipe the conversation (it did,
    # observed as "chat is lost" once the workspace made switching routine).
    from schemantic.agents.chat import _sync_active_board

    session = ChatSession()
    session.board_file = "board_A.pdf"
    session.remember({"role": "user", "content": "what is U6?"})
    session.remember({"role": "assistant", "content": "U6 is the INA219."})

    _sync_active_board(session, "board_B.pdf")

    assert len(session.items) == 3  # both turns kept + one switch marker
    assert session.items[0]["content"] == "what is U6?"
    marker = session.items[-1]
    assert marker["role"] == "system"
    assert "board_A.pdf" in marker["content"] and "board_B.pdf" in marker["content"]
    # the marker must state, readably, that PRIOR messages belong to board A
    assert "above this line" in marker["content"]
    assert session.board_file == "board_B.pdf"


def test_first_board_gets_context_marker_and_repeat_adds_nothing():
    from schemantic.agents.chat import _sync_active_board

    session = ChatSession()
    _sync_active_board(session, "board_A.pdf")  # first board: one context marker
    _sync_active_board(session, "board_A.pdf")  # unchanged: nothing added
    assert len(session.items) == 1
    assert session.items[0]["role"] == "system"
    assert "board_A.pdf" in session.items[0]["content"]
