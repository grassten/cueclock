"""Microphone stream + Vosk model wiring for the extension spotter.

All audio I/O lives here; vosk and sounddevice imports are function-local so
the module imports everywhere (CI has no portaudio and no model — the same
pattern as the deck daemon's StreamDeck imports). The accept/reject logic
itself is `spotter.ExtensionSpotter`, which stays pure.

Every print here flushes, like `deck/session.py`'s. A deck-started session's
stdout is an **append-mode handle on a log shared with every other session**
(`session.RUN_LOG`), so block-buffered lines surface whenever a 4 KB buffer
happens to fill — which put one session's "listener started (mic: …)" line
*below* a later session's on 2026-08-03 and made the log say the running
session had bound a microphone it never touched.
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
# How long the mic stays deaf after a granted extension, because the overlay
# answers it by speaking "Extension called" out of the TV — in the same room,
# into this microphone, containing this keyword. Anchored to the asset:
# scripts/make_voice_assets.py prints the clip's duration — 0.98 s for the
# shipped render (Kokoro af_bella at speed 1.10) — plus the
# broadcast->decode->speaker lag and a margin for a slower voice swapped in
# later. The 2 s spotter cooldown is NOT a substitute: Vosk
# finalizes at end-of-utterance, so the announcement's own final can land
# after the cooldown expires (docs/hardware.md Part D).
ANNOUNCE_MUTE_SECONDS = 2.5
# The iPhone's microphone, over the USB cable the phone is already on for the
# side-angle camera. The Mac mini has no built-in mic, so this is the rig's
# only real input; a name, not an index, and never "system default" — see
# match_input_device in spotter.py for why.
#
# Camo's device, not macOS's Continuity one. Both carry the same physical
# microphone, and the reason to prefer Camo's is that the phone is already
# Camo's: opening `<owner>'s iPhone Microphone` makes macOS claim the phone as
# a Continuity device alongside Camo's USB session, and the side-angle feed is
# what pays for it.
#
# `Camo Microphone` streaming digital silence — measured 2026-08-03, peak 0 /
# rms 0.0 / zero non-zero samples over 4 s with speech in the room, at 48 kHz
# stereo, 48 kHz mono and 16 kHz mono alike — is **not** a property of the
# device, which is what an earlier note here concluded. It is Camo Studio's own
# audio toggle being off (`audioSettings.enabled: false` in its preferences):
# the CoreAudio driver loads and opens without error whether or not Camo is
# feeding it, so "off" and "nobody said anything" look identical from here.
# Turning audio on in Camo Studio is the fix; docs/hardware.md Part D has the
# check that tells the two apart in one command.
DEFAULT_DEVICE = "Camo Microphone"
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
        print(f"[audio] no input device matches {name!r} — available: {available}", flush=True)
        print(
            "[audio] extension listener disabled (never falls back to the system default)",
            flush=True,
        )
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
        self._muted_until = 0.0  # monotonic; see mute()

    def mute(self, seconds: float = ANNOUNCE_MUTE_SECONDS) -> None:
        """Deafen the mic for `seconds` — something is about to be announced.

        Called from the asyncio loop (Hub.on_extension_granted) while the feed
        loop runs on this listener's thread: one attribute rebind, atomic under
        the GIL, same discipline as Hub.note_frame. Never shortens an existing
        window, so overlapping announcements can only extend the silence.
        """
        self._muted_until = max(self._muted_until, time.monotonic() + seconds)

    def muted(self, now: float) -> bool:
        """Is the mic inside a mute window at monotonic time `now`?"""
        return now < self._muted_until

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
            print(f"[audio] failed to load Vosk model at {self.model_path}: {exc}", flush=True)
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

        print(f"[audio] extension listener started (mic: {device_name!r})", flush=True)
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
                    now = time.monotonic()
                    muted = self.muted(now)
                    # Muted feeds silence rather than skipping the chunk: the
                    # recognizer then sees a clean utterance boundary instead
                    # of splicing post-announcement audio onto whatever it was
                    # already holding. A final can still pop here (the mute
                    # began mid-word, e.g. the deck key granted the extension
                    # while someone was mid-shout) — reported, never fired.
                    fired = spotter.feed(bytes(len(chunk)) if muted else chunk, now)
                    if self.on_final is not None and spotter.last_final is not None:
                        self.on_final(spotter.last_final)
                    if fired and not muted:
                        self.on_accept()
        except Exception as exc:
            print(f"[audio] listener error: {exc}", flush=True)
        finally:
            print("[audio] extension listener stopped", flush=True)
