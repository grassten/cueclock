"""Instant replay: save the OBS buffer, seek to the anchor, show it, come back.

The whole feature rests on one observation. The replay buffer holds the last
``RecRBTime`` seconds of **program output** — so a replay is a pixel-exact
rewind of whatever was on the TV, and the interesting part of it is always at
the *end*, a couple of seconds before the press. Playing such a file from the
start means twenty seconds of someone chalking.

So playback seeks. The vision core already timestamps every ShotTaken to about
20 ms of contact (`design.md` §2.1, checkpoint 11), the server reports how long
ago that was (`GET /api/replay_anchors`), and the file ends at the save — which
makes the cue point arithmetic::

    cursor = duration - anchor_age + display_lag - lead

**Two spans, one key** (2026-08-05). A tapped Replay key anchors on the last
shot; a held one anchors on the start of the rack (`Span`), which is the same
arithmetic against the other age the server reports. Nothing else differs
except the ceiling: a shot replay is capped at ``MAX_PLAY_S``, a rack replay is
allowed the whole file, because "the whole rack" is exactly the request to lift
that cap. What makes a rack fit at all is an OBS setting rather than code —
`RecRBTime` has to be long enough to hold one (`scripts/setup_obs_replay.py`
sets it), and 20 s never was.

No cue-stick detection is involved, and deliberately so: the detector discards
the stick/arm blob by design (it is the oversized occluder), so finding it
would be new detection work with no ground truth — to answer a question the
shot timestamp already answers. It would also answer the *wrong* question. The
stick appears at address, which can be ten seconds of standing still; what you
want to see is the moment before contact, which is this anchor minus a lead.

Nothing is cropped or zoomed on the way to the TV. In the live layout the
overhead is a ~586x1080 portrait strip already downscaled to 57%, so cropping
it out and filling a 16:9 canvas would be a 3.3x upscale of a downscale into a
mismatched aspect. The composed frame is the only pixel-native thing in the
buffer, and it carries both angles of the shot. A full-screen overhead replay
is available any time by making the overhead the main *live* view — the buffer
only ever holds what was on the TV.

One thing the badge does is about the *burned-in* live overlay rather than the
footage: it fades to black at the end, so the return to live is a visible event
rather than a cross-fade between two pictures that look the same. That is
explained where it is implemented — the `/replay` page.

It used to paint the shot clock out too, and no longer does (checkpoint 60).
The mask was correct — it measured the clock's rect from OBS at every press, so
it could not drift off the thing it hid — but on the wall it read as damage: a
197x195 black rectangle punched into the side-angle footage, which is the live
clock's own square with its border and its digits deleted. Steven called it
2026-08-03, and removing it is the interim answer, not the fix. The consequence
is the old one: the burned-in countdown is back in every replay, still counting
a countdown that isn't real. The fix is to stop recording the clock over live
video in the first place — the buffer holds program output, so a clean replay
means a clean *program*, which means moving the clock onto flat backdrop in the
live scene (or out of OBS entirely). `hardware.md` §C4a keeps the options.

The pure part (`cue_point`) is unit-tested; the rest is a sequence of
obs-websocket calls that only a live OBS can exercise.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum

from cueclock.deck import client, obsws

# ---------- the OBS objects this feature owns (scripts/setup_obs_replay.py) ----------

SCENE_NAME = "Replay"
MEDIA_INPUT = "Replay Media"
BADGE_INPUT = "Replay Badge"


class Span(Enum):
    """How far back a press reaches — the two things there are to watch.

    The value is the field of `GET /api/replay_anchors` that anchors it, so a
    span is a lookup rather than a branch, and a renamed field fails at the one
    place that reads it.
    """

    SHOT = "shot_age_s"  # the last shot: a tap
    RACK = "rack_age_s"  # this rack from its break: a hold


# ---------- measured / chosen constants ----------

BUFFER_TARGET_S = 600.0
"""How much program output OBS should keep, in seconds — the setting a rack
replay lives or dies by, and the one thing this feature needs that is not code.

`scripts/setup_obs_replay.py` writes it into the active profile's `RecRBTime`;
it is stated here because it is a *replay* decision, not a setup-script one.

