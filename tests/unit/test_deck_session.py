"""Session-key state machine (deck phase 2) — the whole start/stop sequence
without a camera, OBS, or a Stream Deck.

`SessionManager` takes its process work as injected callables precisely so this
file can drive it: `FakeProcess` stands in for `run`/`caffeinate`, and a list
records the fire-and-forget commands (OBS open/quit) in order.
"""

from __future__ import annotations

import math
import signal
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import mkdtemp

import pytest

from cueclock.deck import obsws
from cueclock.deck.session import (
    CAFFEINATE,
    CAMERA_PROBE,
    IDLE_STOP_S,
    OBS_ALIVE,
    OBS_DISMISS,
    OBS_OPEN,
    OBS_QUIT,
    OBS_QUIT_GRACE_S,
    PREP_ATTEMPTS,
    START_TIMEOUT_S,
    STOP_ESCALATE_S,
    WAKE_DISPLAY,
    SessionManager,
    SessionState,
    run_command,
    shot_age_of,
)

TV_ON = ("tv", "EnsureOn")
TV_OFF = ("tv", "PowerOff")
BIND = ("obsws", "BindSyphon")
PROJECTOR = ("obsws", "OpenProjector")
SIDECAM = ("sidecam", "Ensure")
RECORD = ("obsws", "StartRecord")
STOP_RECORD = ("obsws", "StopRecord")
WALL0 = datetime(2026, 11, 16, 22, 0, tzinfo=UTC)


class FakeProcess:
    """A child that exits only when the test says so."""

    def __init__(self, log: list[tuple[str, ...]] | None = None, pid: int = 4242) -> None:
        self.pid = pid
        self.signals: list[int] = []
        self.terminated = False
        self.exit_code: int | None = None
        self._log = log if log is not None else []

    def poll(self) -> int | None:
        return self.exit_code

    def send_signal(self, sig: int) -> None:
        self.signals.append(sig)
        self._log.append(("signal", signal.Signals(sig).name))

    def terminate(self) -> None:
        self.terminated = True


class Harness:
    """A manager wired to fakes, plus the transcript of what it ran."""

    def __init__(
        self,
        outputs_stop: bool = True,
        obs_survives_quit: bool = False,
        syphon_binds: bool = True,
        projector_opens: bool = True,
        tv_wakes: bool = True,
        sidecam_settles: bool = True,
        camera: bool = True,
        pidfile: Path | None = None,
        record_elapsed: float | None = 0.0,
        record_path: str | None = "/Movies/2026-11-16 22-00-00.mov",
    ) -> None:
        self.spawned: list[tuple[str, ...]] = []
        self.called: list[tuple[str, ...]] = []
        self.procs: list[FakeProcess] = []
        self.clock = 0.0
        self.outputs_stop = outputs_stop
        self.obs_survives_quit = obs_survives_quit
        self.syphon_binds = syphon_binds
        self.projector_opens = projector_opens
        self.tv_wakes = tv_wakes
        self.sidecam_settles = sidecam_settles
        self.camera = camera
        self.camera_probes = 0
        self.record_elapsed = record_elapsed
        # What OBS says it has written when asked at the stop; None = can't say.
        self.record_age: float | None = None
        self.record_path = record_path
        self.recordings: list[dict[str, str]] = []
        self.adoptable: dict[int, FakeProcess] = {}
        # What `GET /api/replay_anchors` says: no shot yet, until a test sets one.
        self.shot_age: float | None = math.inf
        self.shot_age_asks = 0
        self.manager = SessionManager(
            pidfile=pidfile or Path(mkdtemp()) / "session.json",
            process_for_pid=self._process_for_pid,
            spawn=self._spawn,
            call=self._call,
            now=lambda: self.clock,
            stop_outputs=self._stop_outputs,
            bind_syphon=self._bind_syphon,
            open_projector=self._open_projector,
            tv_on=self._tv_on,
            tv_off=self._tv_off,
            sidecam_ready=self._sidecam_ready,
            clear_match=self._clear_match,
            start_record=self._start_record,
            record_age=lambda: self.record_age,
            stop_record=self._stop_record,
            log_recording=self.recordings.append,
            shot_age=self._shot_age,
            wall=lambda: WALL0 + timedelta(seconds=self.clock),
        )

    def _start_record(self) -> float | None:
        self.called.append(RECORD)
        return self.record_elapsed

    def _stop_record(self) -> str | None:
        self.called.append(STOP_RECORD)
        return self.record_path

    def _shot_age(self) -> float | None:
        self.shot_age_asks += 1
        return self.shot_age

    def _clear_match(self) -> bool:
        self.called.append(("api", "MatchClear"))
        return True

    def _stop_outputs(self) -> bool:
        self.called.append(("obsws", "StopOutputs"))
        return self.outputs_stop

    def _bind_syphon(self) -> bool:
        self.called.append(("obsws", "BindSyphon"))
        return self.syphon_binds

    def _open_projector(self) -> bool:
        self.called.append(("obsws", "OpenProjector"))
        return self.projector_opens

    def _tv_on(self) -> bool:
        self.called.append(("tv", "EnsureOn"))
        return self.tv_wakes

    def _sidecam_ready(self) -> bool:
        self.called.append(SIDECAM)
        return self.sidecam_settles

    def _tv_off(self) -> bool:
        self.called.append(("tv", "PowerOff"))
        return True

    def _process_for_pid(self, pid: int) -> FakeProcess:
        return self.adoptable.get(pid, FakeProcess(pid=pid, log=self.called))

    def _spawn(self, cmd: Sequence[str]) -> FakeProcess:
        self.spawned.append(tuple(cmd))
        proc = FakeProcess(self.called)
        self.procs.append(proc)
        return proc

    def _call(self, cmd: Sequence[str]) -> int:
        if tuple(cmd) == CAMERA_PROBE:
            # A question, not an action — counted, but kept out of `called`,
            # which is the transcript of what the session actually did.
            self.camera_probes += 1
            return 0 if self.camera else 1
        self.called.append(tuple(cmd))
        if tuple(cmd) == OBS_ALIVE:  # pgrep: 0 = still running
            return 0 if self.obs_survives_quit else 1
        return 0

    @property
    def run_proc(self) -> FakeProcess:
        return self.procs[1]  # procs[0] is the display wake

    @property
    def caffeinate_proc(self) -> FakeProcess:
        return self.procs[2]


