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

import json
import os
import signal
import subprocess  # nosec B404: fixed argv lists, no shell, no user input
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import IO, Protocol

from cueclock.deck import obsws

REPO_ROOT = Path(__file__).resolve().parents[3]
RUN_LOG = Path.home() / "Library" / "Logs" / "cueclock-run.log"
# Which processes this daemon owns, so a restarted daemon can re-adopt them
# instead of staring at its own session as if it were a stranger's.
PIDFILE = Path.home() / ".cueclock-session.json"

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
# How long a graceful OBS quit is given before "still running" is read as the
# modal quit dialog holding it (a clean quit measured 0.3-3 s, 2026-08-02).
OBS_QUIT_GRACE_S = 5.0
# OBS needs ~10 s from launch before its WebSocket server answers, so the
# post-launch setup (Syphon rebind + TV projector) is retried on the poll
# rather than assumed. 20 ticks ~= 40 s.
OBS_PREP_ATTEMPTS = 20


class SessionState(Enum):
    """The session's state; `face` is what the key says about it."""

    OFF = "off"
    STARTING = "starting"
    ON = "on"
    STOPPING = "stopping"
    DEV = "dev"  # a session is up that this daemon didn't spawn — hands off

    @property
    def face(self) -> str:
        """Key text: the **action** when there is one, the state otherwise.

        A key reading `OFF` that turns the rig *on* is backwards (Steven,
        2026-08-02) — at the table you read a button for what pressing it
        does. So idle says `START`. Running has no tap action (a stray tap
        must never kill the camera mid-rack), so it states where you are and
        `sub` names the gesture that gets out.
        """
        return _FACES[self]

    @property
    def sub(self) -> str | None:
        """The dimmed second line, where the state alone wouldn't say enough."""
        return _SUBS.get(self)


_FACES = {
    SessionState.OFF: "START",
    SessionState.STARTING: "STARTING…",
    SessionState.ON: "ON",
    SessionState.STOPPING: "STOPPING…",
    SessionState.DEV: "DEV",
}

# Only the running state needs one: it is the one face that states where you
# are rather than what a tap does, so it has to name the way out.
_SUBS = {SessionState.ON: "HOLD 1s"}


class Process(Protocol):
    """The slice of `subprocess.Popen` this module uses (so fakes stay small)."""

    @property
    def pid(self) -> int: ...
    def poll(self) -> int | None: ...
    def send_signal(self, sig: int) -> None: ...
    def terminate(self) -> None: ...


