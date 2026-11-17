"""`cueclock session maintain` (#57) — every step against a temp tree, with
the subprocesses (hooks, ffmpeg) faked so no OBS, camera or ffmpeg is needed."""

from __future__ import annotations

import json
import os
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pytest

from cueclock import archive, cli
from cueclock.archive import Footage, Maintainer, Recording, plan_cut, prune_plan
from cueclock.capture.session_log import SessionLog, SessionLogWriter, StillFrame

T0 = datetime(2026, 11, 16, 22, 0, tzinfo=UTC)
STAMP = "20261116-220000"


def _iso(seconds: float) -> str:
    return (T0 + timedelta(seconds=seconds)).isoformat(timespec="milliseconds")


def _rec(path: str, start: float, stop: float) -> Recording:
    return Recording(Path(path), T0 + timedelta(seconds=start), T0 + timedelta(seconds=stop))


# --- moment cuts --------------------------------------------------------------


def test_a_moment_is_cut_with_ten_seconds_either_side() -> None:
    cut = plan_cut({"wall": _iso(100)}, [_rec("/m/a.mov", 0, 1800)])
    assert cut == archive.Cut(Path("/m/a.mov"), 90.0, 20.0)


def test_a_moment_span_is_padded_from_its_own_start_and_end() -> None:
    moment = {"wall": _iso(100), "start_wall": _iso(95), "end_wall": _iso(130)}
    cut = plan_cut(moment, [_rec("/m/a.mov", 0, 1800)])
    assert cut is not None
    assert (cut.offset_s, cut.duration_s) == (85.0, 55.0)


def test_a_moment_at_the_edge_of_a_recording_is_clipped_not_dropped() -> None:
    cut = plan_cut({"wall": _iso(3)}, [_rec("/m/a.mov", 0, 1800)])
    assert cut is not None
    assert (cut.offset_s, cut.duration_s) == (0.0, 13.0)


def test_the_recording_covering_most_of_the_moment_wins() -> None:
    recs = [_rec("/m/early.mov", 0, 102), _rec("/m/late.mov", 102, 1800)]
    cut = plan_cut({"wall": _iso(105)}, recs)  # 95-115: 7 s early, 13 s late
    assert cut is not None and cut.source == Path("/m/late.mov")
    assert cut.offset_s == 0.0


def test_a_moment_outside_every_recording_has_no_cut() -> None:
    assert plan_cut({"wall": _iso(5000)}, [_rec("/m/a.mov", 0, 1800)]) is None
    assert plan_cut({"wall": _iso(5)}, []) is None


def test_naive_wall_times_are_refused() -> None:
    with pytest.raises(ValueError):
        archive.parse_wall("2026-11-16T22:00:00")


def test_the_cut_is_a_stream_copy() -> None:
    argv = archive.cut_command(archive.Cut(Path("/m/a.mov"), 90.0, 20.0), Path("/o.mp4"))
    assert argv[0] == archive.FFMPEG
    assert argv[argv.index("-ss") + 1] == "90.000"
    assert argv[argv.index("-ss") + 2 :][:2] == ["-i", "/m/a.mov"]  # input seek
    assert argv[argv.index("-c") + 1] == "copy"


def test_recordings_log_skips_bad_lines(tmp_path: Path) -> None:
    log = tmp_path / "recordings.jsonl"
    good = {"path": "/m/a.mov", "start_wall": _iso(0), "stop_wall": _iso(60)}
    log.write_text(json.dumps(good) + "\nnot json\n" + json.dumps({"path": "x"}) + "\n")
    assert archive.load_recordings(log) == [_rec("/m/a.mov", 0, 60)]
    assert archive.load_recordings(tmp_path / "absent.jsonl") == []


# --- the byte budget --------------------------------------------------------


def _f(name: str, gb: float, mtime: float) -> Footage:
    return Footage(Path(name), int(gb * 1e9), mtime)