def test_idle_daemon_reads_off_and_a_foreign_server_reads_dev() -> None:
    h = Harness()
    assert h.manager.tick(server_up=False) is SessionState.OFF
    # A `run` started from a terminal: the API answers, but this daemon has no
    # handle on it — the key must refuse rather than signal someone else's job.
    assert h.manager.tick(server_up=True) is SessionState.DEV
    assert not h.manager.owns_session


def test_start_spawns_run_and_caffeinate_then_launches_obs_when_the_server_answers() -> None:
    h = Harness()
    h.manager.start()
    # The display wake leads: between sessions the Mac has stopped driving HDMI,
    # so the wall reads "no signal" however awake the TV is.
    assert h.spawned == [WAKE_DISPLAY, run_command(), CAFFEINATE]
    assert h.manager.owns_session
    # And the TV is asked for immediately — it wakes from standby on its own
    # clock, so the magic packet goes out well before OBS needs a display.
    assert h.called == [TV_ON]

    # Server not up yet: STARTING, and OBS stays closed — launching it first
    # parks the overlay browser source on a blank error page (hardware.md §B3).
    assert h.manager.tick(server_up=False) is SessionState.STARTING
    assert h.called == [TV_ON]

    assert h.manager.tick(server_up=True) is SessionState.ON
    assert h.called == [TV_ON, OBS_OPEN]

    # OBS is launched exactly once, however long the session runs; the ticks
    # after it point OBS's Syphon source at this session's server and confirm
    # the TV actually came up.
    assert h.manager.tick(server_up=True) is SessionState.ON
    assert h.called == [TV_ON, OBS_OPEN, TV_ON, BIND, PROJECTOR, SIDECAM, RECORD]


def test_the_syphon_source_is_rebound_once_per_session() -> None:
    # Every `run` publishes a new Syphon UUID under whatever app name launched
    # it, so a source bound by a terminal session shows black under a deck
    # session (2026-08-02). The key repoints it rather than leaving a black
    # table on the TV.
    h = Harness()
    h.manager.start()
    h.manager.tick(server_up=True)  # launches OBS
    h.called.clear()

    h.manager.tick(server_up=True)
    assert h.called == [TV_ON, BIND, PROJECTOR, SIDECAM, RECORD]

    for _ in range(5):  # all five done: stop asking
        h.manager.tick(server_up=True)
    assert h.called == [TV_ON, BIND, PROJECTOR, SIDECAM, RECORD]


