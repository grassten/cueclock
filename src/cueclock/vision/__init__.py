"""Pure detection core: (frames + timestamps) in → events out.

Only the event layer exists so far; segmentation and tracking land once the
camera is here and the §2.3 test corpus is recorded.
"""

from cueclock.vision.events import (
    BallsSettled,
    BallState,
    Event,
    EventConfig,
    EventEngine,
    Scratch,
    ScratchCandidate,
    ScratchRefuted,
    ShotTaken,
)

__all__ = [
    "BallState",
    "BallsSettled",
    "Event",
    "EventConfig",
    "EventEngine",
    "Scratch",
    "ScratchCandidate",
    "ScratchRefuted",
    "ShotTaken",
]
