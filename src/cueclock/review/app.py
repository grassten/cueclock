"""The review page: a session's candidate moments, their footage, right/wrong (#57).

Always up under its own launchd agent (`scripts/com.cueclock.review.plist.
template`), on its own port, with no tie to `run` or the camera — Steven
opens it from the couch on his phone after a session.

A session is a list of moments, one row each, and one of them is open: the
first that has no verdict yet. An open moment is a sentence saying what the
clock did, a clip that is exactly the moment (it plays from its first frame
to its last, and a mark on the strip under it shows the instant that was
flagged), the two verdict buttons, and the journal lines around it. Saving
closes it and opens the next one below.

The page states what the journal recorded and shows the footage; it never
says whether the clock was right. That is Steven's call, and the only reason
the page exists.

It writes two things into a session directory: `verdicts.json`, and the cut
of a moment whose card is opened before the archive pass has made it (he
reviews minutes after a session; the pass gets there later). Served files
are limited to the sessions' own cuts and the recordings listed in
`recordings.jsonl`, addressed by stamp, moment id and line number — never by
a path from the URL.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Sequence
from dataclasses import asdict
from datetime import datetime, timedelta
from html import escape
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from cueclock import archive
from cueclock.review import moments as finder
from cueclock.review import sessions as sess
from cueclock.review.moments import session_journal

DEFAULT_PORT = 8001

# Cuts one moment into a session directory: `Maintainer.cut_one`, or a fake.
Cutter = Callable[[Path, Any, Sequence[archive.Recording]], bool]

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

# What a row calls each kind of moment (`moments.py` lists the kinds). The
# summary sentence under it carries the detail; this is the name in the list.
_TITLES = {
    "reverted": "Not counted as a shot",
    "phantom": "Possible phantom shot",
    "correction": "You corrected the clock",
    "idle_strike": "Strike with the clock idle",
    "synthesized": "Watchdog released the clock",
    "list_scratch": "Scratch called by the ball list",
    "list_cleared": "Scratch cleared by the ball list",
    "placing": "Ball in hand: placing not counted",
    "in_hand_shot": "Ball in hand ended on a shot",
    "cue_only": "Shot counted, only the cue moved",
    "no_change": "Shot counted, cue ball never moved",
    "no_strike": "Shot counted, no strike seen",
    "quiet": "Clock ran with nothing seen",
    "replay": "Replay disagrees with the session",
}

# The clock's state in a word. MOTION is the clock frozen while balls roll,
# and "frozen" is what the summaries call it.
_STATES = {"MOTION": "frozen"}
_NO_COUNT = {"IDLE", "EXPIRED"}  # states whose `remaining` is not a countdown


def title(kind: str) -> str:
    return _TITLES.get(kind, kind.replace("_", " ").capitalize())


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
    return text


def clock_after(record: dict[str, object]) -> str:
    """The clock as the record left it: "running 8 s", "frozen 5 s", "idle"."""
    state, remaining = record.get("state"), record.get("remaining")
    if state is None:
        return ""
    word = _STATES.get(str(state), str(state).lower())
    if isinstance(remaining, int | float) and str(state) not in _NO_COUNT:
        word += f" {remaining:.0f} s"
    return word


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
        --right:#1f7a46; --wrong:#b3261e; --flag:#b25e00; --tint:#0000000d; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#0e1013; --fg:#eceae6; --dim:#9aa0a8; --card:#181b20; --line:#2b2f36;
          --right:#4cc483; --wrong:#ff6b5e; --flag:#ffb454; --tint:#ffffff14; } }
* { box-sizing:border-box; }
html { -webkit-text-size-adjust:100%; }
body { margin:0; background:var(--bg); color:var(--fg);
       font:16px/1.4 -apple-system, system-ui, sans-serif; }
main { max-width:720px; margin:0 auto; padding:12px 12px 48px; }
h1 { font-size:1.3rem; margin:.3rem 0 .2rem; }
a { color:inherit; }
.dim { color:var(--dim); }
.top { display:flex; justify-content:space-between; align-items:center; gap:12px; margin:0; }
.top a { text-decoration:none; padding:6px 0; }
.top a.pill { border:1px solid var(--line); border-radius:99px; padding:6px 14px;
              background:var(--card); }
#progress { margin:0 0 6px; font-size:.9rem; }
.bar { height:4px; border-radius:2px; background:var(--line); overflow:hidden; margin:0 0 12px; }
.bar i { display:block; height:100%; width:0; background:var(--right); transition:width .2s; }

/* The session list. */
.row { display:flex; justify-content:space-between; gap:12px; align-items:baseline;
       text-decoration:none; background:var(--card); border:1px solid var(--line);
       border-radius:12px; padding:14px; margin:0 0 8px; }
.badge { font-size:.8rem; padding:2px 8px; border-radius:99px; border:1px solid var(--line);
         white-space:nowrap; color:var(--dim); }
.badge.open { border-color:var(--flag); color:var(--flag); }

/* One moment: a row, and when open, everything needed to judge it. */
.card { background:var(--card); border:1px solid var(--line); border-radius:12px;
        margin:0 0 8px; }
.card > summary { list-style:none; display:block; padding:12px; cursor:pointer;
                  -webkit-tap-highlight-color:transparent; -webkit-user-select:none;
                  user-select:none; }
.card > summary::-webkit-details-marker { display:none; }
/* The row's layout is on a div inside: older Safari ignores flex on a summary. */
.head-row { display:flex; align-items:flex-start; gap:10px; }
.mark { flex:none; width:22px; height:22px; border-radius:50%; border:2px solid var(--line);
        display:grid; place-items:center; font-size:13px; font-weight:700; line-height:1;
        color:var(--card); margin-top:1px; }
.card.right .mark { border-color:var(--right); background:var(--right); }
.card.right .mark::before { content:"\\2713"; }
.card.wrong .mark { border-color:var(--wrong); background:var(--wrong); }
.card.wrong .mark::before { content:"\\2715"; }
.what { flex:1; min-width:0; }
.what b { display:block; font-weight:600; }
.given { display:-webkit-box; -webkit-box-orient:vertical; -webkit-line-clamp:2;
         overflow:hidden; font-size:.9rem; color:var(--dim); }
.card[open] .given { display:block; }
.verdict-text { font-weight:600; }
.card.right .verdict-text { color:var(--right); }
.card.wrong .verdict-text { color:var(--wrong); }
.note-text:not(:empty)::before { content:" \\2014  "; }
.when { flex:none; color:var(--dim); font-size:.9rem; font-variant-numeric:tabular-nums;
        margin-top:2px; }
.body { padding:0 12px 12px; }
.say { margin:0 0 10px; }

.clip video, video.full { display:block; width:100%; aspect-ratio:16/9; background:#000;
                          border-radius:8px; }
.strip { position:relative; height:28px; margin-top:4px; cursor:pointer;
         touch-action:manipulation; }
.strip::before { content:""; position:absolute; left:0; right:0; top:12px; height:4px;
                 border-radius:2px; background:var(--line); }
.strip i { position:absolute; top:9px; width:2px; height:10px; margin-left:-1px;
           border-radius:1px; background:var(--dim); opacity:.6; }
.strip i.flag { top:4px; height:20px; width:4px; margin-left:-2px; border-radius:2px;
                background:var(--flag); opacity:1; }
.strip i.head { top:2px; height:24px; background:var(--fg); opacity:1;
                transition:left .25s linear; }
.ends { display:flex; justify-content:space-between; font-size:.8rem; color:var(--dim);
        font-variant-numeric:tabular-nums; }
.ends b { font-weight:400; color:var(--flag); }
.oops { display:none; margin:8px 0 0; }
.clip.failed .oops { display:block; }

.buttons { display:flex; gap:8px; margin-top:10px; }
button { font:inherit; padding:12px; border-radius:10px; cursor:pointer;
         border:2px solid var(--line); background:transparent; color:var(--fg);
         touch-action:manipulation; }
.pick { flex:1; font-weight:600; }
.pick.right.on { border-color:var(--right); background:var(--right); color:var(--card); }
.pick.wrong.on { border-color:var(--wrong); background:var(--wrong); color:var(--card); }
.compose { display:none; gap:8px; margin-top:8px; align-items:flex-end; }
.card.chosen .compose { display:flex; }
textarea { flex:1; min-width:0; font:inherit; padding:11px 12px; border-radius:10px;
           border:2px solid var(--line); background:transparent; color:var(--fg);
           resize:none; max-height:40vh; }
.save { flex:none; padding:12px 18px; font-weight:600; background:var(--fg);
        color:var(--bg); border-color:var(--fg); }
.save:disabled { opacity:.5; }

.hint { margin:14px 0 4px; font-size:.8rem; color:var(--dim); }
.events { list-style:none; margin:0; padding:0; font-size:.88rem;
          border-top:1px solid var(--line); }
.events li { display:flex; gap:8px; align-items:baseline; padding:7px 4px;
             border-bottom:1px solid var(--line); }
.events li[data-t] { cursor:pointer; }
.events .at { flex:none; width:3.7em; text-align:right; color:var(--dim);
              font-variant-numeric:tabular-nums; }
.events .ev { flex:1; min-width:0; }
.events .clk { flex:none; color:var(--dim); white-space:nowrap;
               font-variant-numeric:tabular-nums; }
.events li.here .ev, .events li.moment .ev { font-weight:600; }
.events li.here .at { color:var(--flag); }
.events li.now { background:var(--tint); }
.links { display:flex; justify-content:space-between; gap:12px; margin:10px 0 0;
         font-size:.9rem; }
.links a { padding:4px 0; }
.card:not(.right):not(.wrong) .clear { display:none; }
#tail { display:flex; align-items:center; justify-content:space-between; gap:12px;
        margin:14px 2px 0; min-height:44px; }
#tail button { padding:8px 14px; }

/* The full-session player: the video stays put, the event list scrolls. */
video.full { position:sticky; top:0; z-index:1; }
.events.all { position:relative; max-height:45vh; overflow-y:auto; margin:8px 0 16px; }
"""

