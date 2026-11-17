"""#57 review: the moments finder, verdicts, and the review page's file serving."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from cueclock.review import moments, sessions
from cueclock.review.app import create_app

STAMP = "20261116-120000"
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
    assert set(data[0]) == {"id", "wall", "kind", "summary", "start_wall", "end_wall"}

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


def client(roots: tuple[Path, Path], tmp_path: Path) -> TestClient:
    local, archive = roots
    return TestClient(create_app([local, archive], local_root=local, logs_dir=tmp_path / "logs"))


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

    assert f'src="/media/{STAMP}/{mid}.mp4"' in app.get(f"/s/{STAMP}").text
    reply = app.get(f"/media/{STAMP}/{mid}.mp4", headers={"Range": "bytes=100-199"})
    assert reply.status_code == 206
    assert reply.headers["content-range"] == "bytes 100-199/1024"
    assert reply.content == bytes(range(100, 200))


def test_falls_back_to_recording_then_pruned(roots: tuple[Path, Path], tmp_path: Path) -> None:
    local, _ = roots
    video = tmp_path / "obs.mp4"
    video.write_bytes(b"\0" * 4096)
    stop = (T0 + timedelta(hours=1)).isoformat()
    (local / "recordings.jsonl").write_text(
        json.dumps(
            {"path": str(tmp_path / "gone.mp4"), "start_wall": T0.isoformat(), "stop_wall": stop}
        )
        + "\n"
        + json.dumps(
            {
                "path": str(video),
                "start_wall": (T0 - timedelta(seconds=30)).isoformat(),
                "stop_wall": stop,
            }
        )
        + "\n"
    )
    app = client(roots, tmp_path)
    page = app.get(f"/s/{STAMP}").text
    first = json.loads((local / STAMP / "moments.json").read_text())[0]
    offset = (
        datetime.fromisoformat(first["start_wall"]) - (T0 - timedelta(seconds=30))
    ).total_seconds()
    assert f'src="/recording/1#t={offset:.1f}"' in page  # line 0's file is gone
    reply = app.get("/recording/1", headers={"Range": "bytes=0-9"})
    assert reply.status_code == 206 and len(reply.content) == 10
    assert app.get("/recording/0").status_code == 404

    video.unlink()
    assert "Footage pruned" in app.get(f"/s/{STAMP}").text


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
    assert "Strike seen" in page and "data-seek=" in page
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
    assert "/full" not in app.get(f"/s/{STAMP}").text.split("<section")[0]


def test_a_judged_moment_renders_collapsed_with_progress(
    roots: tuple[Path, Path], tmp_path: Path
) -> None:
    local, _ = roots
    mid = first_id(local / STAMP)
    total = len(json.loads((local / STAMP / "moments.json").read_text()))
    app = client(roots, tmp_path)
    assert f"0 of {total} reviewed" in app.get(f"/s/{STAMP}").text
    app.post(f"/api/verdict/{STAMP}/{mid}", json={"verdict": "wrong", "note": "it was a safety"})
    page = app.get(f"/s/{STAMP}").text
    assert f"1 of {total} reviewed" in page
    assert f'<section class="card done" id="{mid}"' in page
    assert "✗ Clock wrong" in page and "— it was a safety" in page
