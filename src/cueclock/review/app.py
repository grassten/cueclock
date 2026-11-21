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
from dataclasses import asdict
from datetime import datetime
from html import escape
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from cueclock import archive
from cueclock.review import moments as finder
from cueclock.review import sessions as sess
from cueclock.review.moments import session_journal

DEFAULT_PORT = 8001

# Journal kinds that are bookkeeping, not something that happened at the table.
_NOISE = {"detect_heartbeat", "reference_drift", "session_summary", "ball_list"}

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
    "scratch_hold": "Clock held 5 s for the scratch",
    "scratch_hold_released": "Scratch hold released",
    "scratch_list": "Ball list read at the settle",
    "scratch_call": "Scratch question closed",
    "ball_in_hand": "Ball in hand: placing won't count as a shot",
    "ball_in_hand_ended": "Ball in hand over",
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
button.save { background:var(--fg); color:var(--bg); border-color:var(--fg); font-weight:600; }
textarea { width:100%; font:inherit; margin-top:10px; padding:10px; border-radius:8px;
           border:1px solid var(--line); background:transparent; color:var(--fg); }
.compose { display:none; }
.card.chosen .compose { display:block; }
.summary { display:none; gap:4px 10px; align-items:baseline; flex-wrap:wrap; }
.summary .meta { flex:1; }
.card.done .summary { display:flex; }
.card.done .body { display:none; }
.card.done { padding:10px 14px; }
.summary .note-text { color:var(--dim); order:9; flex-basis:100%; }
.summary .note-text:empty { display:none; }
.verdict-text { font-weight:600; white-space:nowrap; }
.verdict-text.right { color:var(--right); }
.verdict-text.wrong { color:var(--wrong); }
.links { display:flex; justify-content:space-between; margin-top:8px; font-size:.9rem; }
#progress { margin:-.6rem 0 1rem; }
.events { max-height:45vh; overflow-y:auto; }
.events a { text-decoration:none; display:flex; gap:10px; padding:4px 0; }
.events li.moment a { font-weight:600; }
video.full { position:sticky; top:0; z-index:1; }
"""

_SCRIPT = """
// A verdict is two steps: pick Right/Wrong (opens the note), then Save, which
// collapses the card to a one-line summary and moves on to the next open one.
document.querySelectorAll('.card[data-id]').forEach(card => {
  const note = card.querySelector('textarea');
  const saveBtn = card.querySelector('.save');
  const pick = v => {
    card.dataset.pick = v;
    card.classList.toggle('chosen', !!v);
    card.querySelectorAll('.pick').forEach(b => b.classList.toggle('on', b.value === v));
    if (v) { saveBtn.textContent = v === 'right' ? 'Save: clock right' : 'Save: clock wrong';
             note.focus({preventScroll:true}); }
  };
  const post = verdict => fetch(card.dataset.post, {method:'POST',
      headers:{'content-type':'application/json'},
      body: JSON.stringify({verdict, note: note.value})})
    .then(r => { if (!r.ok) throw r; return r.json(); });
  const progress = () => {
    const cards = [...document.querySelectorAll('.card[data-id]')];
    const done = cards.filter(c => c.classList.contains('done')).length;
    const el = document.getElementById('progress');
    if (el) el.textContent = `${done} of ${cards.length} reviewed`;
  };
  const collapse = v => {
    card.dataset.verdict = v.verdict || '';
    card.classList.toggle('done', !!v.verdict);
    card.classList.remove('editing');
    const sum = card.querySelector('.verdict-text');
    sum.textContent = v.verdict === 'right' ? '✓ Clock right' : '✗ Clock wrong';
    sum.className = 'verdict-text ' + (v.verdict || '');
    card.querySelector('.note-text').textContent = v.note ? '— ' + v.note : '';
    card.querySelectorAll('video').forEach(x => x.pause());
    progress();
  };
  card.querySelectorAll('.pick').forEach(b => b.addEventListener('click', () => pick(b.value)));
  saveBtn.addEventListener('click', () => post(card.dataset.pick)
    .then(v => {
      collapse(v);
      const next = [...document.querySelectorAll('.card[data-id]:not(.done)')][0];
      (next || card).scrollIntoView({behavior:'smooth', block:'start'});
    })
    .catch(() => alert('Not saved — is the review server still up?')));
  card.querySelector('.change').addEventListener('click', e => {
    e.preventDefault(); card.classList.add('editing'); card.classList.remove('done');
    pick(card.dataset.verdict);
  });
  card.querySelector('.clear').addEventListener('click', e => {
    e.preventDefault();
    post(null).then(() => { note.value = ''; card.dataset.verdict = '';
      card.classList.remove('done', 'editing'); pick(''); progress(); })
    .catch(() => alert('Not saved — is the review server still up?'));
  });
  if (card.dataset.verdict) pick(card.dataset.verdict);
});
// Full-session player: tap an event to jump the video to it.
document.querySelectorAll('[data-seek]').forEach(a => a.addEventListener('click', e => {
  e.preventDefault();
  const v = document.getElementById(a.dataset.video);
  v.currentTime = parseFloat(a.dataset.seek); v.play();
  v.scrollIntoView({behavior:'smooth', block:'nearest'});
}));
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

    def moments_of(s: sess.Session) -> list[dict[str, object]] | None:
        """The session's moments: `moments.json` once the archive pass has
        written it, until then found here from the journal.

        The archive pass runs the finder after its parity check, up to 15
        minutes and one whole-session replay after the session stops, and
        the page read "The moments finder has not run here" for all of it
        (2026-11-18: Steven opened it 11 minutes after a session, nothing to
        review). Ids come from the journal's own records, so a verdict given
        now is still attached when the file arrives; a moment the parity
        check adds (`replay`) only appears then. None: no journal at all.
        """
        written = s.moments()
        if written is not None:
            return written
        journal = session_journal(s.path, logs_dir)
        return None if journal is None else [asdict(m) for m in finder.find(journal)]

    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        rows = []
        for s in sess.discover(session_roots):
            moments = moments_of(s)
            judged = s.verdicts()
            open_ = sum(1 for m in moments or [] if m.get("id") not in judged)
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
        moments = moments_of(s)
        head = f'<p><a href="/">← Sessions</a></p><h1>{_local(s.started)}</h1>'
        if moments is None:
            return _page(stamp, head + '<p class="dim">This session has no journal to read.</p>')
        verdicts = s.verdicts()
        records = _records(session_journal(s.path, logs_dir))
        available = sess.recordings(local_root)
        if full_sources(s, records):
            head += (
                f'<a class="card row" href="/s/{s.stamp}/full">'
                "<span>▶ Watch the full session</span></a>"
            )
        cards = [_card(s, m, verdicts.get(str(m.get("id"))), records, available) for m in moments]
        judged = sum(1 for m in moments if str(m.get("id")) in verdicts)
        if moments:
            head += f'<p id="progress" class="dim">{judged} of {len(moments)} reviewed</p>'
        body = "".join(cards) or '<p class="dim">Nothing in this session needs a look.</p>'
        return _page(stamp, head + body, script=True)

    def full_sources(
        s: sess.Session, records: list[dict[str, object]]
    ) -> list[tuple[str, sess.Recording]]:
        """(url, recording) for the whole session: its own kept copy, else the
        listed recordings still in ~/Movies that overlap it."""
        own = s.full_recordings()
        if own:
            return [(f"/full/{s.stamp}/{r.index}", r) for r in own]
        walls = [w for w in (_wall(r) for r in records) if w is not None]
        end = max(walls, default=s.started)
        listed = sess.overlapping(s.started, end, sess.recordings(local_root))
        return [(f"/recording/{r.index}", r) for r in listed]

    @app.get("/s/{stamp}/full", response_class=HTMLResponse)
    async def full_page(stamp: str) -> HTMLResponse:
        s = session(stamp)
        records = _records(session_journal(s.path, logs_dir))
        sources = full_sources(s, records)
        head = f'<p><a href="/s/{s.stamp}">← Moments</a></p><h1>{_local(s.started)}</h1>'
        if not sources:
            return _page(
                stamp, head + '<p class="dim">No full recording kept for this session.</p>'
            )
        moment_walls = {str(m.get("wall")) for m in moments_of(s) or []}
        parts = []
        for n, (url, rec) in enumerate(sources):
            vid = f"full{n}"
            items = []
            for record in records:
                when = _wall(record)
                if when is None or record.get("kind") not in _TIMELINE or not rec.covers(when):
                    continue
                at = (when - rec.start).total_seconds()
                mark = ' class="moment"' if str(record.get("wall")) in moment_walls else ""
                items.append(
                    f'<li{mark}><a href="#" data-video="{vid}" data-seek="{at:.1f}">'
                    f'<span class="at">{_clock(at)}</span>'
                    f"<span>{escape(describe(record))}</span></a></li>"
                )
            parts.append(
                f'<video id="{vid}" class="full" {_VIDEO} src="{url}"></video>'
                f'<ol class="events">{"".join(items)}</ol>'
            )
        return _page(stamp, head + "".join(parts), script=True)

    @app.get("/full/{stamp}/{index}")
    async def full_file(stamp: str, index: int) -> FileResponse:
        for rec in session(stamp).full_recordings():
            if rec.index == index:
                return FileResponse(rec.path, media_type=_media_type(rec.path))
        raise HTTPException(404, "no such recording")

    @app.post("/api/verdict/{stamp}/{moment_id}")
    async def verdict(stamp: str, moment_id: str, request: Request) -> JSONResponse:
        s = session(stamp)
        if moment_id not in {m.get("id") for m in moments_of(s) or []}:
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
                return FileResponse(rec.path, media_type=_media_type(rec.path))
        raise HTTPException(404, "no such recording")

    return app


