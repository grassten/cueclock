"""Review done: the press, and the session-review run it starts (#70 step 3).

Steven presses **Review done** on a session's page once he has judged what he
wants to. The page writes `findings/request.json` (when, and a fingerprint of
the verdicts) and nothing else. The next `session maintain` pass (launchd,
every 15 minutes) runs the `session-review` skill on that session, headless,
and records the outcome in `findings/status.json`, which the page shows
beside the button. One writer per file: the page writes the request, the
pass writes the status.

Nothing runs without a press: not at session close, and not when
`verdicts.json` changes. He judges in passes (two, 9 minutes apart, on
2026-11-21), so only he knows when he is done.

The run is `claude -p "/session-review <stamp> --headless"` in the repo, and
its permissions enforce the skill's scope of action instead of trusting the
skill's prose (`settings`). It may read anything, and write only the drafts
directory under the temp dir. Bash runs inside the macOS sandbox, so a Python
one-off can't write the repo or the T7 either, and has no network. `gh` may
list, view, create and comment on issues, nothing else; commits, pushes and
`launchctl` are denied outright. Anything else that would need approval is
refused, because nobody is there to approve it. The run's last word is a
structured result (`OUTCOME_SCHEMA`), which becomes the status.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import signal
import subprocess  # nosec B404: fixed argv lists, no shell
import sys
import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cueclock.archive import REPO_ROOT
from cueclock.review import sessions as sess
from cueclock.review.moments import write_json

FINDINGS_DIR = "findings"
REQUEST_FILE = "request.json"  # written by the review page
STATUS_FILE = "status.json"  # written by `session maintain`
SETTINGS_FILE = "settings.json"  # the permissions the run had, kept for the record
OUTPUT_FILE = "run.json"  # claude's result
ERRORS_FILE = "run.log"  # claude's stderr

CLAUDE = Path.home() / ".local" / "bin" / "claude"  # absolute: launchd's PATH lacks it
PATH = f"{CLAUDE.parent}:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"  # gh is in Homebrew
# The run's login. Under launchd, claude can't use the login an interactive
# session uses ("this login reached its time limit and cannot be renewed",
# 2026-11-22), so it gets a long-lived token from `claude setup-token`, kept
# here (mode 600, outside the repo) and passed as CLAUDE_CODE_OAUTH_TOKEN.
TOKEN_FILE = Path.home() / ".config" / "cueclock" / "claude-token"
PYTHON = ".venv/bin/python3"  # the skill's own spelling (never `uv run`)
GIT = "/usr/bin/git"

# A ceiling against a hung run, not a measured length: the golden test's
# headless runs (checkpoint 143) finished well inside it.
RUN_LIMIT_S = 2 * 3600.0
POLL_S = 30.0  # how often a running review checks that no session has started
STOP_GRACE_S = 10.0

FINISHED = ("filed", "commented", "already filed", "nothing to file")
OUTCOMES = (*FINISHED, "stopped")
OUTCOME_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "outcome": {"type": "string", "enum": list(OUTCOMES)},
        "issue": {"type": ["integer", "null"]},
        "summary": {"type": "string"},
    },
    "required": ["outcome", "issue", "summary"],
}
# Built-in tools the run gets. No web tools; Write for the issue draft; Agent
# for the skill's diagnosis and measurement subagents.
TOOLS = "Bash,Read,Glob,Grep,Write,Agent"


def _read(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _iso(when: datetime) -> str:
    return when.isoformat(timespec="seconds")


def read_request(session: Path) -> dict[str, Any] | None:
    return _read(session / FINDINGS_DIR / REQUEST_FILE)


def read_status(session: Path) -> dict[str, Any] | None:
    return _read(session / FINDINGS_DIR / STATUS_FILE)


def write_status(session: Path, status: dict[str, Any]) -> None:
    write_json(session / FINDINGS_DIR / STATUS_FILE, status)


def _finished_on(status: dict[str, Any] | None, fingerprint: str) -> bool:
    """Did a run finish on exactly these verdicts?"""
    return (
        status is not None
        and status.get("state") in FINISHED
        and status.get("fingerprint") == fingerprint
    )


def press(session: Path, fingerprint: str, now: datetime) -> bool:
    """Record a Review done press; False when nothing changed since the last
    finished run, so there is nothing to queue.

    FileNotFoundError when the session directory is gone: the archive pass
    moved it between the lookup and this write. Look it up again.
    """
    if _finished_on(read_status(session), fingerprint):
        return False
    folder = session / FINDINGS_DIR
    if not session.is_dir():
        raise FileNotFoundError(session)
    folder.mkdir(exist_ok=True)
    write_json(folder / REQUEST_FILE, {"wall": _iso(now), "fingerprint": fingerprint})
    return True


def _alive(pid: object) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def pending(session: Path) -> dict[str, Any] | None:
    """The press a maintain pass should act on now, or None.

    A press is answered once: by a status naming its request. A run a live
    session interrupted is answered again. One left "running" by a pass that
    died (a reboot mid-run) is not retried, and the page reads it as failed:
    a new press is cheap for Steven, a loop of unattended runs is not.
    """
    request = read_request(session)
    if request is None:
        return None
    status = read_status(session)
    if status is None or status.get("request") != request.get("wall"):
        return request
    return request if status.get("state") == "interrupted" else None


@dataclass(frozen=True)
class Line:
    """What the page says beside the button: `text` (a link when `href` is
    set), then `note`. `busy` while queued or running: the button waits."""

    text: str
    href: str | None = None
    note: str = ""
    busy: bool = False


IDLE_TEXT = (
    "Press when you're done judging: your wrong verdicts become one issue of "
    "proposed changes. Moments left unjudged are left out."
)


def _local(text: object) -> str:
    try:
        return datetime.fromisoformat(str(text)).astimezone().strftime("%H:%M")
    except ValueError:
        return "?"


def view(session: Path, fingerprint: str) -> Line:
    """The run's state, as the page shows it."""
    request, status = read_request(session), read_status(session)
    if request is None:
        return Line(IDLE_TEXT)
    if status is None or status.get("request") != request.get("wall"):
        return Line("Queued: the next maintenance pass starts it (every 15 min).", busy=True)
    state = str(status.get("state"))
    issue, url = status.get("issue"), status.get("url")
    href = url if isinstance(url, str) else None
    summary = str(status.get("summary") or "")
    if state == "running" and _alive(status.get("pid")):
        return Line(f"Running since {_local(status.get('started'))}.", busy=True)
    if state == "running":
        return Line("Failed: cut off when the maintenance pass stopped.", note="Press again.")
    if state == "interrupted":
        return Line("Paused: a session started. It runs again after the session.", busy=True)
    if state == "failed":
        return Line(f"Failed: {status.get('detail') or 'no reason given'}", note="Press again.")
    changed = "" if status.get("fingerprint") == fingerprint else "Verdicts changed since."
    if state in ("filed", "commented", "already filed") and isinstance(issue, int):
        verb = {"filed": "Filed", "commented": "Updated", "already filed": "Already filed as"}
        return Line(f"{verb[state]} #{issue}", href, " ".join(p for p in (summary, changed) if p))
    head = "Nothing to file" if state == "nothing to file" else "Stopped"
    return Line(f"{head}: {summary}" if summary else f"{head}.", note=changed)


