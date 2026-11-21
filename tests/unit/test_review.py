"""#57 review: the moments finder, verdicts, and the review page's file serving."""

import json
import re
import shutil
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from cueclock import archive
from cueclock.review import moments, sessions
from cueclock.review.app import create_app

STAMP = "20261116-120000"
MINUS = "\u2212"  # the page writes times before the moment with a real minus sign
T0 = datetime(2026, 11, 16, 12, 0, 0, tzinfo=UTC)


def rec(s: float, kind: str, **detail: Any) -> dict[str, Any]:
    """A journal record `s` seconds into the session (mono and wall in step)."""
    wall = (T0 + timedelta(seconds=s)).isoformat(timespec="milliseconds")
    return {"wall": wall, "mono": 1000.0 + s, "kind": kind, **detail}


def write_journal(path: Path, records: list[dict[str, Any]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records) + '{"torn')
    return path


def run(s: float, kind: str, state: str = "RUNNING", rack: int = 3, **kw: Any) -> dict[str, Any]:
    return rec(s, kind, state=state, remaining=30.0, rack_shots=rack, **kw)


# A session exercising every kind once, plus a clean shot that must not appear.
SESSION = [
    run(0, "cmd_new_rack", state="IDLE", rack=0),
    run(5, "vision_shot_taken_ignored_idle", state="IDLE", rack=0, t=5.0),
    run(7, "vision_shot_taken_ignored_idle", state="IDLE", rack=0, t=7.0),  # same burst
    # A clean shot: strike, ~4 s roll-out, settle. Not a moment.
    run(20, "vision_shot_taken", state="MOTION", t=20.0),
    run(24, "vision_balls_settled", t=24.0),
    # Phantom candidate: settled 0.4 s after the strike.
    run(40, "vision_shot_taken", state="MOTION", t=40.0),
    run(40.4, "vision_balls_settled", t=40.4),
    # A provisional freeze released as not-a-shot.
    run(60, "shot_provisional", state="MOTION", t=60.0),
    run(62, "shot_provisional_reverted", t=62.0, frame_t=62.0),
    # Manual corrections: restart, and New Rack out of a frozen clock.
    run(80, "cmd_restart"),
    run(100, "vision_shot_taken", state="MOTION", t=100.0),
    run(105, "cmd_new_rack", state="IDLE", rack=0),
    # The watchdog synthesizes a settle.
    run(120, "vision_shot_taken", state="MOTION", t=120.0),
    run(135, "settle_synthesized", quiet_s=12.0),
    # 100 s of heartbeats with the clock running and nothing seen.
    *[run(140 + 5 * i, "detect_heartbeat", hz=156.0) for i in range(21)],
]


def kinds(found: list[moments.Moment]) -> list[str]:
    return [m.kind for m in found]


def test_finds_each_kind_once(tmp_path: Path) -> None:
    found = moments.find(write_journal(tmp_path / "j.jsonl", SESSION))
    assert sorted(kinds(found)) == sorted(
        ["idle_strike", "phantom", "reverted", "correction", "correction", "synthesized", "quiet"]
    )
    assert [m.wall for m in found] == sorted(m.wall for m in found)
    by_kind = {m.kind: m for m in found}
    assert "2 strikes" in by_kind["idle_strike"].summary
    assert "0.40 s" in by_kind["phantom"].summary
    assert by_kind["phantom"].wall == SESSION[6]["wall"]
    quiet = by_kind["quiet"]
    assert quiet.start_wall == SESSION[13]["wall"]  # the last thing seen


def test_the_ball_lists_scratch_calls_are_moments(tmp_path: Path) -> None:
    """#63: a scratch the list called alone and a candidate it cleared are
    shown at the settle the list judged; a scratch both saw is not."""
    calls = [
        run(10, "scratch_list", t=10.0, frame_t=10.0, verdict="scratch"),
        run(30, "scratch_call", by="list", held=True, closed=None),
        run(40, "scratch_list", t=40.0, frame_t=40.0, verdict="cleared"),
        run(60, "scratch_call", by="candidate", held=False, closed="cleared"),
        run(70, "scratch_list", t=70.0, frame_t=70.0, verdict="scratch"),
        run(90, "scratch_call", by="both", held=True, closed=None),
    ]
    found = moments.find(write_journal(tmp_path / "j.jsonl", calls))
    assert kinds(found) == ["list_scratch", "list_cleared"]
    assert [m.wall for m in found] == [calls[0]["wall"], calls[2]["wall"]]
    assert "held 5 s" in found[0].summary


