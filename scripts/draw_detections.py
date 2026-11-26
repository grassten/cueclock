"""Draw what the vision pipeline knows about one frame of a recorded clip.

The README's detection figures are made with this. It replays a clip from its
first frame up to the chosen one, so the tracker holds real tracks and
velocities there, and writes three images of that frame:

    frame.jpg       the raw frame, debayered and nothing else: distorted, as
                    the lens delivers it and as the detector reads it
    difference.jpg  |frame - empty-table reference| in gray inside the cloth
                    mask, the image the detector thresholds into blobs
    detections.jpg  the frame with the pipeline's state drawn on it: the cloth
                    mask, every tracked ball at its predicted radius, the cue
                    ball, velocity arrows with speeds, and the pocket zones

Nothing is detected here. Every position, radius and speed drawn comes from
`VisionPipeline` and the calibration, mapped back from table millimetres into
the distorted image. Only an arrow's direction is worked out in this file, from
where the track was a moment earlier: the tracker reports speeds, not headings.

The reference must be the empty-table clip recorded on the same camera mount as
the clip (`REFERENCES` in tests/corpus/test_corpus_events.py lists the pairs).
Its own `table_calibration.json` is used when it has one, as the corpus tests do.

Usage:
    uv run python scripts/draw_detections.py tests/clips/break_dark_flight_0806 948 \
        docs/media --reference tests/clips/empty_table_0801 --prefix detect-
"""

import argparse
from collections import deque
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
import numpy.typing as npt

from cueclock.capture.clip import ClipReader
from cueclock.vision.calibration import (
    DEFAULT_CALIBRATION_PATH,
    TableCalibration,
    table_mm_to_raw_px,
)
from cueclock.vision.detect import bayer_to_gray
from cueclock.vision.events import BallState, EventConfig
from cueclock.vision.pipeline import BALL_RADIUS_MM, ReferenceModel, VisionPipeline

Image = npt.NDArray[np.uint8]
Points = npt.NDArray[np.float64]
Colour = tuple[int, int, int]  # BGR

REFERENCE_FRAMES = 15  # as `cueclock analyze` and the corpus tests sample it
DIFFERENCE_GAIN = 4.0  # shown brighter than computed, or a shadow on the cloth prints as black
HEADING_S = 0.1  # a ball's heading is its track's displacement over this long
ARROW_S = 0.5  # an arrow is as long as the ball travels in this time
JPEG_QUALITY = 85

MASK: Colour = (255, 255, 0)
POCKET: Colour = (60, 140, 255)
BALL: Colour = (120, 255, 120)
CUE: Colour = (0, 230, 255)
HIDDEN: Colour = (200, 200, 200)
VELOCITY: Colour = (255, 255, 255)
FONT = cv2.FONT_HERSHEY_SIMPLEX
FONT_SCALE = 0.4
SHIFT = 4  # sub-pixel bits OpenCV draws with


@dataclass(frozen=True)
class Scene:
    """One frame, and the pipeline's state once it has processed it."""

    bayer: Image
    reference: ReferenceModel
    calibration: TableCalibration
    events: EventConfig
    balls: list[BallState]
    headings: dict[int, Points]  # ball_id -> unit vector in table mm

    def pixels(self, points_mm: Points) -> Points:
        """Table millimetres -> pixels of the distorted frame."""
        return table_mm_to_raw_px(self.calibration, points_mm)

    def moving(self, ball: BallState) -> bool:
        """Above the event engine's own floor between still and moving."""
        return ball.speed_mms >= self.events.settle_floor_mms


