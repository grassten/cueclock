"""Vision events → Hub calls: the one translation `run` and replay share.

`cueclock run` posts these coroutines to the server's asyncio loop as each
frame's stateful half finishes; `cueclock session replay` awaits them in the
same order on a virtual clock (#57). Keeping the translation in one place is
what lets a replay reproduce the live journal: two copies would drift.
"""

from collections.abc import Coroutine
from typing import TYPE_CHECKING, Any

from cueclock.vision.events import (
    BallsSettled,
    Event,
    ScratchCandidate,
    ScratchRefuted,
    ShotTaken,
)

if TYPE_CHECKING:
    from cueclock.server.hub import Hub
    from cueclock.vision.pipeline import VisionPipeline

HubCall = Coroutine[Any, Any, None]


def hub_calls(
    hub: "Hub",
    vision: "VisionPipeline",
    t: float,
    events: list[Event],
    episode_prev: bool,
) -> tuple[list[HubCall], bool]:
    """The hub coroutines one frame's events call for, in order, and the
    motion-episode latch's state after the frame.

    Call right after :meth:`VisionPipeline.advance` returned ``events`` for
    the frame captured at ``t``: the settle evidence is read off ``vision``,
    which holds it for exactly that call. ``episode_prev`` is what the
    previous frame returned.
    """
    calls: list[HubCall] = []
    for event in events:
        if isinstance(event, ShotTaken):
            calls.append(
                hub.shot_taken(
                    t, ball_id=event.ball_id, speed_mms=event.speed_mms, newborn=event.newborn
                )
            )
        elif isinstance(event, BallsSettled):
            # The rearrangement summary and mover evidence set by the same
            # advance() call that emitted this settle — the hub's E8 break
            # adjudication and #33 settle-time shot adjudication evidence.
            calls.append(
                hub.balls_settled(
                    t,
                    rearrange=vision.last_settle_rearrange,
                    backstop=vision.last_settle_backstop,
                    movers=vision.last_settle_movers,
                    tracker_cue=vision.last_settle_cue_seen,
                )
            )
        elif isinstance(event, ScratchCandidate):
            # Two-stage verdict (#26): the candidate also grants the #25
            # post-scratch hold, which a refutation releases; the confirmed
            # Scratch below stays journal-only.
            calls.append(
                hub.scratch_candidate(
                    t,
                    x_mm=event.x_mm,
                    y_mm=event.y_mm,
                    pocket_x_mm=event.pocket_x_mm,
                    pocket_y_mm=event.pocket_y_mm,
                    whiteness=event.whiteness,
                    color_age_s=event.color_age_s,
                )
            )
        elif isinstance(event, ScratchRefuted):
            calls.append(
                hub.scratch_refuted(
                    t,
                    witness_id=event.witness_id,
                    x_mm=event.x_mm,
                    y_mm=event.y_mm,
                    whiteness=event.whiteness,
                )
            )
        else:
            # Scratch (confirmed verdict): journaled only — the clock action
            # lives on the candidate/refuted pair above (#25)
            calls.append(hub.scratch_seen(t))
    # #33 Part B: the motion-episode latch edges, AFTER this frame's events so
    # a same-frame ShotTaken opens the provisional itself and a same-frame
    # settle resolves it (a falling edge with a settle is the settle; without
    # one it is the engine's pickup discard).
    episode = vision.episode_active
    if episode != episode_prev:
        if episode:
            calls.append(hub.episode_started(t))
        elif not any(isinstance(e, BallsSettled) for e in events):
            calls.append(hub.episode_discarded(t))
    return calls, episode
