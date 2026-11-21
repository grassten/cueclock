"""Extension spotter + device matching — the pure parts.

The recognizer is faked with scripted results, so these run everywhere:
no vosk, no model, no microphone (mic paths are exercised live via
`cueclock listen`, like the camera and the deck).
"""

from __future__ import annotations

import json
import math
import time

import numpy as np
import pytest

from cueclock.audio.listener import MIC_GAIN_DB, ExtensionListener
from cueclock.audio.spotter import ExtensionSpotter, match_input_device


class FakeRecognizer:
    """Scripted vosk stand-in: each fed chunk pops the next scripted result.

    Script items: None = chunk did not finalize; a JSON string = the final
    ``Result()`` for that chunk.
    """

    def __init__(self, script: list[str | None]) -> None:
        self._script = list(script)
        self._result = ""

    def AcceptWaveform(self, data: bytes) -> bool:  # noqa: N802 - vosk API name
        item = self._script.pop(0)
        if item is None:
            return False
        self._result = item
        return True

    def Result(self) -> str:  # noqa: N802 - vosk API name
        return self._result


def final(text: str, conf: float = 0.95) -> str:
    words = [{"word": w, "conf": conf} for w in text.split()]
    return json.dumps({"text": text, "result": words})


def spotter_with(script: list[str | None]) -> ExtensionSpotter:
    return ExtensionSpotter(FakeRecognizer(script))


# --- accept path ---


def test_fires_on_confident_exact_keyword() -> None:
    spotter = spotter_with([final("extension", conf=0.97)])
    assert spotter.feed(b"pcm", now=0.0)  # now=0.0: first fire must not be cooldown-blocked
    assert spotter.last_final is not None
    assert spotter.last_final.accepted
    assert spotter.last_final.confidence == 0.97


def test_confidence_reads_the_keyword_word_only() -> None:
    # [unk] words in the result must not stand in for the keyword's confidence.
    result = json.dumps(
        {
            "text": "extension",
            "result": [{"word": "[unk]", "conf": 1.0}, {"word": "extension", "conf": 0.93}],
        }
    )
    spotter = spotter_with([result])
    assert spotter.feed(b"pcm", now=0.0)
    assert spotter.last_final is not None
    assert spotter.last_final.confidence == 0.93


# --- reject paths ---


def test_ignores_non_final_chunks() -> None:
    spotter = spotter_with([None, None])
    assert not spotter.feed(b"pcm", now=0.0)
    assert not spotter.feed(b"pcm", now=1.0)
    assert spotter.last_final is None  # nothing finalized, nothing to report


def test_ignores_other_words() -> None:
    spotter = spotter_with([final("attention")])
    assert not spotter.feed(b"pcm", now=0.0)
    assert spotter.last_final is not None
    assert not spotter.last_final.accepted


def test_a_back_to_back_repeat_is_one_call() -> None:
    # Measured 2026-11-16: five quick "extension"s merged into one final.
    result = json.dumps(
        {
            "text": "extension extension extension",
            "result": [{"word": "extension", "conf": c} for c in (1.0, 0.95, 1.0)],
        }
    )
    spotter = spotter_with([result])
    assert spotter.feed(b"pcm", now=0.0)


def test_ignores_sentences_containing_the_keyword() -> None:
    # Exact-match rule: "extension please" is not a call (v1 behavior).
    spotter = spotter_with([final("extension please")])
    assert not spotter.feed(b"pcm", now=0.0)
    assert spotter.last_final is not None
    assert not spotter.last_final.accepted


def test_ignores_low_confidence() -> None:
    spotter = spotter_with([final("extension", conf=0.5)])
    assert not spotter.feed(b"pcm", now=0.0)
    assert spotter.last_final is not None
    assert not spotter.last_final.accepted


def test_empty_final_is_reported_with_its_peak_level() -> None:
    # A wordless final never fires, but it is reported with the loudest
    # sample since the last final, so an unheard "extension" leaves a trace.
    spotter = spotter_with([None, json.dumps({"text": ""}), final("extension")])
    loud = np.array([0, -16384, 100], dtype=np.int16).tobytes()  # half scale
    assert not spotter.feed(loud, now=0.0)
    assert not spotter.feed(bytes(6), now=0.25)
    assert spotter.last_final is not None
    assert spotter.last_final.reason == "no words"
    assert spotter.last_final.peak_dbfs == pytest.approx(-6.02, abs=0.01)
    assert spotter.feed(bytes(6), now=0.5)  # the peak resets at each final
    assert spotter.last_final is not None
    assert spotter.last_final.peak_dbfs == -math.inf


