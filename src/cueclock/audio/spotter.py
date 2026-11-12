"""Keyword-spotting gate for the word "extension" — the pure part.

The recognizer is injected (any object with vosk's ``AcceptWaveform``/
``Result`` shape), so this module has no vosk dependency and unit-tests with
a scripted fake. The accept logic is the v1 prototype's proven listener,
ported as-is (docs/hardware.md Part D): finals only, exact keyword match,
per-word confidence >= 0.9, 2 s cooldown.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol


class Recognizer(Protocol):
    """The slice of ``vosk.KaldiRecognizer`` the spotter uses."""

    def AcceptWaveform(self, data: bytes) -> bool:  # noqa: N802 - vosk API name
        ...

    def Result(self) -> str:  # noqa: N802 - vosk API name
        ...


@dataclass(frozen=True)
class Final:
    """One finalized recognition and the spotter's verdict on it."""

    text: str
    confidence: float  # per-word confidence of the keyword (0.0 when absent)
    accepted: bool
    reason: str  # human-readable verdict for the `listen` soak tool


class ExtensionSpotter:
    """Feeds PCM chunks to a recognizer; True from ``feed`` means "fire"."""

    def __init__(
        self,
        recognizer: Recognizer,
        *,
        cooldown_seconds: float = 2.0,
        min_confidence: float = 0.9,
        keyword: str = "extension",
    ) -> None:
        self._rec = recognizer
        self.cooldown_seconds = cooldown_seconds
        self.min_confidence = min_confidence
        self.keyword = keyword
        # None (not 0.0) so the first fire is never suppressed regardless of
        # the caller's clock epoch — the only deliberate change from v1.
        self._last_fire: float | None = None
        self.last_final: Final | None = None  # set per non-empty final, for `listen`

    def feed(self, chunk: bytes, now: float) -> bool:
        """Process one 16 kHz mono int16 chunk; True when the keyword fires."""
        self.last_final = None
        # Only act on finalized results — partials are noisy and misfire on
        # words that merely start like "extension" (v1 lesson).
        if not self._rec.AcceptWaveform(chunk):
            return False
        res = json.loads(self._rec.Result())
        text = str(res.get("text", "")).strip()
        if not text:
            return False  # silence marker, not a recognition
        # Require high confidence on the keyword word itself.
        confidence = max(
            (
                float(w.get("conf", 0.0))
                for w in res.get("result", [])
                if w.get("word") == self.keyword
            ),
            default=0.0,
        )
        if text != self.keyword:
            accepted, reason = False, "not the keyword"
        elif confidence < self.min_confidence:
            accepted, reason = False, f"confidence {confidence:.2f} < {self.min_confidence}"
        elif self._last_fire is not None and now - self._last_fire < self.cooldown_seconds:
            accepted, reason = False, "cooldown"
        else:
            accepted, reason = True, "accepted"
            self._last_fire = now
        self.last_final = Final(text, confidence, accepted, reason)
        return accepted


def match_input_device(name: str, inputs: Sequence[tuple[int, str]]) -> int | None:
    """Pick an input device by CoreAudio name: exact, then case-insensitive substring.

    Returns None when nothing matches — never the system default
    (docs/hardware.md Part D rule: pin by name so a Bluetooth headset or a
    virtual audio device can't silently become the listener).
    """
    for index, device_name in inputs:
        if device_name == name:
            return index
    lowered = name.lower()
    for index, device_name in inputs:
        if lowered in device_name.lower():
            return index
    return None
