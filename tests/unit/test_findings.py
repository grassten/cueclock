"""Review done (#70 step 3): the press, the state the page shows, and the
headless session-review run `session maintain` starts, with claude faked."""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from cueclock import archive
from cueclock.review import findings, moments, sessions
from cueclock.review.app import create_app

STAMP = "20261122-180000"
T0 = datetime(2026, 11, 22, 19, 0, tzinfo=UTC)


def _session(root: Path, verdicts: dict[str, str] | None = None) -> Path:
    path = root / STAMP
    path.mkdir(parents=True)
    given = {
        mid: {"verdict": call, "note": "", "wall": T0.isoformat()}
        for mid, call in (verdicts or {}).items()
    }
    (path / "verdicts.json").write_text(json.dumps(given))
    return path


def _fingerprint(path: Path) -> str:
    return sessions.Session(path.name, path).fingerprint()


class FakeClaude:
    """Stands in for `findings.launch`: records the argv, writes the result."""

    def __init__(self, result: dict[str, Any] | None = None, stopped: str = "") -> None:
        self.argv: list[list[str]] = []
        self.result = result
        self.stopped = stopped

    def __call__(
        self, argv: Sequence[str], folder: Path, live: Callable[[], str | None]
    ) -> tuple[int | None, str]:
        self.argv.append(list(argv))
        if self.stopped:
            return None, self.stopped
        (folder / findings.OUTPUT_FILE).write_text(json.dumps(self.result) + "\n")
        return (1 if (self.result or {}).get("is_error") else 0), ""


def _filed(number: int = 81) -> dict[str, Any]:
    return {
        "type": "result",
        "is_error": False,
        "total_cost_usd": 4.2,
        "num_turns": 60,
        "result": "{}",
        "structured_output": {
            "outcome": "filed",
            "issue": number,
            "summary": "2 changes from 3 wrong verdicts",
        },
    }


def _review(path: Path, claude: FakeClaude, live: str | None = None) -> list[str]:
    said: list[str] = []
    findings.review(
        path,
        lambda: live,
        said.append,
        run=claude,
        now=lambda: T0,
        url_of=lambda n: f"https://github.com/o/r/issues/{n}",
        repo=Path("/repo"),
    )
    return said


# --- the press and what the page says -----------------------------------------


def test_nothing_runs_without_a_press(tmp_path: Path) -> None:
    path = _session(tmp_path, {"phantom-1": "wrong"})
    assert findings.pending(path) is None
    assert findings.view(path, _fingerprint(path)).text == findings.IDLE_TEXT
    claude = FakeClaude(_filed())
    _review(path, claude)
    assert claude.argv == []


def test_a_press_queues_one_run_and_the_page_says_so(tmp_path: Path) -> None:
    path = _session(tmp_path, {"phantom-1": "wrong"})
    assert findings.press(path, _fingerprint(path), T0)
    request = json.loads((path / "findings" / "request.json").read_text())
    assert request == {"wall": T0.isoformat(timespec="seconds"), "fingerprint": _fingerprint(path)}
    line = findings.view(path, _fingerprint(path))
    assert line.busy and line.text.startswith("Queued")
    assert findings.pending(path) == request


def test_a_run_files_and_the_page_links_the_issue(tmp_path: Path) -> None:
    path = _session(tmp_path, {"phantom-1": "wrong", "reverted-2": "right"})
    findings.press(path, _fingerprint(path), T0)
    claude = FakeClaude(_filed(81))
    said = _review(path, claude)

    assert said[-1] == f"{STAMP}: session review — filed #81"
    status = findings.read_status(path)
    assert status is not None
    assert status["state"] == "filed" and status["issue"] == 81
    assert status["url"] == "https://github.com/o/r/issues/81"
    assert status["fingerprint"] == _fingerprint(path)
    assert status["cost_usd"] == 4.2
    line = findings.view(path, _fingerprint(path))
    assert (line.text, line.href, line.busy) == (
        "Filed #81",
        "https://github.com/o/r/issues/81",
        False,
    )
    assert findings.pending(path) is None  # answered: the next pass does nothing
    _review(path, claude)
    assert len(claude.argv) == 1

    # The same verdicts again: nothing to queue, the page keeps the link.
    assert not findings.press(path, _fingerprint(path), T0 + timedelta(minutes=5))
    assert findings.pending(path) is None

    # A verdict changed: the page says so, and a press queues a new run.
    (path / "verdicts.json").write_text(
        json.dumps({"phantom-1": {"verdict": "wrong", "note": "my arm"}})
    )
    assert "Verdicts changed since." in findings.view(path, _fingerprint(path)).note
    assert findings.press(path, _fingerprint(path), T0 + timedelta(minutes=6))
    assert findings.pending(path) is not None