def test_ball_in_hand_placing_and_its_end_are_moments(tmp_path: Path) -> None:
    """#64: every placing kept off the clock is shown once, at its list,
    whether a freeze was reverted or a compensation suppressed; so is the
    counted settle that ended ball in hand (the shot, or a missed placing)."""
    calls = [
        run(10, "shot_provisional_reverted", reason="ball_in_hand"),
        run(10, "scratch_list", frame_t=10.0, row="ball_in_hand", hand="applied"),
        run(20, "compensation_suppressed", reason="ball_in_hand"),
        run(20, "scratch_list", frame_t=20.0, row="ball_in_hand", hand="applied"),
        run(30, "scratch_list", frame_t=30.0, row="ball_in_hand", hand="refused"),
        run(40, "ball_in_hand_ended", reason="shot", placed=2),
        run(50, "scratch_list", frame_t=50.0, row="cue_moved", hand=None, counted=True),
        run(55, "scratch_list", frame_t=55.0, row="still", hand=None, counted=True),
        run(60, "ball_in_hand_ended", reason="new_rack", placed=0),
    ]
    found = moments.find(write_journal(tmp_path / "j.jsonl", calls))
    assert kinds(found) == ["placing", "placing", "in_hand_shot", "cue_only", "no_change"]
    assert "after 2 placing(s)" in found[2].summary


def test_a_hand_move_in_open_play_is_a_moment(tmp_path: Path) -> None:
    """#67: kept off the clock, it is a placing, worded without a scratch;
    counted because its list came late, it is a cue_only to label."""
    calls = [
        run(10, "shot_provisional_reverted", reason="by_hand"),
        run(10, "scratch_list", frame_t=10.0, row="by_hand", hand="applied", counted=False),
        run(30, "scratch_list", frame_t=30.0, row="by_hand", hand="late", counted=True),
    ]
    found = moments.find(write_journal(tmp_path / "j.jsonl", calls))
    assert kinds(found) == ["placing", "cue_only"]
    assert "moved by hand" in found[0].summary and "scratch" not in found[0].summary


def test_a_shot_counted_with_nothing_moved_is_a_moment(tmp_path: Path) -> None:
    """§16: the list read the cue ball on its spot and the settle counted
    anyway (its list came late, or a strike fired). Refused ones show as the
    freeze they released; a still table nobody counted is nothing."""
    calls = [
        run(10, "scratch_list", frame_t=10.0, row="still", counted=True),
        run(20, "shot_provisional_reverted", reason="nothing_moved"),
        run(20, "scratch_list", frame_t=20.0, row="still", counted=False),
        run(30, "scratch_list", frame_t=30.0, row="shot", counted=True),
    ]
    found = moments.find(write_journal(tmp_path / "j.jsonl", calls))
    assert kinds(found) == ["no_change", "reverted"]
    assert "no shot played" in found[0].summary


def test_clean_session_has_no_moments(tmp_path: Path) -> None:
    clean = [
        run(0, "vision_shot_taken", state="MOTION", t=0.0),
        run(4, "vision_balls_settled", t=4.0),
        run(20, "vision_shot_taken", state="MOTION", t=20.0),
        run(25, "vision_balls_settled", t=25.0),
        run(30, "cmd_new_rack", state="IDLE", rack=0),  # end of rack, not a rescue
        *[run(40 + 5 * i, "detect_heartbeat", state="IDLE", rack=0) for i in range(40)],
    ]
    assert moments.find(write_journal(tmp_path / "j.jsonl", clean)) == []


