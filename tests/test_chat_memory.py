"""Semantic chat memory -- offline, deterministic fake embedder (no API).
What's pinned: semantic ranking (synonym phrasing must beat unrelated
text), the noise threshold, persistence across store instances (the actual
'survives restarts' property), per-session history order, the schema
migration for pre-multi-project databases, and that recall never crosses
a project boundary.
"""

import sqlite3
import time
from pathlib import Path

from schemantic.chat_memory import ChatMemoryStore

# fake embedder: maps texts onto 4 topic axes by keyword -- deterministic,
# and 'semantic' in exactly the way the test needs (synonyms share an axis)
_TOPICS = {
    0: ("voltage", "volts", "26 v", "measure", "sensor"),
    1: ("motor", "tb6612", "driver"),
    2: ("uart", "serial", "tx", "rx"),
    3: ("banana", "weather"),
}


def fake_embed(text: str) -> list[float]:
    lowered = text.lower()
    vec = [0.0, 0.0, 0.0, 0.0]
    for axis, keywords in _TOPICS.items():
        vec[axis] = float(sum(lowered.count(k) for k in keywords))
    return vec if any(vec) else [0.0, 0.0, 0.0, 1e-9]


def _store(tmp_path: Path) -> ChatMemoryStore:
    return ChatMemoryStore(tmp_path / "mem.db", fake_embed)


def _seed(store: ChatMemoryStore, project_id: str = "p1") -> None:
    store.store(
        project_id, "s1", "boardA", "what voltage can U6 measure?",
        "0 to 26 V per datasheet.", ["get_datasheet(U6)"],
    )
    store.store(
        project_id, "s1", "boardA", "how does the ESP32 drive the motor?",
        "Via the TB6612 driver.", ["path_between(M3, TB1)"],
    )
    store.store(
        project_id, "s2", "boardB", "which UART pins go to the header?",
        "TX/RX on P1.", ["get_net(U0TX)"],
    )


def test_semantic_recall_ranks_synonym_phrasing_first(tmp_path):
    store = _store(tmp_path)
    _seed(store)
    hits = store.recall("p1", "how many volts can the sensor take?")
    assert hits, "synonym phrasing should recall the voltage exchange"
    assert "voltage" in hits[0]["question"]
    assert hits[0]["board"] == "boardA"


def test_unrelated_query_returns_nothing(tmp_path):
    store = _store(tmp_path)
    _seed(store)
    assert store.recall("p1", "banana weather forecast") == []


def test_memory_survives_reopening_the_store(tmp_path):
    _seed(_store(tmp_path))
    reopened = _store(tmp_path)  # fresh instance, same file = restart
    assert reopened.count() == 3
    hits = reopened.recall("p1", "serial rx tx pins")
    assert hits and "UART" in hits[0]["question"]


def test_history_is_per_session_and_ordered(tmp_path):
    store = _store(tmp_path)
    _seed(store)
    history = store.history("p1", "s1")
    assert [h["question"] for h in history] == [
        "what voltage can U6 measure?",
        "how does the ESP32 drive the motor?",
    ]
    assert store.history("p1", "s2")[0]["question"].startswith("which UART")


def test_store_failure_is_contained(tmp_path):
    def broken_embed(_text: str) -> list[float]:
        raise RuntimeError("embedder down")

    store = ChatMemoryStore(tmp_path / "mem.db", broken_embed)
    assert store.store("p1", "s", "b", "q", "a", []) is False  # no exception escapes
    assert store.recall("p1", "anything") == []


