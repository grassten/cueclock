# /// script
# requires-python = ">=3.11"
# dependencies = ["kokoro-onnx>=0.4.9", "soundfile>=0.12", "numpy"]
# ///
"""Regenerate the overlay's spoken cues (docs/hardware.md Part D).

    uv run scripts/make_voice_assets.py

Only needed to change the phrase or the voice — the rendered clip is committed,
so a fresh clone already has it. Kokoro-82M is Apache-2.0, weights and voices
alike, which is why its output can live in a public repo; macOS `say` (the
first cut of this) could not, and forced the overlay to fall back to a chime.

Requires espeak-ng for phonemization: `brew install espeak-ng`. The ~340 MB
model lands in gitignored models/kokoro/ on first run.
"""

from __future__ import annotations

import os
import sys
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
MODEL_DIR = REPO / "models" / "kokoro"
DEST = REPO / "src" / "cueclock" / "server" / "static" / "voice"
RELEASES = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0"
FILES = {
    "kokoro-v1.0.onnx": f"{RELEASES}/kokoro-v1.0.onnx",
    "voices-v1.0.bin": f"{RELEASES}/voices-v1.0.bin",
}

# Chosen by ear against the Ultimate Pool ref call, 2026-08-03: af_bella with
# the exclamation and a 10% speed bump reads as an announcement rather than a
# recital. The stock macOS voices lost this audition outright (the compact ones
# are robotic and the Premium ones read breathy); Siri voices are unreachable
# by any API — hardware.md Part D records that dead end so it is not re-walked.
CUES = [("extension_called.wav", "Extension called!", "af_bella", 1.10, "en-us")]


def espeak_library() -> str | None:
    """Homebrew's libespeak-ng, which phonemizer does not find on its own."""
    brew = Path("/opt/homebrew/lib/libespeak-ng.dylib")
    return str(brew) if brew.is_file() else None


def fetch_model() -> None:
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    for name, url in FILES.items():
        target = MODEL_DIR / name
        if target.is_file():
            continue
        print(f"downloading {name} ...")
        # The scheme is the whole of what bandit B310 warns about (file:// and
        # custom handlers), so check it rather than wave the rule away: these
        # URLs are fixed constants above, and pinning them to https keeps an
        # edit to RELEASES from quietly becoming a local-file read.
        if not url.startswith("https://"):
            raise ValueError(f"refusing a non-https model URL: {url}")
        urllib.request.urlretrieve(url, target)  # nosec B310: https-only, checked above


def main() -> int:
    library = espeak_library()
    if library is not None:
        os.environ.setdefault("PHONEMIZER_ESPEAK_LIBRARY", library)
    fetch_model()

    import soundfile as sf
    from kokoro_onnx import Kokoro

    kokoro = Kokoro(str(MODEL_DIR / "kokoro-v1.0.onnx"), str(MODEL_DIR / "voices-v1.0.bin"))
    DEST.mkdir(parents=True, exist_ok=True)
    for filename, text, voice, speed, lang in CUES:
        samples, rate = kokoro.create(text, voice=voice, speed=speed, lang=lang)
        out = DEST / filename
        # PCM_16 rather than the float default: half the size, and no codec
        # question in OBS's browser, which is the only thing that plays it.
        sf.write(out, samples, rate, subtype="PCM_16")
        seconds = len(samples) / rate
        print(f"wrote {out.relative_to(REPO)}  {voice} @ {speed}x  {seconds:.2f}s")
        print("ANNOUNCE_MUTE_SECONDS (src/cueclock/audio/listener.py) must stay above that.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