def test_corrections_in_one_burst_are_one_moment(tmp_path: Path) -> None:
    burst = [run(0, "cmd_reset", state="IDLE"), run(3, "cmd_start"), run(40, "cmd_restart")]
    found = moments.find(write_journal(tmp_path / "j.jsonl", burst))
    assert kinds(found) == ["correction", "correction"]
    assert "Reset, then Start" in found[0].summary


def test_ids_are_stable_across_runs(tmp_path: Path) -> None:
    journal = write_journal(tmp_path / "j.jsonl", SESSION)
    first = [m.id for m in moments.find(journal)]
    assert first == [m.id for m in moments.find(journal)]
    assert len(set(first)) == len(first)
    assert "reverted-1062000" in first


def test_replay_diffs_become_moments(tmp_path: Path) -> None:
    journal = write_journal(tmp_path / "j.jsonl", SESSION)
    replay = tmp_path / "replay.json"
    replay.write_text(
        json.dumps(
            {"ok": False, "diffs": [{"mono": 1100.2, "summary": "live froze, replay did not"}]}
        )
    )
    found = [m for m in moments.find(journal, replay) if m.kind == "replay"]
    assert len(found) == 1
    assert found[0].wall == SESSION[10]["wall"]  # nearest record by mono
    assert "live froze" in found[0].summary


def test_write_uses_live_journal_until_archived(tmp_path: Path) -> None:
    logs = tmp_path / "logs"
    write_journal(logs / f"session-{STAMP}.jsonl", SESSION)
    session_dir = tmp_path / "sessions" / STAMP
    found = moments.write(session_dir, logs)
    data = json.loads((session_dir / "moments.json").read_text())
    assert [d["id"] for d in data] == [m.id for m in found]
    assert set(data[0]) == {"id", "wall", "kind", "summary", "start_wall", "end_wall", "kinds"}

    write_journal(session_dir / "journal.jsonl", SESSION[:3])  # the archived copy wins
    assert kinds(moments.write(session_dir, logs)) == ["idle_strike"]

    with pytest.raises(FileNotFoundError):
        moments.write(tmp_path / "sessions" / "20200101-000000", logs)


# --- the review app ---------------------------------------------------------


@pytest.fixture
def roots(tmp_path: Path) -> tuple[Path, Path]:
    local, archive = tmp_path / "local", tmp_path / "t7"
    logs = tmp_path / "logs"
    write_journal(logs / f"session-{STAMP}.jsonl", SESSION)
    moments.write(local / STAMP, logs)
    return local, archive


class FakeFfmpeg:
    """Stands in for ffmpeg under the real `Maintainer.cut_one`: writes the
    output file it was asked for and remembers each command."""

    def __init__(self) -> None:
        self.commands: list[list[str]] = []
        self.cut_one = archive.Maintainer(run=self._run, say=lambda _line: None).cut_one

    def _run(self, argv: Sequence[str], timeout: float) -> tuple[int, str]:
        self.commands.append(list(argv))
        Path(argv[-1]).write_bytes(b"\0" * 4096)
        return 0, ""


def client(
    roots: tuple[Path, Path], tmp_path: Path, ffmpeg: FakeFfmpeg | None = None
) -> TestClient:
    local, t7 = roots
    return TestClient(
        create_app(
            [local, t7],
            local_root=local,
            logs_dir=tmp_path / "logs",
            cutter=(ffmpeg or FakeFfmpeg()).cut_one,
        )
    )


def list_recording(local: Path, video: Path, start_s: float = -30.0) -> None:
    """One line in the local root's `recordings.jsonl`: an hour from `start_s`."""
    video.write_bytes(b"\0" * 4096)
    line = {
        "path": str(video),
        "start_wall": (T0 + timedelta(seconds=start_s)).isoformat(),
        "stop_wall": (T0 + timedelta(hours=1)).isoformat(),
    }
    with (local / "recordings.jsonl").open("a") as fh:
        fh.write(json.dumps(line) + "\n")