def test_recall_never_crosses_a_project_boundary(tmp_path):
    # two projects, semantically-matching content in both -- recall for one
    # project must never surface a hit stored under the other. This is the
    # exact bug multi-project support would otherwise introduce: recall()
    # used to be an unscoped full-table scan by design (single global
    # workspace, "any session, any restart"), which becomes real
    # cross-project leakage the moment a second project exists.
    store = _store(tmp_path)
    _seed(store, project_id="p1")
    store.store(
        "p2", "s3", "boardC", "what voltage does the sensor tolerate?",
        "Different board, different answer: 0 to 5 V.", ["get_datasheet(U9)"],
    )
    hits = store.recall("p1", "how many volts can the sensor take?")
    assert hits and all(h["board"] != "boardC" for h in hits)
    hits_p2 = store.recall("p2", "how many volts can the sensor take?")
    assert hits_p2 and hits_p2[0]["board"] == "boardC"


def test_history_is_scoped_by_project_too(tmp_path):
    store = _store(tmp_path)
    _seed(store, project_id="p1")
    store.store("p2", "s1", "boardC", "unrelated question", "unrelated answer", [])
    # same session id "s1" reused under a different project on purpose --
    # history for p1/s1 must not include the p2/s1 row
    assert all(h["question"] != "unrelated question" for h in store.history("p1", "s1"))
    assert store.history("p2", "s1")[0]["question"] == "unrelated question"


def test_legacy_database_migrates_and_backfills_default_project(tmp_path):
    db_path = tmp_path / "legacy.db"
    # hand-build the pre-multi-project schema (no project_id column) and
    # insert a row the way the old code would have, before ChatMemoryStore
    # ever touches this file
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE exchanges (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            board TEXT NOT NULL,
            question TEXT NOT NULL,
            answer TEXT NOT NULL,
            tool_trace TEXT NOT NULL DEFAULT '',
            created_at REAL NOT NULL,
            embedding BLOB NOT NULL
        );
        """
    )
    conn.execute(
        "INSERT INTO exchanges (session_id, board, question, answer, tool_trace, "
        "created_at, embedding) VALUES ('s1','boardA','legacy question','legacy answer','',0,?)",
        (b"\x00\x00\x00\x00",),  # placeholder embedding bytes -- history() never selects this column
    )
    conn.commit()
    conn.close()

    store = ChatMemoryStore(db_path, fake_embed)  # must open cleanly against the old schema

    with sqlite3.connect(db_path) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(exchanges)")}
    assert "project_id" in columns

    backfilled = store.history("proj_default", "s1")
    assert backfilled and backfilled[0]["question"] == "legacy question"


def test_needs_title_before_first_store_and_not_after(tmp_path):
    store = _store(tmp_path)
    assert store.needs_title("p1", "s1") is True  # session doesn't exist yet
    store.store("p1", "s1", "boardA", "q", "a", [])
    assert store.needs_title("p1", "s1") is True  # exists now, but title still empty
    store.set_session_title("p1", "s1", "IMU location")
    assert store.needs_title("p1", "s1") is False


def test_list_sessions_most_recently_active_first(tmp_path):
    # tiny sleeps between writes: updated_at ordering needs real timestamp
    # separation, and time.time()'s resolution isn't guaranteed fine enough
    # to distinguish back-to-back calls with no gap at all
    store = _store(tmp_path)
    store.store("p1", "s1", "boardA", "first question", "first answer", [])
    store.set_session_title("p1", "s1", "First chat")
    time.sleep(0.01)
    store.store("p1", "s2", "boardA", "second question", "second answer", [])
    store.set_session_title("p1", "s2", "Second chat")
    time.sleep(0.01)
    store.store("p1", "s1", "boardA", "back to first", "still first", [])  # bumps s1's updated_at

    sessions = store.list_sessions("p1")
    assert [s["session_id"] for s in sessions] == ["s1", "s2"]
    assert [s["title"] for s in sessions] == ["First chat", "Second chat"]


def test_list_sessions_is_scoped_by_project(tmp_path):
    store = _store(tmp_path)
    store.store("p1", "s1", "boardA", "q", "a", [])
    store.store("p2", "s2", "boardC", "q", "a", [])
    assert [s["session_id"] for s in store.list_sessions("p1")] == ["s1"]
    assert [s["session_id"] for s in store.list_sessions("p2")] == ["s2"]
