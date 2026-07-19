"""Cross-cutting cost/loop governance for every agent call in the pipeline.

Nothing that spends money or makes a model call should run outside this --
mirrors the pattern that made budgets and manifests meaningful in the prior
project. Thread-safe: the lookup and labeling agents run concurrently.
"""

from __future__ import annotations

import contextlib
import threading
import time
from dataclasses import dataclass


@dataclass
class StageStats:
    calls: int = 0
    spend_usd: float = 0.0
    wall_seconds: float = 0.0


class BudgetExceeded(RuntimeError):
    pass


class Supervisor:
    def __init__(self, max_spend_usd: float, max_wall_seconds: float | None = None):
        self.max_spend_usd = max_spend_usd
        self.max_wall_seconds = max_wall_seconds
        self._lock = threading.Lock()
        self._stats: dict[str, StageStats] = {}
        self._total_spend = 0.0
        self._start = time.monotonic()
        self._halted = False

    @property
    def halted(self) -> bool:
        return self._halted  # lock-free read: a one-tick-stale value is an acceptable overshoot

    @contextlib.contextmanager
    def stage(self, name: str):
        start = time.monotonic()
        with self._lock:
            self._stats.setdefault(name, StageStats())
        try:
            yield
        finally:
            elapsed = time.monotonic() - start
            with self._lock:
                self._stats[name].wall_seconds += elapsed

    def spend(self, name: str, usd: float) -> None:
        with self._lock:
            self._stats.setdefault(name, StageStats())
            self._stats[name].calls += 1
            self._stats[name].spend_usd += usd
            self._total_spend += usd
            if self._total_spend >= self.max_spend_usd:
                self._halted = True
        if self._halted:
            raise BudgetExceeded(
                f"total spend ${self._total_spend:.4f} reached cap ${self.max_spend_usd:.4f}"
            )
        if self.max_wall_seconds and (time.monotonic() - self._start) > self.max_wall_seconds:
            with self._lock:
                self._halted = True
            raise BudgetExceeded("wall-clock budget exceeded")

    def report(self) -> dict:
        with self._lock:
            return {
                "total_spend_usd": round(self._total_spend, 4),
                "wall_seconds": round(time.monotonic() - self._start, 2),
                "halted": self._halted,
                "stages": {
                    name: {
                        "calls": s.calls,
                        "spend_usd": round(s.spend_usd, 4),
                        "wall_seconds": round(s.wall_seconds, 2),
                    }
                    for name, s in self._stats.items()
                },
            }