# --- gain ---
#
# The side camera's mic delivers a voice from the table far below the level
# the recognizer needs (`listener.MIC_GAIN_DB` has the measurement), so the
# samples are amplified on their way in. The reported peak stays the mic's.


class RecordingRecognizer(FakeRecognizer):
    """A fake that keeps what it was fed."""

    def __init__(self, script: list[str | None]) -> None:
        super().__init__(script)
        self.fed: list[bytes] = []

    def AcceptWaveform(self, data: bytes) -> bool:  # noqa: N802 - vosk API name
        self.fed.append(data)
        return super().AcceptWaveform(data)


def test_gain_amplifies_what_the_recognizer_hears_and_clips_at_full_scale() -> None:
    rec = RecordingRecognizer([None])
    spotter = ExtensionSpotter(rec, gain_db=40.0)  # x100
    chunk = np.array([0, 50, -50, 400, -400], dtype=np.int16).tobytes()
    spotter.feed(chunk, now=0.0)
    heard = np.frombuffer(rec.fed[0], dtype=np.int16)
    assert heard.tolist() == [0, 5000, -5000, 32767, -32768]


def test_the_reported_peak_is_the_mic_level_before_the_gain() -> None:
    rec = RecordingRecognizer([json.dumps({"text": ""})])
    spotter = ExtensionSpotter(rec, gain_db=40.0)
    spotter.feed(np.array([0, -16384, 100], dtype=np.int16).tobytes(), now=0.0)
    assert spotter.last_final is not None
    assert spotter.last_final.peak_dbfs == pytest.approx(-6.02, abs=0.01)


def test_no_gain_feeds_the_chunk_untouched() -> None:
    rec = RecordingRecognizer([None])
    ExtensionSpotter(rec).feed(b"pcm", now=0.0)
    assert rec.fed == [b"pcm"]


def test_the_listener_amplifies_by_the_measured_gain() -> None:
    assert ExtensionListener(lambda: None).gain_db == MIC_GAIN_DB == 40.0


# --- cooldown ---


def test_cooldown_suppresses_then_rearms() -> None:
    spotter = spotter_with([final("extension")] * 3)
    assert spotter.feed(b"pcm", now=10.0)
    assert not spotter.feed(b"pcm", now=11.0)  # within the 2 s cooldown
    assert spotter.feed(b"pcm", now=12.0)  # >= 2 s after the fire: re-armed


def test_rejected_hits_do_not_reset_the_cooldown() -> None:
    spotter = spotter_with([final("extension"), final("extension", conf=0.1), final("extension")])
    assert spotter.feed(b"pcm", now=10.0)
    assert not spotter.feed(b"pcm", now=11.5)  # low confidence, no fire
    assert spotter.feed(b"pcm", now=12.0)  # cooldown still counts from 10.0


# --- announcement mute window ---
#
# The overlay speaks "Extension called" out of the TV on every granted
# extension, in the same room as this microphone, containing this keyword.
# The window is what stops the announcement from calling the next extension.
# (The feed loop's use of it — silence in, no fire out — needs a real stream,
# so it is exercised live via `cueclock listen`, like the rest of the mic path.)


def test_mute_window_opens_and_expires() -> None:
    listener = ExtensionListener(lambda: None)
    assert not listener.muted(time.monotonic())
    listener.mute(1.0)
    assert listener.muted(time.monotonic())
    assert not listener.muted(time.monotonic() + 1.5)


def test_mute_never_shortens_an_open_window() -> None:
    # Announcements can overlap — a refused call's tone landing under the
    # granted call's phrase. The mic stays deaf for the longer of the two.
    listener = ExtensionListener(lambda: None)
    listener.mute(5.0)
    listener.mute(0.1)
    assert listener.muted(time.monotonic() + 2.0)


# --- device matching (docs/hardware.md Part D: pin by name, never default) ---

INPUTS = [(0, "iPhone Microphone"), (1, "Mac mini Microphone"), (2, "External Microphone")]


def test_device_exact_match_wins() -> None:
    assert match_input_device("Mac mini Microphone", INPUTS) == 1


def test_device_exact_beats_substring_order() -> None:
    # "Microphone" substring-matches index 0 first, but an exact name elsewhere wins.
    inputs = [(0, "mac mini microphone"), (1, "Mac mini Microphone")]
    assert match_input_device("Mac mini Microphone", inputs) == 1


def test_device_case_insensitive_substring() -> None:
    assert match_input_device("mac mini", INPUTS) == 1
    assert match_input_device("EXTERNAL", INPUTS) == 2


def test_device_no_match_is_none_not_default() -> None:
    assert match_input_device("USB Lavalier", INPUTS) is None
    assert match_input_device("anything", []) is None