def test_no_wrong_verdict_files_nothing_without_a_run(tmp_path: Path) -> None:
    path = _session(tmp_path, {"phantom-1": "right"})
    findings.press(path, _fingerprint(path), T0)
    claude = FakeClaude(_filed())
    _review(path, claude)
    assert claude.argv == []
    status = findings.read_status(path)
    assert status is not None and status["state"] == "nothing to file"
    assert findings.view(path, _fingerprint(path)).text.startswith("Nothing to file")


def test_the_run_is_the_skill_headless_under_locked_permissions(tmp_path: Path) -> None:
    path = _session(tmp_path, {"phantom-1": "wrong"})
    findings.press(path, _fingerprint(path), T0)
    claude = FakeClaude(_filed())
    _review(path, claude)
    (argv,) = claude.argv
    assert argv[0] == str(findings.CLAUDE)  # absolute: launchd's PATH has no ~/.local/bin
    assert argv[argv.index("-p") + 1] == f"/session-review {STAMP} --headless"
    assert argv[argv.index("--permission-mode") + 1] == "dontAsk"
    assert argv[argv.index("--permission-prompts") + 1] == "none"
    assert argv[argv.index("--setting-sources") + 1] == "user,project"  # not local
    assert "--strict-mcp-config" in argv
    assert json.loads(argv[argv.index("--json-schema") + 1]) == findings.OUTCOME_SCHEMA
    given = json.loads(Path(argv[argv.index("--settings") + 1]).read_text())
    assert given == findings.settings(Path("/repo"), findings.drafts_dir())


def test_the_permissions_hold_the_scope_of_action(tmp_path: Path) -> None:
    drafts = tmp_path / "T" / "cueclock-review"
    given = findings.settings(Path("/repo"), drafts)
    allow, deny = given["permissions"]["allow"], given["permissions"]["deny"]
    assert f"Edit(/{drafts}/**)" in allow  # the only place the Write tool may write
    assert not [
        rule for rule in allow if rule.startswith("Edit(") and "cueclock-review" not in rule
    ]
    assert "Edit(//repo/**)" in deny and "Edit(//Volumes/**)" in deny
    assert "Bash(git commit *)" in deny and "Bash(git push *)" in deny
    assert "Bash(gh issue close *)" in deny and "Bash(launchctl *)" in deny
    gh = sorted(rule for rule in allow if rule.startswith("Bash(gh"))
    assert gh == [
        "Bash(gh issue comment *)",
        "Bash(gh issue create *)",
        "Bash(gh issue list *)",
        "Bash(gh issue view *)",
    ]
    box = given["sandbox"]
    assert box["enabled"] and box["failIfUnavailable"] and not box["allowUnsandboxedCommands"]
    assert box["filesystem"]["denyWrite"] == ["/repo", "/Volumes"]
    assert str(drafts) in box["filesystem"]["allowWrite"]


def test_a_session_starting_pauses_the_run_and_the_next_pass_resumes(tmp_path: Path) -> None:
    path = _session(tmp_path, {"phantom-1": "wrong"})
    findings.press(path, _fingerprint(path), T0)
    _review(path, FakeClaude(stopped="interrupted: the deck's session is up (run pid 7)"))
    status = findings.read_status(path)
    assert status is not None and status["state"] == "interrupted"
    assert findings.view(path, _fingerprint(path)).busy
    assert findings.pending(path) is not None  # run again after the session
    claude = FakeClaude(_filed())
    _review(path, claude)
    assert len(claude.argv) == 1


def test_a_failed_run_says_why_and_is_not_retried(tmp_path: Path) -> None:
    path = _session(tmp_path, {"phantom-1": "wrong"})
    findings.press(path, _fingerprint(path), T0)
    claude = FakeClaude({"type": "result", "is_error": True, "result": "Not logged in"})
    _review(path, claude)
    line = findings.view(path, _fingerprint(path))
    assert line.text == "Failed: Not logged in" and line.note == "Press again."
    assert not line.busy and findings.pending(path) is None
    assert findings.press(path, _fingerprint(path), T0 + timedelta(minutes=1))  # a press retries


