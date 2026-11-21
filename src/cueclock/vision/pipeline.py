"""Full vision pipeline: raw Bayer frames + timestamps in → events out.

Chains the §4.1 stages — detect (distorted px) → calibrate coordinates
(undistort points + homography → table mm) → track → event logic — behind
one `process()` call. Pure like every stage it wraps: no I/O, no clocks,
no threads; replaying a recorded clip through it is deterministic.

The empty-table reference and the table mask come from the corpus's own
`empty_table` clip (or, live, from a capture taken at startup).
"""

import hashlib
from collections import deque
from dataclasses import dataclass
from math import hypot
from typing import cast

import cv2
import numpy as np
import numpy.typing as npt

from cueclock.display.undistort_fit import cloth_mask
from cueclock.vision.calibration import TableCalibration
from cueclock.vision.detect import (
    ChromaReference,
    DetectConfig,
    Detection,
    Intrusion,
    Reject,
    ball_churn,
    bayer_to_gray,
    build_reference,
    detect_frame,
    table_edge_of,
    table_roi_of,
)
from cueclock.vision.events import (
    TUNED_HZ,
    BallsSettled,
    BallState,
    Event,
    EventConfig,
    EventEngine,
    SettleMover,
    ShotTaken,
)
from cueclock.vision.roll import CUE_WHITE_MIN, CueRollWitness
from cueclock.vision.track import BallTracker, Observation, TrackConfig


@dataclass(frozen=True)
class RearrangeSummary:
    """How much a settle rearranged the table (accuracy plan §13.2).

    Computed against the last quiet layout before the motion episode — the
    positions of every ``present`` track on the most recent frame where no
    ball reported raw speed at or above the engine's own still definition
    (``settle_floor_mms``). Balls are greedy-matched by distance; a matched
    ball displaced past ``moving_enter_mm`` counts as moved, an unmatched
    current ball as appeared (a break un-merges the racked pack, so most of
    a break's scatter lands here), an unmatched baseline ball as vanished
    (pockets, pickups).

    This rides beside the event stream rather than on ``BallsSettled``
    itself so the corpus goldens — which serialize every event field — stay
    byte-identical until §13.5's verdict tiering makes regenerating them
    cheap. The hub reads it as evidence; nothing in the vision layer acts
    on it (§13.1).
    """

    moved: int
    appeared: int
    vanished: int
    moved_mm: float
    # Max pairwise distance across the involved balls' positions (vanished
    # balls: where the baseline last had them). Journaled for E8's
    # racking-vs-break record: the 2026-08-10 armed-rack racking settle
    # measured 1619 mm — inside the break population (1103-1684 mm) — which
    # is what keeps the dark tier off (#33 Part C); every journaled settle
    # now accumulates both axes for free.
    spread_mm: float = 0.0
    # How many balls the quiet layout this diff was taken against resolved —
    # and **None when there was no quiet layout at all** and the current
    # frame stood in for one (`self._settled_baseline or present`, below).
    #
    # This is the field that makes `involved == 0` readable (#37). A zero diff
    # means one of two opposite things: the table is provably unchanged, or
    # the baseline was the settled layout itself and the diff was arithmetic
    # rather than evidence. Measured over every corpus settle (2026-08-11):
    # THIRTEEN clips read `involved == 0`, and twelve of them read it with no
    # baseline at all — clips that open mid-action, so the pipeline never saw
    # a quiet frame before the episode latched. The thirteenth is
    # `parked_vote_strike_0801`'s phantom, against a 7-ball quiet layout.
    # Without this field the two are byte-identical, which is exactly why #33
    # had to fail open on the shape.
    #
    # Evidence, not a verdict: the hub decides what a baseline this size is
    # worth (§13.1). Defaults to None so any summary built without it is
    # treated as unanchored — the failing-open direction.
    baseline_balls: int | None = None

    @property
    def involved(self) -> int:
        """Balls the episode touched — the break-vs-racking headline number."""
        return self.moved + self.appeared + self.vanished