def replay(clip: Path, frame: int, reference: Path, calibration_path: Path | None) -> Scene:
    """Run the clip through the pipeline up to `frame`, as the corpus tests do."""
    own = reference / DEFAULT_CALIBRATION_PATH.name
    calibration = TableCalibration.load(
        calibration_path or (own if own.exists() else DEFAULT_CALIBRATION_PATH)
    )
    with ClipReader(reference) as empty:
        picks = np.linspace(0, len(empty) - 1, num=min(REFERENCE_FRAMES, len(empty)), dtype=int)
        model = ReferenceModel.build([empty.frame(int(i)) for i in picks], calibration)
    with ClipReader(clip) as reader:
        if not 0 <= frame < len(reader):
            raise SystemExit(f"{clip} holds frames 0 to {len(reader) - 1}, not {frame}")
        pipeline = VisionPipeline(calibration, model, cadence_hz=reader.cadence_hz)
        # Where every track was on each of the last few frames, oldest first.
        trail: deque[dict[int, Points]] = deque(maxlen=round(HEADING_S * reader.cadence_hz) + 1)
        for i in reader.detect_indices()[: frame + 1]:
            pipeline.process(float(reader.timestamps[i]), reader.frame(i))
            trail.append({b.ball_id: np.array([b.x_mm, b.y_mm]) for b in pipeline.last_balls})
        bayer = reader.frame(frame)
    headings: dict[int, Points] = {}
    for ball_id, now in trail[-1].items():
        step = now - next(past[ball_id] for past in trail if ball_id in past)
        if np.any(step):
            headings[ball_id] = step / np.linalg.norm(step)
    return Scene(bayer, model, calibration, pipeline.event_config, pipeline.last_balls, headings)


def difference(scene: Scene) -> Image:
    """The detector's first step: gray |frame - reference|, on the cloth only."""
    diff = cv2.absdiff(bayer_to_gray(scene.bayer), scene.reference.gray)
    diff = cv2.bitwise_and(diff, diff, mask=scene.reference.mask)
    return np.asarray(cv2.convertScaleAbs(diff, alpha=DIFFERENCE_GAIN), dtype=np.uint8)


def _fixed(points: Points, scale: float) -> npt.NDArray[np.int32]:
    """Frame pixels as the fixed-point output pixels OpenCV draws at."""
    return np.asarray(np.round(points * scale * (1 << SHIFT)), dtype=np.int32)


def _text(image: Image, text: str, x: float, y: float) -> None:
    """Small text with a dark edge, so it reads on cloth and on carpet alike."""
    at = (round(x), round(y))
    cv2.putText(image, text, at, FONT, FONT_SCALE, (0, 0, 0), 3, cv2.LINE_AA)
    cv2.putText(image, text, at, FONT, FONT_SCALE, (255, 255, 255), 1, cv2.LINE_AA)


def _text_width(text: str) -> int:
    return int(cv2.getTextSize(text, FONT, FONT_SCALE, 1)[0][0])


def _legend(image: Image, rows: list[tuple[str, Colour]]) -> None:
    """A swatch and a label per row, on a shaded box in the top-left corner."""
    pad, swatch, line = 8, 18, 17
    width = pad * 3 + swatch + max(_text_width(label) for label, _ in rows)
    box = image[pad : pad * 2 + line * len(rows), pad : pad + width]
    box[:] = box // 3
    for row, (label, colour) in enumerate(rows):
        y = pad + line * row + line // 2 + pad // 2
        cv2.line(image, (pad * 2, y), (pad * 2 + swatch, y), colour, 2, cv2.LINE_AA)
        _text(image, label, pad * 3 + swatch, y + 4)