def cards(page: str) -> dict[str, str]:
    """Each moment's `<details …>` opening tag on a session page, by id."""
    tags = re.finditer(r'<details class="card[^>]*id="([^"]+)"[^>]*>', page)
    return {m.group(1): m.group(0) for m in tags}


def first_id(session_dir: Path) -> str:
    return str(json.loads((session_dir / "moments.json").read_text())[0]["id"])


def test_discovery_without_the_t7(roots: tuple[Path, Path], tmp_path: Path) -> None:
    local, archive = roots
    assert not archive.exists()
    found = sessions.discover([local, archive])
    assert [s.stamp for s in found] == [STAMP]

    (archive / "20261201-090000").mkdir(parents=True)
    (archive / STAMP).mkdir()  # an archive copy in flight: listed once, from local
    (archive / "not-a-session").mkdir()
    found = sessions.discover([local, archive])
    assert [(s.stamp, s.path.parent) for s in found] == [
        ("20261201-090000", archive),
        (STAMP, local),
    ]

    page = client((local, tmp_path / "unmounted"), tmp_path).get("/")
    assert page.status_code == 200
    assert "7 of 7 to review" in page.text
    assert "Not reachable" in page.text


def test_a_session_can_be_reviewed_before_the_archive_pass_reaches_it(tmp_path: Path) -> None:
    """The archive pass writes `moments.json` after its parity check, minutes
    after a stop. Until then the page finds the moments from the journal
    itself, and a verdict given meanwhile stays on its moment afterwards."""
    local, logs = tmp_path / "local", tmp_path / "logs"
    write_journal(logs / f"session-{STAMP}.jsonl", SESSION)
    (local / STAMP).mkdir(parents=True)  # a session directory, nothing scanned yet
    app = TestClient(create_app([local], local_root=local, logs_dir=logs))

    assert "7 of 7 to review" in app.get("/").text
    page = app.get(f"/s/{STAMP}")
    assert "has not run" not in page.text and "Clock right" in page.text
    mid = moments.find(logs / f"session-{STAMP}.jsonl")[0].id
    assert app.post(f"/api/verdict/{STAMP}/{mid}", json={"verdict": "right"}).status_code == 200
    assert not (local / STAMP / "moments.json").exists()  # reading it wrote nothing

    moments.write(local / STAMP, logs)  # the archive pass arrives
    assert first_id(local / STAMP) == mid
    assert "6 of 7 to review" in app.get("/").text

    (local / "20200101-000000").mkdir()  # no journal anywhere
    assert "no journal to read" in app.get("/s/20200101-000000").text


def test_verdicts_round_trip(roots: tuple[Path, Path], tmp_path: Path) -> None:
    local, _ = roots
    app = client(roots, tmp_path)
    mid = first_id(local / STAMP)
    url = f"/api/verdict/{STAMP}/{mid}"

    reply = app.post(url, json={"verdict": "wrong", "note": "cue never moved"})
    assert reply.status_code == 200
    stored = json.loads((local / STAMP / "verdicts.json").read_text())
    assert stored[mid]["verdict"] == "wrong"
    assert stored[mid]["note"] == "cue never moved"
    assert "6 of 7 to review" in app.get("/").text
    assert "cue never moved" in app.get(f"/s/{STAMP}").text
    assert not list((local / STAMP).glob(".*.tmp"))  # atomic write leaves no temp file

    assert app.post(url, json={"verdict": "maybe"}).status_code == 400
    assert app.post(f"/api/verdict/{STAMP}/phantom-1", json={"verdict": "right"}).status_code == 404

    assert app.post(url, json={"verdict": None}).json()["verdict"] is None
    assert json.loads((local / STAMP / "verdicts.json").read_text()) == {}