# --- the run -----------------------------------------------------------------


def drafts_dir() -> Path:
    """Where the skill writes its issue draft: `$TMPDIR/cueclock-review`.

    The per-user temp dir, asked of the system: a launchd job may have no
    TMPDIR, and the run is given this one explicitly.
    """
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        out = subprocess.run(  # nosec B603: fixed argv
            ["/usr/bin/getconf", "DARWIN_USER_TEMP_DIR"],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        ).stdout.strip()
        if out:
            return Path(out) / "cueclock-review"
    return Path(tempfile.gettempdir()) / "cueclock-review"


def settings(repo: Path, drafts: Path) -> dict[str, Any]:
    """The headless run's permissions: the skill's scope of action, enforced.

    Read anything. Write only `drafts` (the Write tool by the Edit rules, Bash
    by the sandbox). Run the project's Python, the read-only git commands and
    four `gh issue` verbs. `gh` runs outside the sandbox (Go's TLS fails
    under Seatbelt) and is held to those verbs by the rules. With
    `--permission-mode dontAsk`, anything else is refused. The deny list
    names the scope of action's hard limits, so an allow rule from the
    user's own settings can't reopen them.
    """
    places = sorted({str(drafts), str(drafts.resolve())})  # /var is /private/var
    pythons = (PYTHON, str(repo / PYTHON))
    return {
        "permissions": {
            "allow": [
                "Read",
                "Glob",
                "Grep",
                "Agent",
                *(f"Edit(/{p}/**)" for p in places),  # `//` = an absolute path
                *(f"Bash({py} *)" for py in pythons),  # confined by the sandbox
                *(f"Bash(git {verb} *)" for verb in ("show", "log", "diff", "rev-parse")),
                *(f"Bash(gh issue {verb} *)" for verb in ("list", "view", "create", "comment")),
            ],
            "deny": [
                f"Edit(/{repo}/**)",
                "Edit(//Volumes/**)",
                "WebFetch",
                "WebSearch",
                *(f"Bash(git {verb} *)" for verb in ("commit", "push", "reset", "checkout")),
                *(
                    f"Bash(gh issue {verb} *)"
                    for verb in ("close", "reopen", "edit", "delete", "transfer", "lock")
                ),
                "Bash(gh pr *)",
                "Bash(gh api *)",
                "Bash(launchctl *)",
            ],
        },
        "sandbox": {
            "enabled": True,
            "failIfUnavailable": True,  # never quietly run Bash unconfined
            "allowUnsandboxedCommands": False,
            "excludedCommands": ["gh issue *"],
            "filesystem": {"denyWrite": [str(repo), "/Volumes"], "allowWrite": places},
        },
    }


