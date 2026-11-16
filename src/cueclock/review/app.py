"""The review page: a session's candidate moments, their footage, right/wrong (#57).

Always up under its own launchd agent (`scripts/com.cueclock.review.plist.
template`), on its own port, with no tie to `run` or the camera — Steven
opens it from the couch on his phone after a session. It reads session
directories and footage and writes exactly one thing, each session's
`verdicts.json`.

The page states what the journal recorded and shows the footage; it never
says whether the clock was right. That is Steven's call, and the only reason
the page exists.

Served files are limited to the sessions' own cuts and the recordings listed
in `recordings.jsonl`, addressed by stamp, moment id and line number — never
by a path from the URL.
"""

from __future__ import annotations

import json
from datetime import datetime
from html import escape
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from cueclock import archive
from cueclock.review import sessions as sess
from cueclock.review.moments import session_journal

DEFAULT_PORT = 8001

# Journal kinds that are bookkeeping, not something that happened at the table.
_NOISE = {"detect_heartbeat", "reference_drift", "session_summary"}

_PHRASES = {
    "vision_shot_taken": "Strike seen",
    "vision_balls_settled": "Balls settled",
    "vision_shot_taken_ignored_idle": "Strike seen, ignored (clock idle)",
    "vision_shot_taken_no_clock": "Strike seen, no clock running",
    "vision_balls_settled_no_clock": "Balls settled, no clock running",
    "vision_shot_taken_killed": "Strike seen, vision switched off",
    "vision_scratch": "Scratch called",
    "vision_scratch_candidate": "Possible scratch",
    "vision_scratch_refuted": "Possible scratch withdrawn",
    "shot_provisional": "Clock froze on a strike",
    "shot_provisional_reverted": "Freeze released as not-a-shot, time restored",
    "compensation_suppressed": "Settle not counted as a shot",
    "settle_synthesized": "Watchdog released the frozen clock",
    "break_adjudicated": "Break counted at the settle",
    "tick_transition": "Clock changed",
}


def describe(record: dict[str, object]) -> str:
    """One journal record as a plain sentence fragment."""
    kind = str(record.get("kind"))
    if kind.startswith("cmd_"):
        text = "Pressed " + kind.removeprefix("cmd_").replace("_", " ").title()
    elif kind == "voice_final":
        heard = record.get("text")
        text = f"Heard “{heard}”" + (" (accepted)" if record.get("accepted") else "")
    else:
        text = _PHRASES.get(kind, kind.replace("_", " ").capitalize())
    if record.get("rejected"):
        text += f" — refused ({record['rejected']})"
    state, remaining = record.get("state"), record.get("remaining")
    if state is not None:
        clock = str(state).lower()
        if isinstance(remaining, int | float):
            clock += f", {remaining:.0f} s left"
        text += f" · clock {clock}"
    return text


def _local(when: datetime) -> str:
    return when.astimezone().strftime("%a %d %b, %H:%M")


def _records(path: Path | None) -> list[dict[str, object]]:
    if path is None:
        return []
    out = []
    for line in path.read_text().splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict) and isinstance(record.get("wall"), str):
            out.append(record)
    return out


_STYLE = """
:root { --bg:#f6f6f4; --fg:#16181c; --dim:#666b73; --card:#fff; --line:#dedcd6;
        --right:#1f7a46; --wrong:#b3261e; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#0e1013; --fg:#eceae6; --dim:#9aa0a8; --card:#181b20; --line:#2b2f36;
          --right:#4cc483; --wrong:#ff6b5e; } }
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--fg);
       font:16px/1.45 -apple-system, system-ui, sans-serif; }
main { max-width:760px; margin:0 auto; padding:16px; }
h1 { font-size:1.35rem; margin:.2rem 0 1rem; }
a { color:inherit; }
.dim { color:var(--dim); }
.card { background:var(--card); border:1px solid var(--line); border-radius:12px;
        padding:14px; margin:0 0 14px; }
.row { display:flex; justify-content:space-between; gap:12px; align-items:baseline;
       text-decoration:none; }
.badge { font-size:.8rem; padding:2px 8px; border-radius:99px; border:1px solid var(--line);
         white-space:nowrap; }
video { width:100%; border-radius:8px; background:#000; margin:8px 0; }
ol { padding-left:0; list-style:none; margin:8px 0; font-size:.9rem; }
ol li { display:flex; gap:10px; padding:2px 0; }
ol li .at { color:var(--dim); min-width:4.2em; text-align:right;
            font-variant-numeric:tabular-nums; }
ol li.here { font-weight:600; }
.buttons { display:flex; gap:10px; margin-top:10px; }
button { flex:1; font:inherit; padding:12px; border-radius:10px; cursor:pointer;
         border:2px solid var(--line); background:transparent; color:var(--fg); }
button.right.on { border-color:var(--right); color:var(--right); }
button.wrong.on { border-color:var(--wrong); color:var(--wrong); }
textarea { width:100%; font:inherit; margin-top:8px; padding:8px; border-radius:8px;
           border:1px solid var(--line); background:transparent; color:var(--fg); }
.card.judged { opacity:.7; }
"""

