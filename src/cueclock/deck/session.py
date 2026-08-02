"""Session lifecycle behind the deck's Session key (phase 2, hardware.md §C4).

The Mac mini lives behind the TV with no keyboard in reach, so one key has to
start and stop everything a practice session needs: `cueclock run` (camera,
vision, clock, server, Syphon), OBS on top of it, and a power assertion so the
machine doesn't nap mid-rack.

Everything here is a pure state machine over injected callables — `spawn` for
long-lived children, `call` for fire-and-forget commands, `now` for the clock.
`daemon.py` supplies the real subprocess versions; tests supply fakes, so the
whole start/stop sequence is unit-tested without a camera, OBS, or a deck.

The machine is advanced by `tick(server_up)` from the daemon's existing 2 s
poll rather than by a background thread: the deck must keep rendering and
dispatching other keys while a session comes up, and a thread would buy
nothing but a lock.
"""

from __future__ import annotations

import signal
import subprocess  # nosec B404: fixed argv lists, no shell, no user input
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import IO, Protocol

REPO_ROOT = Path(__file__).resolve().parents[3]
RUN_LOG = Path.home() / "Library" / "Logs" / "cueclock-run.log"

# The empty-table reference the canonical launch command carries (README).
# Refreshed after any camera bump — recipe in hardware.md §E2, which is also
# where this default gets updated. `cueclock deck --reference` overrides it
# without a code edit.
DEFAULT_REFERENCE = "tests/clips/empty_table_0801"

# How long a start may take before it is called failed. Cold camera open +
# reference load + drift check measured ~12 s; 60 s is generous enough that a
# slow disk never trips it and short enough that a broken start doesn't leave
# STARTING… on the key forever.
START_TIMEOUT_S = 60.0
# SIGINT is the documented clean stop (it closes the pylon device so the camera
# cools). If the process is wedged past this, escalate once to SIGTERM — which
# cli.py maps to KeyboardInterrupt, so it takes the same shutdown path.
STOP_ESCALATE_S = 20.0


class SessionState(Enum):
    """What the Session key is showing — and what a press means next."""

    OFF = "OFF"
    STARTING = "STARTING…"
    ON = "ON"
    STOPPING = "STOPPING…"
    DEV = "DEV"  # a session is up that this daemon didn't spawn — hands off

    @property
    def face(self) -> str:
        return self.value


class Process(Protocol):
    """The slice of `subprocess.Popen` this module uses (so fakes stay small)."""

    def poll(self) -> int | None: ...
    def send_signal(self, sig: int) -> None: ...
    def terminate(self) -> None: ...


def run_command(reference: str = DEFAULT_REFERENCE) -> tuple[str, ...]:
    """The canonical `cueclock run` argv (README "Running it").

    `sys.executable` rather than `uv run`: the daemon must never block on uv's
    lock while another cueclock command holds it, and the venv python is the
    same interpreter uv would pick anyway.
    """
    return (
        sys.executable,
        "-m",
        "cueclock.cli",
        "run",
        "--rectify",
        "--reference",
        reference,
        "--host",
        "0.0.0.0",  # nosec B104: LAN-only rig; /control is used from a phone
    )


# `open` returns as soon as OBS is handed the launch, so this is a `call`, not
# a spawn. --startreplaybuffer arms the buffer the Replay key needs (phase 3);
# --disable-shutdown-check skips the "OBS didn't shut down properly" nag when a
# previous session ended badly.
OBS_OPEN = (
    "/usr/bin/open",
    "-a",
    "OBS",
    "--args",
    "--startreplaybuffer",
    "--disable-shutdown-check",
)
# Graceful quit only — never kill(1). A killed OBS reopens in safe mode and
# drops the projector, which is exactly the state a headless rig can't fix.
OBS_QUIT = ("/usr/bin/osascript", "-e", 'tell application "OBS" to quit')
# -d: display sleep is what kills a session mid-rack (the TV is the output).
CAFFEINATE = ("/usr/bin/caffeinate", "-d")


def _spawn(cmd: Sequence[str]) -> Process:
    """Start a long-lived child in the repo root, logging to RUN_LOG."""
    RUN_LOG.parent.mkdir(parents=True, exist_ok=True)
    log: IO[bytes] = RUN_LOG.open("ab")
    return subprocess.Popen(  # nosec B603: fixed argv, shell=False
        list(cmd), cwd=REPO_ROOT, stdout=log, stderr=subprocess.STDOUT, start_new_session=True
    )