def command(stamp: str, settings_path: Path) -> list[str]:
    """The headless run. User settings load (the model and effort Steven
    chose); the repo's local settings don't, and no MCP server does."""
    return [
        str(CLAUDE),
        "-p",
        f"/session-review {stamp} --headless",
        "--output-format",
        "json",
        "--json-schema",
        json.dumps(OUTCOME_SCHEMA),
        "--settings",
        str(settings_path),
        "--setting-sources",
        "user,project",
        "--strict-mcp-config",
        "--permission-mode",
        "dontAsk",
        "--permission-prompts",
        "none",
        "--tools",
        TOOLS,
    ]


# Runs `argv` with its output in `folder`, polling `live`: returns the exit
# code, or None and why it was stopped.
Launcher = Callable[[Sequence[str], Path, Callable[[], str | None]], tuple[int | None, str]]


def _stop(proc: subprocess.Popen[bytes]) -> None:
    with contextlib.suppress(ProcessLookupError):
        os.killpg(proc.pid, signal.SIGTERM)
    try:
        proc.wait(timeout=STOP_GRACE_S)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()


def launch(
    argv: Sequence[str], folder: Path, live: Callable[[], str | None]
) -> tuple[int | None, str]:
    """The real launcher. Stops the run if a session starts, or at the limit.

    The maintain agent is a launchd Background job, and `review check` (a
    replay) runs about 6x slower under it (384 s vs 62 s). No session is live
    while a review runs (this stops it if one starts), so the run is taken
    out of the background band rather than left to time out.
    """
    drafts = drafts_dir()
    drafts.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "PATH": PATH, "TMPDIR": f"{drafts.parent}/"}
    with contextlib.suppress(OSError):
        token = TOKEN_FILE.read_text().strip()
        if token:
            env["CLAUDE_CODE_OAUTH_TOKEN"] = token
    with (folder / OUTPUT_FILE).open("wb") as out, (folder / ERRORS_FILE).open("wb") as err:
        proc = subprocess.Popen(  # nosec B603: fixed argv, shell=False
            list(argv),
            cwd=REPO_ROOT,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=err,
            start_new_session=True,  # its own process group, so a stop takes its children
        )
        if sys.platform == "darwin":  # the constant exists nowhere else
            with contextlib.suppress(OSError):
                os.setpriority(os.PRIO_DARWIN_PROCESS, proc.pid, 0)  # out of the background band
        deadline = time.monotonic() + RUN_LIMIT_S
        while True:
            try:
                return proc.wait(timeout=POLL_S), ""
            except subprocess.TimeoutExpired:
                pass
            reason = live()
            if reason is not None:
                _stop(proc)
                return None, f"interrupted: {reason}"
            if time.monotonic() > deadline:
                _stop(proc)
                return None, f"ran over {RUN_LIMIT_S / 3600:.0f} h and was stopped"