_SCRIPT = """
document.querySelectorAll('.card[data-id]').forEach(card => {
  const note = card.querySelector('textarea');
  const send = verdict => fetch(card.dataset.post, {method:'POST',
      headers:{'content-type':'application/json'},
      body: JSON.stringify({verdict, note: note.value})})
    .then(r => { if (!r.ok) throw r; return r.json(); })
    .then(v => {
      card.dataset.verdict = v.verdict || '';
      card.classList.toggle('judged', !!v.verdict);
      card.querySelectorAll('button').forEach(b =>
        b.classList.toggle('on', b.value === v.verdict));
    })
    .catch(() => alert('Not saved — is the review server still up?'));
  card.querySelectorAll('button').forEach(b => b.addEventListener('click', () =>
    send(card.dataset.verdict === b.value ? null : b.value)));
  note.addEventListener('change', () => { if (card.dataset.verdict)
    send(card.dataset.verdict); });
});
// Seek recordings to the moment even where #t= fragments are ignored.
document.querySelectorAll('video[data-at]').forEach(v =>
  v.addEventListener('loadedmetadata', () => {
    if (v.currentTime < 1) v.currentTime = parseFloat(v.dataset.at); }, {once:true}));
"""


def _page(title: str, body: str, script: bool = False) -> HTMLResponse:
    js = f"<script>{_SCRIPT}</script>" if script else ""
    return HTMLResponse(
        "<!doctype html><html lang=en><head><meta charset=utf-8>"
        '<meta name=viewport content="width=device-width, initial-scale=1">'
        f"<title>{escape(title)}</title><style>{_STYLE}</style></head>"
        f"<body><main>{body}</main>{js}</body></html>"
    )


def create_app(
    roots: list[Path] | None = None,
    local_root: Path = sess.LOCAL_ROOT,
    logs_dir: Path = Path("logs"),
) -> FastAPI:
    """`roots` defaults to the local root, then the T7 archive."""
    session_roots = roots if roots is not None else [local_root, archive.ARCHIVE_ROOT]
    app = FastAPI(title="cueclock review")

    def session(stamp: str) -> sess.Session:
        if sess.STAMP.match(stamp):
            for found in sess.discover(session_roots):
                if found.stamp == stamp:
                    return found
        raise HTTPException(404, "no such session")

    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        rows = []
        for s in sess.discover(session_roots):
            moments, open_ = s.moments(), s.unreviewed()
            if moments is None:
                badge = "not scanned"
            elif not moments:
                badge = "nothing to review"
            elif open_:
                badge = f"{open_} of {len(moments)} to review"
            else:
                badge = f"all {len(moments)} reviewed"
            rows.append(
                f'<a class="card row" href="/s/{s.stamp}"><span>{_local(s.started)}'
                f' <span class="dim">{s.stamp}</span></span>'
                f'<span class="badge">{badge}</span></a>'
            )
        missing = [str(r) for r in session_roots if not r.is_dir()]
        note = f'<p class="dim">Not reachable: {escape(", ".join(missing))}</p>' if missing else ""
        body = "".join(rows) or '<p class="dim">No sessions yet.</p>'
        return _page("Session review", f"<h1>Sessions</h1>{note}{body}")

    @app.get("/s/{stamp}", response_class=HTMLResponse)
    async def session_page(stamp: str) -> HTMLResponse:
        s = session(stamp)
        moments = s.moments()
        head = f'<p><a href="/">← Sessions</a></p><h1>{_local(s.started)}</h1>'
        if moments is None:
            return _page(stamp, head + '<p class="dim">The moments finder has not run here.</p>')
        verdicts = s.verdicts()
        records = _records(session_journal(s.path, logs_dir))
        available = sess.recordings(local_root)
        cards = [_card(s, m, verdicts.get(str(m.get("id"))), records, available) for m in moments]
        body = "".join(cards) or '<p class="dim">Nothing in this session needs a look.</p>'
        return _page(stamp, head + body, script=True)

    @app.post("/api/verdict/{stamp}/{moment_id}")
    async def verdict(stamp: str, moment_id: str, request: Request) -> JSONResponse:
        s = session(stamp)
        if moment_id not in {m.get("id") for m in s.moments() or []}:
            raise HTTPException(404, "no such moment")
        try:
            payload = await request.json()
        except ValueError as exc:
            raise HTTPException(400, "expected JSON") from exc
        call = payload.get("verdict") if isinstance(payload, dict) else None
        note = payload.get("note", "") if isinstance(payload, dict) else ""
        if call is not None and call not in sess.VERDICTS:
            raise HTTPException(400, f"verdict must be one of {sess.VERDICTS} or null")
        s.set_verdict(moment_id, call, str(note)[:2000])
        return JSONResponse({"id": moment_id, **s.verdicts().get(moment_id, {"verdict": None})})

    @app.get("/media/{stamp}/{moment_id}.mp4")
    async def cut(stamp: str, moment_id: str) -> FileResponse:
        path = session(stamp).cut(moment_id)
        if path is None:
            raise HTTPException(404, "no cut")
        return FileResponse(path, media_type="video/mp4")

    @app.get("/recording/{index}")
    async def recording(index: int) -> FileResponse:
        for rec in sess.recordings(local_root):
            if rec.index == index:
                media = "video/quicktime" if rec.path.suffix.lower() == ".mov" else "video/mp4"
                return FileResponse(rec.path, media_type=media)
        raise HTTPException(404, "no such recording")

    return app