def test_prune_deletes_oldest_first_until_under_budget() -> None:
    files = [_f("c", 4, 3), _f("a", 4, 1), _f("b", 4, 2)]
    assert [f.path.name for f in prune_plan(files, int(10e9))] == ["a"]
    assert [f.path.name for f in prune_plan(files, int(4e9))] == ["a", "b"]
    assert prune_plan(files, int(12e9)) == []


def test_one_recording_over_the_whole_budget_goes_too() -> None:
    assert [f.path.name for f in prune_plan([_f("big", 11, 1)], int(10e9))] == ["big"]


def test_footage_is_only_listed_recordings_and_replay_key_saves(tmp_path: Path) -> None:
    movies = tmp_path / "Movies"
    movies.mkdir()
    for name in (
        "2026-11-16 22-00-00.mov",  # the session recording (listed below)
        "2025-12-02 15-00-33.mov",  # an old personal recording: never touched
        "Replay 2026-11-14 20-35-46.mov",  # the Replay key's save
        "Replay notes.txt",
        "Screenshot 2026-09-01 14-13-55.png",
    ):
        (movies / name).write_bytes(b"x")
    recs = [Recording(movies / "2026-11-16 22-00-00.mov", T0, T0)]
    recs.append(Recording(movies / "already-pruned.mov", T0, T0))
    names = sorted(f.path.name for f in archive.footage(recs, movies))
    assert names == ["2026-11-16 22-00-00.mov", "Replay 2026-11-14 20-35-46.mov"]


# --- a whole pass -----------------------------------------------------------


class Tree:
    """A repo-shaped temp tree plus a fake subprocess runner."""

    def __init__(self, tmp: Path, hooks: bool = True, archive_mounted: bool = True) -> None:
        self.root = tmp / "sessions.nosync"
        self.logs = tmp / "logs"
        self.movies = tmp / "Movies"
        self.t7 = tmp / "T7" / "cueclock_sessions"
        for d in (self.root, self.logs, self.movies):
            d.mkdir(parents=True)
        self.session = self.root / STAMP
        (self.session / "detections").mkdir(parents=True)
        (self.session / "detections" / "000000.npz").write_bytes(b"d" * 100)
        (self.session / "header.json").write_text("{}")
        (self.logs / f"session-{STAMP}.jsonl").write_text('{"kind": "x"}\n')
        self.recording = self.movies / "2026-11-16 22-00-00.mov"
        self.recording.write_bytes(b"v" * 1000)
        line = {"path": str(self.recording), "start_wall": _iso(0), "stop_wall": _iso(1800)}
        (self.root / "recordings.jsonl").write_text(json.dumps(line) + "\n")
        self.hooks = hooks
        self.calls: list[list[str]] = []
        self.lines: list[str] = []
        self.maintainer = Maintainer(
            root=self.root,
            archive_root=self.t7 if archive_mounted else None,
            logs=self.logs,
            movies=self.movies,
            budget_bytes=10_000,
            run=self._run,
            say=self.lines.append,
        )

    def _run(self, argv: Sequence[str], timeout: float) -> tuple[int, str]:
        argv = list(argv)
        self.calls.append(argv)
        if argv[0] == archive.FFMPEG:
            Path(argv[-1]).write_bytes(b"cut")
            return 0, ""
        if not self.hooks:
            return 2, "cueclock: error: argument command: invalid choice: 'session'"
        target = Path(argv[-1])
        if argv[3:5] == ["session", "verify"]:
            (target / "replay.json").write_text('{"ok": true}')
        else:
            moments = [{"id": "m1", "wall": _iso(100)}, {"id": "../evil", "wall": _iso(100)}]
            (target / "moments.json").write_text(json.dumps(moments))
        return 0, ""