@dataclass(frozen=True)
class PidProcess:
    """A child this daemon started but no longer holds a handle to.

    The deck daemon gets relaunched constantly (every layout deploy is a
    `launchctl kickstart`), and a session must survive that — killing a live
    practice session to ship a label change would be absurd. But the restarted
    daemon then had no handle, so its own session read `DEV` and the key went
    dead (2026-08-02, mid-session). Re-attaching by pid closes that: same
    ownership rule, just recovered from disk.
    """

    pid: int

    def poll(self) -> int | None:
        try:
            os.kill(self.pid, 0)  # signal 0: liveness probe, changes nothing
        except ProcessLookupError:
            return 0
        except PermissionError:  # alive, just not ours to signal
            return None
        return None

    def send_signal(self, sig: int) -> None:
        os.kill(self.pid, sig)

    def terminate(self) -> None:
        self.send_signal(signal.SIGTERM)


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
# Is OBS still there? (`pgrep` exit status: 0 = running.)
OBS_ALIVE = ("/usr/bin/pgrep", "-x", "OBS")
# Last resort. If an output was still winding down, the quit parks on a modal
# "OBS is still currently active" dialog and AppleScript's quit blocks until
# someone clicks it — with no keyboard at the table, that someone has to be
# us. Verified to clear it 2026-08-02.
OBS_DISMISS = (
    "/usr/bin/osascript",
    "-e",
    'tell application "System Events" to tell process "OBS" to click button "OK" of window 1',
)
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
            # 8 s, not 20: a quit caught by the modal dialog blocks until the
            # timeout, and that wait is dead time before we can dismiss it.
            list(cmd),
            cwd=REPO_ROOT,
            capture_output=True,
            timeout=8.0,
            check=False,
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
    stop_outputs: Callable[[], bool] = obsws.stop_all_outputs
    bind_syphon: Callable[[], bool] = obsws.bind_syphon
    open_projector: Callable[[], bool] = obsws.open_projector
    pidfile: Path = PIDFILE
    process_for_pid: Callable[[int], Process] = PidProcess

    _run: Process | None = None
    _caffeinate: Process | None = None
    _started_at: float = 0.0
    _stopping_since: float | None = None
    _escalated: bool = False
    _obs_launched: bool = False
    _obs_dismissed: bool = False
    _syphon_bound: bool = False
    _projector_open: bool = False
    _obs_prep_tries: int = 0

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
        self._obs_dismissed = False
        self._syphon_bound = False
        self._projector_open = False
        self._obs_prep_tries = 0
        self._write_pidfile()
        print(f"session: starting (log: {RUN_LOG})", flush=True)

    def stop(self) -> None:
        """SIGINT `run`, then take OBS down; `tick` finishes when both are gone."""
        if self._run is None or self._stopping_since is not None:
            return
        self._stopping_since = self.now()
        self._escalated = False
        # `run` first, always: it owns the camera, and it must not wait on
        # anything OBS does. Measured 2026-08-02 — a modal OBS dialog blocked
        # the quit for the full 20 s timeout, which both delayed the camera
        # shutdown and burned SIGINT's entire grace window before it was even
        # sent, forcing a pointless SIGTERM.
        self._signal_run(signal.SIGINT)
        # Then OBS, in three steps because each one alone has been seen to
        # fail (2026-08-02): drive every output to stopped over obs-websocket,
        # quit gracefully, and — if a dialog caught the quit anyway — click it.
        if not self.stop_outputs():
            print("session: OBS outputs not confirmed stopped — may prompt on quit", flush=True)
        self.call(OBS_QUIT)
        print("session: stopping (SIGINT run, OBS quit)", flush=True)

    def tick(self, server_up: bool) -> SessionState:
        """Advance the machine one step and report what the key should show."""
        if self._stopping_since is not None:
            return self._tick_stopping(self._stopping_since)
        if self._run is None:
            if server_up and self._adopt():
                return SessionState.ON
            # Someone else's session (a terminal `run`) or nothing at all.
            return SessionState.DEV if server_up else SessionState.OFF
        if self._run.poll() is not None:
            print("session: `run` exited on its own", flush=True)
            self._release()
            return SessionState.OFF
        if self._obs_launched:
            self._prepare_obs_once()
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

    def _write_pidfile(self) -> None:
        """Record what we own, so a restarted daemon can pick it back up."""
        if self._run is None:
            return
        payload = {"run": self._run.pid}
        if self._caffeinate is not None:
            payload["caffeinate"] = self._caffeinate.pid
        try:
            self.pidfile.write_text(json.dumps(payload))
        except OSError as exc:
            print(f"session: could not write {self.pidfile.name} ({exc})", flush=True)

    def _clear_pidfile(self) -> None:
        try:
            self.pidfile.unlink(missing_ok=True)
        except OSError as exc:
            print(f"session: could not clear {self.pidfile.name} ({exc})", flush=True)

    def _adopt(self) -> bool:
        """Re-attach to a session this daemon started before it was restarted.

        Only ever adopts pids *we* wrote down: a terminal `run` leaves no
        pidfile and still reads `DEV`, so the ownership rule is unchanged —
        this just stops a `launchctl kickstart` from disowning our own work.
        """
        try:
            payload = json.loads(self.pidfile.read_text())
        except (OSError, json.JSONDecodeError):
            return False
        run_pid = payload.get("run")
        if not isinstance(run_pid, int):
            return False
        run = self.process_for_pid(run_pid)
        if run.poll() is not None:  # stale file from a session that ended
            self._clear_pidfile()
            return False
        self._run = run
        caffeinate_pid = payload.get("caffeinate")
        if isinstance(caffeinate_pid, int):
            caffeinate = self.process_for_pid(caffeinate_pid)
            self._caffeinate = caffeinate if caffeinate.poll() is None else None
        self._started_at = self.now()
        self._obs_launched = True  # OBS came up with the session we're adopting
        self._syphon_bound = False  # cheap to redo, and it may have been missed
        self._projector_open = False
        self._obs_prep_tries = 0
        print(f"session: adopted the running session (run pid {run_pid})", flush=True)
        return True

    def _prepare_obs_once(self) -> None:
        """Make OBS show this session: table feed bound, program on the TV.

        Both need OBS's WebSocket, which answers ~10 s after launch, so this
        retries on the poll and then gives up loudly rather than silently
        leaving a black table or a blank TV.
        """
        done = self._syphon_bound and self._projector_open
        if done or self._obs_prep_tries >= OBS_PREP_ATTEMPTS:
            return
        self._obs_prep_tries += 1
        if not self._syphon_bound and self.bind_syphon():
            self._syphon_bound = True
        if not self._projector_open and self.open_projector():
            self._projector_open = True
        if self._obs_prep_tries == OBS_PREP_ATTEMPTS:
            missing = [
                name
                for name, ok in (
                    ("table feed", self._syphon_bound),
                    ("TV projector", self._projector_open),
                )
                if not ok
            ]
            if missing:
                print(f"session: OBS setup incomplete — {', '.join(missing)}", flush=True)

    def _tick_stopping(self, stopping_since: float) -> SessionState:
        # A graceful OBS quit takes a couple of seconds, so "still alive" only
        # means the modal dialog caught it once that grace has passed — check
        # any sooner and every clean quit looks like a stuck one. Checking
        # here rather than inside stop() also keeps the deck loop responsive.
        if not self._obs_dismissed and self.now() - stopping_since > OBS_QUIT_GRACE_S:
            self._obs_dismissed = True
            if self.call(OBS_ALIVE) == 0:
                print("session: OBS still up — dismissing its quit dialog", flush=True)
                self.call(OBS_DISMISS)
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
        self._clear_pidfile()
        self._run = None
        self._caffeinate = None
        self._stopping_since = None
        self._escalated = False
        self._obs_launched = False
        self._obs_dismissed = False
        self._syphon_bound = False
        self._projector_open = False
        self._obs_prep_tries = 0
