"""Ball detection: raw Bayer frame vs. empty-table reference → candidate balls.

Implements the §4.1 detection path: Bayer→gray, absolute difference against a
captured empty-table reference (classical segmentation — the researched
choice for a fixed camera with locked exposure; adaptive subtractors absorb
stopped balls), threshold, morphology, connected components, then geometry
gates sized for ~41-45 px balls.

Two §2.3 lessons are load-bearing here:

- Lesson 2: never shape-filter for circularity — fast balls streak into
  elongated blobs. The gates accept anything ball-sized through
  ~3 diameters of motion streak; only clearly-not-a-ball geometry (cue
  stick: thin; hands/arms: huge) is rejected.
- Lesson 5: blob identity confusion is the tracker's job, not the
  detector's. The detector reports every ball-like blob plus a whiteness
  score (for cue-ball ID, resolved globally downstream) and never guesses.

Pure module: arrays in, detections out. No I/O, no clocks, no threads.
"""

import math
from collections.abc import Callable
from dataclasses import dataclass
from typing import cast

import cv2
import numpy as np
import numpy.typing as npt


@dataclass(frozen=True)
class Intrusion:
    """One hand-scale blob that the ball gates **rejected** (#41).

    The gates below already compute, classify and then discard a per-frame
    record of everything on the cloth that is not a ball — an arm is thrown
    away by ``max_area_px``, a cue stick by ``min_extent_px``, anything
    reaching in from off-table by the mask-border rule. Downstream therefore
    sees ball-shaped blobs and nothing else, and when a hand *engulfs* a ball
    the combined blob exceeds ``max_area_px`` and is discarded outright: the
    ball simply vanishes, with no detection left to carry the ``merged`` flag.
    That is why ``merged`` reads ~0 on every measured hand event (checkpoint
    50, and again in #38) — the arm is not absent from the image, it was
    absent from the exported evidence.

    This is that evidence, and it is purely additive: which blobs become
    :class:`Detection` is untouched (§2.3 lesson 2 — balls are never
    shape-filtered, because fast balls streak). Only the *rejects* are
    described, and only the hand-scale ones (see
    ``DetectConfig.intrusion_min_core_px``), because that is the class #41
    needs and a stick channel measured useless for it.

    ``core_radius_px`` is the radius of the largest disc that fits inside the
    blob — the one shape number that says "solid mass" rather than "long thin
    thing", and the axis on which a hand and a cue actually differ.
    """

    bbox: tuple[int, int, int, int]  # x, y, w, h — distorted pixels
    area: float
    core_radius_px: float


# Reject reason bits (:class:`Reject`). A blob can fail several gates at once
# (an arm reaching in is both BORDER and LARGE), so they combine.
REJECT_BORDER = 1  # touches the cloth mask's edge: reaches in from off-table
REJECT_SMALL = 2  # area under ``min_area_px``
REJECT_LARGE = 4  # area over ``max_area_px`` — a hand, an arm, a tight rack
REJECT_THIN = 8  # short bbox side under ``min_extent_px`` — a cue stick
REJECT_LONG = 16  # long bbox side over ``max_extent_px``
REJECT_SHADOW = 32  # ball-sized but reads as the cloth in shadow


@dataclass(frozen=True)
class Reject:
    """One gray blob the ball gates rejected, and which gates (#57).

    The cheap sibling of :class:`Intrusion`: bbox, area and the reason bits
    the gates already computed, nothing measured beyond them — no distance
    transform. It exists so a session log can say what the detector saw and
    threw away: a tight rack (nine-plus touching balls) is one blob far over
    ``max_area_px``, invisible in the ball output and so undiagnosable after
    the fact (#59). Off unless ``DetectConfig.export_rejects``.
    """

    bbox: tuple[int, int, int, int]  # x, y, w, h — distorted pixels
    area: float
    reason: int  # REJECT_* bits


@dataclass(frozen=True)
class Detection:
    """One ball-like blob in one frame, in distorted pixel coordinates."""

    cx: float
    cy: float
    area: float
    bbox: tuple[int, int, int, int]  # x, y, w, h
    whiteness: float  # 0..1; high = bright and unsaturated (cue-ball-like)
    merged: bool = False  # blob too big to be one clean ball: either a motion
    # streak (legitimate) or the stick/hand/another ball merged into the blob
    # (centroid contaminated). The tracker disambiguates by whether the
    # matched track is moving — a still ball cannot streak.


