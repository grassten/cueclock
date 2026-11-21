"""The ball list (#54): which balls are on a still table, which is the cue, and
which number each carries.

A separate output, read from one frame of a table at rest. It never feeds the
tracker or the event engine, so shot detection cannot regress through it; the
tracker's own ball list is untouched. Pure: ``(Bayer frame, ReferenceModel) in
-> BallList out``, no I/O, no clocks, no threads (docs/design.md §2.1).

How it works (measured in issue #54, prototype in its research folder):

1. **Foreground** is the detector's own segmentation: gray difference against
   the empty table (threshold 25, open 3, close 5) OR its chroma channel.
   Components of at least 600 px are searched; the blob outline is ignored.
2. **Circles of a known radius.** Each strong colour edge inside a component
   votes for a centre along its gradient, both ways, at 0.90-1.12 of the
   ball's visible edge radius (:data:`SEARCH_A`). Peaks are taken
   greedily, each suppressing a disc of 1.5 predicted radii.
3. **Accepted** when strong (vote >= 2.5, outline coverage >= 0.5), or weak but
   enclosed by at least four strong balls of its own component (vote >= 1.2,
   fill >= 0.9): that second rule is what finds the black 8 in the middle of a
   15-ball rack.
4. **Read**: cue by white at the rim (:data:`CUE_RIM_FLOOR`); otherwise the
   Lab distance to each of eight colour centroids over the inner disc, and
   the stripe evidence: the disc's white fraction and white wrapping the rim.
   **The cue once per list**: when more than one circle passes the cue test,
   the whitest is the cue and the others are left off (:func:`_one_cue`).
5. **Numbered, each number once per list** (#65): every (ball, number) pair
   has a cost from those readings and the pairs are taken cheapest first, so
   of two balls that read the same number the better match keeps it and the
   other takes its next best (:func:`assign_numbers`). A ball with no
   duplicate gets exactly the number it reads on its own. Still no memory
   across lists: a list is a pure function of its frame.
6. **Arm**: the detector's hand test on the same frame. A list with an arm
   can show a ball is there, never that one is gone. The test fires on any
   solid blob too fat to be a cue stick, a standing rack included, so a list
   also says when every such blob is just the balls fitted in it
   (``pack``, :data:`PACK_COVER`): nothing is hiding anything there.

Everything the fit reads is in distorted pixels, like the detector; centres are
whole pixels (the accumulator's argmax), which is what the cue floor was
anchored on.
"""

from __future__ import annotations

import dataclasses
import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import cv2
import numpy as np
import numpy.typing as npt

from cueclock.vision.detect import (
    DetectConfig,
    Intrusion,
    bayer_to_gray,
    chroma_binary,
    detect_frame,
    ring_whiteness,
)
from cueclock.vision.pipeline import ReferenceModel

# The visible edge is not the predicted radius: 0.85 of it at the image centre
# and 0.99 at the far ends. Refitted 2026-11-17 on 1,207 clean balls
# (residual p5 -1.93, p50 +0.18, p95 +1.61 px). The cue ring and the rim
# wrap are sized from it, as they were measured.
EDGE_A, EDGE_B = 8.13, 0.497
# The first fit of the same line (1,342 balls, 135 of them circles found
# inside the ball) sizes everything else: the outline search, fill and the
# colour disc, because every finding and label count was measured with it.
# It matters: searching on the refit loses a rack ball on 3 of 21 frames of
# `break_dark_flight_0806` (15 of 15 on all 21 with this one), and a refit
# colour disc gives 3 duplicate numbers at the 162 corpus rests, not 2.
SEARCH_A, SEARCH_B = 7.82, 0.492

MIN_COMPONENT_PX = 600  # the detector's own min_area_px: half a ball disc
EDGE_MAG_MIN = 40.0  # gradient magnitude that counts as an edge pixel
VOTE_BAND = (0.90, 1.12)  # x the search radius
VOTE_SCALE = 1.5**2  # accumulator blur sigma squared, / r_pred: vote ~ outline share
SUPPRESS_R = 1.5  # x r_pred: one ball per disc
COVERAGE_DIRECTIONS = 72
COVERAGE_COS_MIN = 0.9  # gradient within ~26 degrees of radial
FILL_DISC = 0.8  # x the search radius: fill, and the inner disc labels are read from

