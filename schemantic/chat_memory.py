"""Persistent semantic chat memory.

Every completed exchange (question + answer + provenance) is stored in
SQLite with an embedding; each new question is embedded and cosine-matched
against everything stored, so semantically similar past exchanges surface
even across server restarts, sessions, and rephrasings ("what voltage can
U6 measure" later recalled by "how many volts can the current sensor
take"). Recalled exchanges are injected as CONTEXT, clearly marked
possibly-stale -- they never replace tool verification, because the board
state they described (confirmed links, active board) may have changed since.

Right-sizing note, held deliberately: this is SQLite + exact cosine in pure
Python, not a vector database. At chat scale -- thousands of exchanges,
1536-dim vectors -- exact search is milliseconds and needs zero new
infrastructure; approximate-NN indexes earn their complexity at millions of
vectors. The semantic part is the embeddings, not the storage engine.

The embedder is injected (embed_fn), so tests exercise storage, ranking,
thresholds, and persistence with a deterministic fake -- no live API calls
in tests, per project convention.
"""

from __future__ import annotations

import sqlite3
import struct
import time
from collections.abc import Callable
from pathlib import Path

DEFAULT_TOP_K = 3
MIN_SIMILARITY = 0.35  # below this, "matches" are noise -- return nothing

_SCHEMA = """
CREATE TABLE IF NOT EXISTS exchanges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    board TEXT NOT NULL,
    question TEXT NOT NULL,
    answer TEXT NOT NULL,
    tool_trace TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    embedding BLOB NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_exchanges_session ON exchanges(session_id);
"""


def _pack(vector: list[float]) -> bytes:
    return struct.pack(f"{len(vector)}f", *vector)


def _unpack(blob: bytes) -> list[float]:
    return list(struct.unpack(f"{len(blob) // 4}f", blob))


def _cosine(a: list[float], b: list[float]) -> float:
    if len(a) != len(b):
        return 0.0
    # strict=True documents that the length check above is what makes this
    # safe -- if that invariant is ever broken by a future edit, this fails
    # loudly instead of silently truncating one vector.
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(x * x for x in b) ** 0.5
    if not norm_a or not norm_b:
        return 0.0
    return dot / (norm_a * norm_b)


class ChatMemoryStore:
    """embed_fn: text -> embedding vector. Failures in storage/recall are
    contained -- memory is an enhancement, and a memory error must never
    break the chat turn it decorates."""

    def __init__(self, db_path: str | Path, embed_fn: Callable[[str], list[float]]):
        self.db_path = str(db_path)
        self.embed_fn = embed_fn
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        # connection per operation: trivially thread-safe at this scale
        return sqlite3.connect(self.db_path, timeout=10)

    def store(
        self, session_id: str, board: str, question: str, answer: str, tool_trace: list[str]
    ) -> bool:
        try:
            embedding = self.embed_fn(f"Q: {question}\nA: {answer}")
            with self._connect() as conn:
                conn.execute(
                    "INSERT INTO exchanges (session_id, board, question, answer, "
                    "tool_trace, created_at, embedding) VALUES (?,?,?,?,?,?,?)",
                    (
                        session_id,
                        board,
                        question,
                        answer,
                        " | ".join(tool_trace),
                        time.time(),
                        _pack(embedding),
                    ),
                )
            return True
        except Exception as exc:
            print(f"chat memory store failed (non-fatal): {exc}")
            return False

    def recall(
        self, query: str, top_k: int = DEFAULT_TOP_K, min_similarity: float = MIN_SIMILARITY
    ) -> list[dict]:
        try:
            query_vec = self.embed_fn(query)
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT board, question, answer, created_at, embedding FROM exchanges"
                ).fetchall()
        except Exception as exc:
            print(f"chat memory recall failed (non-fatal): {exc}")
            return []
        scored = []
        for board, question, answer, created_at, blob in rows:
            similarity = _cosine(query_vec, _unpack(blob))
            if similarity >= min_similarity:
                scored.append(
                    {
                        "similarity": round(similarity, 3),
                        "board": board,
                        "question": question,
                        "answer": answer,
                        "age_seconds": round(time.time() - created_at),
                    }
                )
        scored.sort(key=lambda r: -r["similarity"])
        return scored[:top_k]

    def history(self, session_id: str, limit: int = 40) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT question, answer, tool_trace, created_at FROM exchanges "
                "WHERE session_id = ? ORDER BY id DESC LIMIT ?",
                (session_id, limit),
            ).fetchall()
        return [
            {"question": q, "answer": a, "tool_trace": t, "created_at": c}
            for q, a, t, c in reversed(rows)
        ]

    def count(self) -> int:
        with self._connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM exchanges").fetchone()[0]