# The settle backstop's rearrangement floor (flag_missed_4, 2026-08-06): a
# full 15-ball break can scatter entirely *between* track identities — every
# pack track dies at impact, every settled ball is a newborn — so the event
# engine's per-track displacement latch never trips and neither fire nor
# settle is emitted. The quiet-to-quiet layout diff is identity-free and sees
# it trivially (12+ balls involved). The floor is the same measured line the
# hub's break adjudication uses (breaks 7-12 involved; racking placement
# cycles 1-3; a ball-in-hand replacement exactly 1, and a settle on one of
# those in RUNNING would reset a live clock — §9 constraint 1), so placement
# and racking stay exactly as event-silent as checkpoints 28-56 left them.
SETTLE_BACKSTOP_MIN_INVOLVED = 5

# REMOVED 2026-08-29 (checkpoint 104, this file's half of the #41 removal):
# `SettleFrame`, the retained rearrangement window, `ARRIVAL_CLEAR_MM`,
# `ARRIVAL_HAND_WINDOW_S`, `SETTLE_WINDOW_MAX_S` and `_hand_delivered`, which
# existed solely to compute `RearrangeSummary.hand_arrivals` for the hub's
# ball-in-hand refusal. The verdict that consumed them is gone — see
# `hub._settle_is_hand_placement`'s removal note for the live measurement.
# Recorded here because deleting a guard silently is how it comes back.


def _match_rearrange(
    prev: list[tuple[float, float]],
    cur: list[tuple[float, float]],
    moved_mm: float,
    baseline_balls: int | None = None,
) -> RearrangeSummary:
    """Greedy nearest-pair matching — n is ~16, O(n² log n) is nothing.

    ``baseline_balls`` rides through untouched; the caller passes it because
    only the caller knows whether ``prev`` was a real quiet layout or the
    current frame standing in for one (see the field's note above).
    """
    dists = sorted(
        (hypot(p[0] - c[0], p[1] - c[1]), i, j)
        for i, p in enumerate(prev)
        for j, c in enumerate(cur)
    )
    used_p: set[int] = set()
    used_c: set[int] = set()
    moved_dists: list[float] = []
    involved_pos: list[tuple[float, float]] = []
    for d, i, j in dists:
        if i in used_p or j in used_c:
            continue
        used_p.add(i)
        used_c.add(j)
        if d > moved_mm:
            moved_dists.append(d)
            involved_pos.append(cur[j])
    involved_pos += [c for j, c in enumerate(cur) if j not in used_c]  # appeared
    involved_pos += [p for i, p in enumerate(prev) if i not in used_p]  # vanished
    spread = 0.0
    if len(involved_pos) > 1:
        spread = max(
            hypot(a[0] - b[0], a[1] - b[1])
            for k, a in enumerate(involved_pos)
            for b in involved_pos[k + 1 :]
        )
    return RearrangeSummary(
        moved=len(moved_dists),
        appeared=len(cur) - len(used_c),
        vanished=len(prev) - len(used_p),
        moved_mm=sum(moved_dists),
        spread_mm=spread,
        baseline_balls=baseline_balls,
    )


def build_table_mask(reference_bayer: npt.NDArray[np.uint8]) -> npt.NDArray[np.uint8]:
    """Cloth region of the empty table, dilated so cushion-hugging balls fit."""
    bgr = cv2.cvtColor(reference_bayer, cv2.COLOR_BayerBG2BGR_EA)
    mask = cloth_mask(cast(npt.NDArray[np.uint8], bgr))
    return cast(
        npt.NDArray[np.uint8],
        cv2.dilate(mask, np.ones((31, 31), np.uint8)),
    )


BALL_RADIUS_MM = 28.575  # a US 8-ball ball is 2 1/4" = 57.15 mm (WPA spec)