def test_a_run_with_no_result_fails_with_its_last_stderr_line(tmp_path: Path) -> None:
    path = _session(tmp_path, {"phantom-1": "wrong"})
    findings.press(path, _fingerprint(path), T0)

    def crash(argv: Sequence[str], folder: Path, live: object) -> tuple[int | None, str]:
        (folder / findings.ERRORS_FILE).write_text("starting\nError: sandbox unavailable\n")
        return 1, ""

    findings.review(path, lambda: None, lambda _: None, run=crash, now=lambda: T0)
    status = findings.read_status(path)
    assert status is not None and status["detail"] == "Error: sandbox unavailable"


def test_a_run_cut_off_by_a_dead_pass_reads_failed(tmp_path: Path) -> None:
    path = _session(tmp_path, {"phantom-1": "wrong"})
    findings.press(path, _fingerprint(path), T0)
    request = findings.read_request(path)
    assert request is not None
    running = {"request": request["wall"], "state": "running", "started": T0.isoformat()}
    findings.write_status(path, {**running, "pid": os.getpid()})
    assert findings.view(path, _fingerprint(path)).text.startswith("Running since")
    findings.write_status(path, {**running, "pid": 2**22 + 7})  # no such process
    assert findings.view(path, _fingerprint(path)).text.startswith("Failed: cut off")
    assert findings.pending(path) is None  # never an unattended retry


def test_launch_stops_the_run_when_a_session_starts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(findings, "POLL_S", 0.05)
    code, why = findings.launch(
        [sys.executable, "-c", "import time; time.sleep(30)"], tmp_path, lambda: "a run is live"
    )
    assert (code, why) == (None, "interrupted: a run is live")
    code, why = findings.launch(
        [sys.executable, "-c", "print('{\"ok\": 1}')"], tmp_path, lambda: None
    )
    assert (code, why) == (0, "")
    assert json.loads((tmp_path / findings.OUTPUT_FILE).read_text()) == {"ok": 1}


# --- the maintain pass ---------------------------------------------------------


def test_maintain_runs_pressed_reviews_on_the_t7_only(tmp_path: Path) -> None:
    local, t7 = tmp_path / "local", tmp_path / "t7"
    for root in (local, t7):
        path = _session(root, {"phantom-1": "wrong"})
        findings.press(path, _fingerprint(path), T0)
    claude = FakeClaude(_filed())
    said: list[str] = []
    maintainer = archive.Maintainer(
        root=tmp_path / "empty",
        archive_root=t7,
        logs=tmp_path / "logs",
        say=said.append,
        review_launch=claude,
    )
    assert maintainer.review_pressed()
    assert len(claude.argv) == 1  # the T7's; the local copy has steps left
    assert findings.read_status(t7 / STAMP) is not None
    assert findings.read_status(local / STAMP) is None

    findings.press(t7 / STAMP, "changed", T0 + timedelta(minutes=1))
    maintainer.dry_run = True
    assert maintainer.review_pressed()
    assert len(claude.argv) == 1
    assert said[-1] == f"{STAMP}: would run the session review (Review done pressed)"


# --- the page -------------------------------------------------------------------

SESSION = [
    {"mono": 100.0, "kind": "cmd_start", "state": "RUNNING", "remaining": 30.0},
    {"mono": 110.0, "kind": "vision_shot_taken", "state": "MOTION", "remaining": 20.0},
    {
        "mono": 113.0,
        "kind": "vision_balls_settled",
        "state": "RUNNING",
        "remaining": 30.0,
        "moved": 0,
    },
]


def test_the_page_has_review_done_and_shows_the_run(tmp_path: Path) -> None:
    logs, local = tmp_path / "logs", tmp_path / "local"
    logs.mkdir()
    with (logs / f"session-{STAMP}.jsonl").open("w") as fh:
        for n, record in enumerate(SESSION):
            wall = (T0 + timedelta(seconds=n)).isoformat(timespec="milliseconds")
            fh.write(json.dumps({**record, "wall": wall}) + "\n")
    (local / STAMP).mkdir(parents=True)
    moments.write(local / STAMP, logs)
    app = TestClient(create_app([local], local_root=local, logs_dir=logs))

    page = app.get(f"/s/{STAMP}").text  # one moment: a shot counted with nothing moved
    assert f'data-url="/api/review_done/{STAMP}"' in page and ">Review done</button>" in page
    assert app.get(f"/api/review_done/{STAMP}").json()["text"] == findings.IDLE_TEXT

    reply = app.post(f"/api/review_done/{STAMP}")
    assert reply.status_code == 200 and reply.json()["busy"]
    assert (local / STAMP / "findings" / "request.json").is_file()
    assert "<button disabled>Review done</button>" in app.get(f"/s/{STAMP}").text
    assert app.post("/api/review_done/20990101-000000").status_code == 404