_VIDEO = 'controls playsinline preload="metadata"'


def _card(
    s: sess.Session,
    moment: dict[str, object],
    given: dict[str, object] | None,
    records: list[dict[str, object]],
    available: list[sess.Recording],
) -> str:
    mid = str(moment.get("id"))
    try:
        at = datetime.fromisoformat(str(moment["wall"]))
        start = datetime.fromisoformat(str(moment["start_wall"]))
        end = datetime.fromisoformat(str(moment["end_wall"]))
    except (KeyError, ValueError):
        return ""

    if s.cut(mid) is not None:
        video = f'<video {_VIDEO} src="/media/{s.stamp}/{mid}.mp4"></video>'
    elif rec := sess.recording_for(start, available) or sess.recording_for(at, available):
        offset = max(0.0, (start - rec.start).total_seconds())
        video = (
            f'<video {_VIDEO} data-at="{offset:.1f}" '
            f'src="/recording/{rec.index}#t={offset:.1f}"></video>'
        )
    else:
        video = '<p class="dim">Footage pruned — the journal below is what is left.</p>'

    lines = []
    for record in records:
        try:
            when = datetime.fromisoformat(str(record["wall"]))
        except ValueError:
            continue
        if record.get("kind") in _NOISE or not start <= when <= end:
            continue
        delta = (when - at).total_seconds()
        here = ' class="here"' if abs(delta) < 0.0005 else ""
        lines.append(
            f'<li{here}><span class="at">{delta:+.1f} s</span>'
            f"<span>{escape(describe(record))}</span></li>"
        )
    journal = f"<ol>{''.join(lines)}</ol>" if lines else ""

    call = str(given.get("verdict")) if given else ""
    note = escape(str(given.get("note", ""))) if given else ""
    on = {v: " on" if call == v else "" for v in sess.VERDICTS}
    return (
        f'<section class="card{" judged" if call else ""}" id="{mid}" data-id="{mid}" '
        f'data-verdict="{call}" data-post="/api/verdict/{s.stamp}/{mid}">'
        f'<div class="row"><strong>{escape(str(moment.get("kind", "")).replace("_", " "))}</strong>'
        f'<span class="dim">{at.astimezone().strftime("%H:%M:%S")}</span></div>'
        f"<p>{escape(str(moment.get('summary', '')))}</p>{video}{journal}"
        f'<div class="buttons">'
        f'<button class="right{on["right"]}" value="right">Clock right</button>'
        f'<button class="wrong{on["wrong"]}" value="wrong">Clock wrong</button></div>'
        f'<textarea rows="1" placeholder="Note (optional)">{note}</textarea></section>'
    )
