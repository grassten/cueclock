"""Microphone stream + Vosk model wiring for the extension spotter.

All audio I/O lives here; vosk and sounddevice imports are function-local so
the module imports everywhere (CI has no portaudio and no model — the same
pattern as the deck daemon's StreamDeck imports). The accept/reject logic
itself is `spotter.ExtensionSpotter`, which stays pure.
"""

from __future__ import annotations

import json
import queue
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from cueclock.audio.spotter import ExtensionSpotter, Final, match_input_device

SAMPLE_RATE = 16_000  # Vosk small-EN models are trained at 16 kHz
BLOCKSIZE_FRAMES = 4000  # 250 ms per chunk at 16 kHz (v1 value)
DEFAULT_DEVICE = "Mac mini Microphone"  # the built-in mic's CoreAudio name
DEFAULT_MODEL = Path("models/vosk-model-small-en-us-0.15")  # scripts/fetch_vosk_model.sh


def input_devices() -> list[tuple[int, str]]:
    """(index, name) for every input-capable device, in sounddevice order."""
    import sounddevice as sd

    return [
        (i, str(d["name"]))
        for i, d in enumerate(sd.query_devices())
        if int(d["max_input_channels"]) > 0
    ]


def resolve_input_device(name: str) -> tuple[int, str] | None:
    """Resolve a device name via `match_input_device`; report failure to stdout."""
    devices = input_devices()
    index = match_input_device(name, devices)
    if index is None:
        available = ", ".join(repr(n) for _, n in devices) or "(none)"
        print(f"[audio] no input device matches {name!r} — available: {available}")
        print("[audio] extension listener disabled (never falls back to the system default)")
        return None
    return index, dict(devices)[index]


class ExtensionListener:
    """Owns the mic stream and the feed loop for the word "extension".

    `start()` spawns a daemon thread and `stop()` unwinds it (`cueclock run`);
    `run()` is the blocking loop itself (`cueclock listen` calls it directly).
    `on_accept` fires on every accepted hit; `on_final` (optional) sees every
    non-empty finalized recognition — the `listen` soak tool's feed.
    """

    def __init__(
        self,
        on_accept: Callable[[], None],
        *,
        model_path: Path = DEFAULT_MODEL,
        device_name: str = DEFAULT_DEVICE,
        on_final: Callable[[Final], None] | None = None,
    ) -> None:
        self.on_accept = on_accept
        self.model_path = model_path
        self.device_name = device_name
        self.on_final = on_final
        self._stop = False
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self.run, name="cueclock-audio", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop = True
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def run(self) -> None:
        """Open mic + model and feed the spotter until `stop()` (or Ctrl-C)."""
        import sounddevice as sd
        from vosk import KaldiRecognizer, Model, SetLogLevel

        resolved = resolve_input_device(self.device_name)
        if resolved is None:
            return
        device_index, device_name = resolved
        SetLogLevel(-1)  # silence Kaldi's multi-line model-loading chatter
        try:
            model = Model(str(self.model_path))
        except Exception as exc:
            print(f"[audio] failed to load Vosk model at {self.model_path}: {exc}")
            return
        # Grammar-constrained recognizer: only "extension" (plus [unk]) can
        # ever appear in a result; SetWords gives per-word confidence.
        rec = KaldiRecognizer(model, SAMPLE_RATE, json.dumps(["extension", "[unk]"]))
        rec.SetWords(True)
        spotter = ExtensionSpotter(rec)
        chunks: queue.Queue[bytes] = queue.Queue()

        def _callback(indata: Any, frames: int, t: Any, status: Any) -> None:
            # status = overruns etc. — non-fatal, keep the bytes flowing (v1).
            chunks.put(bytes(indata))

        print(f"[audio] extension listener started (mic: {device_name!r})")
        try:
            with sd.RawInputStream(
                samplerate=SAMPLE_RATE,
                blocksize=BLOCKSIZE_FRAMES,
                dtype="int16",
                channels=1,
                device=device_index,
                callback=_callback,
            ):
                while not self._stop:
                    try:
                        chunk = chunks.get(timeout=0.2)
                    except queue.Empty:
                        continue
                    fired = spotter.feed(chunk, time.monotonic())
                    if self.on_final is not None and spotter.last_final is not None:
                        self.on_final(spotter.last_final)
                    if fired:
                        self.on_accept()
        except Exception as exc:
            print(f"[audio] listener error: {exc}")
        finally:
            print("[audio] extension listener stopped")
