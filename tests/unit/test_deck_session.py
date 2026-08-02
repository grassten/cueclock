"""Session-key state machine (deck phase 2) — the whole start/stop sequence
without a camera, OBS, or a Stream Deck.

`SessionManager` takes its process work as injected callables precisely so this
file can drive it: `FakeProcess` stands in for `run`/`caffeinate`, and a list
records the fire-and-forget commands (OBS open/quit) in order.
"""

from __future__ import annotations

import signal
from collections.abc import Sequence
from pathlib import Path
from tempfile import mkdtemp

from cueclock.deck.session import (
    CAFFEINATE,
    OBS_ALIVE,
    OBS_DISMISS,
    OBS_OPEN,
    OBS_PREP_ATTEMPTS,
    OBS_QUIT,
    OBS_QUIT_GRACE_S,
    START_TIMEOUT_S,
    STOP_ESCALATE_S,
    SessionManager,
    SessionState,
    run_command,
)


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
        pidfile: Path | None = None,
    ) -> None:
        self.spawned: list[tuple[str, ...]] = []
        self.called: list[tuple[str, ...]] = []
        self.procs: list[FakeProcess] = []
        self.clock = 0.0
        self.outputs_stop = outputs_stop
        self.obs_survives_quit = obs_survives_quit
        self.syphon_binds = syphon_binds
        self.projector_opens = projector_opens
        self.adoptable: dict[int, FakeProcess] = {}
        self.manager = SessionManager(
            pidfile=pidfile or Path(mkdtemp()) / "session.json",
            process_for_pid=self._process_for_pid,
            spawn=self._spawn,
            call=self._call,
            now=lambda: self.clock,
            stop_outputs=self._stop_outputs,
            bind_syphon=self._bind_syphon,
            open_projector=self._open_projector,
        )

    def _stop_outputs(self) -> bool:
        self.called.append(("obsws", "StopOutputs"))
        return self.outputs_stop

    def _bind_syphon(self) -> bool:
        self.called.append(("obsws", "BindSyphon"))
        return self.syphon_binds

    def _open_projector(self) -> bool:
        self.called.append(("obsws", "OpenProjector"))
        return self.projector_opens

    def _process_for_pid(self, pid: int) -> FakeProcess:
        return self.adoptable.get(pid, FakeProcess(pid=pid, log=self.called))

    def _spawn(self, cmd: Sequence[str]) -> FakeProcess:
        self.spawned.append(tuple(cmd))
        proc = FakeProcess(self.called)
        self.procs.append(proc)
        return proc

    def _call(self, cmd: Sequence[str]) -> int:
        self.called.append(tuple(cmd))
        if tuple(cmd) == OBS_ALIVE:  # pgrep: 0 = still running
            return 0 if self.obs_survives_quit else 1
        return 0

    @property
    def run_proc(self) -> FakeProcess:
        return self.procs[0]

    @property
    def caffeinate_proc(self) -> FakeProcess:
        return self.procs[1]


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
    assert h.spawned == [run_command(), CAFFEINATE]
    assert h.manager.owns_session

    # Server not up yet: STARTING, and OBS stays closed — launching it first
    # parks the overlay browser source on a blank error page (hardware.md §B3).
    assert h.manager.tick(server_up=False) is SessionState.STARTING
    assert h.called == []

    assert h.manager.tick(server_up=True) is SessionState.ON
    assert h.called == [OBS_OPEN]

    # OBS is launched exactly once, however long the session runs; the ticks
    # after it point OBS's Syphon source at this session's server.
    assert h.manager.tick(server_up=True) is SessionState.ON
    assert h.called == [OBS_OPEN, ("obsws", "BindSyphon"), ("obsws", "OpenProjector")]


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
    assert h.called == [("obsws", "BindSyphon"), ("obsws", "OpenProjector")]

    for _ in range(5):  # both done: stop asking
        h.manager.tick(server_up=True)
    assert h.called == [("obsws", "BindSyphon"), ("obsws", "OpenProjector")]


def test_a_syphon_rebind_that_never_takes_gives_up() -> None:
    # OBS's WebSocket server needs ~10 s after launch, so failures are normal
    # at first — but they must not retry forever.
    h = Harness(syphon_binds=False)
    h.manager.start()
    h.manager.tick(server_up=True)
    h.called.clear()

    for _ in range(OBS_PREP_ATTEMPTS + 10):
        h.manager.tick(server_up=True)
    # The projector succeeds on the first pass; only the rebind keeps retrying.
    assert h.called.count(("obsws", "BindSyphon")) == OBS_PREP_ATTEMPTS
    assert h.called.count(("obsws", "OpenProjector")) == 1


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
    assert h.manager.tick(server_up=False) is SessionState.OFF
    assert h.run_proc.signals == [signal.SIGINT]
    assert h.caffeinate_proc.terminated
    assert not h.manager.owns_session


def test_stop_quits_obs_then_interrupts_run_and_settles_on_off() -> None:
    h = Harness()
    h.manager.start()
    h.manager.tick(server_up=True)
    h.called.clear()

    h.manager.stop()
    # Order matters, every step measured the hard way (2026-08-02): `run` is
    # signalled first so nothing OBS does can delay the camera shutdown, OBS's
    # outputs are stopped before the quit because any active output raises a
    # modal dialog no one can click at the table, and the quit is verified
    # because that dialog blocks it silently.
    assert h.called == [("signal", "SIGINT"), ("obsws", "StopOutputs"), OBS_QUIT]

    # `run` closes the camera on its way out, so OFF waits for the exit.
    assert h.manager.tick(server_up=True) is SessionState.STOPPING
    assert not h.caffeinate_proc.terminated

    h.run_proc.exit_code = 0
    assert h.manager.tick(server_up=False) is SessionState.OFF
    assert h.caffeinate_proc.terminated
    assert not h.manager.owns_session


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
    # dead child decides, not the probe.
    assert h.manager.tick(server_up=True) is SessionState.OFF
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
    assert h.spawned == [run_command(), CAFFEINATE, run_command(), CAFFEINATE]
    assert h.manager.tick(server_up=False) is SessionState.STARTING


def test_double_press_while_starting_does_not_spawn_a_second_session() -> None:
    h = Harness()
    h.manager.start()
    h.manager.start()
    assert h.spawned == [run_command(), CAFFEINATE]


def test_reference_override_reaches_the_run_command() -> None:
    h = Harness()
    h.manager.reference = "tests/clips/empty_table_9999"
    h.manager.start()
    assert h.spawned[0] == run_command("tests/clips/empty_table_9999")
    assert "--rectify" in h.spawned[0]


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
    ]
    # Only the running state needs one — the rest already say what a tap does.
    assert [s.name for s in SessionState if s.sub is not None] == ["ON"]