def build_radius_map(
    shape: tuple[int, int], calibration: TableCalibration, step: int = 16
) -> npt.NDArray[np.float32]:
    """Predicted ball radius, in raw pixels, at every pixel of the frame.

    A ball is 57.15 mm everywhere (WPA spec), but its *pixel* radius is not a
    constant: measured over 106 149 corpus detections it runs **18.1-24.3 px**
    (p1-p99), and almost all of that is perspective within a single frame
    rather than difference between mounts (per-mount medians 21.6 / 21.8 /
    22.2). So anything expressed in ball radii — the whiteness annulus — has
    to ask where on the table it is standing. A fixed pixel ring is
    0.66-0.99 ball radii at one end of this table and 0.49-0.74 at the other,
    which is enough to put the sample on the shaded limb in one place and
    inside the specular cap in another.

    Computed as the local scale of :meth:`TableCalibration.to_table_mm`: step
    one pixel in +x and in +y, see how far that is in mm, and invert.
    Averaged over the two axes so perspective anisotropy does not bias it.
    Sampled on a `step` grid and bilinearly resized — the field is smooth
    (~6 px of variation across 1920), so 16 px sampling costs ~10 ms instead
    of ~2 s and is exact to well under a tenth of a pixel.
    """
    h, w = shape
    ys, xs = np.mgrid[0 : h + step : step, 0 : w + step : step].astype(np.float64)
    grid = np.stack([xs.ravel(), ys.ravel()], axis=1)
    base = calibration.to_table_mm(grid)
    dx = calibration.to_table_mm(grid + np.array([1.0, 0.0]))
    dy = calibration.to_table_mm(grid + np.array([0.0, 1.0]))
    mm_per_px = (
        np.hypot(*(dx - base).T) + np.hypot(*(dy - base).T)
    ) / 2.0  # mm moved per pixel stepped
    with np.errstate(divide="ignore", invalid="ignore"):
        radius = np.where(mm_per_px > 0, BALL_RADIUS_MM / mm_per_px, 0.0)
    coarse = radius.reshape(xs.shape).astype(np.float32)
    return cast(
        npt.NDArray[np.float32],
        cv2.resize(coarse, (w, h), interpolation=cv2.INTER_LINEAR),
    )


# `VisionPipeline.find_blobs` output: balls, intrusions, rejects (#57), and
# the frame's gray.
DetectedBlobs = tuple[list[Detection], list[Intrusion], list[Reject], npt.NDArray[np.uint8]]


@dataclass(eq=False)
class ReferenceModel:
    """The static arrays `detect_balls` consumes, built once per camera position.

    Bundled for two reasons. It costs ~0.30 s to build (median of 15 grays +
    the dilated cloth mask), which the corpus battery otherwise pays 51 times
    over; and it is the *complete* non-frame input to detection, so hashing
    these arrays content-addresses everything upstream — the reference clip's
    pixels, how many frames were sampled and which, `build_reference`,
    `cloth_mask`, the dilation, and (since checkpoint 73) the calibration the
    radius map is derived from. That is what makes a detection cache key
    provably complete instead of a list of source files someone must remember
    to extend — and it is why `radius_px` is a required field rather than an
    optional one with a fallback: an optional one could differ silently.
    """

    gray: npt.NDArray[np.uint8]  # per-pixel median of the empty-table frames
    bayer: npt.NDArray[np.uint8]  # first sampled frame, for shadow rejection
    mask: npt.NDArray[np.uint8]  # dilated cloth region
    radius_px: npt.NDArray[np.float32]  # predicted ball radius per pixel
    chroma: ChromaReference  # the empty cloth in the chroma-rescue channel (#56)

    @classmethod
    def build(
        cls, reference_frames: list[npt.NDArray[np.uint8]], calibration: TableCalibration
    ) -> "ReferenceModel":
        gray = build_reference(reference_frames)
        mask = build_table_mask(reference_frames[0])
        return cls(
            gray=gray,
            bayer=reference_frames[0],
            mask=mask,
            radius_px=build_radius_map(gray.shape, calibration),
            chroma=ChromaReference.build(reference_frames, mask),
        )

    def digest(self) -> str:
        """Content hash of every array (memoized — ~20 ms over 6.9 MB)."""
        if self._digest is None:
            h = hashlib.sha256()
            arrays = (self.gray, self.bayer, self.mask, self.radius_px)
            for array in (*arrays, self.chroma.axes, self.chroma.cloth):
                h.update(f"{array.shape}{array.dtype}".encode())
                h.update(array.tobytes())
            self._digest = h.hexdigest()
        return self._digest

    @property
    def table_edge(self) -> npt.NDArray[np.bool_]:
        """`detect.table_edge_of` the mask (memoized — it never changes)."""
        if self._table_edge is None:
            self._table_edge = table_edge_of(self.mask)
        return self._table_edge

    @property
    def table_roi(self) -> tuple[int, int, int, int]:
        """`detect.table_roi_of` the mask (memoized — it never changes)."""
        if self._table_roi is None:
            self._table_roi = table_roi_of(self.mask)
        return self._table_roi

    _digest: str | None = None
    _table_edge: npt.NDArray[np.bool_] | None = None
    _table_roi: tuple[int, int, int, int] | None = None


