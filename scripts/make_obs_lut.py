"""Generate 3D LUTs (.cube) for OBS's Apply LUT filters.

Three files land in assets/obs/:

  cueclock-tone.cube      — hue-preserving tone map + saturation only (safe)
  cueclock-tone-ccm.cube  — the same, preceded by the color-correction matrix
                            measured from the ball set (CHANGELOG.md
                            2026-07-27). The CCM amplifies chroma noise ~3x;
                            OBS's Apply LUT "amount" slider tempers it live.
  cueclock-sidecam.cube   — the side camera's colour fix, on the Video Capture
                            Device source (hardware.md Part F, 2026-11-15).
  cueclock-overhead.cube  — the overhead's grade, on the Syphon source at
                            amount 1.0: the tone map at its dialed-in strength
                            plus a hue/saturation fix for the warm balls
                            (hardware.md §B3, 2026-11-16).

The tone math is a port of scripts/live_view.py: black/white point + gamma +
contrast S-curve applied to LUMINANCE, all channels scaled by one factor
(per-channel curves bend hue: red -> orange), then a saturation blend.

Run:  uv run python scripts/make_obs_lut.py
"""

from collections.abc import Callable
from pathlib import Path

import numpy as np

BLACK, WHITE, GAMMA = 0.04, 0.94, 2.2
CONTRAST, SATURATION = 1.12, 1.20
LUT_SIZE = 33

# Banked CCM in BGR row/column order (fitted 2026-07-27, 85% tempered,
# rows sum to 1 so neutrals stay neutral).
CCM_BGR = np.array(
    [
        [2.4454, -1.256, -0.1894],
        [-0.1135, 1.4985, -0.385],
        [-0.109, -1.2503, 2.3593],
    ]
)
# .cube domain is RGB — reverse both axes.
CCM_RGB = CCM_BGR[::-1, ::-1]

Grade = Callable[[np.ndarray, np.ndarray | None], np.ndarray]

LUMA = np.array([0.299, 0.587, 0.114])  # matches cv2 BGR2GRAY weights


def grade(rgb: np.ndarray, ccm: np.ndarray | None) -> np.ndarray:
    """(N, 3) float RGB in [0, 1] -> graded RGB in [0, 1]."""
    if ccm is not None:
        rgb = np.clip(rgb @ ccm.T, 0.0, 1.0)
    y = rgb @ LUMA
    x = np.clip((y - BLACK) / (WHITE - BLACK), 0.0, 1.0)
    y_mapped = np.clip(0.5 + (x ** (1.0 / GAMMA) - 0.5) * CONTRAST, 0.0, 1.0)
    scale = y_mapped / np.maximum(y, 1.0 / 255.0)
    # cap so no channel clips: clipping pales the color and bends its hue
    scale = np.minimum(scale, 1.0 / np.maximum(rgb.max(axis=1), 1.0 / 255.0))
    rgb = rgb * scale[:, None]
    y2 = (rgb @ LUMA)[:, None]
    return np.clip(y2 + SATURATION * (rgb - y2), 0.0, 1.0)


# The side camera (Insta360 Link 2C Pro) skews saturated colours while keeping
# neutrals neutral: the red 3 reads magenta, purple reads blue, cloth reads
# teal. Its hue ORDER is intact, so a hue-to-hue curve fixes it where a 3x3
# matrix cannot (it renders the green 6 at the cloth's hue). Anchors are
# (camera hue -> true hue) in degrees, measured on staged balls 2026-11-15 at
# camera saturation 40: cloth 193 -> 206 (the overhead's hue), cushion shadow
# 211 -> 224, purple 4 226 -> 256, maroon 15 279 -> 340, red 11 326 -> 350,
# red 3 333 -> 358, orange 13 7 -> 25, yellow 9 52 -> 48. Purple can't go
# further: shadowed cushion cloth sits 15 degrees below it and turns violet.
SIDECAM_HUE = [
    (7, 25),
    (52, 48),
    (120, 120),
    (170, 182),
    (193, 206),
    (211, 224),
    (226, 256),
    (279, 340),
    (326, 350),
    (333, 358),
]
# Saturation gain by camera hue: puts back what camera saturation 40 holds
# back (higher clips the cloth's blue channel), less on the cloth itself.
SIDECAM_SAT = [
    (0, 1.4),
    (30, 1.5),
    (60, 1.45),
    (100, 1.2),
    (170, 1.0),
    (193, 0.9),
    (215, 1.0),
    (240, 1.15),
    (300, 1.2),
    (333, 1.25),
    (360, 1.4),
]


