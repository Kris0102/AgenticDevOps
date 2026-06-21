"""In-memory, per-run log ring buffer for the live dashboard feed.

A structlog processor tees every (already level-filtered, contextvar-enriched) log
line in here; the dashboard SSE endpoint drains it. Deliberately ephemeral and
process-local — this is the *live* feed only. The durable, queryable record is the
`events` table (audit trail). Fits the deployment model: self-hosted, single-user,
one process. No DB, no schema, no cross-restart persistence by design.
"""
from __future__ import annotations

import threading
from collections import defaultdict, deque
from typing import Any, Deque, Dict, List, Tuple

_Entry = Tuple[int, Dict[str, Any]]   # (in-run monotonic seq, structlog event dict)


class RunLogBus:
    def __init__(self, maxlen: int = 2000) -> None:
        # Bounded per run: a long run drops its oldest live lines (the durable record
        # is the events table, so this loss is cosmetic — the tail is what's on screen).
        self._buffers: Dict[str, Deque[_Entry]] = defaultdict(lambda: deque(maxlen=maxlen))
        self._counters: Dict[str, int] = defaultdict(int)
        self._done: set[str] = set()
        self._lock = threading.Lock()   # tee writes from the run's worker thread; SSE reads from the event loop

    def append(self, run_id: str, entry: Dict[str, Any]) -> None:
        with self._lock:
            self._counters[run_id] += 1
            self._buffers[run_id].append((self._counters[run_id], entry))

    def since(self, run_id: str, cursor: int) -> Tuple[List[_Entry], int]:
        """New entries with seq > cursor, plus the advanced cursor. Cursor lives in the
        per-run seq space so a maxlen overflow can't desync it."""
        with self._lock:
            buf = self._buffers.get(run_id)
            if not buf:
                return [], cursor
            fresh = [(seq, ed) for (seq, ed) in buf if seq > cursor]
            new_cursor = fresh[-1][0] if fresh else cursor
            return fresh, new_cursor

    def mark_done(self, run_id: str) -> None:
        """Signal the run reached a terminal/paused-terminal state so the live stream
        can close after draining (rather than heartbeating forever)."""
        with self._lock:
            self._done.add(run_id)

    def is_done(self, run_id: str) -> bool:
        with self._lock:
            return run_id in self._done

    def reset(self, run_id: str) -> None:
        """Clear the done flag — a paused run that's about to resume produces more."""
        with self._lock:
            self._done.discard(run_id)


# Process-wide singleton — imported by both the logging tee and the API service.
log_bus = RunLogBus()
