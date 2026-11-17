"""A settle's still with its ball list (#54) drawn on it, for the review page.

So a wrong label can be seen from the couch: a ring at each listed ball's
visible edge and its label beside it, on the raw still the list was read
from (or the settle still it was in force at).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import cv2
import numpy as np

from cueclock.vision.ball_list import BallList, edge_radius

WIDTH = 1280  # px: sharp on a phone held sideways, ~150 KB a JPEG
_INK = (255, 255, 255)
_FONT = cv2.FONT_HERSHEY_SIMPLEX
_RING = {"cue": (255, 255, 255), "?": (0, 165, 255)}  # BGR; numbers get green


def render(still_png: Path, record: dict[str, Any] | None) -> bytes:
    """JPEG bytes of the still, demosaiced, with ``record``'s balls drawn."""
    bayer = cv2.imread(str(still_png), cv2.IMREAD_UNCHANGED)
    if bayer is None:
        raise FileNotFoundError(still_png)
    img = cv2.cvtColor(np.asarray(bayer, dtype=np.uint8), cv2.COLOR_BayerBG2BGR)
    k = WIDTH / img.shape[1]
    img = cv2.resize(img, None, fx=k, fy=k, interpolation=cv2.INTER_AREA)
    if record is not None:
        for ball in BallList.from_dict(record).balls:
            x, y = round(ball.cx * k), round(ball.cy * k)
            r = round(edge_radius(ball.r_pred) * k)
            cv2.circle(img, (x, y), r + 2, _RING.get(ball.label, (80, 220, 80)), 2, cv2.LINE_AA)
            org = (x + r + 3, y - r)
            for colour, thick in (((0, 0, 0), 4), (_INK, 1)):  # outlined, readable on cloth
                cv2.putText(img, ball.label, org, _FONT, 0.6, colour, thick, cv2.LINE_AA)
    ok, jpg = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 82])
    if not ok:
        raise ValueError(f"could not encode {still_png}")
    return bytes(jpg.tobytes())
