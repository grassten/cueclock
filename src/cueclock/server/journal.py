"""Session journal: what the live engine actually did, timestamped.

A flag-key ring dump captures raw frames, but diagnosing a live failure also
needs the other half of the story — which events the running engine fired,
what the clock state was, and which buttons were pressed, at what time
(2026-08-01 lesson: replaying a dump through *current* code shows what that
code would do, not what the live session did). The Hub logs every operator
command, vision event, and clock state transition here; ``cueclock run``
copies the recent tail into each ring dump as ``events.jsonl``.

Records carry both wall time (for humans) and ``time.monotonic()`` (the same
clock the vision pipeline timestamps use), so journal lines align with dumped
frame timestamps.

Added 2026-08-01: vision-event records also carry ``frame_t`` — the
confirming frame's *capture* timestamp (camera clock, seconds since the
session's first frame), unambiguous against the record's emit-time ``mono``
stamp — and a ``detect_heartbeat`` record every ~5 s reports the effective
detect rate and the age of the newest processed frame (hub.py), so
detect-loop starvation is measurable from the journal alone. New kinds and
fields are strictly additive: journal readers must ignore what they don't
recognize.
"""

import json
import time
from collections import deque
from datetime import UTC, datetime
from pathlib import Path


class SessionJournal:
    """Append-only in-memory ring of engine happenings, optionally on disk.

    Not thread-safe by design: all writers live on the server's asyncio loop
    (Hub commands, vision dispatch coroutines, the tick loop).
    """

    def __init__(self, path: Path | None = None, keep: int = 4000) -> None:
        self._records: deque[dict[str, object]] = deque(maxlen=keep)
        self._path = path
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)

    def log(self, kind: str, **detail: object) -> None:
        record: dict[str, object] = {
            "wall": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "mono": round(time.monotonic(), 3),
            "kind": kind,
            **detail,
        }
        self._records.append(record)
        if self._path is not None:
            with self._path.open("a") as fh:
                fh.write(json.dumps(record) + "\n")

    def tail(self, seconds: float) -> list[dict[str, object]]:
        """Records from the last ``seconds`` of monotonic time."""
        cutoff = time.monotonic() - seconds
        return [r for r in self._records if isinstance(r["mono"], float) and r["mono"] >= cutoff]

    def dump_tail(self, path: Path, seconds: float = 60.0) -> None:
        """Write the recent tail as JSONL (one flag dump's worth of context)."""
        with path.open("w") as fh:
            for record in self.tail(seconds):
                fh.write(json.dumps(record) + "\n")
