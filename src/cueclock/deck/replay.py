"""Instant replay: save the OBS buffer, seek to the shot, show it, come back.

The whole feature rests on one observation. The replay buffer holds the last
``RecRBTime`` seconds of **program output** — so a replay is a pixel-exact
rewind of whatever was on the TV, and the interesting part of it is always at
the *end*, a couple of seconds before the press. Playing such a file from the
start means twenty seconds of someone chalking.

So playback seeks. The vision core already timestamps every ShotTaken to about
20 ms of contact (`design.md` §2.1, checkpoint 11), the server reports how long
ago that was (`GET /api/last_shot`), and the file ends at the save — which
makes the cue point arithmetic::

    cursor = duration - shot_age + display_lag - lead

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

The pure part (`cue_point`) is unit-tested; the rest is a sequence of
obs-websocket calls that only a live OBS can exercise.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from cueclock.deck import client, obsws

# ---------- the OBS objects this feature owns (scripts/setup_obs_replay.py) ----------

SCENE_NAME = "Replay"
MEDIA_INPUT = "Replay Media"
BADGE_INPUT = "Replay Badge"

# ---------- measured / chosen constants ----------

LEAD_S = 1.5
"""How much runway before the shot. Enough to watch the stroke land, short
enough to skip the address."""

DISPLAY_LAG_S = 0.1
"""Camera -> Syphon -> OBS lag (`hardware.md` §B3 budgets ~100 ms end to end).
The detector's timestamp is when the *vision core* saw the shot; the recording
shows it this much later, so the seek lands where the video is."""

MAX_PLAY_S = 20.0
"""Ceiling on one replay. A stale anchor (no shot for a minute, then a press)
must not strand the TV staring at the whole buffer."""

MIN_PLAY_S = 2.0
"""Floor. Pressing the instant the balls stop should still show something."""

SPEED = 1.0
"""Playback rate. 1.0 because OBS records at 30 fps and half speed from 30 fps
is frame-doubled mush — real slow-mo needs OBS set to 60 fps first, at which
point this becomes 0.5 and the setup script's `speed_percent` follows it."""

SAVE_TIMEOUT_S = 3.0  # how long to wait for OBS to write the file
LOAD_TIMEOUT_S = 2.0  # how long to wait for the media source to report a duration
POLL_S = 0.1


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
    shot_age_s: float | None,
    *,
    lead_s: float = LEAD_S,
    display_lag_s: float = DISPLAY_LAG_S,
    max_play_s: float = MAX_PLAY_S,
    min_play_s: float = MIN_PLAY_S,
    speed: float = SPEED,
) -> CuePoint:
    """Where to start playing a just-saved replay, given the last shot's age.

    ``shot_age_s`` is measured at the save, and the file ends at the save, so
    the shot sits ``shot_age_s`` from the end — plus ``display_lag_s``, because
    the recording lags the detector.

    No shot this session, or a shot older than the buffer, plays the whole
    file (capped) rather than failing: a press always shows something.
    """
    if shot_age_s is None:
        cursor = duration_s - max_play_s
    else:
        cursor = duration_s - shot_age_s + display_lag_s - lead_s
    cursor = min(cursor, duration_s - min_play_s)  # always some footage left
    cursor = max(cursor, duration_s - max_play_s)  # never more than the ceiling
    cursor = max(0.0, cursor)  # never before the file starts
    return CuePoint(cursor_s=cursor, play_s=(duration_s - cursor) / speed)


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
        duration = obsws.media_duration_s(MEDIA_INPUT)
        if duration is not None:
            return duration
        time.sleep(POLL_S)
    return None


def show(base: str, *, now: float | None = None) -> Playing | None:
    """Save the buffer and cut to it on the TV. None if anything failed.

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
    payload = client.get_json(base, "/api/last_shot")
    raw_age = payload.get("age_s") if payload else None
    shot_age_s = float(raw_age) if isinstance(raw_age, int | float) else None

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

    cue = cue_point(duration, shot_age_s)
    obsws.set_media_cursor(MEDIA_INPUT, cue.cursor_s)
    obsws.set_input_settings(BADGE_INPUT, {"url": f"{base}/replay?s={cue.play_s:.1f}"})
    if not obsws.set_scene(SCENE_NAME):
        return None

    age = "no shot" if shot_age_s is None else f"shot {shot_age_s:.1f}s ago"
    print(
        f"replay: {age}, {duration:.1f}s file, cue {cue.cursor_s:.1f}s, {cue.play_s:.1f}s on air",
        flush=True,
    )
    started = time.monotonic() if now is None else now
    return Playing(live_scene=live_scene, until=started + cue.play_s)


def stop(playing: Playing) -> bool:
    """Cut back to the scene that was live when the replay started."""
    return obsws.set_scene(playing.live_scene)
