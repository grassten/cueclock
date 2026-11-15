"""The RECAL key's process: runs `cueclock recalibrate` and keeps its result.

The command does the work (`cueclock/recalibrate.py`) and is what gets tested
and what works over SSH. This side only starts it, notices when it is done,
and turns its JSON result into a key face. It runs as a child process for the
same reason the camera probe does (`session.CAMERA_PROBE`): the always-on deck
daemon never loads the pylon runtime itself.

It takes ~10-20 s (camera open, 3 s of capture, two fits), so it is polled
rather than waited on. The deck keeps drawing, and the Session key can refuse
a start while the camera is in use.
"""

from __future__ import annotations

import json
import subprocess  # nosec B404: fixed argv, no shell, no user input
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import IO, Protocol

from cueclock.deck.faces import BG_ERR, BG_OK, Face, Rgb
from cueclock.deck.session import REPO_ROOT, RUN_LOG

RECAL_COMMAND = (sys.executable, "-u", "-m", "cueclock.cli", "recalibrate", "--json")
HOLD_SUB = "HOLD 2s"


class RecalProcess(Protocol):
    """The slice of `subprocess.Popen` used here."""

    def poll(self) -> int | None: ...
    def communicate(self) -> tuple[str, str]: ...


def _spawn(cmd: Sequence[str]) -> RecalProcess:
    """Start the command with stdout piped (the result is its last line).

    stderr goes to the run log, where `record`'s camera messages belong.
    """
    RUN_LOG.parent.mkdir(parents=True, exist_ok=True)
    log: IO[bytes] = RUN_LOG.open("ab")
    return subprocess.Popen(  # nosec B603: fixed argv, shell=False
        list(cmd), cwd=REPO_ROOT, stdout=subprocess.PIPE, stderr=log, text=True
    )


@dataclass(frozen=True)
class Outcome:
    """The fields of `recalibrate.Result` the key shows.

    Read from the command's JSON rather than by importing `Result`, which
    would pull OpenCV into the always-on daemon for three fields.
    """

    ok: bool
    reason: str
    residual_px: float | None = None
    drift_after: float | None = None


def _number(value: object) -> float | None:
    return float(value) if isinstance(value, int | float) else None


def parse_result(stdout: str) -> Outcome:
    """The command's result from its last JSON line; anything else is a failure."""
    for line in reversed(stdout.splitlines()):
        if not line.startswith("{"):
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            break
        if isinstance(data, dict) and isinstance(data.get("ok"), bool):
            return Outcome(
                ok=data["ok"],
                reason=str(data.get("reason") or ""),
                residual_px=_number(data.get("residual_px")),
                drift_after=_number(data.get("drift_after")),
            )
        break
    return Outcome(ok=False, reason="RECAL FAILED")


@dataclass
class Recalibrator:
    """At most one recalibration at a time, and the result of the last one."""

    spawn: Callable[[Sequence[str]], RecalProcess] = _spawn
    result: Outcome | None = None
    _proc: RecalProcess | None = None

    @property
    def busy(self) -> bool:
        return self._proc is not None

    def start(self) -> bool:
        """Start one; False if one is already running."""
        if self._proc is not None:
            return False
        self.result = None
        self._proc = self.spawn(RECAL_COMMAND)
        print("recal: started", flush=True)
        return True

    def poll(self) -> bool:
        """True exactly once, on the poll that finds the command finished."""
        if self._proc is None or self._proc.poll() is None:
            return False
        stdout, _ = self._proc.communicate()
        self._proc = None
        print(stdout.rstrip(), flush=True)  # the command's own summary, into the deck log
        self.result = parse_result(stdout)
        return True

    def face(self, stripe: Rgb | None = None) -> Face:
        """What the RECAL key says: the gesture, the work, or the last result.

        A result stays on the key until the next hold or until the key goes
        dark at the next session start, the same way a failed start stays on
        Session. Nobody has to be looking at the moment it finishes.
        """
        if self._proc is not None:
            return Face(text="RECAL…", stripe=stripe)
        result = self.result
        if result is None:
            return Face(text="RECAL", sub=HOLD_SUB, stripe=stripe)
        if not result.ok:
            # The reason in the main tier (it can be two words), the gesture
            # that retries it underneath.
            text = result.reason.replace(" ", "\n", 1)
            return Face(text=text, sub=HOLD_SUB, bg=BG_ERR, stripe=stripe)
        residual = "—" if result.residual_px is None else f"{result.residual_px:.1f}PX"
        drift = "—" if result.drift_after is None else f"{result.drift_after:.0f}"
        return Face(text=f"OK {residual}\nDRIFT {drift}", sub=HOLD_SUB, bg=BG_OK, stripe=stripe)
