"""Session-key state machine (deck phase 2) — the whole start/stop sequence
without a camera, OBS, or a Stream Deck.

`SessionManager` takes its process work as injected callables precisely so this
file can drive it: `FakeProcess` stands in for `run`/`caffeinate`, and a list
records the fire-and-forget commands (OBS open/quit) in order.
"""

from __future__ import annotations

import signal
from collections.abc import Sequence

from cueclock.deck.session import (
    CAFFEINATE,
    OBS_ALIVE,
    OBS_DISMISS,
    OBS_OPEN,
    OBS_QUIT,
    OBS_QUIT_GRACE_S,
    START_TIMEOUT_S,
    STOP_ESCALATE_S,
    SYPHON_BIND_ATTEMPTS,
    SessionManager,
    SessionState,
    run_command,
)


class FakeProcess:
    """A child that exits only when the test says so."""

    def __init__(self, log: list[tuple[str, ...]] | None = None) -> None:
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
    ) -> None:
        self.spawned: list[tuple[str, ...]] = []
        self.called: list[tuple[str, ...]] = []
        self.procs: list[FakeProcess] = []
        self.clock = 0.0
        self.outputs_stop = outputs_stop
        self.obs_survives_quit = obs_survives_quit
        self.syphon_binds = syphon_binds
        self.manager = SessionManager(
            spawn=self._spawn,
            call=self._call,
            now=lambda: self.clock,
            stop_outputs=self._stop_outputs,
            bind_syphon=self._bind_syphon,
        )

    def _stop_outputs(self) -> bool:
        self.called.append(("obsws", "StopOutputs"))
        return self.outputs_stop

    def _bind_syphon(self) -> bool:
        self.called.append(("obsws", "BindSyphon"))
        return self.syphon_binds

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
    assert h.called == [OBS_OPEN, ("obsws", "BindSyphon")]


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
    assert h.called == [("obsws", "BindSyphon")]

    for _ in range(5):  # bound: stop asking
        h.manager.tick(server_up=True)
    assert h.called == [("obsws", "BindSyphon")]


def test_a_syphon_rebind_that_never_takes_gives_up() -> None:
    # OBS's WebSocket server needs ~10 s after launch, so failures are normal
    # at first — but they must not retry forever.
    h = Harness(syphon_binds=False)
    h.manager.start()
    h.manager.tick(server_up=True)
    h.called.clear()

    for _ in range(SYPHON_BIND_ATTEMPTS + 10):
        h.manager.tick(server_up=True)
    assert h.called == [("obsws", "BindSyphon")] * SYPHON_BIND_ATTEMPTS


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


def test_state_faces_are_the_documented_words() -> None:
    # hardware.md §C2's button map quotes these — keep the two in step.
    assert [s.face for s in SessionState] == ["OFF", "STARTING…", "ON", "STOPPING…", "DEV"]