@pytest.mark.parametrize(
    "path",
    [
        "/s/..%2F..%2Fetc",
        f"/s/{STAMP}%2F..",
        "/s/20261116-12000",  # not a stamp
        "/s/20990101-000000",  # a stamp, but no such session
        f"/media/{STAMP}/..%2Fmoments.mp4",
        f"/media/{STAMP}/%2E%2E%2F%2E%2E%2Fjournal.mp4",
        "/recording/99",
        "/recording/..%2F..%2Fetc%2Fpasswd",
    ],
)
def test_refuses_paths_outside_the_roots(
    roots: tuple[Path, Path], tmp_path: Path, path: str
) -> None:
    (tmp_path / "secret.mp4").write_bytes(b"secret")
    reply = client(roots, tmp_path).get(path)
    assert reply.status_code in (404, 422)
    assert b"secret" not in reply.content


def test_cut_served_with_range_requests(roots: tuple[Path, Path], tmp_path: Path) -> None:
    local, _ = roots
    mid = first_id(local / STAMP)
    (local / STAMP / "cuts").mkdir()
    (local / STAMP / "cuts" / f"{mid}.mp4").write_bytes(bytes(range(256)) * 4)
    app = client(roots, tmp_path)

    assert f' src="/media/{STAMP}/{mid}.mp4#t=0.001"' in app.get(f"/s/{STAMP}").text
    reply = app.get(f"/media/{STAMP}/{mid}.mp4", headers={"Range": "bytes=100-199"})
    assert reply.status_code == 206
    assert reply.headers["content-range"] == "bytes 100-199/1024"
    assert reply.content == bytes(range(100, 200))


def test_a_moment_opened_before_the_archive_pass_gets_its_own_clip(
    roots: tuple[Path, Path], tmp_path: Path
) -> None:
    """Steven reviews minutes after a session; the archive pass cuts later.
    Until 2026-11-21 a card with no cut played the whole recording from an
    offset, with nothing to say where the moment ended. The page now cuts the
    moment it is asked for, with the archive pass's own cutter."""
    local, _ = roots
    video = tmp_path / "obs.mov"
    list_recording(local, video, start_s=-30.0)
    ffmpeg = FakeFfmpeg()
    app = client(roots, tmp_path, ffmpeg)
    found = json.loads((local / STAMP / "moments.json").read_text())
    mid = next(m["id"] for m in found if m["kind"] == "phantom")

    page = app.get(f"/s/{STAMP}").text
    # A closed moment's video carries its address as data: nothing is fetched,
    # and so nothing is cut, until the moment is opened. The open one has it.
    assert f' data-src="/media/{STAMP}/{mid}.mp4#t=0.001"' in page
    assert len(re.findall(r'<video [^>]* src="/media/', page)) == 1
    assert "/recording/" not in page  # never the whole recording in a card
    assert not (local / STAMP / "cuts").exists()  # rendering the page cut nothing

    assert app.get(f"/media/{STAMP}/{mid}.mp4").status_code == 200
    assert app.get(f"/media/{STAMP}/{mid}.mp4", headers={"Range": "bytes=0-9"}).status_code == 206
    assert (local / STAMP / "cuts" / f"{mid}.mp4").is_file()
    assert len(ffmpeg.commands) == 1  # cut once, then served from disk
    argv = ffmpeg.commands[0]
    # The settle is at 40.4 s; its span starts 12 s before, 30 s into the file.
    assert argv[argv.index("-ss") + 1] == "58.400" and argv[argv.index("-t") + 1] == "20.000"
    assert argv[argv.index("-i") + 1] == str(video)

    assert app.get(f"/media/{STAMP}/phantom-1.mp4").status_code == 404  # not a moment
    assert len(ffmpeg.commands) == 1


def test_a_clip_marks_the_flagged_instant_and_the_journal_lines_in_it(
    roots: tuple[Path, Path], tmp_path: Path
) -> None:
    local, _ = roots
    list_recording(local, tmp_path / "obs.mov")
    page = client(roots, tmp_path).get(f"/s/{STAMP}").text
    phantom = page[page.index('id="phantom-') :].split("</details>")[0]
    assert 'data-len="20.00"' in phantom
    assert '<i class="flag" style="left:60.00%">' in phantom  # 12 s into 20
    assert f"<span>{MINUS}12 s</span>" in phantom and "<span>+8 s</span>" in phantom
    # The strike 0.4 s before the settle: 11.6 s into the clip.
    assert f'<li data-t="11.60"><span class="at">{MINUS}0.4 s</span>' in phantom
    assert 'class="here" data-t="12.00"><span class="at">0.0 s</span>' in phantom
    assert '<span class="ev">Strike seen</span><span class="clk">frozen 30 s</span>' in phantom