_SCRIPT = """
(() => {
'use strict';
const LEAD = 2;  // seconds of run-up before a journal line that was tapped
const unsaved = () => alert('Not saved — is the review server still up?');
const play = v => { const p = v.play(); if (p) p.catch(() => {}); };
// A closed moment's video has no address yet (`data-src`), so no browser can
// fetch it, or have the server cut it, before the moment is opened.
const load = v => { if (v && !v.getAttribute('src')) v.src = v.dataset.src; };
// The journal line the video has reached: the last one at or before `t`.
const reached = (lines, t) => {
  let now = null;
  for (const li of lines) if (parseFloat(li.dataset.t) <= t + 0.1) now = li;
  return now;
};

// ---- a session's moments ---------------------------------------------------
const cards = [...document.querySelectorAll('details.card')];
const judged = c => !!c.dataset.verdict;
const tally = () => {
  const done = cards.filter(judged).length, left = cards.length - done;
  const line = document.getElementById('progress');
  if (line) line.textContent = `${done} of ${cards.length} reviewed`;
  const bar = document.querySelector('.bar i');
  if (bar) bar.style.width = (cards.length ? 100 * done / cards.length : 0) + '%';
  const tail = document.getElementById('tail');
  if (tail) {
    tail.querySelector('span').textContent =
      left ? `${left} still to review` : `All ${cards.length} reviewed`;
    tail.querySelector('button').hidden = !left;
  }
};
// An opened moment goes to the top of the screen, with the row before it
// still showing above: after a save, that row is the verdict just given.
// A jump, not a glide: the list has just changed shape under the finger, and
// an animated scroll is one more thing a closing keyboard can interrupt.
const reveal = card => {
  const prev = card.previousElementSibling;
  const lead = prev && prev.matches('details.card') ? prev.offsetHeight + 16 : 8;
  window.scrollTo(0, Math.max(0, card.getBoundingClientRect().top + window.scrollY - lead));
};

cards.forEach(card => {
  const video = card.querySelector('video');
  const note = card.querySelector('textarea');
  const save = card.querySelector('.save');
  const picks = [...card.querySelectorAll('.pick')];
  let wasOpen = card.open;

  const pick = v => {
    card.dataset.pick = v;
    card.classList.toggle('chosen', !!v);
    picks.forEach(b => b.classList.toggle('on', b.value === v));
  };
  const grow = () => { note.style.height = 'auto';
                       note.style.height = (note.scrollHeight + 4) + 'px'; };
  const post = verdict => fetch(card.dataset.post, {method: 'POST',
      headers: {'content-type': 'application/json'},
      body: JSON.stringify({verdict, note: note.value})})
    .then(r => { if (!r.ok) throw new Error(r.status); return r.json(); });
  // The row, as the server now holds it.
  const settle = v => {
    card.dataset.verdict = v.verdict || '';
    card.classList.toggle('right', v.verdict === 'right');
    card.classList.toggle('wrong', v.verdict === 'wrong');
    card.querySelector('.verdict-text').textContent = v.verdict ? 'Clock ' + v.verdict : '';
    card.querySelector('.note-text').textContent = v.verdict && v.note ? v.note : '';
    tally();
  };
  const ready = () => { pick(card.dataset.pick || card.dataset.verdict || ''); grow();
                        load(video); };

  // One moment open at a time; its video is fetched when it opens.
  card.addEventListener('toggle', () => {
    if (card.open === wasOpen) return;
    wasOpen = card.open;
    if (!card.open) { if (video) video.pause(); return; }
    cards.forEach(c => { if (c !== card) c.open = false; });
    ready();
    reveal(card);
  });
  picks.forEach(b => b.addEventListener('click', () => {
    pick(b.value); grow(); note.focus();
  }));
  note.addEventListener('input', grow);
  save.addEventListener('click', () => {
    if (!card.dataset.pick) return;
    save.disabled = true;
    post(card.dataset.pick).then(v => {
      settle(v);
      note.blur();
      // On to the next open moment below this one. Never back up the page:
      // a moment skipped on purpose stays skipped until the list's end.
      const next = cards.slice(cards.indexOf(card) + 1).find(c => !judged(c));
      card.open = false;
      if (next) next.open = true; else reveal(card);
    }).catch(unsaved).finally(() => { save.disabled = false; });
  });
  card.querySelector('.clear').addEventListener('click', e => {
    e.preventDefault();
    post(null).then(v => { note.value = ''; pick(''); grow(); settle(v); }).catch(unsaved);
  });

  if (video) {
    const clip = card.querySelector('.clip');
    const strip = clip.querySelector('.strip');
    const head = clip.querySelector('.head');
    const lines = [...card.querySelectorAll('.events li[data-t]')];
    const length = () => parseFloat(clip.dataset.len) || video.duration || 0;
    const paint = () => {
      const t = video.currentTime, len = length();
      head.style.left = (len ? Math.max(0, Math.min(1, t / len)) * 100 : 0) + '%';
      const now = video.played.length ? reached(lines, t) : null;
      lines.forEach(li => li.classList.toggle('now', li === now));
    };
    const jump = t => { video.currentTime = Math.max(0, Math.min(t, length() - 0.2));
                        play(video); };
    ['timeupdate', 'seeked', 'loadedmetadata'].forEach(e => video.addEventListener(e, paint));
    video.addEventListener('error', () => clip.classList.add('failed'));
    strip.addEventListener('click', e => {
      const box = strip.getBoundingClientRect();
      jump((e.clientX - box.left) / box.width * length());
    });
    lines.forEach(li => li.addEventListener('click', () => {
      jump(parseFloat(li.dataset.t) - LEAD);
      video.scrollIntoView({block: 'nearest'});
    }));
  }
  if (card.open) ready();
});

if (cards.length) {
  if ('scrollRestoration' in history) history.scrollRestoration = 'manual';
  tally();
  document.querySelector('#tail button').addEventListener('click', () => {
    const c = cards.find(c => !judged(c));
    if (c && c.open) reveal(c); else if (c) c.open = true;
  });
  // A link to one moment (/s/<stamp>#<id>) opens it; otherwise the page
  // starts on the first open one, scrolled to only if it is far down.
  const linked = location.hash && document.getElementById(location.hash.slice(1));
  const open = cards.find(c => c.open);
  if (linked && cards.includes(linked) && linked !== open) linked.open = true;
  else if (open && open.getBoundingClientRect().top > window.innerHeight * 0.4) reveal(open);
}

// ---- the full-session player ----------------------------------------------
document.querySelectorAll('video.full').forEach(video => {
  const list = document.querySelector(`ol[data-video="${video.id}"]`);
  const lines = [...list.querySelectorAll('li[data-t]')];
  let now = null, touched = 0;
  ['touchstart', 'wheel'].forEach(e =>
    list.addEventListener(e, () => { touched = Date.now(); }, {passive: true}));
  const paint = () => {
    const cur = reached(lines, video.currentTime);
    if (cur === now) return;
    if (now) now.classList.remove('now');
    if (cur) cur.classList.add('now');
    now = cur;
    // The list follows the video, unless it was just scrolled by hand.
    if (cur && !video.paused && Date.now() - touched > 6000)
      list.scrollTop = cur.offsetTop - list.clientHeight / 3;
  };
  ['timeupdate', 'seeked'].forEach(e => video.addEventListener(e, paint));
  lines.forEach(li => li.addEventListener('click', () => {
    video.currentTime = Math.max(0, parseFloat(li.dataset.t) - LEAD);
    play(video);
  }));
});
// A moment's "full session" link lands on its video at its time: #v<n>=<s>.
const at = /^#v(\\d+)=([\\d.]+)$/.exec(location.hash);
const target = at && document.getElementById('full' + at[1]);
if (target) {
  const go = () => { target.currentTime = parseFloat(at[2]); };
  if (target.readyState) go(); else target.addEventListener('loadedmetadata', go, {once: true});
}
})();
"""