STRONG_VOTE, STRONG_COVERAGE = 2.5, 0.5
WEAK_VOTE, WEAK_FILL = 1.2, 0.9
ENCLOSED_GAP, ENCLOSED_REACH, ENCLOSED_NEIGHBOURS = 1.5, 2.4, 4  # x r_pred

# The cue test (#54, final 2026-11-17): rim whiteness, the detector's
# `ring_whiteness`, on a ring 0.70-0.97 of the edge radius at the fitted
# centre. Corpus at 1 Hz: the cue's lowest reading 0.72 (a hand on it), the
# highest pole-up stripe 0.57, every other white-looking ball 0.53 or less.
# The floor is the midpoint of 0.57 and 0.72. Inner-disc white fraction must
# not be used: a pole-up stripe reads 0.85-1.00 there.
#
# The floor alone lets things through that are not balls: a table has one cue
# ball, and 21 of the 549 lists of the first four logged sessions held two.
# Read off their stills, every second one was the white logo on a black
# sleeve (19) or a gloved hand on the cloth (2). No floor separates them from
# the cue: the logo reads rim 0.66-0.89 and inner white 0.86-0.93, and the
# real cue ball held in a hand reads rim 0.65-0.97 and white 0.85-1.00 (10
# lists). So the list keeps one, by rank (`_one_cue`).
CUE_RING = dataclasses.replace(DetectConfig(), whiteness_ring_lo=0.70, whiteness_ring_hi=0.97)
CUE_RIM_FLOOR = 0.65

WHITE_FLOOR = 127  # darkest channel at or above this reads white (inner disc)
MIN_COLOUR_PX = 10  # fewer coloured pixels than this: no number (a stripe white cap up)
STRIPE_WHITE = 0.20  # inner-disc white fraction that makes a stripe
# Rim wrap: 36 ten-degree sectors in 0.70-0.95 of the edge radius, each
# counting when it holds 2+ pixels whose darkest channel is >= 110. 16 or more
# is a stripe (1 error over 400 ball sites with white fraction 0.06-0.24).
RIM_WRAP_FLOOR, RIM_WRAP_RING, RIM_WRAP_NEED, RIM_WRAP_STRIPE = 110, (0.70, 0.95), 2, 16

# Lab centroids (L 0-100, a and b centred on 0), each the mean of a solid and
# its stripe twin read off one frame of `all_balls_still_1115` labelled by
# eye; black stands alone. Index k is ball k+1, or k+9 for a stripe.
COLOUR_NAMES = ("yellow", "blue", "red", "purple", "orange", "green", "maroon", "black")
CENTROIDS = np.array(
    [
        [74.72, 9.57, 54.42],
        [37.25, 1.98, -27.65],
        [50.40, 35.71, 36.34],
        [25.00, 11.50, -21.24],
        [68.57, 30.17, 51.31],
        [38.25, -16.05, -0.03],
        [30.68, 14.78, 13.01],
        [16.05, -1.65, -2.08],
    ],
    dtype=np.float64,
)

# Each number once per list (#65): what a number costs a ball, in typical
# margins. Each spread is a median over the 1,327 numbered readings at the 162
# corpus rests (2026-11-17), so a cost of 1 is how far a typical ball sits from
# that decision's boundary: the colour margin (second-nearest centroid minus
# nearest, Lab), |white - STRIPE_WHITE| and |wrap - RIM_WRAP_STRIPE|. Measured
# on the corpus, confirmed on the two real sessions' truth sets.
COLOUR_SPREAD, WHITE_SPREAD, WRAP_SPREAD = 19.6, 0.175, 11.0