def _rgb_to_hsv(rgb: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    v = rgb.max(axis=1)
    c = v - rgb.min(axis=1)
    s = np.where(v > 0, c / np.maximum(v, 1e-9), 0.0)
    r, g, b = rgb.T
    cc = np.maximum(c, 1e-9)
    h = np.where(v == r, (g - b) / cc, np.where(v == g, 2 + (b - r) / cc, 4 + (r - g) / cc))
    return (h * 60) % 360, s, v


def _hsv_to_rgb(h: np.ndarray, s: np.ndarray, v: np.ndarray) -> np.ndarray:
    k = (np.array([5, 3, 1])[None, :] + h[:, None] / 60) % 6
    return v[:, None] - v[:, None] * s[:, None] * np.clip(np.minimum(k, 4 - k), 0, 1)


def _circular_interp(h: np.ndarray, anchors: list[tuple[int, int]]) -> np.ndarray:
    xs = [float(a) for a, _ in anchors]
    ys = [float(b) for _, b in anchors]
    for i in range(1, len(ys)):  # unwrap the targets so the curve is monotone
        while ys[i] < ys[i - 1]:
            ys[i] += 360
    xs = [xs[-1] - 360, *xs, xs[0] + 360]
    ys = [ys[-1] - 360, *ys, ys[0] + 360]
    return np.interp(h, xs, ys) % 360


def _hue_sat(
    rgb: np.ndarray,
    hue: list[tuple[int, int]],
    sat: list[tuple[int, float]],
    fade_lo: float,
    fade_hi: float,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Hue-to-hue curve + saturation gain by hue, faded in over saturation
    `fade_lo`..`fade_hi` so near-neutrals keep their (noisy) hue. -> (h, s, v)."""
    h, s, v = _rgb_to_hsv(rgb)
    fade = np.clip((s - fade_lo) / (fade_hi - fade_lo), 0, 1)
    dh = (_circular_interp(h, hue) - h + 180) % 360 - 180
    gain = np.interp(h, [a for a, _ in sat], [g for _, g in sat])
    return (h + fade * dh) % 360, s * (1 + fade * (gain - 1)), v


def sidecam(rgb: np.ndarray, _ccm: np.ndarray | None = None) -> np.ndarray:
    """(N, 3) float RGB in [0, 1] -> the side camera's corrected RGB."""
    h, s, v = _hue_sat(rgb, SIDECAM_HUE, SIDECAM_SAT, 0.0, 0.10)
    dark = np.clip((v - 0.10) / 0.25, 0.3, 1)  # the cloth's teal spill on blacks
    return np.clip(_hsv_to_rgb(h, np.clip(s * dark, 0, 1), v), 0, 1)


# The overhead (Basler, no colour matrix) has the opposite fault to the side
# camera: channel overlap pulls the warm balls together and washes them out.
# The red 3 reads orange (15), the maroon 7 a brown-orange (10), the yellow 1
# an orange-yellow (38), all at saturation ~0.65 where the corrected side
# camera shows ~1.0. The banked CCM fixes that but triples the cloth's chroma
# noise; a hue curve leaves the cloth (205, gain 1.0) untouched. Anchors are
# (hue after the tone grade -> true hue), measured on staged balls through OBS
# 2026-11-16: maroon 7 10 -> 348, red 3 15 -> 0, orange 5 27 -> 26, yellow 1
# 38 -> 48. Green (166), cloth (205), blue 2 (214) and purple 4 (251) already
# read true and only gain saturation, ramping up either side of the cloth.
OVERHEAD_TONE = 0.459  # the tone grade's Apply LUT amount Steven dialed in
OVERHEAD_HUE = [(10, 348), (15, 0), (27, 26), (38, 48), (120, 120), (260, 260)]
OVERHEAD_SAT = [
    (0, 1.45),
    (40, 1.45),
    (120, 1.4),
    (175, 1.4),
    (195, 1.0),
    (210, 1.0),
    (222, 1.25),
    (260, 1.4),
    (360, 1.45),
]


def overhead(rgb: np.ndarray, _ccm: np.ndarray | None = None) -> np.ndarray:
    """(N, 3) float RGB in [0, 1] -> the overhead's graded RGB: the tone grade
    at its dialed-in amount, then the hue/saturation correction."""
    rgb = rgb + OVERHEAD_TONE * (grade(rgb, None) - rgb)
    # Balls sit at saturation >= 0.42 and the cue ball / black 8 at ~0.12, so
    # the correction fades in between; below that the tint is halved (the cue
    # ball reads cream at 0.10, where the side camera shows it white).
    h, s, v = _hue_sat(rgb, OVERHEAD_HUE, OVERHEAD_SAT, 0.15, 0.35)
    s = s * np.interp(s, [0.10, 0.25], [0.5, 1.0])
    return np.clip(_hsv_to_rgb(h, np.clip(s, 0, 1), v), 0, 1)


def write_cube(path: Path, title: str, ccm: np.ndarray | None, fn: Grade = grade) -> None:
    n = LUT_SIZE
    axis = np.linspace(0.0, 1.0, n)
    # .cube ordering: red index varies fastest, then green, then blue
    b, g, r = np.meshgrid(axis, axis, axis, indexing="ij")
    rgb = np.column_stack([r.ravel(), g.ravel(), b.ravel()])
    out = fn(rgb, ccm)
    lines = [f'TITLE "{title}"', f"LUT_3D_SIZE {n}"]
    lines += [f"{p[0]:.6f} {p[1]:.6f} {p[2]:.6f}" for p in out]
    path.write_text("\n".join(lines) + "\n")
    print(f"wrote {path} ({n}^3 = {n**3} entries)")


if __name__ == "__main__":
    out_dir = Path(__file__).resolve().parents[1] / "assets" / "obs"
    out_dir.mkdir(parents=True, exist_ok=True)
    write_cube(out_dir / "cueclock-tone.cube", "cueclock tone map", None)
    write_cube(out_dir / "cueclock-tone-ccm.cube", "cueclock tone map + CCM", CCM_RGB)
    write_cube(out_dir / "cueclock-sidecam.cube", "cueclock side camera", None, sidecam)
    write_cube(out_dir / "cueclock-overhead.cube", "cueclock overhead", None, overhead)