def test_a_pass_verifies_finds_cuts_and_moves_the_session(tmp_path: Path) -> None:
    t = Tree(tmp_path)
    assert t.maintainer.pass_() == 0

    assert not t.session.exists()
    dest = t.t7 / STAMP
    assert (dest / "journal.jsonl").read_text() == '{"kind": "x"}\n'
    assert (dest / "replay.json").is_file() and (dest / "moments.json").is_file()
    assert (dest / "cuts" / "m1.mp4").read_bytes() == b"cut"
    assert (dest / "detections" / "000000.npz").stat().st_size == 100
    assert not list((dest / "cuts").glob(".*"))  # no half-written leftovers
    assert len(list((dest / "cuts").iterdir())) == 1  # the unsafe id was skipped
    assert (t.logs / f"session-{STAMP}.jsonl").is_file()  # the journal stays put
    assert t.recording.is_file()  # 1000 bytes: inside the 10 kB budget


def test_a_second_pass_does_nothing(tmp_path: Path) -> None:
    t = Tree(tmp_path)
    t.maintainer.pass_()
    t.calls.clear()
    t.lines.clear()
    assert t.maintainer.pass_() == 0
    assert t.calls == []
    assert t.lines == ["footage: 0.00 GB of 0 GB budget (1 files)"]


def test_without_the_drive_the_session_is_finished_but_kept_local(tmp_path: Path) -> None:
    t = Tree(tmp_path, archive_mounted=False)
    assert t.maintainer.pass_() == 0
    assert (t.session / "cuts" / "m1.mp4").is_file()
    t.calls.clear()
    t.maintainer.pass_()  # and nothing is redone while it waits for the drive
    assert t.calls == []


def test_missing_hooks_are_skipped_and_the_session_waits_for_them(tmp_path: Path) -> None:
    t = Tree(tmp_path, hooks=False)
    assert t.maintainer.pass_() == 0
    assert t.session.is_dir()  # not archived without its parity check + moments
    assert (t.session / "journal.jsonl").is_file()
    assert any("doesn't exist here" in line for line in t.lines)

    t.hooks = True  # the lead wires them in: the next pass finishes the job
    t.maintainer.pass_()
    assert (t.t7 / STAMP / "cuts" / "m1.mp4").is_file()


def test_a_failing_hook_keeps_the_session_local_and_fails_the_pass(tmp_path: Path) -> None:
    t = Tree(tmp_path)
    t.maintainer.run = lambda argv, timeout: (1, "Traceback\nRuntimeError: boom")
    assert t.maintainer.pass_() == 1
    assert t.session.is_dir()
    assert any("RuntimeError: boom" in line for line in t.lines)


def test_a_diverged_verify_still_counts_as_done(tmp_path: Path) -> None:
    """`session verify` exits 1 on a divergence after writing replay.json — a
    finding for the review page, not a reason to hold the session back."""
    t = Tree(tmp_path)
    real = t.maintainer.run

    def diverging(argv: list[str], timeout: float) -> tuple[int, str]:
        code, err = real(argv, timeout)
        return (1, "diverged") if "verify" in argv else (code, err)

    t.maintainer.run = diverging
    assert t.maintainer.pass_() == 0
    assert (t.t7 / STAMP / "replay.json").is_file()


def test_nothing_to_work_from_does_not_block_the_archive(tmp_path: Path) -> None:
    """Exit 2 without writing (a `run` that died before logging anything)."""
    t = Tree(tmp_path)
    t.maintainer.run = lambda argv, timeout: (2, "no detection log in x")
    assert t.maintainer.pass_() == 0
    assert (t.t7 / STAMP).is_dir()


def test_dry_run_changes_nothing(tmp_path: Path) -> None:
    t = Tree(tmp_path)
    t.maintainer.dry_run = True
    t.maintainer.budget_bytes = 0
    before = sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*"))
    assert t.maintainer.pass_() == 0
    assert sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*")) == before
    assert t.calls == []
    assert any(line.startswith("footage: would delete") for line in t.lines)


def test_the_budget_prunes_after_the_cuts_are_taken(tmp_path: Path) -> None:
    t = Tree(tmp_path)
    t.maintainer.budget_bytes = 500
    assert t.maintainer.pass_() == 0
    assert (t.t7 / STAMP / "cuts" / "m1.mp4").is_file()
    assert not t.recording.exists()


