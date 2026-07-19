"""Semantic chat memory -- offline, deterministic fake embedder (no API).
What's pinned: semantic ranking (synonym phrasing must beat unrelated
text), the noise threshold, persistence across store instances (the actual
'survives restarts' property), and per-session history order.
"""

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


def _seed(store: ChatMemoryStore) -> None:
    store.store("s1", "boardA", "what voltage can U6 measure?", "0 to 26 V per datasheet.", ["get_datasheet(U6)"])
    store.store("s1", "boardA", "how does the ESP32 drive the motor?", "Via the TB6612 driver.", ["path_between(M3, TB1)"])
    store.store("s2", "boardB", "which UART pins go to the header?", "TX/RX on P1.", ["get_net(U0TX)"])


def test_semantic_recall_ranks_synonym_phrasing_first(tmp_path):
    store = _store(tmp_path)
    _seed(store)
    hits = store.recall("how many volts can the sensor take?")
    assert hits, "synonym phrasing should recall the voltage exchange"
    assert "voltage" in hits[0]["question"]
    assert hits[0]["board"] == "boardA"


def test_unrelated_query_returns_nothing(tmp_path):
    store = _store(tmp_path)
    _seed(store)
    assert store.recall("banana weather forecast") == []


def test_memory_survives_reopening_the_store(tmp_path):
    _seed(_store(tmp_path))
    reopened = _store(tmp_path)  # fresh instance, same file = restart
    assert reopened.count() == 3
    hits = reopened.recall("serial rx tx pins")
    assert hits and "UART" in hits[0]["question"]


def test_history_is_per_session_and_ordered(tmp_path):
    store = _store(tmp_path)
    _seed(store)
    history = store.history("s1")
    assert [h["question"] for h in history] == [
        "what voltage can U6 measure?",
        "how does the ESP32 drive the motor?",
    ]
    assert store.history("s2")[0]["question"].startswith("which UART")


def test_store_failure_is_contained(tmp_path):
    def broken_embed(_text: str) -> list[float]:
        raise RuntimeError("embedder down")

    store = ChatMemoryStore(tmp_path / "mem.db", broken_embed)
    assert store.store("s", "b", "q", "a", []) is False  # no exception escapes
    assert store.recall("anything") == []