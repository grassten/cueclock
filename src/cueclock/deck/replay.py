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

Two things the badge does are about the *burned-in* live overlay rather than
the footage: it paints out the shot clock (which is in the recorded pixels and
keeps counting a countdown that isn't real), and it fades to black at the end
so the return to live is a visible event rather than a cross-fade between two
pictures that look the same. Both are explained where they are implemented —
`clock_masks` and the `/replay` page.

The pure parts (`cue_point`, `clock_masks`) are unit-tested; the rest is a
sequence of obs-websocket calls that only a live OBS can exercise.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass

from cueclock.deck import client, obsws

# ---------- the OBS objects this feature owns (scripts/setup_obs_replay.py) ----------

SCENE_NAME = "Replay"
MEDIA_INPUT = "Replay Media"
BADGE_INPUT = "Replay Badge"

# ---------- measured / chosen constants ----------

LEAD_S = 1.5
"""How much runway before the shot. Enough to watch the stroke land, short
enough to skip the address. The seek then lands on the preceding keyframe, so
what you actually get is 1.5-3.5 s — see ``SEEK_SNAP_S``."""

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

So a badge cut to instantly is invisible for half a second — long enough to
show the burned-in clock unmasked — while a badge given time to paint is
already half a second into its own animation. Waiting fixes the first and
`badge_url` compensates for the second by handing the page a timeline that
includes the head start. Rounded up from 0.57: overrunning only means the badge
begins a little further along, while underrunning cuts to a blank page.

The wait overlaps the seek, so a press costs less than this in practice."""

CLOCK_PAGE = "/shotclock"
"""What identifies the shot clock in an OBS scene: the page it renders.

Not a source name and not a rectangle. The overlay is *whatever browser source
shows this page*, so a renamed source, a second copy or a moved one all stay
covered — which is the objection that kept the patch out of checkpoint 46
("five lines, deliberately skipped as brittle", issue #13). Reading the rect
from the same OBS that composed the recorded frame is what makes it not
brittle: the mask cannot drift from the thing it hides without OBS lying."""

MASK_PAD_PX = 2.0
"""Canvas pixels of slop around a mask, so no seam of the old clock survives
the recording's own scaling and re-encode. One stray pixel row of a live
countdown reads as a bug; two pixels of extra black does not."""

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
class Mask:
    """A rectangle of the replayed frame to paint out, in canvas percent."""

    x: float
    y: float
    w: float
    h: float

    def __str__(self) -> str:
        """The `?mask=` wire form: four percentages, two decimals (~0.2 px)."""
        return f"{self.x:.2f},{self.y:.2f},{self.w:.2f},{self.h:.2f}"


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


# ---------- painting the burned-in shot clock out of the footage ----------

# OBS alignment is a bit field on the *unrotated* box: which of its corners the
# item's position refers to. Neither bit of a pair means centred on that axis.
_ALIGN_LEFT = 1
_ALIGN_RIGHT = 2
_ALIGN_TOP = 4
_ALIGN_BOTTOM = 8


def _as_float(value: object) -> float:
    """A number from an OBS payload, or 0.0 — a field OBS omits is a zero."""
    return float(value) if isinstance(value, int | float) else 0.0


def _anchor_offset(alignment: int, size: float, low: int, high: int) -> float:
    """Where the box starts relative to its position point, on one axis."""
    if alignment & low:
        return 0.0
    if alignment & high:
        return -size
    return -size / 2.0


def item_mask(transform: dict[str, object], canvas_w: float, canvas_h: float) -> Mask | None:
    """The canvas rectangle a scene item covers, as percentages.

    Percentages rather than pixels because the media source fills the canvas
    proportionally: OBS may record at a rescaled output, and a fraction of the
    frame survives that where a pixel offset would not.

    Rotation is handled by covering the rotated box's bounding box. That can
    over-cover a diagonally rotated overlay, which is the safe direction — an
    under-covering mask leaves a sliver of live countdown on the wall, which is
    the exact failure this exists to remove.
    """
    width = _as_float(transform.get("width"))
    height = _as_float(transform.get("height"))
    if width <= 0.0 or height <= 0.0:
        return None

    alignment = int(_as_float(transform.get("alignment")))
    x0 = _anchor_offset(alignment, width, _ALIGN_LEFT, _ALIGN_RIGHT)
    y0 = _anchor_offset(alignment, height, _ALIGN_TOP, _ALIGN_BOTTOM)
    theta = math.radians(_as_float(transform.get("rotation")))
    cos, sin = math.cos(theta), math.sin(theta)
    corners = [
        (x * cos - y * sin, x * sin + y * cos)
        for x, y in ((x0, y0), (x0 + width, y0), (x0, y0 + height), (x0 + width, y0 + height))
    ]
    px = _as_float(transform.get("positionX"))
    py = _as_float(transform.get("positionY"))

    left = max(0.0, px + min(x for x, _ in corners) - MASK_PAD_PX)
    right = min(canvas_w, px + max(x for x, _ in corners) + MASK_PAD_PX)
    top = max(0.0, py + min(y for _, y in corners) - MASK_PAD_PX)
    bottom = min(canvas_h, py + max(y for _, y in corners) + MASK_PAD_PX)
    if right <= left or bottom <= top:
        return None  # entirely off-canvas: none of it reached the recording
    return Mask(
        x=100.0 * left / canvas_w,
        y=100.0 * top / canvas_h,
        w=100.0 * (right - left) / canvas_w,
        h=100.0 * (bottom - top) / canvas_h,
    )


def clock_masks(
    items: list[dict[str, object]],
    canvas: tuple[int, int],
    url_of: Callable[[str], str | None],
) -> list[Mask]:
    """Every rectangle of the live scene that a shot clock was drawn into.

    The replay buffer records *program output*, so the countdown that was on
    the TV is in the recorded pixels — it cannot be switched off after the
    fact, only covered. Hiding the live overlay source would do nothing at all
    to a frame that was recorded while it was showing.

    Only *enabled* browser items count (a hidden overlay was never burned in),
    and only the ones actually rendering the clock page: `url_of` is injected
    so the whole selection stays pure and testable.
    """
    canvas_w, canvas_h = float(canvas[0]), float(canvas[1])
    masks = []
    for item in items:
        if not item.get("sceneItemEnabled", True):
            continue
        if not str(item.get("inputKind") or "").startswith("browser"):
            continue  # only a browser source can be showing the clock page
        name = item.get("sourceName")
        if not isinstance(name, str):
            continue
        url = url_of(name)
        if url is None or CLOCK_PAGE not in url:
            continue
        transform = item.get("sceneItemTransform")
        if not isinstance(transform, dict):
            continue
        mask = item_mask(transform, canvas_w, canvas_h)
        if mask is not None:
            masks.append(mask)
    return masks


def badge_url(base: str, badge_s: float, masks: list[Mask]) -> str:
    """The `/replay` page's URL for one press: its length, and what to cover.

    ``badge_s`` is the page's own timeline, which is longer than the replay's
    airtime: its clock starts when this URL is set and the TV only sees it
    ``BADGE_PAINT_S`` later. Handing it the airtime instead would drain the bar
    and finish the fade to black that much before the deck cuts back.
    """
    url = f"{base}/replay?s={badge_s:.1f}"
    if masks:
        url += "&mask=" + ";".join(str(mask) for mask in masks)
    return url


def _live_masks(scene: str) -> list[Mask]:
    """Ask OBS what the shot clock covers in the scene that was on the TV."""
    canvas = obsws.canvas_size()
    items = obsws.scene_items(scene)
    if canvas is None or items is None:
        # Degrade to checkpoint 46's behaviour rather than refusing the cut: a
        # visible burned-in clock under a REPLAY badge is the old bug, not a
        # new one. Say so, because otherwise it looks like the mask is broken.
        print(f"replay: could not measure {scene!r} — burned-in clock stays visible", flush=True)
        return []
    return clock_masks(items, canvas, obsws.input_url)


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

    # Measure the live scene *before* the anchor read, so nothing sits between
    # that read and the save it has to be simultaneous with.
    masks = _live_masks(live_scene)

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

    # Start the badge loading now — before the seek, whose round trips it can
    # hide behind — and reload it explicitly. The source deliberately does not
    # restart on activate (see the setup script): a reload triggered by the cut
    # would leave OBS showing the *previous* replay's last frame for the half
    # second it takes to load, which is a fully faded-to-black one.
    badge = badge_url(base, cue.play_s + BADGE_PAINT_S, masks)
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

    age = "no shot" if shot_age_s is None else f"shot {shot_age_s:.1f}s ago"
    print(
        f"replay: {age}, {duration:.1f}s file, cue {cue.cursor_s:.1f}s, "
        f"{cue.play_s:.1f}s on air, {len(masks)} clock mask(s)",
        flush=True,
    )
    started = time.monotonic() if now is None else now
    return Playing(live_scene=live_scene, until=started + cue.play_s)


def stop(playing: Playing) -> bool:
    """Cut back to the scene that was live when the replay started."""
    return obsws.set_scene(playing.live_scene)