@dataclass(frozen=True)
class DetectConfig:
    """Geometry gates sized for the 2026-07-27 mount (ball ≈ 41-45 px)."""

    diff_threshold: int = 25
    min_area_px: float = 600.0  # ~half a nominal ball disc (partial occlusion)
    max_area_px: float = 6000.0  # ~4x nominal: streak allowance, arms rejected
    min_extent_px: int = 25  # cue stick (~12 px wide) fails this
    max_extent_px: int = 170  # ~3 ball diameters of motion streak passes
    patch_radius_px: int = 10  # central-disk radius for the shadow sample
    # Cue-ball whiteness, sampled on the ball's *annulus* rather than its
    # centre (checkpoint 73). The centre is the specular cap every glossy ball
    # wears under an overhead light, so a central patch scores the lamp, not
    # the ball: measured over the 126 607 still corpus detections it separates
    # the cue from the object balls only 2.7x (p50 0.845 vs 0.310, and the
    # cue's p5 0.581 sits *below* the object balls' p95 0.720 — no separation
    # at the tails at all). In 3 px rings the signal is at 0.55-0.83 ball
    # radii, where the same population separates far harder. Both bounds are
    # forced by the measurement: inside ~0.3 radii every ball is white, and
    # past ~0.85 the sphere's own limb shading destroys the cue ball's own
    # score. Three earlier cuts of this measurement expressed the ring in
    # PIXELS and were wrong every time — the ball images 18.1-24.3 px across
    # this table (p1-p99), nearly all of it perspective within one frame, so a
    # fixed ring lands on the limb at one end and inside the cap at the other.
    # Hence the ratio, against ReferenceModel.radius_px.
    whiteness_ring_lo: float = 0.55  # in ball radii
    whiteness_ring_hi: float = 0.83
    nominal_ball_radius_px: float = 21.9  # fallback only, for callers with no
    # calibration (synthetic unit tests): the corpus-wide median of the
    # predicted radius. Production always passes the per-pixel map.
    whiteness_quad_floor: float = 0.50  # per-sample "reads white" level; the
    # argmin of equal-error and of AUC across a 0.25-0.60 sweep, and
    # independently the value at which this reproduces the HSV radial profile
    # that found the ring.
    # Shadow rejection (measured on the 2026-07-27 corpus): a shadow darkens
    # the cloth multiplicatively, keeping its chroma direction — brightness
    # ratio ~0.7-0.9 vs. the reference with near-identical normalized RGB.
    # The black ball, the nearest neighbor in signature, measures ratio 0.61
    # / chroma-diff 0.17 — outside on both axes.
    shadow_ratio_lo: float = 0.65
    shadow_ratio_hi: float = 0.95
    shadow_chroma_max: float = 0.15
    # ...but that signature is also a blue ball on blue cloth: the solid 2,
    # the 10's stripe, the green 6 at the gate edge (#51). Exempted by shape:
    # a blob that is a filled disc of ball size is a ball. The blob's holes
    # are filled first (a blue ball's white number disc sits within threshold
    # of the cloth, so its raw blob is a ring), then compared with a disc of
    # its own filled area at its own centroid, so the IoU is scale-free.
    # Measured 2026-11-15 over every colour reject in the corpus (9621 blobs,
    # 68 clips, every 8th frame), checked by eye on contact sheets. Still
    # blobs: touching pairs below 0.55, none at 0.55-0.65, single balls
    # mostly 0.80-1.0. Moving blobs: hand and forearm shadows reach 0.76, rail
    # and pocket corners 0.82; nothing that is not a ball at 0.83 or above.
    # Ball sizes run 0.73-1.11x the predicted area (still balls, p1-p99).
    # The cut recovers 68% of the rejects; the rest stay rejected as before.
    shadow_disc_iou_min: float = 0.84
    shadow_disc_area_lo: float = 0.65  # x the predicted ball area
    shadow_disc_area_hi: float = 1.15
    # Appearance-churn sample (settle-at-spin-stop gate, §4.2 rules caveat):
    # disk radius safely inside the ~21 px ball radius, so centroid jitter
    # never lets cloth pixels into the sample.
    churn_radius_px: int = 16
    # Merged-blob flag (the phantom-settle / blinded-strike root cause,
    # checkpoint 28): a stick or hand merging into a still ball's blob yanks
    # the centroid tens of mm. Measured on address_yank_strike_0801 and
    # merge_yank_0731
    # (2026-08-01): clean single still balls read area <= 1471 px^2 with
    # bbox extent <= 47 px across every frame; stick-merged blobs read
    # >= 2071 px^2 and >= 102 px. Thresholds sit in the gap with margin on
    # the clean side; contamination small enough to slip under them cannot
    # yank a centroid past the 30 mm motion latch (the yank scales with the
    # contaminating area). Not a detection gate — merged blobs are still
    # reported (§2.3 lesson 2: fast balls streak); the flag only tells the
    # tracker the centroid is untrustworthy *if* the ball is known-still.
    merged_area_px: float = 1800.0
    merged_extent_px: int = 70
    # Intrusion export gate (#41): a rejected blob is reported as an
    # :class:`Intrusion` only if a disc of this radius fits inside it. Set to
    # the nominal ball radius above, and that is the whole argument: a hand
    # covering a ball is at minimum as fat as the ball it is covering, while
    # a cue stick (~12 px wide) and the cushion-edge artifacts (4-8 px) are
    # nowhere near it. Measured on the 2026-08-11 carries against every
    # corpus clip that puts a hand near a ball: a hand carrying a ball reads
    # core 23-62 px (n=4), while the closest competing population — the
    # bridge hand at address, fingers spread with the shaft running through
    # them — reads 8-20 px (n=7). The band 20-23 is empty, the same
    # empty-band anchoring the break floors use. Also the pre-filter that
    # keeps this cheap: a blob can only hold such a disc if its short bbox
    # side is >= 2r and its area >= pi*r^2, so the distance transform runs on
    # a handful of blobs per frame, never on the rails.
    intrusion_min_core_px: float = 21.9
    # Off since checkpoint 109 (#50): nothing has read the channel since #41
    # was removed at checkpoint 104, and describing the rejects is not free —
    # a distance transform per large rejected blob, 1.5 ms/frame (27% of
    # `find_blobs`) on static_spread, where stale-reference rail blobs are big
    # enough to reach it. The seam stays: set True and `detect_frame` returns
    # the list exactly as before.
    export_intrusions: bool = False
    # Session-log evidence (#57): report every gray blob the gates reject as a
    # :class:`Reject`. Off by default so offline corpus output is unchanged;
    # `cueclock run` turns it on for the session log.
    export_rejects: bool = False
    # Chroma rescue (#56): gray differencing is blind to a ball whose
    # luminance matches the cloth's. The solid red 3 on this blue cloth reads
    # centre |diff| 21 against the 25 threshold, yet it moves R by +131 and
    # B by -77. The rescue channel is the frame's colour change *orthogonal*
    # to the cloth's own colour direction (`ChromaReference`): a shadow scales
    # the cloth's RGB along that direction and reads ~0 there, so this channel
    # cannot resurrect what the shadow gate rejects. Measured 2026-11-15 on
    # Bayer quads with a 3x3 quad box blur: every empty reference (empty_table,
    # _0729, _0801, _1115) against itself, 6 frames each over the whole cloth
    # mask, reads p99.99 6.4-6.7 and max 10.3-12.4. The threshold is 2x that
    # maximum, and is also where the red 3's chroma blob matches its predicted
    # ball area (1476 px at 25 vs ~1500).
    chroma_threshold: float = 25.0
    chroma_blur_quads: int = 3
    # A rescued blob must hold a disc of this many predicted ball radii — fat,
    # not round (§2.3 lesson 2: a streaking ball is still as thick as a ball).
    # Same corpus survey, all 7228 rescues measured: every one at >= 0.78 was
    # a ball; below, diagonal cue sticks the 3x3 blur had fattened (0.22-0.30,
    # n=23), a glove logo (0.39) and one heavily blurred stripe in flight
    # (0.57). The cut sits in that empty band and keeps the stripe.
    chroma_core_min: float = 0.5
    # A whole-ball colour blob replaces a gray detection only when that one
    # sits at least this many predicted radii off the colour blob's centre: an
    # off-centre fragment (the crescent of a red ball's shadow), not a centred
    # partial view. Over the corpus survey the candidates split cleanly: 7558
    # at 0-12 px (centred partial views, keep gray), none at 12-14, 27 at
    # 14-25 px (the crescents).
    chroma_replace_offset: float = 0.6
    # Touching-pair split (#56, `_split_touching`): the core level, in
    # predicted ball radii. On all_balls_still_1115 the two pairs read peaks
    # 0.98-1.02 and saddles ~0.65; cores separate from 0.7 up. Over every
    # merged gray blob in the corpus (6442, 71 clips, every 8th frame) 0.75
    # makes 4212 candidates and 3199 pass the per-part disc test; checked on
    # contact sheets (96 random, plus 79 distinct ones from break and power
    # clips where streaks live) every one was two or more real balls. Those
    # that fail stay one merged detection, as before.
    pair_core_min: float = 0.75
    # A part of a split is gray *and* colour, so it is larger than the gray
    # blobs `shadow_disc_area_hi` was measured on: over the same survey, parts
    # disc-like by IoU run 0.82-1.28x the predicted ball area (p1-p99), and
    # nothing lies between 1.35 and 1.7. Every distinct split the gray bound
    # (1.15) refused and this admits (34, contact sheet) was real touching
    # balls, `static_spread`'s cue and 14 among them.
    pair_part_area_hi: float = 1.35