**Ten minutes, sized for the worst rack rather than the average one** (Steven,
2026-08-05: *just in case there's a safety battle and a long rack*). A rack that
outruns the buffer does not fail — the cue point clamps to the file's start —
it just quietly begins mid-rack, which is the one failure here that looks like
the feature working. Five minutes covers a normal rack; a safety battle is
exactly the rack worth replaying, so the buffer is cut for that.

The cost is linear and it is disk, not RAM: measured 2026-08-05, program output
runs **0.55-0.66 MB/s** (`ffprobe` on real saves: 12.2 MB / 18.4 s, 43 MB /
77.6 s), so ten minutes is ~400 MB held in OBS and **~400 MB written to
`~/Movies` on every press**. Twenty presses in a session is 8 GB. Nothing prunes
that directory — worth a look after a long night.

Raising it does not change a tapped press: `MAX_PLAY_S` caps that from the end
of the file, whatever the file's length."""

LEAD_S = 1.5
"""How much runway before the shot. Enough to watch the stroke land, short
enough to skip the address. The seek then lands on the preceding keyframe, so
what you actually get is 1.5-3.5 s — see ``SEEK_SNAP_S``."""

DISPLAY_LAG_S = 0.1
"""Camera -> Syphon -> OBS lag (`hardware.md` §B3 budgets ~100 ms end to end).
The detector's timestamp is when the *vision core* saw the shot; the recording
shows it this much later, so the seek lands where the video is."""

MAX_PLAY_S = 20.0
"""Ceiling on a *shot* replay. A stale anchor (no shot for a minute, then a
press) must not strand the TV staring at the whole buffer.

A rack replay lifts this to the file's own length instead — that request is
precisely "show me more than the last shot", so the cap that protects a tap
would be the bug in a hold. What bounds a rack replay is the buffer
(`BUFFER_TARGET_S`) and the Replay key itself, which says BACK throughout."""

MIN_PLAY_S = 2.0
"""Floor. Pressing the instant the balls stop should still show something."""

SPEED = 1.0
"""Playback rate. 1.0 because OBS records at 30 fps and half speed from 30 fps
is frame-doubled mush — real slow-mo needs OBS set to 60 fps first, at which
point this becomes 0.5 and the setup script's `speed_percent` follows it.

Fast-forward has no such problem (dropping frames needs no frames that were
never recorded), so a rack replay at 2x is one per-press `speed_percent` away —
but it is *unverified* whether OBS honours that setting on a hardware-decoded
media source, and getting it wrong is not cosmetic: `play_s` would be half the
real airtime and the deck would cut back to live mid-rack. So it stays 1.0
until someone watches a 2x replay end where the arithmetic says it will."""

SAVE_TIMEOUT_S = 8.0
"""How long to wait for OBS to write the file.

Was 3.0 against a 20 s buffer (~12 MB). A rack-length buffer writes ~200 MB per
press, so the ceiling grew with it — the wait is bounded by a poll for a
changed path, and only a save that never lands ever pays the whole timeout."""
LOAD_TIMEOUT_S = 2.0  # how long to wait for the media source to report a duration
SEEK_TIMEOUT_S = 1.5  # how long to keep re-asking for a seek that hasn't landed
POLL_S = 0.1

BADGE_PAINT_S = 0.7
"""How long before the cut the badge is handed its URL, so it has pixels by then.

Both halves of this were measured against the live rig on 2026-08-02, and they
are *different instants*, which is the whole subtlety:

  * the page's own clock starts **0.03-0.05 s** after `SetInputSettings` (timed
    backwards from when its blackout completed, three trials);
  * its first frame reaches OBS **0.567-0.570 s** after that (three trials,
    repeatable to 3 ms).

So a badge cut to instantly is invisible for half a second — half a second of
naked, unbadged replay on the wall — while a badge given time to paint is
already half a second into its own animation. Waiting fixes the first and
`badge_url` compensates for the second by handing the page a timeline that
includes the head start. Rounded up from 0.57: overrunning only means the badge
begins a little further along, while underrunning cuts to a blank page.

The wait overlaps the seek, so a press costs less than this in practice."""

SEEK_SNAP_S = 4.0
"""How far *before* the target a seek may legitimately land.

A media source seeks to a **keyframe**, so it always lands early. Measured on
real replay files 2026-08-02 (`ffprobe`): keyframes every 1.90-1.97 s, so the
worst case is one full interval. This is 2x that — the tolerance only has to
separate "landed near the target" from "never moved" (targets sit 13-19 s into
a 20 s file, a dropped seek leaves the cursor near 0), and a tight margin would
start refusing good seeks the day OBS's encoder preset changes.

Landing early is the harmless direction: extra runway before the shot, never
less. It does mean the **effective lead is 1.5-3.5 s**, not a flat `LEAD_S` —
so lowering `LEAD_S` alone cannot tighten the framing past the keyframe grid."""


@dataclass(frozen=True)
class CuePoint:
    cursor_s: float  # where in the file playback starts
    play_s: float  # how long it runs on the TV, at SPEED


@dataclass(frozen=True)
class Playing:
    """A replay currently on the TV: what to go back to, and when."""

    live_scene: str
    until: float  # monotonic deadline for the automatic return


def cue_point(
    duration_s: float,
    anchor_age_s: float | None,
    *,
    lead_s: float = LEAD_S,
    display_lag_s: float = DISPLAY_LAG_S,
    max_play_s: float = MAX_PLAY_S,
    min_play_s: float = MIN_PLAY_S,
    speed: float = SPEED,
) -> CuePoint:
    """Where to start playing a just-saved replay, given its anchor's age.

    ``anchor_age_s`` is measured at the save, and the file ends at the save, so
    the anchor sits ``anchor_age_s`` from the end — plus ``display_lag_s``,
    because the recording lags the detector. The anchor is the last shot for a
    tapped press and the rack's break for a held one; the arithmetic cannot
    tell them apart, which is why there is only one of it.

    No anchor this session, or one older than the buffer, plays the whole
    file (capped) rather than failing: a press always shows something.
    """
    if anchor_age_s is None:
        cursor = duration_s - max_play_s
    else:
        cursor = duration_s - anchor_age_s + display_lag_s - lead_s
    cursor = min(cursor, duration_s - min_play_s)  # always some footage left
    cursor = max(cursor, duration_s - max_play_s)  # never more than the ceiling
    cursor = max(0.0, cursor)  # never before the file starts
    return CuePoint(cursor_s=cursor, play_s=(duration_s - cursor) / speed)


def badge_url(base: str, badge_s: float) -> str:
    """The `/replay` page's URL for one press: how long it has to run.

    ``badge_s`` is the page's own timeline, which is longer than the replay's
    airtime: its clock starts when this URL is set and the TV only sees it
    ``BADGE_PAINT_S`` later. Handing it the airtime instead would drain the bar
    and finish the fade to black that much before the deck cuts back.
    """
    return f"{base}/replay?s={badge_s:.1f}"


def _await_new_replay(before: str | None, deadline: float) -> str | None:
    """Poll until OBS reports a replay path it did not report before.

    `SaveReplayBuffer` returns when OBS *accepts* the command, not when the
    file exists, so a changed path is the only honest completion signal.
    """
    while time.monotonic() < deadline:
        path = obsws.last_replay_path()
        if path is not None and path != before:
            return path
        time.sleep(POLL_S)
    return None


def _await_duration(deadline: float) -> float | None:
    """Poll the media source until it has the file open and knows its length."""
    while time.monotonic() < deadline:
        status = obsws.media_status(MEDIA_INPUT)
        if status is not None:
            return status[1]
        time.sleep(POLL_S)
    return None


def _seek(target_s: float, deadline: float) -> bool:
    """Seek, then prove it landed — re-asking until it does.

    A media source that has only just reported its duration is not yet ready to
    seek, and OBS **drops the request silently** rather than failing it
    (measured 2026-08-02 against OBS 32: honoured on the second attempt, ~70 ms
    after the duration first appeared; a burst of un-spaced attempts inside that
    window all vanished). An unverified seek is the worst failure this feature
    has, because it is invisible: the replay simply plays from the top, showing
    twenty seconds of chalking, and nothing anywhere says the seek was lost.

    Landing early is fine and expected — see ``SEEK_SNAP_S``.
    """
    while True:
        obsws.set_media_cursor(MEDIA_INPUT, target_s)
        status = obsws.media_status(MEDIA_INPUT)
        if status is not None and status[0] >= target_s - SEEK_SNAP_S:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(POLL_S)


def show(base: str, *, span: Span = Span.SHOT, now: float | None = None) -> Playing | None:
    """Save the buffer and cut to it on the TV. None if anything failed.

    ``span`` picks which anchor to seek to — the last shot (tap) or the start
    of the rack (hold). Everything downstream of the cue point is identical:
    one file, one seek, one badge, one way back.

    Every failure leaves the program scene alone: an unreachable OBS, a missing
    Replay scene or a save that never lands means the TV keeps showing live,
    which is always the safe state for a key pressed mid-session.
    """
    live_scene = obsws.current_scene()
    if live_scene is None:
        print("replay: OBS unreachable", flush=True)
        return None
    if live_scene == SCENE_NAME:
        # Already in the Replay scene with no handle — a previous run left it
        # there (daemon restart mid-replay). Nothing sane to return to.
        print(f"replay: already in {SCENE_NAME}", flush=True)
        return None

    # Read the anchor before saving, so its age matches the file's end. Its own
    # request rather than the 2 s state poll: two seconds of staleness is two
    # seconds of the replay starting in the wrong place.
    payload = client.get_json(base, "/api/replay_anchors")
    raw_age = payload.get(span.value) if payload else None
    anchor_age_s = float(raw_age) if isinstance(raw_age, int | float) else None

    before = obsws.last_replay_path()
    if not obsws.save_replay_buffer():
        return None
    path = _await_new_replay(before, time.monotonic() + SAVE_TIMEOUT_S)
    if path is None:
        print("replay: no new file from the buffer", flush=True)
        return None

    # Loading the file starts it playing while the scene is still inactive,
    # which is what lets the seek land before anything reaches the TV. It
    # depends on the source having restart_on_activate off — see the setup
    # script, which would otherwise rewind us to zero at the scene switch.
    if not obsws.set_input_settings(MEDIA_INPUT, {"local_file": path}):
        return None
    duration = _await_duration(time.monotonic() + LOAD_TIMEOUT_S)
    if duration is None:
        print(f"replay: {MEDIA_INPUT} never reported a duration", flush=True)
        return None

    # A held press asks for more than the last shot, so the ceiling that
    # protects a tap becomes the file itself — the buffer is the only bound
    # left, and the key says BACK for the whole time it runs.
    cue = cue_point(
        duration,
        anchor_age_s,
        max_play_s=MAX_PLAY_S if span is Span.SHOT else duration,
    )

    # Start the badge loading now — before the seek, whose round trips it can
    # hide behind — and reload it explicitly. The source deliberately does not
    # restart on activate (see the setup script): a reload triggered by the cut
    # would leave OBS showing the *previous* replay's last frame for the half
    # second it takes to load, which is a fully faded-to-black one.
    badge = badge_url(base, cue.play_s + BADGE_PAINT_S)
    obsws.set_input_settings(BADGE_INPUT, {"url": badge})
    obsws.refresh_browser(BADGE_INPUT)
    painted = time.monotonic() + BADGE_PAINT_S

    if cue.cursor_s > 0.0 and not _seek(cue.cursor_s, time.monotonic() + SEEK_TIMEOUT_S):
        # Say so rather than cutting anyway: a replay that quietly plays from
        # the top reads as the lead being wrong, not as the seek being lost.
        print(f"replay: seek to {cue.cursor_s:.1f}s never landed", flush=True)
        return None

    time.sleep(max(0.0, painted - time.monotonic()))
    if not obsws.set_scene(SCENE_NAME):
        return None

    anchor = "no anchor" if anchor_age_s is None else f"{span.name.lower()} {anchor_age_s:.1f}s ago"
    print(
        f"replay: {anchor}, {duration:.1f}s file, "
        f"cue {cue.cursor_s:.1f}s, {cue.play_s:.1f}s on air",
        flush=True,
    )
    started = time.monotonic() if now is None else now
    return Playing(live_scene=live_scene, until=started + cue.play_s)


def stop(playing: Playing) -> bool:
    """Cut back to the scene that was live when the replay started."""
    return obsws.set_scene(playing.live_scene)