_VIDEO = 'controls playsinline preload="metadata"'

# What the full-session event list shows: what happened at the table and what
# the clock did about it. Bookkeeping (heartbeats, voice finals) stays out.
_TIMELINE = {
    "vision_shot_taken",
    "vision_balls_settled",
    "shot_provisional_reverted",
    "settle_synthesized",
    "break_adjudicated",
    "ball_in_hand",
    "ball_in_hand_ended",
    "vision_scratch",
    "cmd_extension",
    "cmd_new_rack",
    "cmd_restart",
    "cmd_start",
    "cmd_reset",
    "cmd_pause",
    "cmd_push",
}


def _media_type(path: Path) -> str:
    return "video/quicktime" if path.suffix.lower() == ".mov" else "video/mp4"


def _wall(record: dict[str, object]) -> datetime | None:
    try:
        return datetime.fromisoformat(str(record["wall"]))
    except (KeyError, ValueError):
        return None


def _clock(seconds: float) -> str:
    m, sec = divmod(int(max(0.0, seconds)), 60)
    return f"{m // 60}:{m % 60:02d}:{sec:02d}" if m >= 60 else f"{m}:{sec:02d}"


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
        if record.get("kind") == "voice_final" and not record.get("text"):
            continue  # a wordless final (checkpoint 121): mic noise, not something said
        delta = (when - at).total_seconds()
        here = ' class="here"' if abs(delta) < 0.0005 else ""
        lines.append(
            f'<li{here}><span class="at">{delta:+.1f} s</span>'
            f"<span>{escape(describe(record))}</span></li>"
        )
    journal = f"<ol>{''.join(lines)}</ol>" if lines else ""

    call = str(given.get("verdict")) if given else ""
    note = escape(str(given.get("note", ""))) if given else ""
    kind = escape(str(moment.get("kind", "")).replace("_", " "))
    at_clock = at.astimezone().strftime("%H:%M:%S")
    shown = {"right": "✓ Clock right", "wrong": "✗ Clock wrong"}.get(call, "")
    return (
        f'<section class="card{" done" if call else ""}" id="{mid}" data-id="{mid}" '
        f'data-verdict="{call}" data-post="/api/verdict/{s.stamp}/{mid}">'
        # Collapsed, once judged: one line, and a way back in.
        f'<div class="summary"><span class="verdict-text {call}">{shown}</span>'
        f'<span class="note-text">{"— " + note if note else ""}</span>'
        f'<span class="dim meta">{kind} · {at_clock}</span>'
        f'<a href="#" class="change">Change</a></div>'
        f'<div class="body">'
        f'<div class="row"><strong>{kind}</strong><span class="dim">{at_clock}</span></div>'
        f"<p>{escape(str(moment.get('summary', '')))}</p>{video}{journal}"
        f'<div class="buttons">'
        f'<button class="pick right" value="right">Clock right</button>'
        f'<button class="pick wrong" value="wrong">Clock wrong</button></div>'
        f'<div class="compose">'
        f'<textarea rows="2" placeholder="Note (optional): what you saw">{note}</textarea>'
        f'<div class="buttons"><button class="save">Save</button></div>'
        f'<div class="links"><span></span>'
        f'<a href="#" class="clear dim">{"Clear verdict" if call else ""}</a></div>'
        f"</div></div></section>"
    )