def test_side_camera_settings_retry_until_they_take_then_stop() -> None:
    # The settings wait for OBS to open the camera's stream, so the first few
    # asks fail by design; once they take, the session stops asking.
    h = Harness(sidecam_settles=False)
    h.manager.start()
    h.manager.tick(server_up=True)  # launches OBS
    h.called.clear()

    for _ in range(3):
        h.manager.tick(server_up=True)
    assert h.called.count(SIDECAM) == 3

    h.sidecam_settles = True
    for _ in range(5):
        h.manager.tick(server_up=True)
    assert h.called.count(SIDECAM) == 4


def test_a_side_camera_that_never_settles_warns_and_never_blocks(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # The side angle only feeds OBS. A camera that refuses its settings costs a
    # warning in the log, never the session: it stays ON, and the ask stops.
    h = Harness(sidecam_settles=False)
    h.manager.start()
    h.manager.tick(server_up=True)

    for _ in range(PREP_ATTEMPTS + 10):
        assert h.manager.tick(server_up=True) is SessionState.ON
    assert h.called.count(SIDECAM) == PREP_ATTEMPTS
    assert "setup incomplete — side camera" in capsys.readouterr().out


def test_a_syphon_rebind_that_never_takes_gives_up() -> None:
    # OBS's WebSocket server needs ~10 s after launch, so failures are normal
    # at first — but they must not retry forever.
    h = Harness(syphon_binds=False)
    h.manager.start()
    h.manager.tick(server_up=True)
    h.called.clear()

    for _ in range(PREP_ATTEMPTS + 10):
        h.manager.tick(server_up=True)
    # The projector succeeds on the first pass; only the rebind keeps retrying.
    assert h.called.count(BIND) == PREP_ATTEMPTS
    assert h.called.count(PROJECTOR) == 1


def test_a_restarted_daemon_adopts_the_session_it_started() -> None:
    # Every layout deploy is a `launchctl kickstart`, and a live session must
    # survive it. Before adoption the restarted daemon disowned its own work
    # and the key went dead mid-session (2026-08-02).
    pidfile = Path(mkdtemp()) / "session.json"
    first = Harness(pidfile=pidfile)
    first.manager.start()
    first.manager.tick(server_up=True)
    run_pid, caffeinate_pid = first.run_proc.pid, first.caffeinate_proc.pid

    reborn = Harness(pidfile=pidfile)  # the daemon comes back with no handles
    reborn.adoptable = {
        run_pid: FakeProcess(pid=run_pid),
        caffeinate_pid: FakeProcess(pid=caffeinate_pid),
    }
    assert reborn.manager.tick(server_up=True) is SessionState.ON
    assert reborn.manager.owns_session

    # And it can stop what it adopted — that is the whole point.
    reborn.manager.stop()
    assert reborn.adoptable[run_pid].signals == [signal.SIGINT]
    assert reborn.called.count(OBS_OPEN) == 0  # OBS is already up; don't relaunch


def test_a_dead_session_is_not_adopted_and_its_pidfile_is_cleared() -> None:
    pidfile = Path(mkdtemp()) / "session.json"
    first = Harness(pidfile=pidfile)
    first.manager.start()
    run_pid = first.run_proc.pid

    reborn = Harness(pidfile=pidfile)
    dead = FakeProcess(pid=run_pid)
    dead.exit_code = 0
    reborn.adoptable = {run_pid: dead}
    # Server up but our pid is gone: someone else's server, hands off.
    assert reborn.manager.tick(server_up=True) is SessionState.DEV
    assert not pidfile.exists()


def test_a_terminal_session_is_still_never_adopted() -> None:
    # No pidfile means we did not start it — the ownership rule is unchanged.
    h = Harness()
    assert h.manager.tick(server_up=True) is SessionState.DEV
    assert not h.manager.owns_session


def test_start_that_never_serves_gives_up_and_cleans_up() -> None:
    h = Harness()
    h.manager.start()
    h.clock = START_TIMEOUT_S + 1.0
    # The camera is attached, so the timeout is not a cabling problem: FAILED
    # points at the log rather than sending anyone to check a good cable.
    assert h.manager.tick(server_up=False) is SessionState.FAILED
    assert h.run_proc.signals == [signal.SIGINT]
    assert h.caffeinate_proc.terminated
    assert not h.manager.owns_session


def test_stop_quits_obs_then_interrupts_run_and_settles_on_off() -> None:
    h = Harness()
    h.manager.start()
    h.manager.tick(server_up=True)
    h.called.clear()

    h.manager.stop()
    # Order matters, every step measured the hard way (2026-08-02): the match
    # clear goes first because it needs the server alive to hear it
    # (2026-08-13 — stopping ends the match day, so the next session comes up
    # on PLAYER 1 / PLAYER 2), `run` is signalled next so nothing OBS does can
    # delay the camera shutdown, OBS's outputs are stopped before the quit
    # because any active output raises a modal dialog no one can click at the
    # table, and the quit is verified because that dialog blocks it silently.
    assert h.called == [
        ("api", "MatchClear"),
        ("signal", "SIGINT"),
        ("obsws", "StopOutputs"),
        OBS_QUIT,
    ]

    # `run` closes the camera on its way out, so OFF waits for the exit.
    assert h.manager.tick(server_up=True) is SessionState.STOPPING
    assert not h.caffeinate_proc.terminated

    h.run_proc.exit_code = 0
    assert h.manager.tick(server_up=False) is SessionState.OFF
    assert h.caffeinate_proc.terminated
    assert not h.manager.owns_session


def test_a_failed_match_clear_never_blocks_the_stop() -> None:
    # The server may already be wedged when the hold lands — the worst that
    # may cost is yesterday's match reappearing, never a camera left open.
    h = Harness()
    h.manager.start()
    h.manager.tick(server_up=True)
    h.manager.clear_match = lambda: False
    h.manager.stop()
    assert ("signal", "SIGINT") in h.called


def test_a_clean_quit_is_never_mistaken_for_the_dialog() -> None:
    h = Harness(obs_survives_quit=False)
    h.manager.start()
    h.manager.tick(server_up=True)
    h.manager.stop()
    h.called.clear()

    h.clock = OBS_QUIT_GRACE_S + 1.0
    h.manager.tick(server_up=False)
    # It looked, found OBS gone, and clicked nothing. Checking any sooner
    # fires on every clean quit — OBS takes seconds to actually exit, which
    # is exactly the false positive that made the first fix look broken.
    assert h.called == [OBS_ALIVE]


def test_a_quit_caught_by_the_dialog_gets_clicked_once() -> None:
    h = Harness(obs_survives_quit=True)
    h.manager.start()
    h.manager.tick(server_up=True)
    h.manager.stop()
    h.called.clear()

    h.manager.tick(server_up=False)  # inside the grace: hands off
    assert h.called == []

    h.clock = OBS_QUIT_GRACE_S + 1.0
    h.manager.tick(server_up=False)
    # Still alive past the grace means the modal caught the quit; with no
    # keyboard at the table the deck clicks OK itself — once.
    assert h.called == [OBS_ALIVE, OBS_DISMISS]

    h.clock += 5.0  # still short of the SIGTERM escalation
    h.manager.tick(server_up=False)
    assert h.called == [OBS_ALIVE, OBS_DISMISS]


def test_stop_still_quits_obs_when_the_outputs_cannot_be_stopped() -> None:
    # obs-websocket disabled or OBS already gone: the quit must still happen —
    # a possible dialog beats leaving the session up.
    h = Harness(outputs_stop=False)
    h.manager.start()
    h.manager.tick(server_up=True)
    h.called.clear()

    h.manager.stop()
    assert OBS_QUIT in h.called
    assert h.run_proc.signals == [signal.SIGINT]


def test_a_wedged_run_escalates_to_sigterm_once() -> None:
    h = Harness()
    h.manager.start()
    h.manager.tick(server_up=True)
    h.manager.stop()

    h.clock = STOP_ESCALATE_S + 1.0
    assert h.manager.tick(server_up=True) is SessionState.STOPPING
    assert h.run_proc.signals == [signal.SIGINT, signal.SIGTERM]

    h.clock += STOP_ESCALATE_S  # still wedged — no signal storm
    assert h.manager.tick(server_up=True) is SessionState.STOPPING
    assert h.run_proc.signals == [signal.SIGINT, signal.SIGTERM]


def test_a_crashed_run_falls_back_to_off_and_releases_the_assertion() -> None:
    h = Harness()
    h.manager.start()
    h.manager.tick(server_up=True)

    h.run_proc.exit_code = 1
    # Server may still look up for a poll or two while the socket closes; the
    # dead child decides, not the probe. (`server_up=True` here is that stale
    # socket, not a live session — the failure is set from the dead child on
    # the same tick, so it is what the key reports.)
    assert h.manager.tick(server_up=True) is SessionState.FAILED
    assert h.caffeinate_proc.terminated
    assert not h.manager.owns_session


def test_restart_after_a_stop_spawns_a_fresh_pair() -> None:
    h = Harness()
    h.manager.start()
    h.manager.tick(server_up=True)
    h.manager.stop()
    h.run_proc.exit_code = 0
    h.manager.tick(server_up=False)

    h.manager.start()
    assert h.spawned == [
        WAKE_DISPLAY,
        run_command(),
        CAFFEINATE,
        WAKE_DISPLAY,
        run_command(),
        CAFFEINATE,
    ]
    assert h.manager.tick(server_up=False) is SessionState.STARTING


def test_double_press_while_starting_does_not_spawn_a_second_session() -> None:
    h = Harness()
    h.manager.start()
    h.manager.start()
    assert h.spawned == [WAKE_DISPLAY, run_command(), CAFFEINATE]


def test_reference_override_reaches_the_run_command() -> None:
    h = Harness()
    h.manager.reference = "tests/clips/empty_table_9999"
    h.manager.start()
    assert h.spawned[1] == run_command("tests/clips/empty_table_9999")
    assert "--rectify" in h.spawned[1]


def test_the_key_reads_as_the_action_not_the_state() -> None:
    # hardware.md §C2's button map quotes these — keep the two in step.
    # Idle says START, because a key labelled OFF that turns the rig on reads
    # backwards at the table (Steven, 2026-08-02). Running has no tap action,
    # so it states where you are and names the way out.
    # The gesture moved off the face text and onto `sub` in layout v2, so it
    # renders as the deck's dim second line — same words, the face grammar
    # every other key now follows.
    assert SessionState.OFF.face == "START"
    assert SessionState.ON.face == "ON"
    assert SessionState.ON.sub == "HOLD 1s"
    assert [s.face for s in SessionState] == [
        "START",
        "STARTING…",
        "ON",
        "STOPPING…",
        "DEV",
        "NO\nCAMERA",
        "START\nFAILED",
    ]
    # The running state names its gesture; the two failures name the next move
    # instead, because "what do I do about this" is the table-side question.
    assert [s.name for s in SessionState if s.sub is not None] == [
        "ON",
        "NO_CAMERA",
        "FAILED",
    ]
    # Only the failures are painted red (daemon.build_state reads this).
    assert [s.name for s in SessionState if s.failed] == ["NO_CAMERA", "FAILED"]


def test_stop_turns_the_tv_off_last_of_all() -> None:
    h = Harness()
    h.manager.start()
    h.manager.tick(server_up=True)
    h.manager.stop()
    h.called.clear()

    # Mid-stop the TV stays lit: the wall should keep reading STOPPING… while
    # the camera is still closing, not go black and leave you guessing.
    assert h.manager.tick(server_up=True) is SessionState.STOPPING
    assert TV_OFF not in h.called

    h.run_proc.exit_code = 0
    assert h.manager.tick(server_up=False) is SessionState.OFF
    assert h.called[-1] == TV_OFF


def test_a_crashed_run_leaves_the_tv_on() -> None:
    # Only an explicit stop darkens the wall. If `run` fell over on its own,
    # the TV is the one place the failure is visible — killing it would hide
    # exactly what someone standing at the table needs to see.
    h = Harness()
    h.manager.start()
    h.manager.tick(server_up=True)
    h.called.clear()

    h.run_proc.exit_code = 1
    assert h.manager.tick(server_up=True) is SessionState.FAILED
    assert TV_OFF not in h.called


def test_a_tv_that_never_wakes_gives_up_instead_of_packeting_forever() -> None:
    h = Harness(tv_wakes=False)
    h.manager.start()
    h.manager.tick(server_up=True)
    h.called.clear()

    for _ in range(PREP_ATTEMPTS + 10):
        h.manager.tick(server_up=True)
    # Retried on the poll, because a TV climbing out of standby is not ready
    # on the first ask — but bounded, and the session runs on regardless.
    assert h.called.count(TV_ON) == PREP_ATTEMPTS
    assert h.called.count(BIND) == 1


# --- a start that fails says so (2026-09-01) -------------------------------
#
# The bug these cover: after a power cycle the camera did not come back on the
# USB bus, `run` died at `open_camera()` within a second, and the Session key
# flashed STARTING… and dropped straight back to START. Pressed again, it did
# the same thing. Nothing on the deck, the wall or the TV said "no camera" —
# the reason was only in ~/Library/Logs/cueclock-run.log, behind the TV.


def test_a_start_with_no_camera_refuses_to_spawn_and_names_the_reason() -> None:
    h = Harness(camera=False)
    h.manager.start()
    # Nothing was spawned: no `run` to die, no caffeinate to leak, and — the
    # point — no 60 s of STARTING… before a failure that was knowable at 0 s.
    assert h.spawned == []
    assert not h.manager.owns_session
    assert h.camera_probes == 1
    # And it stays on the key. This is the half that was missing: the old
    # machine went back to OFF, so the key said START and invited the same
    # press again.
    assert h.manager.tick(server_up=False) is SessionState.NO_CAMERA
    assert h.manager.tick(server_up=False) is SessionState.NO_CAMERA


def test_a_run_that_dies_on_a_missing_camera_reads_no_camera() -> None:
    # The camera goes away between the probe and the grab — the power-cycle
    # case where pylon enumerates something half-attached.
    h = Harness()
    h.manager.start()
    assert h.spawned == [WAKE_DISPLAY, run_command(), CAFFEINATE]

    h.camera = False
    h.run_proc.exit_code = 1
    assert h.manager.tick(server_up=False) is SessionState.NO_CAMERA
    assert h.caffeinate_proc.terminated
    assert not h.manager.owns_session


def test_pressing_a_failed_key_retries_and_starts_once_the_cable_is_back() -> None:
    h = Harness(camera=False)
    h.manager.start()
    assert h.manager.tick(server_up=False) is SessionState.NO_CAMERA

    # Steven reseats the cable and presses the key again. `start()` is the
    # retry — it clears the reason and asks the bus again, so the press after
    # the fix just works with nothing else to reset.
    h.camera = True
    h.manager.start()
    assert h.spawned == [WAKE_DISPLAY, run_command(), CAFFEINATE]
    assert h.manager.tick(server_up=False) is SessionState.STARTING
    assert h.manager.tick(server_up=True) is SessionState.ON


def test_a_held_failure_yields_to_a_session_that_is_actually_up() -> None:
    # Sticky, but never against the evidence: if a `run` is answering (started
    # from a terminal, say), a key still reading NO CAMERA would be a lie.
    h = Harness(camera=False)
    h.manager.start()
    assert h.manager.tick(server_up=False) is SessionState.NO_CAMERA
    assert h.manager.tick(server_up=True) is SessionState.DEV
    assert h.manager.tick(server_up=False) is SessionState.OFF


def test_a_deliberate_stop_is_never_a_failure() -> None:
    # Only *unrequested* exits are diagnosed. A held stop ends on OFF, and the
    # camera is never probed on the way down — the key must go back to START.
    h = Harness()
    h.manager.start()
    h.manager.tick(server_up=True)
    probes = h.camera_probes

    h.manager.stop()
    h.run_proc.exit_code = 0
    assert h.manager.tick(server_up=False) is SessionState.OFF
    assert h.camera_probes == probes


def test_a_failed_start_takes_obs_down_with_it() -> None:
    # The 2026-09-01 grey wall: this start launched OBS, then `run` died. OBS
    # used to be left running, and the session that finally came up half an
    # hour later inherited it — with a GL context older than the TV's current
    # HDMI mode, so every projector it opened had no renderer behind it.
    h = Harness()
    h.manager.start()
    assert h.manager.tick(server_up=True) is SessionState.ON
    assert OBS_OPEN in h.called
    h.called.clear()

    h.run_proc.exit_code = 1
    assert h.manager.tick(server_up=False) is SessionState.FAILED
    assert h.called == [("obsws", "StopOutputs"), OBS_QUIT]


def test_a_start_that_died_before_obs_has_nothing_to_quit() -> None:
    # The common case — no camera, so `run` never served and OBS never
    # launched. Quitting an OBS this daemon did not start would be reaching
    # into someone else's window.
    h = Harness()
    h.manager.start()
    h.called.clear()

    h.run_proc.exit_code = 1
    assert h.manager.tick(server_up=False) is SessionState.FAILED
    assert OBS_QUIT not in h.called


# --- OBS session recording (#57) -------------------------------------------


def _session_on(h: Harness) -> None:
    h.manager.start()
    h.manager.tick(server_up=True)  # server up: OBS launched
    h.clock = 30.0
    h.manager.tick(server_up=True)  # prep: recording starts here


def test_stop_ends_the_recording_by_name_before_obs_is_told_to_quit() -> None:
    h = Harness()
    _session_on(h)
    h.called.clear()
    h.clock = 1830.0

    h.manager.stop()
    # StopRecord before the blanket output stop: it is the one request that
    # names the file, and a quit over a live recording raises the dialog.
    assert h.called == [
        ("api", "MatchClear"),
        ("signal", "SIGINT"),
        STOP_RECORD,
        ("obsws", "StopOutputs"),
        OBS_QUIT,
    ]
    assert h.recordings == [
        {
            "path": "/Movies/2026-11-16 22-00-00.mov",
            "start_wall": "2026-11-16T22:00:30.000+00:00",
            "stop_wall": "2026-11-16T22:30:30.000+00:00",
        }
    ]


def test_the_logged_start_is_when_obs_wrote_the_first_frame() -> None:
    # OBS acknowledges StartRecord and starts writing a second or two later,
    # so the start is back-dated from what it has written by the stop: every
    # clip and every seek is an offset from this instant.
    h = Harness()
    _session_on(h)  # asked for at 22:00:30
    h.clock = 1830.0
    h.record_age = 1798.25
    h.manager.stop()
    assert h.recordings[0]["start_wall"] == "2026-11-16T22:00:31.750+00:00"
    assert h.recordings[0]["stop_wall"] == "2026-11-16T22:30:30.000+00:00"


def test_obs_says_how_much_it_has_recorded(monkeypatch: pytest.MonkeyPatch) -> None:
    replies: dict[str, dict[str, object] | None] = {
        "GetRecordStatus": {"outputActive": True, "outputDuration": 1798250}
    }
    sent: list[str] = []

    def request(kind: str, *args: object, **kwargs: object) -> dict[str, object] | None:
        sent.append(kind)
        return replies.get(kind)

    monkeypatch.setattr(obsws, "request", request)
    assert obsws.record_age() == 1798.25
    assert obsws.start_record() == 1798.25  # already recording: adopted as it is
    assert "StartRecord" not in sent

    replies["GetRecordStatus"] = {"outputActive": False}
    replies["StartRecord"] = {}
    assert obsws.record_age() is None
    assert obsws.start_record() == 0.0 and sent[-1] == "StartRecord"

    replies["GetRecordStatus"] = None  # OBS can't be asked
    assert obsws.record_age() is None and obsws.start_record() is None


def test_the_recording_starts_once_and_retries_until_obs_answers() -> None:
    h = Harness(record_elapsed=None)  # OBS's WebSocket not up yet
    h.manager.start()
    h.manager.tick(server_up=True)
    for _ in range(3):
        h.manager.tick(server_up=True)
    assert h.called.count(RECORD) == 3

    h.record_elapsed = 0.0
    for _ in range(5):
        h.manager.tick(server_up=True)
    assert h.called.count(RECORD) == 4


def test_a_recording_that_never_starts_warns_and_logs_nothing_on_stop(
    capsys: pytest.CaptureFixture[str],
) -> None:
    h = Harness(record_elapsed=None)
    h.manager.start()
    for _ in range(PREP_ATTEMPTS + 2):
        assert h.manager.tick(server_up=True) is SessionState.ON
    assert "setup incomplete — recording" in capsys.readouterr().out

    h.manager.stop()
    assert STOP_RECORD not in h.called  # nothing of ours to stop
    assert h.recordings == []


def test_an_adopted_recording_is_back_dated_to_its_first_frame(tmp_path: Path) -> None:
    # A restarted daemon finds OBS already recording: the file began when the
    # first daemon started it, and cuts seek by offsets from that instant.
    h = Harness(pidfile=tmp_path / "session.json", record_elapsed=600.0)
    h.adoptable[777] = FakeProcess(pid=777)
    (tmp_path / "session.json").write_text('{"run": 777}')
    h.clock = 900.0
    assert h.manager.tick(server_up=True) is SessionState.ON  # adopts
    h.manager.tick(server_up=True)  # prep
    h.manager.stop()
    assert h.recordings[0]["start_wall"] == "2026-11-16T22:05:00.000+00:00"


def test_an_unconfirmed_stop_leaves_the_file_unlisted() -> None:
    # An unlisted file is one `session maintain` never cuts or deletes —
    # the safe direction to be wrong in.
    h = Harness(record_path=None)
    _session_on(h)
    h.manager.stop()
    assert STOP_RECORD in h.called
    assert h.recordings == []


def test_a_failed_start_still_logs_what_it_recorded() -> None:
    h = Harness()
    _session_on(h)
    h.called.clear()
    h.run_proc.exit_code = 1
    assert h.manager.tick(server_up=False) is SessionState.FAILED
    assert h.called[:2] == [STOP_RECORD, ("obsws", "StopOutputs")]
    assert len(h.recordings) == 1


# ---------- #60: a forgotten session stops itself ----------


def _running() -> Harness:
    """A session that is up, with OBS launched and its setup done."""
    h = Harness()
    h.manager.start()
    h.manager.tick(server_up=True)  # launches OBS
    h.manager.tick(server_up=True)  # setup
    h.called.clear()
    return h


def test_a_session_never_played_stops_itself_after_the_idle_limit() -> None:
    h = _running()
    h.clock = IDLE_STOP_S - 1
    assert h.manager.tick(server_up=True) is SessionState.ON

    h.clock = IDLE_STOP_S
    assert h.manager.tick(server_up=True) is SessionState.STOPPING
    # The same sequence the held key runs, nothing more and nothing less.
    assert h.called == [
        ("api", "MatchClear"),
        ("signal", "SIGINT"),
        STOP_RECORD,
        ("obsws", "StopOutputs"),
        OBS_QUIT,
    ]
    assert len(h.recordings) == 1  # the recording is closed and listed

    h.run_proc.exit_code = 0
    assert h.manager.tick(server_up=False) is SessionState.OFF
    assert h.called[-1] == TV_OFF
    assert h.caffeinate_proc.terminated


def test_a_recent_shot_keeps_the_session_up_until_it_too_is_old() -> None:
    h = _running()
    h.clock = IDLE_STOP_S + 10
    h.shot_age = 30.0  # a shot 30 s ago, 580 s into the session
    assert h.manager.tick(server_up=True) is SessionState.ON

    h.clock = 580 + IDLE_STOP_S - 1
    assert h.manager.tick(server_up=True) is SessionState.ON

    h.clock = 580 + IDLE_STOP_S
    h.shot_age = IDLE_STOP_S  # no shot since
    assert h.manager.tick(server_up=True) is SessionState.STOPPING


def test_the_server_is_only_asked_once_the_session_might_be_idle() -> None:
    # Playing, the question is asked about once per idle limit — not on every
    # 2 s poll.
    h = _running()
    for second in range(0, int(IDLE_STOP_S), 2):
        h.clock = second
        h.manager.tick(server_up=True)
    assert h.shot_age_asks == 0

    h.shot_age = 5.0
    for second in range(int(IDLE_STOP_S), int(IDLE_STOP_S) + 60, 2):
        h.clock = second
        h.manager.tick(server_up=True)
    assert h.shot_age_asks == 1


def test_an_unanswered_question_never_stops_the_session() -> None:
    # The server missing a poll says nothing about shots; stopping on it could
    # end a session whose last shot simply went unreported.
    h = _running()
    h.shot_age = None
    h.clock = IDLE_STOP_S * 3
    assert h.manager.tick(server_up=True) is SessionState.ON
    assert h.run_proc.signals == []


def test_a_terminal_session_is_never_stopped_for_idling() -> None:
    h = Harness()
    h.clock = IDLE_STOP_S * 3
    assert h.manager.tick(server_up=True) is SessionState.DEV
    assert h.shot_age_asks == 0


def test_an_adopted_session_gets_a_fresh_idle_limit() -> None:
    pidfile = Path(mkdtemp()) / "session.json"
    first = Harness(pidfile=pidfile)
    first.manager.start()
    run_pid = first.run_proc.pid

    reborn = Harness(pidfile=pidfile)
    reborn.adoptable = {run_pid: FakeProcess(pid=run_pid)}
    reborn.clock = 5000.0
    reborn.shot_age = 4000.0  # long idle before the daemon came back
    assert reborn.manager.tick(server_up=True) is SessionState.ON

    reborn.clock = 5000.0 + IDLE_STOP_S - 1
    assert reborn.manager.tick(server_up=True) is SessionState.ON
    reborn.clock = 5000.0 + IDLE_STOP_S
    assert reborn.manager.tick(server_up=True) is SessionState.STOPPING


def test_the_idle_stop_reads_the_replay_anchors_endpoint() -> None:
    assert shot_age_of(None) is None  # server down
    assert shot_age_of({"shot_age_s": None, "rack_age_s": None}) == math.inf
    assert shot_age_of({"shot_age_s": 12.5, "rack_age_s": 40.0}) == 12.5
