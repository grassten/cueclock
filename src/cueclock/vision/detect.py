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
    x0, y0 = max(0, x - _ROI_PAD_PX), max(0, y - _ROI_PAD_PX)
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


def _ring_whiteness(
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
) -> tuple[list[Detection], list[Intrusion]]:
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
    and which blobs become balls is unchanged.

    ``gray``, ``table_edge`` and ``table_roi`` are precomputed
    :func:`bayer_to_gray` of this frame, :func:`table_edge_of` and
    :func:`table_roi_of` the mask — pure savings for a caller that has them
    (the pipeline needs the gray for churn anyway, and the mask never
    changes). With a mask, segmentation runs only inside its padded bounding
    box; coordinates are offset back, so the output is identical.
    """
    cfg = config or DetectConfig()
    if gray is None:
        gray = bayer_to_gray(bayer)
    ox, oy = 0, 0
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
    detections: list[Detection] = []
    intrusions: list[Intrusion] = []
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
        if reference_bayer is not None:
            rgb = _patch_rgb_means(bayer, cx, cy, cfg.patch_radius_px)
            ref_rgb = _patch_rgb_means(reference_bayer, cx, cy, cfg.patch_radius_px)
            if _is_shadow(rgb, ref_rgb, cfg):
                continue
        detections.append(
            Detection(
                cx=cx,
                cy=cy,
                area=area,
                bbox=bbox,
                whiteness=_ring_whiteness(
                    bayer,
                    cx,
                    cy,
                    cfg.nominal_ball_radius_px
                    if radius_map is None
                    else float(radius_map[int(cy), int(cx)]),
                    cfg,
                ),
                merged=area > cfg.merged_area_px or max(bw, bh) > cfg.merged_extent_px,
            )
        )
    return detections, intrusions