class VisionPipeline:
    """Feed frames in timestamp order; collect shot/settle/scratch events."""

    def __init__(
        self,
        calibration: TableCalibration,
        reference: list[npt.NDArray[np.uint8]] | ReferenceModel | None,
        detect_config: DetectConfig | None = None,
        track_config: TrackConfig | None = None,
        event_config: EventConfig | None = None,
        cadence_hz: float = TUNED_HZ,
    ) -> None:
        """``cadence_hz`` is the rate frames will arrive at: 156.25 for every
        camera frame, TUNED_HZ for stride 2. Configs are given as tuned and
        converted here (`EventConfig.at_cadence`, `TrackConfig.at_cadence`),
        so every caller passes the same constants whatever its cadence.

        ``reference`` None builds the stateful half only — :meth:`advance` and
        :meth:`churn` work, the detector does not. That is a session-log
        replay (#57), which feeds logged detections and has no pixels."""
        self._calibration = calibration
        self.cadence_hz = cadence_hz
        self._reference = (
            None
            if reference is None
            else reference
            if isinstance(reference, ReferenceModel)
            else ReferenceModel.build(reference, calibration)
        )
        self.detect_config = detect_config or DetectConfig()
        self.event_config = (event_config or EventConfig()).at_cadence(cadence_hz)
        self._tracker = BallTracker((track_config or TrackConfig()).at_cadence(cadence_hz))
        self._engine = EventEngine(self.event_config)
        # Churn differences against the gray one *tuned* interval back — the
        # previous frame at TUNED_HZ, two back at 156 Hz — so spin_churn_max
        # reads the same amount of rotation at any cadence.
        self._grays: deque[npt.NDArray[np.uint8]] = deque(
            maxlen=max(1, round(cadence_hz / TUNED_HZ))
        )
        self.last_balls: list[BallState] = []  # diagnostics for analyze/scoreboard
        # E9's identity-free table-motion bit (accuracy plan §13.3): some
        # confirmed track was *observed* at or above the engine's still floor
        # on two consecutive reports. Observed-only on purpose — a coasting
        # phantom repeating its last velocity forever must not be able to
        # hold the bit high, because the frozen-clock class it guards is
        # often exactly a phantom's aftermath.
        self.last_motion = False
        self._supra: dict[int, bool] = {}  # ball_id → last observed report was supra-floor
        # E8's settle rearrangement: the quiet-layout baseline the summary
        # diffs against. Slides forward on quiet frames while no episode is
        # pending, freezes the moment anything moves, resets after each
        # settle. The pending latch matters: a settle fires only after
        # dwell-seconds of renewed quiet, so a baseline that kept sliding
        # through that stillness would reach the settled layout first and
        # every summary would read zero.
        self._settled_baseline: list[tuple[float, float]] | None = None
        self._rearrange_pending = False
        self._quiet_since: float | None = None
        self._prev_present: list[tuple[float, float]] | None = None
        self.last_settle_rearrange: RearrangeSummary | None = None
        # True when the most recent BallsSettled was the pipeline's backstop
        # rather than the engine's own — journaled by the hub so triage sees
        # the engine's miss instead of a clean settle.
        self.last_settle_backstop = False
        # #33 settle evidence: the engine's per-mover episode travel/liveness
        # for the most recent settle (empty for a backstop settle — the
        # engine slept through that episode, there is no mover evidence).
        self.last_settle_movers: list[SettleMover] = []
        # #63 settle evidence: did the tracker hold a cue-grade ball at the
        # most recent settle (`EventEngine.cue_witnessed`)? The hub reads it
        # beside the ball list to call a scratch the tracker never suspected.
        self.last_settle_cue_seen = False
        # #67 settle evidence: how long the cue ball was seen rolling in the
        # motion the most recent settle ended (`vision.roll`); None when no
        # motion started since the settle before (a backstop settle).
        self._roll = CueRollWitness(cadence_hz)
        self.last_settle_cue_roll_s: float | None = None

    @property
    def reference(self) -> ReferenceModel:
        if self._reference is None:
            raise RuntimeError("this pipeline was built without a reference: it cannot detect")
        return self._reference

    @property
    def episode_active(self) -> bool:
        """Is the engine's motion episode latched? (#33's provisional-freeze
        trigger — the hardened roster-mover latch, deliberately not the raw
        E9 motion bit, which gear can set during an ordinary address.)"""
        return self._engine.table_moving

    def process(self, t: float, bayer: npt.NDArray[np.uint8]) -> list[Event]:
        detections, churn, intrusions = self.detect(bayer)
        return self.advance(t, detections, churn, intrusions)

    @property
    def churn_lag(self) -> int:
        """Frames between the two grays churn differences (1 at TUNED_HZ)."""
        return self._grays.maxlen or 1

    def detect(
        self, bayer: npt.NDArray[np.uint8]
    ) -> tuple[list[Detection], list[float | None], list[Intrusion]]:
        detections, intrusions, _rejects, gray = self.find_blobs(bayer)
        return self.churn(detections, intrusions, gray)

    def find_blobs(self, bayer: npt.NDArray[np.uint8]) -> DetectedBlobs:
        """Frame-bound half: ball-like blobs plus each one's appearance churn.

        The expensive half (~5.9 ms/frame on the M2 against 0.08 ms for
        :meth:`advance`) and the half that does *not* change while event logic
        is being tuned — which is why it is separable and cacheable offline
        (`capture.detect_cache`).

        Stateless, so the live loop runs it on worker threads (OpenCV drops
        the GIL); :meth:`churn` is the stateful remainder, run in frame order.
        """
        gray = bayer_to_gray(bayer)
        detections, intrusions, rejects = detect_frame(
            bayer,
            self.reference.gray,
            self.detect_config,
            self.reference.mask,
            self.reference.bayer,
            self.reference.radius_px,
            gray=gray,
            table_edge=self.reference.table_edge,
            table_roi=self.reference.table_roi,
            chroma=self.reference.chroma,
        )
        return detections, intrusions, rejects, gray

    def churn(
        self,
        detections: list[Detection],
        intrusions: list[Intrusion],
        gray: npt.NDArray[np.uint8],
    ) -> tuple[list[Detection], list[float | None], list[Intrusion]]:
        """Each detection's appearance churn; call once per frame, in order."""
        prev = self._grays[0] if len(self._grays) == self._grays.maxlen else None
        churn = [
            None
            if prev is None
            else ball_churn(gray, prev, d.cx, d.cy, self.detect_config.churn_radius_px)
            for d in detections
        ]
        self._grays.append(gray)
        return detections, churn, intrusions

    def advance(
        self,
        t: float,
        detections: list[Detection],
        churn: list[float | None],
        intrusions: list[Intrusion] | None = None,
    ) -> list[Event]:
        """State-bound half: pixels → table mm → tracker → events.

        Stateful only through the tracker and event engine, so a whole clip's
        :meth:`detect` results may be computed (or loaded) up front and fed
        here afterwards: the two halves share no state and interleaving them
        is not required.

        ``intrusions`` is **accepted and not consumed** since checkpoint 104,
        when #41's removal took the only reader with it, and since checkpoint
        109 the detector no longer measures it by default
        (``DetectConfig.export_intrusions``) — so it arrives empty. Say so
        rather than drop the parameter: this stays the seam a future consumer
        plugs into, and a parameter quietly ignored is worse than one
        documented as such.
        """
        observations: list[Observation] = []
        whites = np.zeros((0, 2))
        if detections:
            px = np.array([[d.cx, d.cy] for d in detections], dtype=np.float64)
            mm = self._calibration.to_table_mm(px)
            clean = [not d.merged and d.whiteness >= CUE_WHITE_MIN for d in detections]
            whites = mm[np.array(clean, dtype=bool)]
            observations = [
                Observation(
                    x_mm=float(p[0]),
                    y_mm=float(p[1]),
                    whiteness=d.whiteness,
                    churn=c,
                    merged=d.merged,
                )
                for p, d, c in zip(mm, detections, churn, strict=True)
            ]
        balls = self._tracker.update(t, observations)
        self.last_balls = balls
        events = self._engine.update(t, balls)

        floor = self.event_config.settle_floor_mms
        raws = {
            b.ball_id: (b.speed_mms if b.raw_speed_mms is None else b.raw_speed_mms)
            for b in balls
            if b.observed
        }
        self.last_motion = any(
            raw >= floor and self._supra.get(ball_id, False) for ball_id, raw in raws.items()
        )
        self._supra = {ball_id: raw >= floor for ball_id, raw in raws.items()}

        present = [(b.x_mm, b.y_mm) for b in balls if b.present]
        settled = any(isinstance(e, BallsSettled) for e in events)
        if settled:
            self.last_settle_rearrange = _match_rearrange(
                self._settled_baseline or present,
                present,
                self.event_config.moving_enter_mm,
                baseline_balls=None
                if self._settled_baseline is None
                else len(self._settled_baseline),
            )
            self.last_settle_backstop = False
            self.last_settle_movers = list(self._engine.last_settle_movers)
            self.last_settle_cue_seen = self._engine.cue_witnessed(balls)
        # Quiet needs two things, because speed alone lies here (the
        # flag_missed_4 lesson): a breaking pack's tracks die at impact and
        # coast at their remembered ~0 speed while the in-flight balls are
        # too fast to ever confirm — the confirmed-track view reads calm
        # through the whole scatter. So quiet = no ball carries a supra-floor
        # *smoothed* speed (raw is one-step: 40 mm/s is half a millimetre
        # per frame at 78 Hz, under centroid noise, so a settled 11-ball
        # table never reads raw-calm) AND the present layout took no step
        # since the previous frame (a present ball vanishing, appearing, or
        # standing displaced IS the motion, whichever track claims to be
        # calm). Zero-ball frames are not stillness evidence (mid-scatter
        # detection desert), mirroring the engine's own rule.
        calm = bool(balls) and all(b.speed_mms < floor for b in balls)
        layout_step = (
            self._prev_present is not None
            and _match_rearrange(
                self._prev_present, present, self.event_config.moving_enter_mm
            ).involved
            > 0
        )
        self._prev_present = present
        quiet = calm and not layout_step
        self._quiet_since = (self._quiet_since or t) if quiet else None
        if (
            not settled
            and self._rearrange_pending
            and not self._engine.table_moving
            and self._quiet_since is not None
            and t - self._quiet_since >= self.event_config.settle_dwell_s
        ):
            # Settle backstop (see SETTLE_BACKSTOP_MIN_INVOLVED): pixel-level
            # motion came and went without the engine latching an episode.
            # If the quiet layout provably rearranged at break scale, the
            # settle happened whether or not any track lived to show it.
            # Below the floor the episode is released either way — a frozen
            # baseline must not inflate some much-later settle's diff.
            summary = _match_rearrange(
                self._settled_baseline or present,
                present,
                self.event_config.moving_enter_mm,
                baseline_balls=None
                if self._settled_baseline is None
                else len(self._settled_baseline),
            )
            if summary.involved >= SETTLE_BACKSTOP_MIN_INVOLVED:
                self.last_settle_rearrange = summary
                self.last_settle_backstop = True
                self.last_settle_movers = []
                self.last_settle_cue_seen = self._engine.cue_witnessed(balls)
                events = [*events, BallsSettled(t)]
            settled = True
        self._roll.frame(
            whites,
            moving=self._engine.table_moving,
            shot=any(isinstance(e, ShotTaken) for e in events),
        )
        if any(isinstance(e, BallsSettled) for e in events):
            self.last_settle_cue_roll_s = self._roll.settle()
        if settled:
            self._settled_baseline = present
            self._rearrange_pending = False
        elif quiet:
            if not self._rearrange_pending:
                self._settled_baseline = present
        elif balls:
            self._rearrange_pending = True
        return events