def detections(scene: Scene, frame: Image, scale: float) -> Image:
    """`frame` (already at output size) with the pipeline's state drawn on it."""
    image = frame.copy()
    drawn: set[Colour] = {MASK, POCKET}
    outlines, _ = cv2.findContours(scene.reference.mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    for outline in outlines:
        points = _fixed(outline.reshape(-1, 2).astype(np.float64), scale)
        cv2.polylines(image, [points], True, MASK, 1, cv2.LINE_AA, SHIFT)

    turn = np.linspace(0.0, 2 * np.pi, 72, endpoint=False)
    ring = scene.events.pocket_zone_radius_mm * np.column_stack([np.cos(turn), np.sin(turn)])
    for pocket in scene.events.pocket_centers():
        zone = _fixed(scene.pixels(np.array(pocket) + ring), scale)
        cv2.polylines(image, [zone], True, POCKET, 1, cv2.LINE_AA, SHIFT)

    height, width = scene.bayer.shape
    for ball in scene.balls:
        at_mm = np.array([ball.x_mm, ball.y_mm])
        at = scene.pixels(at_mm[None])[0]
        if not (0 <= at[0] < width and 0 <= at[1] < height):
            continue  # a track coasting on with nothing to see can leave the frame
        radius = float(scene.reference.radius_px[int(at[1]), int(at[0])]) * scale
        colour = HIDDEN if not ball.observed else CUE if ball.is_cue else BALL
        drawn.add(colour)
        cv2.circle(
            image,
            _fixed(at, scale).tolist(),
            round(radius * (1 << SHIFT)),
            colour,
            2 if ball.is_cue else 1,
            cv2.LINE_AA,
            SHIFT,
        )

        heading = scene.headings.get(ball.ball_id)
        if heading is None or not scene.moving(ball):
            continue
        drawn.add(VELOCITY)
        # From the ball's edge, as far as it will roll in ARROW_S at this speed.
        reach = BALL_RADIUS_MM + np.array([0.0, ball.speed_mms * ARROW_S])
        ends = scene.pixels(at_mm + np.outer(reach, heading))
        along = (ends[1] - ends[0]) * scale
        length = max(float(np.linalg.norm(along)), 1.0)
        tail, head = _fixed(ends, scale).tolist()
        cv2.arrowedLine(
            image, tail, head, VELOCITY, 1, cv2.LINE_AA, SHIFT, tipLength=min(0.5, 6.0 / length)
        )
        # The speed sits behind the ball, clear of its own arrow.
        label = f"{ball.speed_mms:.0f}"
        behind = at * scale - (radius + 12) * along / length
        _text(image, label, behind[0] - _text_width(label) / 2, behind[1] + 4)

    legend = [
        ("cloth mask", MASK),
        ("pocket zone", POCKET),
        ("tracked ball, predicted radius", BALL),
        ("cue ball", CUE),
        ("tracked, not seen in this frame", HIDDEN),
        ("velocity, mm/s", VELOCITY),
    ]
    _legend(image, [row for row in legend if row[1] in drawn])
    return image


def main() -> None:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("clip", type=Path, help="clip directory (meta.json, frames.raw)")
    parser.add_argument("frame", type=int, help="index of the frame to draw")
    parser.add_argument("out", type=Path, help="directory the three images are written to")
    parser.add_argument(
        "--reference",
        type=Path,
        default=Path("tests/clips/empty_table"),
        help="empty-table clip from the same camera mount (default tests/clips/empty_table)",
    )
    parser.add_argument(
        "--calibration",
        type=Path,
        help="table calibration JSON (default: the reference clip's own, else config/)",
    )
    parser.add_argument("--width", type=int, default=1280, help="output width in pixels")
    parser.add_argument("--prefix", default="", help="put before each file name")
    args = parser.parse_args()

    scene = replay(args.clip, args.frame, args.reference, args.calibration)
    scale = args.width / scene.bayer.shape[1]
    size = (args.width, round(scene.bayer.shape[0] * scale))
    frame = np.asarray(
        cv2.resize(
            cv2.cvtColor(scene.bayer, cv2.COLOR_BayerBG2BGR), size, interpolation=cv2.INTER_AREA
        ),
        dtype=np.uint8,
    )
    images = {
        "frame": frame,
        "difference": cv2.resize(difference(scene), size, interpolation=cv2.INTER_AREA),
        "detections": detections(scene, frame, scale),
    }
    args.out.mkdir(parents=True, exist_ok=True)
    for name, image in images.items():
        path = args.out / f"{args.prefix}{name}.jpg"
        cv2.imwrite(str(path), image, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
        print(f"{path}  {image.shape[1]}x{image.shape[0]}  {path.stat().st_size} bytes")
    moving = sum(scene.moving(ball) for ball in scene.balls)
    print(f"{len(scene.balls)} tracked balls, {moving} moving")


if __name__ == "__main__":
    main()