ARM_CONFIG = DetectConfig(export_intrusions=True)  # the detector's hand test, this frame only
# A hand-scale blob is a pack of balls when the discs of the balls fitted in
# it cover at least this share of its area. Measured on every hand-scale blob
# in the stills of four sessions (218 blobs, 204 frames): a rack standing
# alone reads 1.02-1.10 (19 blobs, 14-15 balls each); a rack with hands or the
# triangle on it 0.50-0.62; any other arm or hand, with or without a ball
# under it, at most 0.43. Nothing falls between 0.62 and 1.02, and 0.8 is the
# geometric middle of that gap.
PACK_COVER = 0.8

# Two lists are the same table when every ball has a same-label ball within
# this share of its predicted radius in the other (~5 px; whole-pixel centres
# jitter by 1).
SAME_POSITION_R = 0.25

_ANGLES = np.linspace(0.0, 2.0 * math.pi, COVERAGE_DIRECTIONS, endpoint=False)
_COS, _SIN = np.cos(_ANGLES), np.sin(_ANGLES)


def edge_radius(r_pred: float) -> float:
    """The visible ball edge, in px, where the predicted radius is ``r_pred``."""
    return EDGE_A + EDGE_B * r_pred


def _search_radius(r_pred: float) -> float:
    """The edge radius the search, fill and colour disc use (see SEARCH_A)."""
    return SEARCH_A + SEARCH_B * r_pred


@dataclass(frozen=True)
class ListedBall:
    """One ball on the list. ``number`` is 1-15, or None when it is the cue or
    shows no colour."""

    cx: float  # distorted pixels
    cy: float
    r_pred: float  # ReferenceModel.radius_px at the fitted centre
    cue: bool
    number: int | None
    rim: float  # rim whiteness, the cue test's reading
    white: float  # inner-disc white fraction
    margin: float  # colour distance to the second-nearest centroid minus the nearest
    colour_px: int  # non-white pixels in the inner disc
    vote: float
    coverage: float
    wrap: int = 0  # rim-wrap sectors, the stripe test's second reading
    own: int | None = None  # the number it read on its own, when the list gave it another

    @property
    def label(self) -> str:
        """``cue``, the number, or ``?``."""
        return "cue" if self.cue else "?" if self.number is None else str(self.number)


def _strip_key(ball: ListedBall) -> tuple[int, int, float, float]:
    rank = 0 if ball.cue else 2 if ball.number is None else 1
    return (rank, ball.number or 0, ball.cy, ball.cx)