def test_a_moment_at_the_start_of_the_recording_keeps_its_mark_in_place(
    roots: tuple[Path, Path], tmp_path: Path
) -> None:
    # The idle strikes at 5-7 s: their span starts 12 s earlier, before the
    # recording did (2 s before the session), so the clip is clipped to it.
    local, _ = roots
    list_recording(local, tmp_path / "obs.mov", start_s=-2.0)
    page = client(roots, tmp_path).get(f"/s/{STAMP}").text
    idle = page[page.index('id="idle_strike-') :].split("</details>")[0]
    assert 'data-len="17.00"' in idle  # from the recording's start to 8 s after the last
    assert '<i class="flag" style="left:41.18%">' in idle  # 7 s into 17
    assert f"<span>{MINUS}7 s</span>" in idle and "<span>+10 s</span>" in idle


def test_no_recording_left_means_no_footage_and_still_a_verdict(
    roots: tuple[Path, Path], tmp_path: Path
) -> None:
    local, _ = roots
    video = tmp_path / "obs.mov"
    list_recording(local, video)
    video.unlink()  # pruned by the footage budget
    ffmpeg = FakeFfmpeg()
    app = client(roots, tmp_path, ffmpeg)
    page = app.get(f"/s/{STAMP}").text
    mid = first_id(local / STAMP)
    assert "No footage to show" in page and "<video" not in page
    assert "data-t=" not in page  # nothing to seek in
    assert app.get(f"/media/{STAMP}/{mid}.mp4").status_code == 404
    assert ffmpeg.commands == []
    assert app.post(f"/api/verdict/{STAMP}/{mid}", json={"verdict": "right"}).status_code == 200

    assert app.get("/recording/0").status_code == 404  # the pruned file is not served