def _last_line(text: str) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return lines[-1][:300] if lines else ""


def parse(folder: Path) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """(the structured outcome or None, the whole result object) from a run.

    The result is the JSON object on stdout; a failure inside the run (no
    auth, say) is that object too, with the reason as its `result`.
    """
    try:
        text = (folder / OUTPUT_FILE).read_text(errors="replace")
    except OSError:
        return None, {}
    result: dict[str, Any] = {}
    for line in reversed(text.splitlines()):
        with contextlib.suppress(ValueError):
            found = json.loads(line)
            if isinstance(found, dict):
                result = found
                break
    outcome = result.get("structured_output")
    if (
        isinstance(outcome, dict)
        and not result.get("is_error")
        and outcome.get("outcome") in OUTCOMES
    ):
        return outcome, result
    return None, result


def _failure(folder: Path, result: dict[str, Any], code: int) -> str:
    said = _last_line(str(result.get("result") or ""))
    if not said:
        with contextlib.suppress(OSError):
            said = _last_line((folder / ERRORS_FILE).read_text(errors="replace"))
    return said or f"claude exited {code} with no output"


def issue_url(number: int) -> str | None:
    """The issue's address, from the repo's `origin` (github.com only)."""
    try:
        origin = subprocess.run(  # nosec B603: fixed argv
            [GIT, "-C", str(REPO_ROOT), "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    found = re.search(r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?$", origin)
    return f"https://github.com/{found.group(1)}/issues/{number}" if found else None


def review(
    session: Path,
    live: Callable[[], str | None],
    say: Callable[[str], None],
    *,
    run: Launcher = launch,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    url_of: Callable[[int], str | None] = issue_url,
    repo: Path = REPO_ROOT,
) -> None:
    """Answer the session's pending press, if it has one."""
    request = pending(session)
    if request is None:
        return
    found = sess.Session(session.name, session)
    fingerprint = found.fingerprint()
    base = {"request": request.get("wall"), "fingerprint": fingerprint, "started": _iso(now())}
    last = read_status(session)
    wrong = [v for v in found.verdicts().values() if v.get("verdict") == "wrong"]
    if not wrong and not (last and isinstance(last.get("issue"), int)):
        # Nothing to diagnose and no issue of its own to update: no run.
        write_status(
            session,
            {
                **base,
                "state": "nothing to file",
                "summary": "no verdict says the clock was wrong",
                "finished": _iso(now()),
            },
        )
        say(f"{session.name}: session review — no wrong verdicts, nothing to file")
        return
    folder = session / FINDINGS_DIR
    settings_path = folder / SETTINGS_FILE
    write_json(settings_path, settings(repo, drafts_dir()))
    write_status(session, {**base, "state": "running", "pid": os.getpid()})
    say(f"{session.name}: session review started ({len(wrong)} wrong verdicts)")
    code, why = run(command(session.name, settings_path), folder, live)
    if code is None:
        state = "interrupted" if why.startswith("interrupted") else "failed"
        write_status(session, {**base, "state": state, "detail": why, "finished": _iso(now())})
        say(f"{session.name}: session review {why}")
        return
    outcome, result = parse(folder)
    spend = {"cost_usd": result.get("total_cost_usd"), "turns": result.get("num_turns")}
    if outcome is None:
        detail = _failure(folder, result, code)
        write_status(
            session,
            {**base, **spend, "state": "failed", "detail": detail, "finished": _iso(now())},
        )
        say(f"{session.name}: session review failed ({code}): {detail}")
        return
    issue = outcome.get("issue")
    number = issue if isinstance(issue, int) and issue > 0 else None
    write_status(
        session,
        {
            **base,
            **spend,
            "state": outcome["outcome"],
            "issue": number,
            "url": url_of(number) if number is not None else None,
            "summary": str(outcome.get("summary", ""))[:500],
            "finished": _iso(now()),
        },
    )
    say(
        f"{session.name}: session review — {outcome['outcome']}"
        + (f" #{number}" if number is not None else "")
    )