@dataclass(frozen=True)
class BallList:
    """The balls on one frame, in strip order: the cue, 1 to 15, then the
    balls with no number. ``arm``: the hand test found something hand-sized
    over the table, so the list can show that a ball is present, never that
    one is absent. ``pack``: everything it found is balls this list fitted (a
    standing rack, a tight cluster), so the list is as good to *show* as one
    with no arm. What the clock may conclude from it is unchanged: the hand
    test also misses real arms (a bare forearm is too thin for it), and
    before a break that miss used to hide behind the rack's own flag
    (`break_0801b`: a hand on the cue ball, a rack standing, no cue listed).
    """

    balls: tuple[ListedBall, ...]
    arm: bool
    pack: bool = False
    cues_dropped: int = 0  # circles that passed the cue test and were not the cue

    @property
    def labels(self) -> list[str]:
        return [b.label for b in self.balls]

    def as_dict(self) -> dict[str, Any]:
        return {
            "arm": self.arm,
            **({"pack": True} if self.pack else {}),
            **({"cues_dropped": self.cues_dropped} if self.cues_dropped else {}),
            "balls": [
                {
                    "label": b.label,
                    "cx": b.cx,
                    "cy": b.cy,
                    "r": round(b.r_pred, 2),
                    "rim": round(b.rim, 3),
                    "white": round(b.white, 3),
                    "margin": round(b.margin, 1),
                    "colour_px": b.colour_px,
                    "vote": round(b.vote, 2),
                    "coverage": round(b.coverage, 3),
                    "wrap": b.wrap,
                    **({"own": str(b.own)} if b.own is not None else {}),
                }
                for b in self.balls
            ],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BallList:
        balls = []
        for b in data["balls"]:
            label = str(b["label"])
            balls.append(
                ListedBall(
                    cx=float(b["cx"]),
                    cy=float(b["cy"]),
                    r_pred=float(b["r"]),
                    cue=label == "cue",
                    number=int(label) if label.isdigit() else None,
                    rim=float(b["rim"]),
                    white=float(b["white"]),
                    margin=float(b["margin"]),
                    colour_px=int(b["colour_px"]),
                    vote=float(b["vote"]),
                    coverage=float(b["coverage"]),
                    wrap=int(b.get("wrap", 0)),  # absent before #65
                    own=int(b["own"]) if "own" in b else None,
                )
            )
        return cls(
            balls=tuple(balls),
            arm=bool(data["arm"]),
            pack=bool(data.get("pack", False)),
            cues_dropped=int(data.get("cues_dropped", 0)),
        )


def same_table(a: BallList, b: BallList) -> bool:
    """Do two lists say the same thing? Same ``arm`` and ``pack``, and every
    ball has a ball with the same label at the same spot in the other, both
    ways."""
    if (a.arm, a.pack) != (b.arm, b.pack) or sorted(a.labels) != sorted(b.labels):
        return False

    def covered(xs: Sequence[ListedBall], ys: Sequence[ListedBall]) -> bool:
        return all(
            any(
                y.label == x.label
                and math.hypot(x.cx - y.cx, x.cy - y.cy) < SAME_POSITION_R * x.r_pred
                for y in ys
            )
            for x in xs
        )

    return covered(a.balls, b.balls) and covered(b.balls, a.balls)


# ---------------------------------------------------------------- the fit


@dataclass
class _Candidate:
    cx: int
    cy: int
    r_pred: float
    vote: float
    coverage: float
    fill: float
    component: int


def _foreground(
    bayer: npt.NDArray[np.uint8], gray: npt.NDArray[np.uint8], ref: ReferenceModel
) -> tuple[npt.NDArray[np.uint8], tuple[int, int, int, int]]:
    """The detector's segmentation inside the table's window: gray difference
    OR chroma change, on the cloth. Returns (binary, window)."""
    cfg = DetectConfig()
    ox, oy, rw, rh = ref.table_roi
    window = (slice(oy, oy + rh), slice(ox, ox + rw))
    diff = cv2.absdiff(gray[window], ref.gray[window])
    over = (diff > cfg.diff_threshold).astype(np.uint8) & ref.mask[window]
    opened = cv2.morphologyEx(over, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    binary = np.asarray(
        cv2.morphologyEx(opened, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8)), dtype=np.uint8
    )
    quads = chroma_binary(bayer, ref.chroma, ref.table_roi, cfg)
    up = np.repeat(np.repeat(quads, 2, axis=0), 2, axis=1)
    binary[: up.shape[0], : up.shape[1]] |= up
    return binary, ref.table_roi


def _colour_gradient(
    bgr: npt.NDArray[np.uint8],
) -> tuple[npt.NDArray[np.float32], npt.NDArray[np.float32], npt.NDArray[np.float32]]:
    """Per pixel, the gradient (gx, gy, magnitude) of whichever colour channel
    changes most there."""
    img = cv2.GaussianBlur(bgr, (0, 0), 1.0).astype(np.float32)
    gx = cv2.Sobel(img, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(img, cv2.CV_32F, 0, 1, ksize=3)
    mag = np.sqrt(gx * gx + gy * gy)
    k = mag.argmax(axis=2)[..., None]

    def pick(a: npt.NDArray[np.float32]) -> npt.NDArray[np.float32]:
        return np.asarray(np.take_along_axis(a, k, 2)[..., 0], dtype=np.float32)

    return pick(np.asarray(gx)), pick(np.asarray(gy)), pick(np.asarray(mag))


def _coverage(
    gx: npt.NDArray[np.float32],
    gy: npt.NDArray[np.float32],
    mag: npt.NDArray[np.float32],
    cx: int,
    cy: int,
    r_pred: float,
) -> float:
    """Share of 72 directions with a radial edge in the vote band."""
    h, w = mag.shape
    e = _search_radius(r_pred)
    radii = np.arange(VOTE_BAND[0] * e, VOTE_BAND[1] * e + 0.01, 1.0)
    px = np.rint(cx + radii[:, None] * _COS[None, :]).astype(int)
    py = np.rint(cy + radii[:, None] * _SIN[None, :]).astype(int)
    inside = (px >= 0) & (px < w) & (py >= 0) & (py < h)
    px, py = np.clip(px, 0, w - 1), np.clip(py, 0, h - 1)
    m = mag[py, px]
    radial = np.abs(gx[py, px] * _COS[None, :] + gy[py, px] * _SIN[None, :]) / np.maximum(m, 1e-6)
    hit = inside & (m > EDGE_MAG_MIN) & (radial > COVERAGE_COS_MIN)
    return float(hit.any(axis=0).mean())


def _candidates(
    bgr: npt.NDArray[np.uint8],
    binary: npt.NDArray[np.uint8],
    origin: tuple[int, int],
    radius_px: npt.NDArray[np.float32],
) -> list[_Candidate]:
    """Every circle with a vote of at least WEAK_VOTE, in every component.

    ``bgr`` and ``binary`` share one window whose top-left is ``origin`` in
    the frame; candidates come back in frame pixels."""
    ox, oy = origin
    n, labels, stats, _ = cv2.connectedComponentsWithStats(binary)
    hh, ww = binary.shape
    found: list[_Candidate] = []
    for i in range(1, n):
        x, y, w, h, area = (int(v) for v in stats[i])
        if area < MIN_COMPONENT_PX:
            continue
        # Voting needs one radius before any centre is known: the component's.
        r = float(radius_px[oy + min(hh - 1, y + h // 2), ox + min(ww - 1, x + w // 2)])
        pad = int(1.3 * r) + 4
        x0, y0 = max(0, x - pad), max(0, y - pad)
        x1, y1 = min(ww, x + w + pad), min(hh, y + h + pad)
        comp = (labels[y0:y1, x0:x1] == i).astype(np.uint8)
        gx, gy, mag = _colour_gradient(bgr[y0:y1, x0:x1])
        region = cv2.dilate(comp, np.ones((7, 7), np.uint8)).astype(bool)
        ys, xs = np.nonzero((mag > EDGE_MAG_MIN) & region)
        if len(xs) == 0:
            continue
        ux, uy = gx[ys, xs] / mag[ys, xs], gy[ys, xs] / mag[ys, xs]
        ch, cw = comp.shape
        e = _search_radius(r)
        rho = np.arange(VOTE_BAND[0] * e, VOTE_BAND[1] * e + 0.01, 1.0)[:, None]
        votes = []
        for sign in (1.0, -1.0):
            vx = np.rint(xs[None, :] + sign * rho * ux[None, :]).astype(np.int64).ravel()
            vy = np.rint(ys[None, :] + sign * rho * uy[None, :]).astype(np.int64).ravel()
            ok = (vx >= 0) & (vx < cw) & (vy >= 0) & (vy < ch)
            votes.append(vy[ok] * cw + vx[ok])
        acc = np.bincount(np.concatenate(votes), minlength=ch * cw).astype(np.float32)
        acc = cv2.GaussianBlur(acc.reshape(ch, cw), (0, 0), 1.5) * np.float32(VOTE_SCALE / r)
        while True:
            _, v, _, (px, py) = cv2.minMaxLoc(acc)
            if v < WEAK_VOTE:
                break
            cv2.circle(acc, (px, py), int(SUPPRESS_R * r), 0, -1)
            # From here on, everything is read at the fitted centre.
            rc = float(radius_px[oy + y0 + py, ox + x0 + px])
            disc = np.zeros(comp.shape, np.uint8)
            cv2.circle(disc, (px, py), round(FILL_DISC * _search_radius(rc)), 1, -1)
            found.append(
                _Candidate(
                    cx=ox + x0 + px,
                    cy=oy + y0 + py,
                    r_pred=rc,
                    vote=float(v),
                    coverage=_coverage(gx, gy, mag, px, py, rc),
                    fill=float((disc & comp).sum()) / max(1, int(disc.sum())),
                    component=i,
                )
            )
    return found


def _accept(found: list[_Candidate]) -> list[_Candidate]:
    """Strong circles, plus weak ones enclosed by >= 4 accepted ones."""
    out = [c for c in found if c.vote >= STRONG_VOTE and c.coverage >= STRONG_COVERAGE]
    strong = {id(c) for c in out}
    for c in sorted(found, key=lambda c: -c.vote):
        if id(c) in strong or c.vote < WEAK_VOTE or c.fill < WEAK_FILL:
            continue
        d = [
            math.hypot(c.cx - s.cx, c.cy - s.cy) / c.r_pred
            for s in out
            if s.component == c.component
        ]
        if (
            d
            and min(d) >= ENCLOSED_GAP
            and sum(x <= ENCLOSED_REACH for x in d) >= ENCLOSED_NEIGHBOURS
        ):
            out.append(c)
    return out


def _disc(
    bgr: npt.NDArray[np.uint8], x: int, y: int, radius: float
) -> tuple[npt.NDArray[np.uint8], npt.NDArray[np.int64], npt.NDArray[np.int64]] | None:
    """The square patch around (x, y) and each pixel's offsets from it, or
    None when the patch leaves the image."""
    half = int(radius) + 1
    h, w = bgr.shape[:2]
    if x - half < 0 or y - half < 0 or x + half >= w or y + half >= h:
        return None
    yy, xx = np.mgrid[-half : half + 1, -half : half + 1]
    return bgr[y - half : y + half + 1, x - half : x + half + 1], yy, xx


@dataclass(frozen=True)
class _Reading:
    """One accepted circle's measurements, before numbers are assigned."""

    c: _Candidate
    cue: bool
    rim: float
    white: float
    wrap: int  # rim-wrap sectors (0 when not read: the cue, or no colour)
    colour_px: int
    dist: npt.NDArray[np.float64] | None  # Lab distance to each centroid; None: no number


def _read(
    bgr: npt.NDArray[np.uint8],
    origin: tuple[int, int],
    bayer: npt.NDArray[np.uint8],
    c: _Candidate,
) -> _Reading | None:
    """Measure one accepted circle; ``bgr`` is the demosaiced window at ``origin``."""
    e = edge_radius(c.r_pred)
    disc = _disc(bgr, c.cx - origin[0], c.cy - origin[1], e)
    if disc is None:
        return None
    patch, yy, xx = disc
    rr = np.hypot(xx, yy)
    pixels = patch[rr <= FILL_DISC * _search_radius(c.r_pred)]
    is_white = pixels.min(axis=1) >= WHITE_FLOOR
    white = float(is_white.mean())
    colour = pixels[~is_white]
    rim = ring_whiteness(bayer, c.cx, c.cy, e, CUE_RING)
    cue = rim >= CUE_RIM_FLOOR
    if cue or len(colour) < MIN_COLOUR_PX:
        return _Reading(c, cue, rim, white, 0, len(colour), None)
    lab = cv2.cvtColor(colour.reshape(-1, 1, 3), cv2.COLOR_BGR2LAB).reshape(-1, 3)
    med = np.median(lab.astype(np.float64), axis=0)
    point = np.array([med[0] * 100.0 / 255.0, med[1] - 128.0, med[2] - 128.0])
    lo, hi = RIM_WRAP_RING
    rim_white = (patch.min(axis=2) >= RIM_WRAP_FLOOR) & (rr >= lo * e) & (rr <= hi * e)
    sector = ((np.degrees(np.arctan2(yy, xx)) + 360.0) % 360.0 // 10).astype(int)
    wrap = int((np.bincount(sector[rim_white], minlength=36) >= RIM_WRAP_NEED).sum())
    dist = np.linalg.norm(CENTROIDS - point, axis=1)
    return _Reading(c, cue, rim, white, wrap, len(colour), dist)


def _one_cue(readings: Sequence[_Reading]) -> tuple[list[_Reading], int]:
    """The cue once per list: ``readings`` with at most one cue, and how many
    circles that passed the cue test were left off.

    The cue is the one whose inner disc is whitest (rim whiteness breaks a
    tie). In each of the 21 two-cue lists on record the real cue ball read
    white 0.968-0.992 and the other circle 0.613-0.927, the smallest gap
    0.046; the rim reading orders all 21 the same way, but by as little as
    0.012. The others are left off the list, not read as object balls: none
    of the 21 was a ball, and the highest rim reading of any object ball in
    the corpus is 0.57 (a stripe pole up), under the floor.
    """
    cues = [r for r in readings if r.cue]
    if len(cues) < 2:
        return list(readings), 0
    cue = max(cues, key=lambda r: (r.white, r.rim))
    return [r for r in readings if not r.cue or r is cue], len(cues) - 1


def stripe_evidence(white: float, wrap: int) -> float:
    """How striped a ball reads, in typical margins: >= 0 is a stripe.

    The stripe test is ``white >= STRIPE_WHITE or wrap >= RIM_WRAP_STRIPE``;
    each reading's distance past its own threshold is divided by that
    reading's typical distance (:data:`WHITE_SPREAD`, :data:`WRAP_SPREAD`), and
    the larger counts, so the sign is exactly the test's verdict."""
    return max((white - STRIPE_WHITE) / WHITE_SPREAD, (wrap - RIM_WRAP_STRIPE) / WRAP_SPREAD)


def number_costs(dist: npt.NDArray[np.float64], stripe: float) -> npt.NDArray[np.float64]:
    """What each number 1-15 costs one ball (index n-1), in typical margins.

    Colour: the distance to the number's centroid over :data:`COLOUR_SPREAD`.
    Half: a number whose half agrees with the stripe evidence earns half its
    size, one that disagrees pays it, so a ball's twin costs exactly its
    evidence more (the 8 has no twin and always agrees). A ball's cheapest
    number is the one it reads on its own; between two balls wanting one
    number, the nearer colour and the clearer half win."""
    costs = np.empty(15)
    for n in range(1, 16):
        k = n - 1 if n <= 8 else n - 9
        agree = n == 8 or (n >= 9) == (stripe >= 0.0)
        costs[n - 1] = dist[k] / COLOUR_SPREAD + (-0.5 if agree else 0.5) * abs(stripe)
    return costs


def _own_number(dist: npt.NDArray[np.float64], stripe: float) -> int:
    k = int(np.argmin(dist))
    return k + 9 if stripe >= 0.0 and k < 7 else k + 1


def assign_numbers(readings: Sequence[tuple[npt.NDArray[np.float64], float]]) -> list[int | None]:
    """Each number at most once per list (#65, Steven's decision 3).

    ``readings`` are each ball's (centroid distances, stripe evidence). Every
    (ball, number) pair is taken cheapest first; a ball takes its number when
    neither is taken yet, so of two balls that read the same number the
    better match keeps it and the other falls to its next best, and so on.
    Ties go to the number whose half agrees with the evidence, then to the
    earlier ball. A ball left with no number would get None (``?``); with 15
    numbers and at most 15 object balls on a table it cannot happen unless a
    list holds more than 15 numbered circles, i.e. junk."""
    pairs = []
    for i, (dist, stripe) in enumerate(readings):
        for n, cost in enumerate(number_costs(dist, stripe), start=1):
            agree = n == 8 or (n >= 9) == (stripe >= 0.0)
            pairs.append((float(cost), not agree, i, n))
    pairs.sort()
    numbers: list[int | None] = [None] * len(readings)
    taken: set[int] = set()
    for _, _, i, n in pairs:
        if numbers[i] is None and n not in taken:
            numbers[i] = n
            taken.add(n)
    return numbers


def _listed(readings: Sequence[_Reading]) -> list[ListedBall]:
    """The readings as balls, numbered list-wide."""
    numbered = [
        (r, r.dist, stripe_evidence(r.white, r.wrap)) for r in readings if r.dist is not None
    ]
    assigned = assign_numbers([(dist, stripe) for _, dist, stripe in numbered])
    by_id = {
        id(r): (n, _own_number(dist, stripe), dist)
        for (r, dist, stripe), n in zip(numbered, assigned, strict=True)
    }
    out = []
    for r in readings:
        number = own = None
        margin = 0.0
        if id(r) in by_id:
            number, mine, dist = by_id[id(r)]
            own = None if mine == number else mine
            first, second = np.sort(dist)[:2]
            margin = float(second - first)
        out.append(
            ListedBall(
                cx=float(r.c.cx),
                cy=float(r.c.cy),
                r_pred=r.c.r_pred,
                cue=r.cue,
                number=number,
                rim=r.rim,
                white=r.white,
                margin=margin,
                colour_px=r.colour_px,
                vote=r.c.vote,
                coverage=r.c.coverage,
                wrap=r.wrap,
                own=own,
            )
        )
    return out


def _is_pack(
    intrusions: Sequence[Intrusion],
    readings: Sequence[_Reading],
    binary: npt.NDArray[np.uint8],
    origin: tuple[int, int],
) -> bool:
    """Is every hand-scale blob on the frame accounted for by the balls
    fitted in it (:data:`PACK_COVER`)?

    ``binary`` is the list's own foreground, the one `_candidates` labelled,
    so a reading's ``component`` names the same blob here."""
    if not intrusions:
        return False
    ox, oy = origin
    n, labels, stats, _ = cv2.connectedComponentsWithStats(binary)
    for blob in intrusions:
        x, y, w, h = blob.bbox
        under = labels[max(0, y - oy) : y - oy + h, max(0, x - ox) : x - ox + w]
        sizes = np.bincount(np.asarray(under, dtype=np.int64).ravel(), minlength=n)
        sizes[0] = 0  # the cloth
        component = int(sizes.argmax())
        if not sizes[component]:
            return False  # nothing of the list's under it
        discs = sum(math.pi * r.c.r_pred**2 for r in readings if r.c.component == component)
        if discs < PACK_COVER * float(stats[component, cv2.CC_STAT_AREA]):
            return False
    return True


def read_ball_list(bayer: npt.NDArray[np.uint8], reference: ReferenceModel) -> BallList:
    """The ball list of one Bayer frame (BayerRG8, decoded as OpenCV's BG)."""
    gray = bayer_to_gray(bayer)
    binary, (ox, oy, rw, rh) = _foreground(bayer, gray, reference)
    # The window's origin is even (`detect.table_roi_of`), so its Bayer
    # pattern is the frame's.
    bgr = np.asarray(
        cv2.cvtColor(bayer[oy : oy + rh, ox : ox + rw], cv2.COLOR_BayerBG2BGR), dtype=np.uint8
    )
    accepted = _accept(_candidates(bgr, binary, (ox, oy), reference.radius_px))
    readings, cues_dropped = _one_cue(
        [r for c in accepted if (r := _read(bgr, (ox, oy), bayer, c)) is not None]
    )
    balls = _listed(readings)
    _, intrusions, _ = detect_frame(
        bayer,
        reference.gray,
        ARM_CONFIG,
        reference.mask,
        reference.bayer,
        reference.radius_px,
        gray=gray,
        table_edge=reference.table_edge,
        table_roi=reference.table_roi,
        chroma=reference.chroma,
    )
    return BallList(
        balls=tuple(sorted(balls, key=_strip_key)),
        arm=bool(intrusions),
        pack=_is_pack(intrusions, readings, binary, (ox, oy)),
        cues_dropped=cues_dropped,
    )