def _page(title_text: str, body: str, script: bool = False) -> HTMLResponse:
    js = f"<script>{_SCRIPT}</script>" if script else ""
    return HTMLResponse(
        "<!doctype html><html lang=en><head><meta charset=utf-8>"
        '<meta name=viewport content="width=device-width, initial-scale=1">'
        f"<title>{escape(title_text)}</title><style>{_STYLE}</style></head>"
        f"<body><main>{body}</main>{js}</body></html>"
    )


def create_app(
    roots: list[Path] | None = None,
    local_root: Path = sess.LOCAL_ROOT,
    logs_dir: Path = Path("logs"),
    cutter: Cutter | None = None,
) -> FastAPI:
    """`roots` defaults to the local root, then the T7 archive; `cutter` to
    the archive pass's own `cut_one` (ffmpeg)."""
    session_roots = roots if roots is not None else [local_root, archive.ARCHIVE_ROOT]
    cut_one = cutter if cutter is not None else archive.Maintainer().cut_one
    cutting = threading.Lock()  # one ffmpeg at a time, and one per cut
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
                f'<a class="row" href="/s/{s.stamp}"><span>{_local(s.started)}'
                f' <span class="dim">{s.stamp}</span></span>'
                f'<span class="badge{" open" if open_ else ""}">{badge}</span></a>'
            )
        missing = [str(r) for r in session_roots if not r.is_dir()]
        note = f'<p class="dim">Not reachable: {escape(", ".join(missing))}</p>' if missing else ""
        body = "".join(rows) or '<p class="dim">No sessions yet.</p>'
        return _page("Session review", f"<h1>Sessions</h1>{note}{body}")

    @app.get("/s/{stamp}", response_class=HTMLResponse)
    async def session_page(stamp: str) -> HTMLResponse:
        s = session(stamp)
        moments = moments_of(s)
        records = _records(session_journal(s.path, logs_dir))
        full = full_sources(s, records)
        watch = f'<a class="pill" href="/s/{s.stamp}/full">▶ Full session</a>' if full else ""
        head = (
            f'<p class="top"><a href="/">&lsaquo; Sessions</a>{watch}</p>'
            f"<h1>{_local(s.started)}</h1>"
        )
        if moments is None:
            return _page(stamp, head + '<p class="dim">This session has no journal to read.</p>')
        if not moments:
            return _page(stamp, head + '<p class="dim">Nothing in this session needs a look.</p>')
        verdicts = s.verdicts()
        sources = sess.clip_sources(s, local_root)
        # Open on arrival: the first moment still to judge.
        first = next((m.get("id") for m in moments if str(m.get("id")) not in verdicts), None)
        cards = [
            _card(
                s, m, verdicts.get(str(m.get("id"))), records, sources, full, m.get("id") == first
            )
            for m in moments
        ]
        judged = sum(1 for m in moments if str(m.get("id")) in verdicts)
        head += (
            f'<p id="progress" class="dim">{judged} of {len(moments)} reviewed</p>'
            f'<div class="bar"><i style="width:{100 * judged / len(moments):.0f}%"></i></div>'
        )
        tail = (
            '<p id="tail" class="dim"><span></span>'
            "<button hidden>Next to review &rsaquo;</button></p>"
        )
        return _page(stamp, head + "".join(cards) + tail, script=True)

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
        head = (
            f'<p class="top"><a href="/s/{s.stamp}">&lsaquo; Moments</a></p>'
            f"<h1>{_local(s.started)}</h1>"
        )
        if not sources:
            return _page(
                stamp, head + '<p class="dim">No full recording kept for this session.</p>'
            )
        head += '<p class="hint">Tap a line to watch it happen.</p>'
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
                items.append(_line(record, _clock(at), at, mark))
            parts.append(
                f'<video id="{vid}" class="full" {_VIDEO} preload="metadata" src="{url}"></video>'
                f'<ol class="events all" data-video="{vid}">{"".join(items)}</ol>'
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
        if moment_id not in {m.get("id") for m in moments_of(session(stamp)) or []}:
            raise HTTPException(404, "no such moment")
        try:
            payload = await request.json()
        except ValueError as exc:
            raise HTTPException(400, "expected JSON") from exc
        call = payload.get("verdict") if isinstance(payload, dict) else None
        note = payload.get("note", "") if isinstance(payload, dict) else ""
        if call is not None and call not in sess.VERDICTS:
            raise HTTPException(400, f"verdict must be one of {sess.VERDICTS} or null")
        # Looked up again at the write, and once more if the directory went
        # away under it: the archive pass moves a session to the T7 minutes
        # after it ends, which is when it is being judged.
        for _ in range(2):
            s = session(stamp)
            try:
                s.set_verdict(moment_id, call, str(note)[:2000])
            except FileNotFoundError:
                continue
            return JSONResponse({"id": moment_id, **s.verdicts().get(moment_id, {"verdict": None})})
        raise HTTPException(503, "the session was being archived — save again")

    @app.get("/media/{stamp}/{moment_id}.mp4")
    def cut(stamp: str, moment_id: str) -> FileResponse:
        """A moment's cut, made here if the archive pass has not made it yet.

        Not `async`: a missing cut runs ffmpeg (a stream copy, well under a
        second), and that belongs on a worker thread.
        """
        s = session(stamp)
        path = s.cut(moment_id)
        if path is None and sess.MOMENT_ID.match(moment_id):
            moment = next((m for m in moments_of(s) or [] if m.get("id") == moment_id), None)
            with cutting:
                if moment is not None and s.cut(moment_id) is None:
                    cut_one(s.path, moment, sess.clip_sources(s, local_root))
            path = s.cut(moment_id)
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


_VIDEO = "controls playsinline"

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


def _line(record: dict[str, object], at: str, t: float | None, attrs: str = "") -> str:
    """One journal record as a list row: when, what, and the clock after it.
    `t` is where it sits in the video beside it, in seconds; None = no video."""
    seek = f' data-t="{max(0.0, t):.2f}"' if t is not None else ""
    return (
        f'<li{attrs}{seek}><span class="at">{at}</span>'
        f'<span class="ev">{escape(describe(record))}</span>'
        f'<span class="clk">{escape(clock_after(record))}</span></li>'
    )


def _signed(seconds: float, digits: int = 1) -> str:
    """Seconds either side of the moment: "-0.6 s" (with a real minus sign),
    "0.0 s", "+3.1 s"."""
    if round(seconds, digits) == 0:
        return f"{0:.{digits}f} s"
    return f"{seconds:+.{digits}f} s".replace("-", "\u2212")


def _percent(t: float, length: float) -> str:
    return f"{100 * min(1.0, max(0.0, t / length)):.2f}%"


def _clip_span(
    moment: dict[str, object], sources: Sequence[archive.Recording]
) -> tuple[datetime, float] | None:
    """When a moment's cut begins (wall time) and how long it runs, as
    `archive.plan_cut` cuts it; None when nothing is left to cut it from."""
    try:
        plan = archive.plan_cut(moment, sources)
    except (KeyError, ValueError, TypeError):
        return None
    if plan is None:
        return None
    start = next(r.start for r in sources if r.path == plan.source)
    return start + timedelta(seconds=plan.offset_s), plan.duration_s


def _card(
    s: sess.Session,
    moment: dict[str, object],
    given: dict[str, object] | None,
    records: list[dict[str, object]],
    sources: Sequence[archive.Recording],
    full: list[tuple[str, sess.Recording]],
    opened: bool,
) -> str:
    mid = str(moment.get("id"))
    try:
        at = datetime.fromisoformat(str(moment["wall"]))
        start = datetime.fromisoformat(str(moment["start_wall"]))
        end = datetime.fromisoformat(str(moment["end_wall"]))
    except (KeyError, ValueError):
        return ""

    # The clip is the cut, which is the moment's span. A cut on disk whose
    # recording is gone is taken to be the whole span.
    span = _clip_span(moment, sources)
    if span is None and s.cut(mid) is not None:
        span = (start, (end - start).total_seconds())
    if span is not None and span[1] <= 0:
        span = None

    lines, ticks = [], []
    for record in records:
        when = _wall(record)
        if when is None or record.get("kind") in _NOISE or not start <= when <= end:
            continue
        if record.get("kind") == "voice_final" and not record.get("text"):
            continue  # a wordless final (checkpoint 121): mic noise, not something said
        delta = (when - at).total_seconds()
        t = (when - span[0]).total_seconds() if span else None
        here = ' class="here"' if abs(delta) < 0.0005 else ""
        lines.append(_line(record, _signed(delta), t, here))
        if span and t is not None:
            ticks.append(f'<i style="left:{_percent(t, span[1])}"></i>')

    if span:
        clip_start, length = span
        flagged = (at - clip_start).total_seconds()
        clip = (
            f'<div class="clip" data-len="{length:.2f}">'
            # Only the open moment's video is given its address: the others
            # get it when they open, so opening the page fetches (and has the
            # server cut) one clip, not eighteen.
            f'<video {_VIDEO} preload="metadata" {"src" if opened else "data-src"}='
            f'"/media/{s.stamp}/{mid}.mp4#t=0.001"></video>'
            f'<div class="strip">{"".join(ticks)}'
            f'<i class="flag" style="left:{_percent(flagged, length)}"></i>'
            f'<i class="head" style="left:0%"></i></div>'
            f'<div class="ends"><span>{_signed(-flagged, 0)}</span>'
            f"<b>▮ what was flagged</b>"
            f"<span>{_signed(length - flagged, 0)}</span></div>"
            f'<p class="oops dim">This clip would not load.</p></div>'
        )
        hint = "What the clock recorded. Tap a line to watch it happen."
    else:
        clip = (
            '<p class="dim">No footage to show for this moment: its recording is gone, '
            "or still being written.</p>"
        )
        hint = "What the clock recorded."
    journal = f'<p class="hint">{hint}</p><ol class="events">{"".join(lines)}</ol>' if lines else ""

    whole = ""
    for n, (_, rec) in enumerate(full):
        if rec.covers(at):
            offset = max(0.0, (start - rec.start).total_seconds())
            whole = (
                f'<a href="/s/{s.stamp}/full#v{n}={offset:.1f}">'
                "See it in the full session &rsaquo;</a>"
            )
            break

    call = str(given.get("verdict")) if given else ""
    call = call if call in sess.VERDICTS else ""
    note = escape(str(given.get("note", ""))) if given and call else ""
    shown = f"Clock {call}" if call else ""
    classes = "card" + (f" {call}" if call else "")
    return (
        f'<details class="{classes}" id="{mid}" data-verdict="{call}" '
        f'data-post="/api/verdict/{s.stamp}/{mid}"{" open" if opened else ""}>'
        f'<summary><div class="head-row"><span class="mark"></span><span class="what">'
        f"<b>{escape(title(str(moment.get('kind', ''))))}</b>"
        f'<span class="given"><span class="verdict-text">{shown}</span>'
        f'<span class="note-text">{note}</span></span></span>'
        f'<span class="when">{at.astimezone().strftime("%H:%M:%S")}</span></div></summary>'
        f'<div class="body"><p class="say">{escape(str(moment.get("summary", "")))}</p>{clip}'
        f'<div class="buttons">'
        f'<button class="pick right" value="right">Clock right</button>'
        f'<button class="pick wrong" value="wrong">Clock wrong</button></div>'
        f'<div class="compose">'
        f'<textarea rows="1" placeholder="Note (optional): what you saw">{note}</textarea>'
        f'<button class="save">Save</button></div>'
        f"{journal}"
        f'<p class="links"><span>{whole}</span><a href="#" class="clear dim">Clear verdict</a></p>'
        f"</div></details>"
    )
