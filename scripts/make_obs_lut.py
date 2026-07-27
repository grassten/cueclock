"""Generate 3D LUTs (.cube) for OBS's Apply LUT filter on the Syphon source.

Two files land in assets/obs/:

  cueclock-tone.cube      — hue-preserving tone map + saturation only (safe)
  cueclock-tone-ccm.cube  — the same, preceded by the color-correction matrix
                            measured from the ball set (docs/progress.md
                            2026-07-27). The CCM amplifies chroma noise ~3x;
                            OBS's Apply LUT "amount" slider tempers it live.

The tone math is a port of scripts/live_view.py: black/white point + gamma +
contrast S-curve applied to LUMINANCE, all channels scaled by one factor
(per-channel curves bend hue: red -> orange), then a saturation blend.

Run:  uv run python scripts/make_obs_lut.py
"""

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


def write_cube(path: Path, title: str, ccm: np.ndarray | None) -> None:
    n = LUT_SIZE
    axis = np.linspace(0.0, 1.0, n)
    # .cube ordering: red index varies fastest, then green, then blue
    b, g, r = np.meshgrid(axis, axis, axis, indexing="ij")
    rgb = np.column_stack([r.ravel(), g.ravel(), b.ravel()])
    out = grade(rgb, ccm)
    lines = [f'TITLE "{title}"', f"LUT_3D_SIZE {n}"]
    lines += [f"{p[0]:.6f} {p[1]:.6f} {p[2]:.6f}" for p in out]
    path.write_text("\n".join(lines) + "\n")
    print(f"wrote {path} ({n}^3 = {n**3} entries)")


if __name__ == "__main__":
    out_dir = Path(__file__).resolve().parents[1] / "assets" / "obs"
    out_dir.mkdir(parents=True, exist_ok=True)
    write_cube(out_dir / "cueclock-tone.cube", "cueclock tone map", None)
    write_cube(out_dir / "cueclock-tone-ccm.cube", "cueclock tone map + CCM", CCM_RGB)