def test_a_verdict_saved_as_the_session_is_archived_lands_in_the_archive(
    roots: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The archive pass deletes the local copy minutes after a session, which
    is when it is being judged. A save that finds the directory gone must not
    bring it back holding one file (the next pass would copy that file over
    the archived verdicts): it goes to the copy on the T7."""
    local, t7 = roots
    shutil.copytree(local / STAMP, t7 / STAMP)
    mid = first_id(local / STAMP)
    set_verdict = sessions.Session.set_verdict

    def archived_under_it(self: sessions.Session, *args: Any) -> None:
        if self.path.parent == local:
            shutil.rmtree(self.path)  # the pass finishes between lookup and write
        set_verdict(self, *args)

    monkeypatch.setattr(sessions.Session, "set_verdict", archived_under_it)
    reply = client(roots, tmp_path).post(f"/api/verdict/{STAMP}/{mid}", json={"verdict": "wrong"})
    assert reply.status_code == 200 and reply.json()["verdict"] == "wrong"
    assert not (local / STAMP).exists()
    assert json.loads((t7 / STAMP / "verdicts.json").read_text())[mid]["verdict"] == "wrong"


def test_session_page_reads_journal_in_plain_language(
    roots: tuple[Path, Path], tmp_path: Path
) -> None:
    page = client(roots, tmp_path).get(f"/s/{STAMP}").text
    assert "Strike seen" in page
    assert "Pressed Restart" in page
    assert "detect_heartbeat" not in page and "Detect heartbeat" not in page


def test_full_session_player_lists_events_and_serves_the_kept_copy(
    roots: tuple[Path, Path], tmp_path: Path
) -> None:
    local, _ = roots
    session_dir = local / STAMP
    (session_dir / "recording").mkdir()
    (session_dir / "recording" / "full.mov").write_bytes(b"\0" * 4096)
    (tmp_path / "outside.mov").write_bytes(b"\0")
    start = (T0 - timedelta(seconds=10)).isoformat()
    stop = (T0 + timedelta(hours=1)).isoformat()
    (session_dir / "recording.json").write_text(
        json.dumps(
            [
                {"file": "recording/full.mov", "start_wall": start, "stop_wall": stop},
                {"file": "../../outside.mov", "start_wall": start, "stop_wall": stop},
            ]
        )
    )
    app = client(roots, tmp_path)
    assert f'href="/s/{STAMP}/full"' in app.get(f"/s/{STAMP}").text
    page = app.get(f"/s/{STAMP}/full").text
    assert f'src="/full/{STAMP}/0"' in page
    assert "Strike seen" in page and re.search(r'<li data-t="[\d.]+"><span class="at">0:30', page)
    # A moment's card links to its place in that recording (10 s before T0).
    assert f'href="/s/{STAMP}/full#v0=' in app.get(f"/s/{STAMP}").text
    reply = app.get(f"/full/{STAMP}/0", headers={"Range": "bytes=0-9"})
    assert reply.status_code == 206 and len(reply.content) == 10
    assert app.get(f"/full/{STAMP}/1").status_code == 404  # resolves outside the session


def test_full_session_falls_back_to_the_listed_recording(
    roots: tuple[Path, Path], tmp_path: Path
) -> None:
    local, _ = roots
    video = tmp_path / "obs.mov"
    video.write_bytes(b"\0" * 64)
    line = {
        "path": str(video),
        "start_wall": T0.isoformat(),
        "stop_wall": (T0 + timedelta(hours=1)).isoformat(),
    }
    (local / "recordings.jsonl").write_text(json.dumps(line) + "\n")
    app = client(roots, tmp_path)
    assert 'src="/recording/0"' in app.get(f"/s/{STAMP}/full").text
    video.unlink()
    assert "No full recording" in app.get(f"/s/{STAMP}/full").text
    assert "/full" not in app.get(f"/s/{STAMP}").text.split("<details")[0]


def test_one_moment_is_open_and_it_is_the_first_still_to_judge(
    roots: tuple[Path, Path], tmp_path: Path
) -> None:
    """Every moment is a row; the page arrives with one of them open. (The
    first page showed all of them at full height: 13,570 px for 18 moments,
    and a save scrolled back to the first unjudged one above.)"""
    local, _ = roots
    ids = [m["id"] for m in json.loads((local / STAMP / "moments.json").read_text())]
    app = client(roots, tmp_path)

    page = app.get(f"/s/{STAMP}").text
    rows = cards(page)
    assert list(rows) == ids  # session order
    assert [mid for mid, tag in rows.items() if tag.endswith(" open>")] == [ids[0]]
    assert f"0 of {len(ids)} reviewed" in page

    app.post(f"/api/verdict/{STAMP}/{ids[0]}", json={"verdict": "wrong", "note": "it was a safety"})
    app.post(f"/api/verdict/{STAMP}/{ids[1]}", json={"verdict": "right"})
    page = app.get(f"/s/{STAMP}").text
    rows = cards(page)
    assert [mid for mid, tag in rows.items() if tag.endswith(" open>")] == [ids[2]]
    assert rows[ids[0]].startswith('<details class="card wrong"')
    assert rows[ids[1]].startswith('<details class="card right"')
    assert '<span class="verdict-text">Clock wrong</span>' in page
    assert '<span class="note-text">it was a safety</span>' in page
    assert f"2 of {len(ids)} reviewed" in page

    for mid in ids[2:]:
        app.post(f"/api/verdict/{STAMP}/{mid}", json={"verdict": "right"})
    assert " open>" not in app.get(f"/s/{STAMP}").text  # all judged: all rows


def test_rows_are_named_in_plain_words(roots: tuple[Path, Path], tmp_path: Path) -> None:
    page = client(roots, tmp_path).get(f"/s/{STAMP}").text
    for name in (
        "Strike with the clock idle",
        "Possible phantom shot",
        "Not counted as a shot",
        "You corrected the clock",
        "Watchdog released the clock",
        "Clock ran with nothing seen",
    ):
        assert f"<b>{name}</b>" in page