def _call(cmd: Sequence[str]) -> int:
    """Run a short command to completion; never raise into the deck loop."""
    try:
        return subprocess.run(  # nosec B603: fixed argv, shell=False
            list(cmd), cwd=REPO_ROOT, capture_output=True, timeout=20.0, check=False
        ).returncode
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"session: {cmd[0]} failed: {exc}", flush=True)
        return 1


@dataclass
class SessionManager:
    """Owns `run` + `caffeinate` and drives OBS around them.

    Ownership is the whole point of the DEV state: this object only ever
    signals children it spawned itself. A `run` started from a terminal shows
    up as a live server with no handle here, and the key refuses to touch it.
    """

    reference: str = DEFAULT_REFERENCE
    spawn: Callable[[Sequence[str]], Process] = _spawn
    call: Callable[[Sequence[str]], int] = _call
    now: Callable[[], float] = time.monotonic

    _run: Process | None = None
    _caffeinate: Process | None = None
    _started_at: float = 0.0
    _stopping_since: float | None = None
    _escalated: bool = False
    _obs_launched: bool = False

    @property
    def owns_session(self) -> bool:
        return self._run is not None

    def start(self) -> None:
        """Spawn `run` + the power assertion. OBS waits for the server (tick)."""
        if self._run is not None:
            return
        self._run = self.spawn(run_command(self.reference))
        self._caffeinate = self.spawn(CAFFEINATE)
        self._started_at = self.now()
        self._stopping_since = None
        self._escalated = False
        self._obs_launched = False
        print(f"session: starting (log: {RUN_LOG})", flush=True)

    def stop(self) -> None:
        """Quit OBS, then SIGINT `run`; `tick` finishes when the child exits."""
        if self._run is None or self._stopping_since is not None:
            return
        self._stopping_since = self.now()
        self._escalated = False
        self.call(OBS_QUIT)
        self._signal_run(signal.SIGINT)
        print("session: stopping (OBS quit, SIGINT run)", flush=True)

    def tick(self, server_up: bool) -> SessionState:
        """Advance the machine one step and report what the key should show."""
        if self._stopping_since is not None:
            return self._tick_stopping(self._stopping_since)
        if self._run is None:
            # Someone else's session (a terminal `run`) or nothing at all.
            return SessionState.DEV if server_up else SessionState.OFF
        if self._run.poll() is not None:
            print("session: `run` exited on its own", flush=True)
            self._release()
            return SessionState.OFF
        if self._obs_launched:
            return SessionState.ON
        if server_up:
            self.call(OBS_OPEN)  # server first, or the overlay loads blank
            self._obs_launched = True
            print("session: server up — OBS launched", flush=True)
            return SessionState.ON
        if self.now() - self._started_at > START_TIMEOUT_S:
            print(f"session: no server after {START_TIMEOUT_S:.0f} s — see {RUN_LOG}", flush=True)
            self._signal_run(signal.SIGINT)
            self._release()
            return SessionState.OFF
        return SessionState.STARTING

    def _tick_stopping(self, stopping_since: float) -> SessionState:
        if self._run is None or self._run.poll() is not None:
            self._release()
            print("session: stopped", flush=True)
            return SessionState.OFF
        if not self._escalated and self.now() - stopping_since > STOP_ESCALATE_S:
            self._escalated = True
            print("session: `run` still up — escalating to SIGTERM", flush=True)
            self._signal_run(signal.SIGTERM)
        return SessionState.STOPPING

    def _signal_run(self, sig: signal.Signals) -> None:
        if self._run is None:
            return
        try:
            self._run.send_signal(sig)
        except (OSError, ValueError) as exc:  # already reaped / bad handle
            print(f"session: signal {sig.name} failed: {exc}", flush=True)

    def _release(self) -> None:
        """Drop both children and the power assertion; back to a clean OFF."""
        if self._caffeinate is not None:
            try:
                self._caffeinate.terminate()
            except (OSError, ValueError) as exc:
                print(f"session: caffeinate release failed: {exc}", flush=True)
        self._run = None
        self._caffeinate = None
        self._stopping_since = None
        self._escalated = False
        self._obs_launched = False