def test_a_partial_earlier_copy_is_completed_not_duplicated(tmp_path: Path) -> None:
    t = Tree(tmp_path)
    partial = t.t7 / STAMP / "header.json"
    partial.parent.mkdir(parents=True)
    partial.write_text("{}")
    assert t.maintainer.pass_() == 0
    assert (t.t7 / STAMP / "detections" / "000000.npz").is_file()


def test_non_session_entries_in_the_root_are_ignored(tmp_path: Path) -> None:
    t = Tree(tmp_path)
    (t.root / "scratch").mkdir()
    assert [p.name for p in t.maintainer.sessions()] == [STAMP]


# --- the live-session refusal ----------------------------------------------


def test_maintain_refuses_while_a_session_is_live(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(archive, "live_session", lambda server: "a cueclock server answers")
    built: list[Maintainer] = []
    monkeypatch.setattr(archive, "Maintainer", lambda **kw: built.append(kw))  # type: ignore[arg-type,return-value]
    assert cli.main(["session", "maintain", "--root", str(tmp_path)]) == 0
    assert built == []
    assert "nothing done — a cueclock server answers" in capsys.readouterr().out


def test_a_live_deck_session_is_detected_from_its_pidfile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cueclock.deck import session

    pidfile = tmp_path / "session.json"
    pidfile.write_text(json.dumps({"run": os.getpid()}))  # this process: alive
    monkeypatch.setattr(session, "PIDFILE", pidfile)
    assert archive.live_session("http://127.0.0.1:1") == (
        f"the deck's session is up (run pid {os.getpid()})"
    )


def test_an_unmounted_volume_is_not_an_archive() -> None:
    assert not archive.archive_available(Path("/Volumes/no-such-cueclock-drive/x"))


def test_the_full_recording_travels_to_the_t7_with_its_session(tmp_path: Path) -> None:
    t = Tree(tmp_path)
    (t.logs / f"session-{STAMP}.jsonl").write_text(
        "".join(json.dumps({"wall": _iso(s), "kind": "x"}) + "\n" for s in (5, 240))
    )
    assert t.maintainer.pass_() == 0
    dest = t.t7 / STAMP
    kept = json.loads((dest / archive.FULL_INDEX).read_text())
    assert kept == [
        {"file": f"recording/{t.recording.name}", "start_wall": _iso(0), "stop_wall": _iso(1800)}
    ]
    assert (dest / kept[0]["file"]).read_bytes() == t.recording.read_bytes()
    assert t.recording.is_file()  # the original stays under the footage budget


def test_a_session_waiting_for_the_drive_holds_no_link(tmp_path: Path) -> None:
    """A link in a session stuck on the SSD would keep pruned footage alive."""
    t = Tree(tmp_path, archive_mounted=False)
    t.maintainer.pass_()
    assert not (t.session / archive.FULL_INDEX).exists()
    assert os.stat(t.recording).st_nlink == 1


def test_only_recordings_overlapping_the_session_are_kept() -> None:
    recs = [_rec("/m/before.mov", -900, -60), _rec("/m/this.mov", 0, 600)]
    span = (T0, T0 + timedelta(seconds=300))
    assert [r.path.name for r in archive.session_recordings(span, recs)] == ["this.mov"]


# --- retention -----------------------------------------------------------------


def _archived(t7: Path, stamp: str, *, verdicts: dict[str, object] | None = None) -> Path:
    """An archived session: 4 still sets (1 inside moment m1's window), footage, a cut."""
    d = t7 / stamp
    (d / "detections").mkdir(parents=True)
    (d / "detections" / "000000.npz").write_bytes(b"d" * 100)
    (d / "stills").mkdir()
    lines = []
    for n, at in enumerate((10, 95, 100, 300)):
        names = [f"{n:09d}.png", "shared.png" if at in (95, 300) else f"{n:09d}b.png"]
        for name in names:
            (d / "stills" / name).write_bytes(b"s" * 1000)
        lines.append(json.dumps({"wall": _iso(at), "files": names}))
    (d / "stills" / "index.jsonl").write_text("\n".join(lines) + "\n")
    moments = [{"id": "m1", "wall": _iso(100), "start_wall": _iso(90), "end_wall": _iso(110)}]
    (d / "moments.json").write_text(json.dumps(moments))
    (d / archive.FULL_DIR).mkdir()
    (d / archive.FULL_DIR / "full.mov").write_bytes(b"v" * 5000)
    (d / archive.FULL_INDEX).write_text("[]")
    (d / "cuts").mkdir()
    (d / "cuts" / "m1.mp4").write_bytes(b"c")
    if verdicts is not None:
        (d / archive.VERDICTS_FILE).write_text(json.dumps(verdicts))
    return d


STAMPS = ["20261101-120000", "20261102-120000", "20261103-120000"]


def test_a_judged_cut_is_dropped_and_unjudged_ones_stay(tmp_path: Path) -> None:
    t = Tree(tmp_path)
    judged = _archived(t.t7, STAMPS[0], verdicts={"m1": {"verdict": "right"}})
    open_ = _archived(t.t7, STAMPS[1])
    t.maintainer.drop_judged_cuts()
    assert not (judged / "cuts" / "m1.mp4").exists()
    assert (open_ / "cuts" / "m1.mp4").is_file()


def test_only_the_newest_sessions_keep_any_footage(tmp_path: Path) -> None:
    t = Tree(tmp_path)
    t.maintainer.keep_full = 2
    dirs = [_archived(t.t7, s) for s in STAMPS]
    t.maintainer.expire_footage()
    assert not any((dirs[0] / p).exists() for p in (archive.FULL_DIR, archive.FULL_INDEX, "cuts"))
    assert (dirs[0] / "detections" / "000000.npz").is_file()  # the log stays
    assert all((d / archive.FULL_INDEX).is_file() and (d / "cuts").is_dir() for d in dirs[1:])


def test_over_budget_the_oldest_session_goes_and_a_judged_one_is_pinned(tmp_path: Path) -> None:
    t = Tree(tmp_path)
    judged = _archived(t.t7, STAMPS[0], verdicts={"m1": {"verdict": "wrong"}})
    plain = _archived(t.t7, STAMPS[1])
    newest = _archived(t.t7, STAMPS[2])
    t.maintainer.archive_bytes = archive._size(newest)  # room for exactly one
    t.maintainer.fit_archive()

    assert not plain.exists()
    assert newest.exists() and not (newest / archive.PINNED_MARK).exists()
    assert (judged / archive.PINNED_MARK).is_file()
    assert (judged / "detections" / "000000.npz").is_file()
    stills = sorted(p.name for p in (judged / "stills").glob("*.png"))
    # Kept: the two sets inside m1's 90-110 s window, incl. the PNG shared with
    # the 300 s set; gone: the 10 s and 300 s sets' own frames.
    assert stills == ["000000001.png", "000000002.png", "000000002b.png", "shared.png"]
    assert len((judged / "stills" / "index.jsonl").read_text().splitlines()) == 2
    assert not (judged / archive.FULL_DIR).exists() and not (judged / "cuts").exists()


def test_pinned_sessions_have_their_own_budget(tmp_path: Path) -> None:
    t = Tree(tmp_path)
    for stamp in STAMPS[:2]:
        d = _archived(t.t7, stamp, verdicts={"m1": {"verdict": "right"}})
        t.maintainer.pin(d)
    t.maintainer.pinned_bytes = archive._size(t.t7 / STAMPS[1])
    t.maintainer.fit_archive()
    assert not (t.t7 / STAMPS[0]).exists()
    assert (t.t7 / STAMPS[1] / archive.PINNED_MARK).is_file()


def _with_rest_stills(
    t7: Path, stamp: str, at: Sequence[float], *, judged: bool = False
) -> tuple[Path, int]:
    """A session as `run` writes it, with a ball list's `rest` still at each
    of ``at`` seconds (#65); moment m1 spans 90-110 s. Returns (dir, bytes of
    one rest still)."""
    d = t7 / stamp
    writer = SessionLogWriter(d, {"stamp": stamp})
    rng = np.random.default_rng(int(stamp[-6:]))
    for n, s in enumerate(at):
        frame = StillFrame(1000 + n, s, rng.integers(0, 255, (64, 64), dtype=np.uint8))
        writer.still("rest", mono=s, wall=_iso(s), t=s, frame_i=frame.frame_i, frames=[frame])
    writer.close()
    moments = [{"id": "m1", "wall": _iso(100), "start_wall": _iso(90), "end_wall": _iso(110)}]
    (d / "moments.json").write_text(json.dumps(moments))
    if judged:
        (d / archive.VERDICTS_FILE).write_text(json.dumps({"m1": {"verdict": "right"}}))
    return d, (d / "stills" / "000001000.png").stat().st_size


def test_rest_stills_are_in_the_session_budget_oldest_session_first(tmp_path: Path) -> None:
    t = Tree(tmp_path)
    old, _ = _with_rest_stills(t.t7, STAMPS[0], (10, 20))
    new, one = _with_rest_stills(t.t7, STAMPS[1], (10, 20, 30))
    # Over by less than one rest still: the stills are what break the budget.
    t.maintainer.archive_bytes = archive._size(old) + archive._size(new) - one // 2
    t.maintainer.fit_archive()
    assert not old.exists()
    assert [e["reason"] for e in SessionLog(new).stills()] == ["rest"] * 3


def test_pinning_keeps_rest_stills_only_inside_judged_moments(tmp_path: Path) -> None:
    t = Tree(tmp_path)
    d, _ = _with_rest_stills(t.t7, STAMPS[0], (10, 95, 105, 300), judged=True)
    t.maintainer.pin(d)
    kept = SessionLog(d).stills()
    assert [(e["reason"], e["t"]) for e in kept] == [("rest", 95), ("rest", 105)]
    assert sorted(p.name for p in (d / "stills").glob("*.png")) == [
        "000001001.png",
        "000001002.png",
    ]


def test_rest_stills_kept_by_a_pin_are_in_the_pinned_budget(tmp_path: Path) -> None:
    t = Tree(tmp_path)
    old, _ = _with_rest_stills(t.t7, STAMPS[0], (95,), judged=True)
    new, one = _with_rest_stills(t.t7, STAMPS[1], (95, 105), judged=True)
    for d in (old, new):
        t.maintainer.pin(d)
    t.maintainer.pinned_bytes = archive._size(old) + archive._size(new) - one // 2
    t.maintainer.fit_archive()
    assert not old.exists()
    assert len(SessionLog(new).stills()) == 2


def test_journals_are_held_to_their_budget_oldest_first(tmp_path: Path) -> None:
    t = Tree(tmp_path)
    for stamp in STAMPS:
        (t.logs / f"session-{stamp}.jsonl").write_bytes(b"j" * 100)
    (t.logs / "bracket-old.json").write_bytes(b"b" * 1000)  # not a journal: untouched
    t.maintainer.journals_bytes = 250
    t.maintainer.fit_journals()
    left = sorted(p.name for p in t.logs.iterdir())
    assert left == [
        "bracket-old.json",
        f"session-{STAMPS[1]}.jsonl",
        f"session-{STAMPS[2]}.jsonl",
        f"session-{STAMP}.jsonl",
    ]


def test_retention_dry_run_changes_nothing(tmp_path: Path) -> None:
    t = Tree(tmp_path)
    _archived(t.t7, STAMPS[0], verdicts={"m1": {"verdict": "right"}})
    _archived(t.t7, STAMPS[1])
    t.maintainer.dry_run = True
    t.maintainer.keep_full = 0
    t.maintainer.archive_bytes = t.maintainer.journals_bytes = 0
    before = sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*"))
    assert t.maintainer.retain()
    assert sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*")) == before
    assert any("would pin" in line for line in t.lines)
