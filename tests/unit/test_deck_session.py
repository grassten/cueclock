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
    OBS_OPEN,
    OBS_QUIT,
    START_TIMEOUT_S,
    STOP_ESCALATE_S,
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

    def __init__(self, replay_stops: bool = True) -> None:
        self.spawned: list[tuple[str, ...]] = []
        self.called: list[tuple[str, ...]] = []
        self.procs: list[FakeProcess] = []
        self.clock = 0.0
        self.replay_stops = replay_stops
        self.manager = SessionManager(
            spawn=self._spawn,
            call=self._call,
            now=lambda: self.clock,
            stop_replay=self._stop_replay,
        )

    def _stop_replay(self) -> bool:
        self.called.append(("obsws", "StopReplayBuffer"))
        return self.replay_stops

    def _spawn(self, cmd: Sequence[str]) -> FakeProcess:
        self.spawned.append(tuple(cmd))
        proc = FakeProcess(self.called)
        self.procs.append(proc)
        return proc

    def _call(self, cmd: Sequence[str]) -> int:
        self.called.append(tuple(cmd))
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

    # OBS is launched exactly once, however long the session runs.
    assert h.manager.tick(server_up=True) is SessionState.ON
    assert h.called == [OBS_OPEN]


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
    # Order matters, both halves measured the hard way (2026-08-02):
    # `run` is signalled first so nothing OBS does can delay the camera
    # shutdown, and the replay buffer is stopped before the quit because OBS
    # with any output active raises a modal dialog no one can click at the
    # table — which blocked the quit for its full 20 s timeout.
    assert h.called == [("signal", "SIGINT"), ("obsws", "StopReplayBuffer"), OBS_QUIT]

    # `run` closes the camera on its way out, so OFF waits for the exit.
    assert h.manager.tick(server_up=True) is SessionState.STOPPING
    assert not h.caffeinate_proc.terminated

    h.run_proc.exit_code = 0
    assert h.manager.tick(server_up=False) is SessionState.OFF
    assert h.caffeinate_proc.terminated
    assert not h.manager.owns_session


def test_stop_still_quits_obs_when_the_replay_buffer_cannot_be_stopped() -> None:
    # obs-websocket disabled or OBS already gone: the quit must still happen —
    # a possible dialog beats leaving the session up.
    h = Harness(replay_stops=False)
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
