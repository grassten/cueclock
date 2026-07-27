# Audio / Microphone Plan

Plan of record for the microphone path. **Not built yet** — this documents what it
will do and how it will be set up, so the build (queue item 6 in `progress.md`) is
mechanical when we get to it.

## What the microphone is used for

1. **v1 — voice "extension"** (the only shipped use). Player says "extension" →
   clock extension, same as the Stream Deck button. Re-implements the old app's
   proven listener verbatim (`cv-app-rewrite.md` §2.1 audio row): Vosk
   small-EN model, grammar `["extension", "[unk]"]`, 16 kHz mono, min confidence
   0.9, 2 s cooldown.
2. **Backlog — acoustic strike-click corroborator** (`cv-app-rewrite.md` §4.2,
   deferred by decision §8). A broadband transient ("click") detector on the same
   audio stream, coincident within ~100 ms of the cue-ball velocity impulse. It
   resolves the one genuinely ambiguous vision case: slow cue-tip nudge (no click)
   vs. soft legal shot (click). Build **only if** the recorded corpus shows slow
   nudges still leaking through per-ball tracking.
3. **Nothing else.** Overlay chime sounds come from the browser page's Web Audio
   (through OBS), not the mic. OBS owns the iPhone Continuity Camera and its mic —
   cueclock must never grab that device.

## Is any of this microphone-specific? (the "calibration" question)

No calibration in the camera-intrinsics sense. Keyword spotting is mic-agnostic;
audio is resampled/captured at 16 kHz mono regardless of hardware. The
device-specific surface is small and all config-level:

- **Device selection** — pin the input by CoreAudio device *name* in config (e.g.
  `"Studio Display Microphone"`), never "system default": macOS reassigns the
  default when devices appear/disappear, and OBS must not steal or be robbed of
  its own inputs.
- **Input level** — macOS System Settings → Sound → Input; set so speech peaks
  well below clipping. One-time per mic.
- **Two knobs, not calibration files** — `min_confidence` (0.9) and
  `cooldown_seconds` (2.0) may want a light retune if the mic or its placement
  changes. Expected drift: small.
- **Future click detector only:** the transient threshold depends on mic distance
  to the table and room acoustics — design it relative to a rolling noise floor
  (not absolute level) so a mic/placement swap is a re-check, not a redesign.

**Consequence:** building against the Studio Display mic now is fine; swapping to
a discrete mic later is a config-string change + a short verification session.

## Hardware path

- **Now:** Mac Studio display's built-in mic. Perfectly adequate for keyword
  spotting.
- **Later (wall TV, no display mic):** any cheap wired USB mic works for
  "extension" — this is not a demanding task. If/when the click corroborator is
  built, prefer placement with line-of-sight to the table a few meters max, since
  it must hear both speech and ball impacts. A USB boundary/conference mic near
  the table covers both. Avoid Bluetooth (latency + macOS input flakiness).

## Software design (when built)

- `src/cueclock/audio/` — port of the old `audio.py` `ExtensionListener`:
  `sounddevice` input stream (16 kHz mono int16) → Vosk `KaldiRecognizer` with the
  two-word grammar → confidence + cooldown gate → extension callback into the
  server `Hub` (worker-thread → asyncio bridge, same pattern as the old app).
- **Deps:** `uv add vosk sounddevice`.
- **Model:** `vosk-model-small-en-us-0.15` (~40 MB) — local download into
  `models/` (gitignored, like `tests/clips/`); never committed. Document the
  download URL/command in the module docstring.
- **Testability without a mic:** the recognizer+gate logic should accept raw PCM
  chunks, so it can be unit-tested by feeding a recorded WAV of "extension"
  (local gitignored fixture, test skips when absent — same policy as clips).
  Only the device-capture shell is untestable offline.

## Bring-up checklist (when built)

1. `uv add vosk sounddevice`; download model to `models/`.
2. List devices: `uv run python -c "import sounddevice as sd; print(sd.query_devices())"`.
3. Put the device name in config; confirm OBS inputs are untouched.
4. Live test: 10× "extension" at playing distance (standing at the table, head
   down over a shot — not facing the mic), confirm all accepted; confirm the 2 s
   cooldown swallows the echo of your own "extension" repeated back.
5. False-positive soak: one full practice session with the listener logging but
   not acting — balls clacking, chalk taps, any music/podcast audio must produce
   zero accepted "extension" hits before wiring it to the clock.