# Morphology spill past the cloth mask's bounding box: OPEN (3x3) then CLOSE
# (5x5) reach 1 + 1 + 2 + 2 = 6 px, so a crop padded by more than that sees
# exactly the pixels the full frame does wherever a blob can exist.
_ROI_PAD_PX = 16


def table_roi_of(table_mask: npt.NDArray[np.uint8]) -> tuple[int, int, int, int]:
    """The mask's bounding box padded by ``_ROI_PAD_PX``, clipped to the frame.

    Segmentation is cropped to this (#50): every pixel outside it is zero
    after the mask is applied, so cropping changes no blob and halves the
    work — the cloth's box is 51% of the 1920x1200 frame (2.8 → 1.55 ms).
    """
    x, y, w, h = cv2.boundingRect(table_mask)
    fh, fw = table_mask.shape
    # Even origin, so the crop's Bayer quads are the frame's (chroma rescue).
    x0, y0 = max(0, x - _ROI_PAD_PX) & ~1, max(0, y - _ROI_PAD_PX) & ~1
    return x0, y0, min(fw, x + w + _ROI_PAD_PX) - x0, min(fh, y + h + _ROI_PAD_PX) - y0


def bayer_to_gray(bayer: npt.NDArray[np.uint8]) -> npt.NDArray[np.uint8]:
    """Detection-path luminance (0.12 ms on the M2 — §4 frame budget)."""
    return cast(npt.NDArray[np.uint8], cv2.cvtColor(bayer, cv2.COLOR_BayerBG2GRAY))


def build_reference(frames: list[npt.NDArray[np.uint8]]) -> npt.NDArray[np.uint8]:
    """Per-pixel median gray of empty-table frames (robust to transients)."""
    if not frames:
        raise ValueError("no frames to build a reference from")
    stack = np.stack([bayer_to_gray(f) for f in frames])
    return cast(npt.NDArray[np.uint8], np.median(stack, axis=0).astype(np.uint8))


def bayer_quads(bayer: npt.NDArray[np.uint8]) -> npt.NDArray[np.float32]:
    """Half-resolution RGB from the 2x2 Bayer quads — no demosaic.

    BayerRG8 parity is absolute (see :func:`_patch_rgb_means`), so ``bayer``
    must start on an even row and column of the frame.
    """
    h, w = bayer.shape[0] // 2 * 2, bayer.shape[1] // 2 * 2
    b = bayer[:h, :w]
    quads = np.empty((h // 2, w // 2, 3), np.float32)
    quads[..., 0] = b[0::2, 0::2]
    quads[..., 1] = b[0::2, 1::2]
    quads[..., 1] += b[1::2, 0::2]
    quads[..., 1] *= 0.5
    quads[..., 2] = b[1::2, 1::2]
    return quads


@dataclass(frozen=True, eq=False)
class ChromaReference:
    """The empty table in the chroma-rescue channel (#56).

    ``axes`` are two orthonormal RGB directions perpendicular to the cloth's
    median colour, so projecting onto them discards brightness along the
    cloth's own colour: a shadow, which scales the cloth's RGB, projects to
    the cloth's own value and differences to ~0. ``cloth`` is the per-quad
    median of the reference frames projected onto ``axes``, at half resolution.
    ``mask`` (0/255) and ``edge`` are the table mask and :func:`table_edge_of`
    it, sampled at the same quads.
    """

    axes: npt.NDArray[np.float32]  # (2, 3)
    cloth: npt.NDArray[np.float32]  # (H/2, W/2, 2)
    mask: npt.NDArray[np.uint8]  # (H/2, W/2)
    edge: npt.NDArray[np.bool_]  # (H/2, W/2)

    @classmethod
    def build(
        cls, frames: list[npt.NDArray[np.uint8]], table_mask: npt.NDArray[np.uint8]
    ) -> "ChromaReference":
        median = np.median(np.stack([bayer_quads(f) for f in frames]), axis=0).astype(np.float32)
        cloth_rgb = np.median(median[table_mask[0::2, 0::2].astype(bool)], axis=0)
        u = cloth_rgb / np.linalg.norm(cloth_rgb)
        e1 = np.cross(u, [1.0, 0.0, 0.0])
        e1 /= np.linalg.norm(e1)
        axes = np.stack([e1, np.cross(u, e1)]).astype(np.float32)
        return cls(
            axes=axes,
            cloth=cast(npt.NDArray[np.float32], cv2.transform(median, axes)),
            mask=np.ascontiguousarray(table_mask[0::2, 0::2] * np.uint8(255)),
            edge=np.ascontiguousarray(table_edge_of(table_mask)[0::2, 0::2]),
        )


def chroma_binary(
    bayer: npt.NDArray[np.uint8],
    chroma: ChromaReference,
    roi: tuple[int, int, int, int],
    cfg: DetectConfig,
) -> npt.NDArray[np.uint8]:
    """Where the cloth's colour changed (0/1), per Bayer quad of the ``roi``
    window (whose origin is even, see :func:`table_roi_of`). See
    ``DetectConfig.chroma_threshold``."""
    ox, oy, rw, rh = roi
    qh, qw = rh // 2, rw // 2
    window = bayer[oy : oy + 2 * qh, ox : ox + 2 * qw]
    # Rows of quads as 2-channel views: (R, G) on even rows, (G, B) on odd —
    # strided views, no demosaic; projecting each onto the axes and summing is
    # the quad's projection. The whole channel costs ~1.7 ms/frame on the M2
    # (3.0 ms through `bayer_quads`); `run` has three detect workers.
    even = window[0::2].reshape(qh, qw, 2).astype(np.float32)
    odd = window[1::2].reshape(qh, qw, 2).astype(np.float32)
    a = chroma.axes
    crop = (slice(oy // 2, oy // 2 + qh), slice(ox // 2, ox // 2 + qw))
    delta = cv2.add(
        cv2.transform(even, np.stack([a[:, 0], a[:, 1] / 2], axis=1)),
        cv2.transform(odd, np.stack([a[:, 1] / 2, a[:, 2]], axis=1)),
    )
    k = cfg.chroma_blur_quads
    du, dv = cv2.split(cv2.blur(cv2.subtract(delta, chroma.cloth[crop]), (k, k)))
    over = np.asarray(cv2.magnitude(du, dv)) > cfg.chroma_threshold
    return cast(npt.NDArray[np.uint8], cv2.bitwise_and(over.view(np.uint8), chroma.mask[crop]))


def _chroma_rescues(
    binary: npt.NDArray[np.uint8],
    edge: npt.NDArray[np.bool_],
    roi: tuple[int, int, int, int],
    labels: "cv2.typing.MatLike",
    sizable: set[int],
    fragments: set[int],
    radius_at: Callable[[float, float], float],
    cfg: DetectConfig,
) -> list[tuple[float, float, float, tuple[int, int, int, int], bool, bool]]:
    """Ball-shaped chroma blobs where the gray path segmented nothing (#56).

    Rescue only: a chroma blob that overlaps any gray blob of a whole ball's
    size or more (``sizable``) is skipped, whether the gray gates accepted
    that blob or rejected it. An accepted one is a ball already found, and it
    keeps its gray blob untouched, so this adds balls and never moves one. A
    rejected one is the gray path's verdict on a hand, a stick, a cluster or
    an arm reaching in, and the chroma blob inside it (a glove's logo, a
    stick and the cue ball it touches) must not overturn that verdict. Over
    the corpus survey (71 clips, every 8th frame) those were the only
    non-balls the rescue produced. What remains is a ball the gray path saw
    nothing of, or only crumbs below the area floor.

    ``binary`` is :func:`chroma_binary` and ``edge`` the table edge at the
    same quads; ``labels`` is the gray path's full-resolution crop of the
    ``roi`` window. Returns ``(cx, cy, area, bbox, whole, over_fragment)`` in
    frame pixels, through the same geometry gates and border rule as a gray
    blob: ``whole`` when the blob, holes filled, is a disc of ball size (the
    split's part test), ``over_fragment`` when it overlaps one of the smaller
    gray blobs in ``fragments`` — see the reconciliation in
    :func:`detect_frame`.
    """
    ox, oy = roi[0], roi[1]
    qh, qw = binary.shape
    n, qlabels, stats, centroids = cv2.connectedComponentsWithStats(binary)
    gray_labels = labels[0 : 2 * qh : 2, 0 : 2 * qw : 2]
    found = []
    for i in range(1, n):
        x, y = int(stats[i, cv2.CC_STAT_LEFT]), int(stats[i, cv2.CC_STAT_TOP])
        w, h = int(stats[i, cv2.CC_STAT_WIDTH]), int(stats[i, cv2.CC_STAT_HEIGHT])
        area = 4.0 * float(stats[i, cv2.CC_STAT_AREA])
        if not (
            cfg.min_area_px <= area <= cfg.max_area_px
            and min(2 * w, 2 * h) >= cfg.min_extent_px
            and max(2 * w, 2 * h) <= cfg.max_extent_px
        ):
            continue
        box = (slice(y, y + h), slice(x, x + w))
        blob = qlabels[box] == i
        if edge[box][blob].any():
            continue  # reaches in from off the cloth, as for a gray blob
        cx = ox + 2.0 * float(centroids[i, 0]) + 0.5
        cy = oy + 2.0 * float(centroids[i, 1]) + 0.5
        radius = radius_at(cx, cy)
        under = set(np.unique(gray_labels[box][blob]).tolist())
        if sizable & under:
            continue  # the gray path segmented something here: its call
        quads = blob.view(np.uint8)
        padded = np.pad(quads, 1)
        core = 2.0 * float(cv2.distanceTransform(padded, cv2.DIST_L2, 5).max())
        if core < cfg.chroma_core_min * radius:
            continue  # thin: a cue stick, not a ball
        # At quad resolution the ball's radius is half its pixel radius.
        whole = _is_disc_of_ball_size(_filled(quads), radius / 2.0, cfg.pair_part_area_hi, cfg)
        bbox = (ox + 2 * x, oy + 2 * y, 2 * w, 2 * h)
        found.append((cx, cy, area, bbox, whole, bool(fragments & under)))
    return found


def _patch_rgb_means(
    bayer: npt.NDArray[np.uint8], cx: float, cy: float, radius: int
) -> tuple[float, float, float]:
    """Mean R, G, B over the Bayer sites of a square patch around (cx, cy).

    BayerRG8 (GenICam): R at (even row, even col), B at (odd row, odd col),
    G at the other two sites — absolute image parity, hence the global
    coordinates below.
    """
    h, w = bayer.shape
    x0, x1 = max(0, int(cx) - radius), min(w, int(cx) + radius + 1)
    y0, y1 = max(0, int(cy) - radius), min(h, int(cy) + radius + 1)
    # shift the window start to even global parity so slicing stays aligned
    y0 += y0 % 2
    x0 += x0 % 2
    patch = bayer[y0:y1, x0:x1].astype(np.float64)
    if patch.shape[0] < 2 or patch.shape[1] < 2:
        return 0.0, 0.0, 0.0
    r = float(patch[0::2, 0::2].mean())
    b = float(patch[1::2, 1::2].mean())
    g = float((patch[0::2, 1::2].mean() + patch[1::2, 0::2].mean()) / 2.0)
    return r, g, b


def ring_whiteness(
    bayer: npt.NDArray[np.uint8],
    cx: float,
    cy: float,
    ball_radius_px: float,
    cfg: DetectConfig,
) -> float:
    """Fraction of the ball's 0.55-0.83-radius annulus that reads white.

    Sampled at 2x2 Bayer-quad resolution — ~140 samples per annulus, so no
    demosaic is needed. A quad reads white when its *darkest* channel clears
    ``whiteness_quad_floor``, which is exactly what "bright and unsaturated"
    reduces to: the old score was ``(1 - (hi - lo) / hi) * hi / 255``, and that
    algebra cancels to ``lo / 255`` for every non-black patch.

    ``ball_radius_px`` is the radius predicted at this pixel from the
    calibration (``ReferenceModel.radius_px``) — not a constant, because
    perspective takes the ball from 18.1 to 24.3 px across this table and a
    ring in fixed pixels therefore samples a different part of the sphere
    depending on where it stands.

    Position, not detection: this is a property of the pixels around a point,
    so it can be evaluated anywhere — including at a track's own position, with
    no blob to back it. That independence is why the annulus survived where
    "require the white core to coincide with a ball detection" died (plan
    §4.11): tying colour to a detection re-imports the detection's failures.
    """
    lo, hi = cfg.whiteness_ring_lo * ball_radius_px, cfg.whiteness_ring_hi * ball_radius_px
    if hi <= lo:
        return 0.0
    h, w = bayer.shape
    r = int(hi) + 1
    y0, y1 = max(0, int(cy) - r), min(h, int(cy) + r + 1)
    x0, x1 = max(0, int(cx) - r), min(w, int(cx) + r + 1)
    # BayerRG8 parity is absolute (R at even row/col), so snap the window start
    # to even global parity — as _patch_rgb_means does — and the quads align.
    y0 += y0 % 2
    x0 += x0 % 2
    patch = bayer[y0:y1, x0:x1]
    ny, nx = patch.shape[0] // 2, patch.shape[1] // 2
    if ny < 2 or nx < 2:
        return 0.0
    quads = patch[: 2 * ny, : 2 * nx].astype(np.float32)
    red, blue = quads[0::2, 0::2], quads[1::2, 1::2]
    green = (quads[0::2, 1::2] + quads[1::2, 0::2]) * 0.5
    darkest = np.minimum(np.minimum(red, green), blue)
    dy = (y0 + 2.0 * np.arange(ny, dtype=np.float32) + 0.5) - cy
    dx = (x0 + 2.0 * np.arange(nx, dtype=np.float32) + 0.5) - cx
    rr = dy[:, None] ** 2 + dx[None, :] ** 2
    ring = (rr >= lo * lo) & (rr < hi * hi)
    if not ring.any():
        return 0.0  # ball too close to the frame edge to have an annulus
    return float((darkest[ring] >= cfg.whiteness_quad_floor * 255.0).mean())


def _filled(blob: npt.NDArray[np.uint8]) -> npt.NDArray[np.uint8]:
    """The blob with its holes filled (a ball's number disc leaves one)."""
    contours, _ = cv2.findContours(blob, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    filled = np.zeros_like(blob)
    cv2.drawContours(filled, contours, -1, 1, -1)
    return filled


def _is_disc_of_ball_size(
    filled: npt.NDArray[np.uint8],
    ball_radius_px: float,
    area_hi: float,
    cfg: DetectConfig,
) -> bool:
    """Is this hole-free mask a disc of ball size — ``shadow_disc_area_lo`` to
    ``area_hi`` times the predicted ball area? Compared with a disc of its own
    area at its own centroid, so the IoU is scale-free (#51; see
    ``DetectConfig.shadow_disc_iou_min``)."""
    m = cv2.moments(filled, True)
    area = float(m["m00"])
    ball_area = math.pi * ball_radius_px * ball_radius_px
    if not (cfg.shadow_disc_area_lo * ball_area <= area <= area_hi * ball_area):
        return False
    cx, cy = m["m10"] / area, m["m01"] / area
    h, w = filled.shape
    yy, xx = np.ogrid[0:h, 0:w]
    disc = (xx - cx) ** 2 + (yy - cy) ** 2 <= area / math.pi
    inside = filled.astype(bool)
    return float((inside & disc).sum()) / float((inside | disc).sum()) >= cfg.shadow_disc_iou_min


def _is_ball_disc(
    labels: "cv2.typing.MatLike",
    label: int,
    bbox: tuple[int, int, int, int],
    ball_radius_px: float,
    cfg: DetectConfig,
) -> bool:
    """Is this blob, holes filled, a disc of ball size? (#51 — the shadow
    exemption.) ``bbox`` is in ``labels``' own coordinates."""
    x, y, w, h = bbox
    blob = (labels[y : y + h, x : x + w] == label).astype(np.uint8)
    return _is_disc_of_ball_size(_filled(blob), ball_radius_px, cfg.shadow_disc_area_hi, cfg)


def _split_touching(
    labels: "cv2.typing.MatLike",
    label: int,
    bbox: tuple[int, int, int, int],
    roi: tuple[int, int, int, int],
    chroma_binary: npt.NDArray[np.uint8] | None,
    sizable: set[int],
    radius_at: Callable[[float, float], float],
    cfg: DetectConfig,
) -> list[tuple[float, float, float, tuple[int, int, int, int]]] | None:
    """Touching balls in one merged gray blob, one entry each — or None (#56).

    The gray blob of a touching pair is a poor picture of it: a ball near the
    cloth's luminance (the yellow 9, the green 6) shows as a sliver or an open
    ring. So the shape is read from the blob together with the chroma blob it
    touches, holes filled. Its distance transform peaks at ~1.0 ball radius at
    each ball's centre, with a saddle of ~0.65 radii at the contact (the shadow
    between the balls fills the waist); a motion streak is a capsule whose
    ridge is flat, with no saddle at all. Thresholding at
    ``pair_core_min`` radii therefore leaves one core per touching ball. Each
    pixel goes to its nearest core, and the split stands only if *every* part
    is a disc of ball size by the same test the shadow exemption uses (with
    the area band of a gray-and-colour part, ``pair_part_area_hi``) — else the
    blob stays one merged detection, as before.

    ``labels`` and ``bbox`` are in the ``roi`` crop's coordinates;
    ``chroma_binary`` is :func:`chroma_binary` of the same crop, or None;
    ``sizable`` the gray labels of at least ``min_area_px``.
    """
    x, y, w, h = bbox
    ox, oy = roi[0], roi[1]
    reach = int(radius_at(ox + x + w / 2.0, oy + y + h / 2.0)) + 1  # past a sliver's ball
    lh, lw = labels.shape
    x0, y0 = max(0, x - reach) & ~1, max(0, y - reach) & ~1  # even: quads align
    x1, y1 = min(lw, x + w + reach), min(lh, y + h + reach)
    blob = (labels[y0:y1, x0:x1] == label).astype(np.uint8)
    if chroma_binary is not None:
        quads = chroma_binary[y0 // 2 : (y1 + 1) // 2, x0 // 2 : (x1 + 1) // 2]
        colour = np.zeros_like(blob)  # an odd crop edge has no last quad row
        up = np.repeat(np.repeat(quads, 2, axis=0), 2, axis=1)[: y1 - y0, : x1 - x0]
        colour[: up.shape[0], : up.shape[1]] = up
        _, comps = cv2.connectedComponents(colour)
        # Colour that also touches another gray blob is that blob's ball, not
        # this one's: a halo joining a neighbour would hand it to this split
        # (walk_around_settle: the red 3 next to a pair, reported twice).
        others = np.isin(labels[y0:y1, x0:x1], list(sizable - {label}))
        mine = set(np.unique(comps[blob > 0]).tolist()) - set(np.unique(comps[others]).tolist())
        mine.discard(0)
        if mine:
            blob |= np.isin(comps, list(mine)).astype(np.uint8)
    filled = _filled(blob)
    dist = cv2.distanceTransform(np.pad(filled, 1), cv2.DIST_L2, 5)[1:-1, 1:-1]
    m = cv2.moments(filled, True)
    r = radius_at(ox + x0 + m["m10"] / m["m00"], oy + y0 + m["m01"] / m["m00"])
    n, cores = cv2.connectedComponents((dist >= cfg.pair_core_min * r).astype(np.uint8))
    if n < 3:
        return None  # one core: one ball, a streak, or a ball with a stick
    # Every pixel labelled with its nearest core in one pass: the zero-pixel
    # components of the source are the cores.
    _, nearest = cv2.distanceTransformWithLabels(
        (cores == 0).astype(np.uint8), cv2.DIST_L2, 5, labelType=cv2.DIST_LABEL_CCOMP
    )
    parts = []
    for j in range(1, n):
        ys, xs = np.nonzero(cores == j)
        part = ((nearest == nearest[ys[0], xs[0]]) & (filled > 0)).astype(np.uint8)
        pm = cv2.moments(part, True)
        cx, cy = ox + x0 + pm["m10"] / pm["m00"], oy + y0 + pm["m01"] / pm["m00"]
        if not _is_disc_of_ball_size(part, radius_at(cx, cy), cfg.pair_part_area_hi, cfg):
            return None
        px, py, pw, ph = cv2.boundingRect(part)
        parts.append((cx, cy, float(pm["m00"]), (ox + x0 + px, oy + y0 + py, pw, ph)))
    return parts


def _is_shadow(
    rgb: tuple[float, float, float], ref_rgb: tuple[float, float, float], cfg: DetectConfig
) -> bool:
    """Mildly darker than the reference with the same chroma direction."""
    cur = sum(rgb)
    ref = sum(ref_rgb)
    if cur <= 0.0 or ref <= 0.0:
        return False
    ratio = cur / ref
    if not (cfg.shadow_ratio_lo <= ratio <= cfg.shadow_ratio_hi):
        return False
    chroma_diff = sum(abs(c / cur - rc / ref) for c, rc in zip(rgb, ref_rgb, strict=True))
    return chroma_diff <= cfg.shadow_chroma_max


def ball_churn(
    gray: npt.NDArray[np.uint8],
    prev_gray: npt.NDArray[np.uint8],
    cx: float,
    cy: float,
    radius: int,
) -> float:
    """Mean |gray - prev_gray| inside a disk at (cx, cy) — appearance churn.

    A stationary-but-spinning ball keeps changing its own pixels (the marking
    rotates) while its centroid holds still; a truly still ball shows only
    sensor noise. Measured on the spin_in_place corpus clip at 78 Hz:
    still-ball floor 1.2-1.5 gray levels (max 1.66), spinning cue 2.6-6.5,
    reaching the floor exactly when the spin visibly stops.
    """
    h, w = gray.shape
    x, y = round(cx), round(cy)
    if x - radius < 0 or y - radius < 0 or x + radius >= w or y + radius >= h:
        return 0.0  # partially off-frame: no honest sample
    window = (slice(y - radius, y + radius + 1), slice(x - radius, x + radius + 1))
    diff = np.abs(gray[window].astype(np.float32) - prev_gray[window].astype(np.float32))
    ys, xs = np.ogrid[-radius : radius + 1, -radius : radius + 1]
    return float(diff[(xs * xs + ys * ys) <= radius * radius].mean())


def _as_intrusion(
    labels: "cv2.typing.MatLike",
    label: int,
    bbox: tuple[int, int, int, int],
    offset: tuple[int, int],
    area: float,
    bayer: npt.NDArray[np.uint8],
    reference_bayer: npt.NDArray[np.uint8] | None,
    cfg: DetectConfig,
) -> Intrusion | None:
    """Describe a rejected blob as an :class:`Intrusion`, or None if it is not
    hand-scale. See ``DetectConfig.intrusion_min_core_px`` for the gate and
    for why the two cheap necessary conditions come first.

    ``labels`` and ``bbox`` are in the segmentation crop's coordinates;
    ``offset`` is the crop's origin in the frame (see :func:`table_roi_of`)."""
    x, y, w, h = bbox
    ox, oy = offset
    r = cfg.intrusion_min_core_px
    if min(w, h) < 2.0 * r or area < math.pi * r * r:
        return None  # cannot hold a disc of radius r, whatever its shape
    padded = np.zeros((h + 2, w + 2), np.uint8)
    padded[1:-1, 1:-1] = labels[y : y + h, x : x + w] == label
    # Distance to the nearest background pixel: its maximum IS the radius of
    # the largest inscribed disc, and where it peaks is the blob's thickest
    # point — which is also the only point guaranteed to be inside a concave
    # blob, so it is where the shadow sample is taken.
    dist = cv2.distanceTransform(padded, cv2.DIST_L2, 5)
    core = float(dist.max())
    if core < r:
        return None
    if reference_bayer is not None:
        py, px = np.unravel_index(int(dist.argmax()), dist.shape)
        cx, cy = float(ox + x + int(px) - 1), float(oy + y + int(py) - 1)
        if _is_shadow(
            _patch_rgb_means(bayer, cx, cy, cfg.patch_radius_px),
            _patch_rgb_means(reference_bayer, cx, cy, cfg.patch_radius_px),
            cfg,
        ):
            return None  # the player's shadow on the cloth is not a hand
    return Intrusion(bbox=(ox + x, oy + y, w, h), area=area, core_radius_px=core)


def detect_balls(
    bayer: npt.NDArray[np.uint8],
    reference_gray: npt.NDArray[np.uint8],
    config: DetectConfig | None = None,
    table_mask: npt.NDArray[np.uint8] | None = None,
    reference_bayer: npt.NDArray[np.uint8] | None = None,
    radius_map: npt.NDArray[np.float32] | None = None,
) -> list[Detection]:
    """The ball half of :func:`detect_frame` — see it for the gates."""
    return detect_frame(bayer, reference_gray, config, table_mask, reference_bayer, radius_map)[0]


def table_edge_of(table_mask: npt.NDArray[np.uint8]) -> npt.NDArray[np.bool_]:
    """The mask's inner border band: blobs touching it reach in from outside."""
    ring = cv2.subtract(table_mask, cv2.erode(table_mask, np.ones((7, 7), np.uint8)))
    return ring.astype(bool)


def detect_frame(
    bayer: npt.NDArray[np.uint8],
    reference_gray: npt.NDArray[np.uint8],
    config: DetectConfig | None = None,
    table_mask: npt.NDArray[np.uint8] | None = None,
    reference_bayer: npt.NDArray[np.uint8] | None = None,
    radius_map: npt.NDArray[np.float32] | None = None,
    *,
    gray: npt.NDArray[np.uint8] | None = None,
    table_edge: npt.NDArray[np.bool_] | None = None,
    table_roi: tuple[int, int, int, int] | None = None,
    chroma: ChromaReference | None = None,
) -> tuple[list[Detection], list[Intrusion], list[Reject]]:
    """Find ball-like blobs in one frame. See module docstring for the gates.

    ``table_mask`` (optional, uint8 0/1) restricts detection to the cloth.
    Any blob touching the mask's border is rejected outright: nothing that is
    genuinely on the table can cross the table boundary, so a border-touching
    blob is the on-table stub of something reaching in from outside (cue
    stick, hand, arm) that the mask truncated into a deceptively ball-sized
    piece — the §2.3 lesson-5 identity thieves.

    ``radius_map`` (optional) is the predicted ball radius at every pixel —
    ``ReferenceModel.radius_px``, built from the calibration. The whiteness
    annulus is measured in ball radii, so without it the sample falls back to
    ``nominal_ball_radius_px`` and reads the wrong part of the sphere wherever
    perspective disagrees with that nominal. Every production path supplies it.

    Returns the balls and, beside them, the hand-scale blobs these gates threw
    away (:class:`Intrusion`) — empty unless ``DetectConfig.export_intrusions``.
    The second list is additive evidence: it never feeds back into the first,
    and which blobs become balls is unchanged. The third is every gray blob
    the gates rejected (:class:`Reject`) — empty unless
    ``DetectConfig.export_rejects``, and additive in the same way.

    ``gray``, ``table_edge`` and ``table_roi`` are precomputed
    :func:`bayer_to_gray` of this frame, :func:`table_edge_of` and
    :func:`table_roi_of` the mask — pure savings for a caller that has them
    (the pipeline needs the gray for churn anyway, and the mask never
    changes). With a mask, segmentation runs only inside its padded bounding
    box; coordinates are offset back, so the output is identical.

    ``chroma`` (optional) turns on the chroma rescue (#56): after the gray
    blobs, a ball-shaped blob of colour change that overlaps no accepted gray
    blob is reported too — a ball whose luminance matches the cloth's. See
    ``DetectConfig.chroma_threshold``. Every production path supplies it.
    """
    cfg = config or DetectConfig()
    if gray is None:
        gray = bayer_to_gray(bayer)
    ox, oy = 0, 0
    rh, rw = gray.shape
    if table_mask is not None:
        edge = table_edge_of(table_mask) if table_edge is None else table_edge
        ox, oy, rw, rh = table_roi_of(table_mask) if table_roi is None else table_roi
        window = (slice(oy, oy + rh), slice(ox, ox + rw))
        gray, reference_gray = gray[window], reference_gray[window]
        table_mask, edge = table_mask[window], edge[window]
    diff = cv2.absdiff(gray, reference_gray)
    _, binary = cv2.threshold(diff, cfg.diff_threshold, 255, cv2.THRESH_BINARY)
    border_ids: set[int] = set()
    if table_mask is not None:
        binary = cv2.bitwise_and(binary, binary, mask=table_mask)
    binary = cv2.morphologyEx(binary, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    binary = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))

    n, labels, stats, centroids = cv2.connectedComponentsWithStats(binary)
    if table_mask is not None:
        border_ids = set(np.unique(labels[edge]).tolist())
    roi = (ox, oy, rw, rh)
    colour_binary = None if chroma is None else chroma_binary(bayer, chroma, roi, cfg)
    sizable = {i for i in range(1, n) if stats[i, cv2.CC_STAT_AREA] >= cfg.min_area_px}
    detections: list[Detection] = []
    intrusions: list[Intrusion] = []
    rejects: list[Reject] = []

    def radius_at(cx: float, cy: float) -> float:
        if radius_map is None:
            return cfg.nominal_ball_radius_px
        return float(radius_map[int(cy), int(cx)])

    def ball(cx: float, cy: float, area: float, bbox: tuple[int, int, int, int]) -> Detection:
        return Detection(
            cx=cx,
            cy=cy,
            area=area,
            bbox=bbox,
            whiteness=ring_whiteness(bayer, cx, cy, radius_at(cx, cy), cfg),
            merged=area > cfg.merged_area_px or max(bbox[2], bbox[3]) > cfg.merged_extent_px,
        )

    for i in range(1, n):
        area = float(stats[i, cv2.CC_STAT_AREA])
        bw = int(stats[i, cv2.CC_STAT_WIDTH])
        bh = int(stats[i, cv2.CC_STAT_HEIGHT])
        lx, ly = int(stats[i, cv2.CC_STAT_LEFT]), int(stats[i, cv2.CC_STAT_TOP])
        bbox = (lx + ox, ly + oy, bw, bh)
        ball_shaped = (
            i not in border_ids
            and cfg.min_area_px <= area <= cfg.max_area_px
            and min(bw, bh) >= cfg.min_extent_px
            and max(bw, bh) <= cfg.max_extent_px
        )
        if not ball_shaped:
            if cfg.export_rejects:
                rejects.append(
                    Reject(
                        bbox=bbox,
                        area=area,
                        reason=(REJECT_BORDER if i in border_ids else 0)
                        | (REJECT_SMALL if area < cfg.min_area_px else 0)
                        | (REJECT_LARGE if area > cfg.max_area_px else 0)
                        | (REJECT_THIN if min(bw, bh) < cfg.min_extent_px else 0)
                        | (REJECT_LONG if max(bw, bh) > cfg.max_extent_px else 0),
                    )
                )
            # Rejected on geometry — the three gates that between them throw
            # away every arm, stick and reaching hand. Describe it instead of
            # dropping it on the floor (#41); shadow-rejected blobs below are
            # deliberately NOT offered, they are already known to be shadows.
            if cfg.export_intrusions:
                found = _as_intrusion(
                    labels, i, (lx, ly, bw, bh), (ox, oy), area, bayer, reference_bayer, cfg
                )
                if found is not None:
                    intrusions.append(found)
            continue
        cx, cy = float(centroids[i, 0]) + ox, float(centroids[i, 1]) + oy
        merged = area > cfg.merged_area_px or max(bw, bh) > cfg.merged_extent_px
        parts = (
            _split_touching(
                labels, i, (lx, ly, bw, bh), roi, colour_binary, sizable, radius_at, cfg
            )
            if merged
            else None
        )
        if parts is not None:
            # Touching balls, each one passing the shadow exemption's own disc
            # test — so this also stands when the shadow gate below would
            # reject the blob. It did, for a pair whose centroid sample lands
            # in the shadow at their contact: a white cue touching the green 6
            # in cluster_shot_0801 was invisible for 6 s (checkpoint 110
            # recorded the gate rejecting touching pairs).
            detections += [ball(*part) for part in parts]
            continue
        if reference_bayer is not None:
            rgb = _patch_rgb_means(bayer, cx, cy, cfg.patch_radius_px)
            ref_rgb = _patch_rgb_means(reference_bayer, cx, cy, cfg.patch_radius_px)
            if _is_shadow(rgb, ref_rgb, cfg) and not _is_ball_disc(
                labels, i, (lx, ly, bw, bh), radius_at(cx, cy), cfg
            ):
                if cfg.export_rejects:
                    rejects.append(Reject(bbox=bbox, area=area, reason=REJECT_SHADOW))
                continue
        detections.append(ball(cx, cy, area, bbox))
    if chroma is not None and colour_binary is not None:
        edge_quads = chroma.edge[oy // 2 : oy // 2 + rh // 2, ox // 2 : ox // 2 + rw // 2]
        # The rescue defers to gray blobs of a whole ball's size or more: a
        # ball, or a hand / stick / cluster verdict. A smaller one is at most
        # a partial view of a ball (reconciled below), so it does not block.
        ball_sized = {
            i
            for i in sizable
            if stats[i, cv2.CC_STAT_AREA]
            >= cfg.shadow_disc_area_lo
            * math.pi
            * radius_at(centroids[i, 0] + ox, centroids[i, 1] + oy) ** 2
        }
        rescues = _chroma_rescues(
            colour_binary, edge_quads, roi, labels, ball_sized, sizable - ball_sized, radius_at, cfg
        )
        for rcx, rcy, rarea, rbbox, whole, over_fragment in rescues:
            radius = radius_at(rcx, rcy)
            near = [d for d in detections if math.hypot(d.cx - rcx, d.cy - rcy) < radius]
            if near or over_fragment:
                # Gray saw part of this ball. Usually a centred part — the
                # white half of a stripe, a dim limb — and it stays: a colour
                # blob trading places with it frame by frame jitters the
                # centroid (live_divergence_0731a, 3-4 px each way). But a red
                # ball can read in gray as only the dark crescent of its own
                # shadow, hovering at the area floor well off its centre, and
                # deferring to that made a still ball's track jump 25 mm each
                # time it crossed the floor (scratch_replace), latching
                # motion. So a whole ball in colour replaces gray views that
                # sit off its centre (``chroma_replace_offset``), and nothing
                # else. Only a whole one: a stripe's colour is half a ball too.
                offset = cfg.chroma_replace_offset * radius
                if not whole or any(math.hypot(d.cx - rcx, d.cy - rcy) < offset for d in near):
                    continue
                detections = [d for d in detections if d not in near]
            detections.append(ball(rcx, rcy, rarea, rbbox))
    return detections, intrusions, rejects
