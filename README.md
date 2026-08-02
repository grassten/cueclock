# cueclock

Automated shot clock for Ultimate Pool (USA 8-ball) solo practice, driven by an
overhead machine-vision camera. A Basler ace 2 (1920×1200, global shutter,
~156 fps) watches the table from the ceiling; classical segmentation + per-ball
tracking in true table millimeters detects shots, settles, and scratches; the
events drive a shot-clock state machine rendered as an OBS overlay on a wall
TV, controlled from a Stream Deck. Everything runs on one Mac mini.

## Status (July 2026): the rig is live

Hardware installed and calibrated; the clock runs real practice sessions.
Detection accuracy work is ongoing — every live miss gets captured by a
Stream Deck "Flag" key (last ~20 s of raw frames → a clip named for the
failure reason) and turned into a graded regression scenario. Current
corpus: 22 scenarios, all green.

| Piece | State |
|---|---|
| Basler a2A1920-160ucBAS + Kowa 3.5 mm, ceiling mount | live, settings locked in UserSet1 |
| Vision pipeline (`cueclock run`): detect → track → events → clock | live |
| OBS: Syphon table feed + clock overlay → wall TV (Hisense 55" CanvasTV) | live |
| Stream Deck MK.2 (`cueclock deck` daemon, no Elgato software) | live — Reset / Extension / Ruleset / reason-coded Flag keys (phantom, missed, other) / New Rack / Force 30 / Push / score keys |
| Match mode: WNT 9-ball rack context (60 s first shot, per-rack extensions), scoreboard overlay | live |
| Voice "extension" (built-in mic, offline Vosk) | live |
| Session toggle, instant replay, music keys | planned — `BACKLOG.md` |

## How it works

```
grab thread (BayerRG8 @ 156 fps)
 ├─► ring buffer (last ~20 s, POST /api/dump saves it as a corpus clip)
 ├─► detection @ 78 Hz: |frame − empty-table reference| → blobs → tracker
 │      → undistortPoints + homography → table mm → event engine
 │      → ShotTaken / BallsSettled / Scratch → clock state machine
 │      → FastAPI + WebSocket → OBS browser-source overlay + Stream Deck
 └─► display @ 30 fps: edge-aware debayer → undistort → Syphon → OBS → TV
```

Design commitments (rationale in `docs/design.md` — don't relitigate casually):
classical segmentation against an empty-table reference, **no neural detector**;
all thresholds in physical units; detection runs on distorted pixels (only
coordinates get undistorted); the vision core is **pure** —
`(frame, timestamp) in → events out`, no I/O, no clocks, no threads — so
recorded-clip replay tests are deterministic.

## Running it

```bash
# the session (camera + vision + clock + server + Syphon display):
uv run python -m cueclock.cli run --rectify \
    --reference tests/clips/empty_table_0801 --host 0.0.0.0

# the Stream Deck daemon (normally installed as a launchd agent):
uv run python -m cueclock.cli deck
```

- At the table there is no keyboard: the deck's **Session** key (top-left)
  runs that first command for you and launches OBS behind it — tap to start,
  hold 1 s to stop (`docs/hardware.md` §C4).
- After any camera bump: record a fresh empty-table reference and point
  `--reference` at it — recipe in `docs/hardware.md` §E2 (the deck daemon
  takes the same path via `deck --reference`). The startup drift check warns
  loudly if the reference has gone stale.
- OBS browser sources: `http://localhost:8000/shotclock` (corner overlay,
  ~200×200, "Control audio via OBS"),
  `http://localhost:8000/shotclock?view=full&audio=off` (full-screen
  broadcast-style clock scene), and `http://localhost:8000/score`
  (1920×1080 bottom-bar scoreboard, driven by the Stream Deck score keys).
  Phone control page: `/control`.
- Voice "extension": say the word and the clock grants one. Needs the Vosk
  model (`scripts/fetch_vosk_model.sh`, once) and a wired USB mic named via
  `--mic-device` — the Mac mini has no built-in mic. `cueclock listen` is the
  bring-up/soak tool (`docs/hardware.md` Part D).
- Other subcommands: `record`, `replay`, `analyze`, `calibrate`,
  `fit-undistort`, `live` — see `--help`.

## Development

```bash
uv sync                      # venv + deps (incl. dev group)
uv run pre-commit install    # one-time: git hooks
uv run pytest                # unit tests everywhere; corpus tests need the T7
uv run pytest -n 4           # full battery, ~5 min (add --detect-cache: ~21 s)
uv run ruff check . && uv run mypy
```

- **uv only** — `uv add <pkg>`, never bare pip.
- Test clips are real table footage: local-only, gitignored, on an external
  SSD behind the `tests/clips` symlink (see `tests/README.md`). Clip-based
  tests skip cleanly when the drive is absent; unit tests always run.
- The corpus scoreboard (`tests/corpus/`) is the accuracy ground truth —
  every detection change is graded against all recorded scenarios. Two tiers:
  cold is the gate, `--detect-cache` is the iteration loop (`tests/README.md`).

## Documentation

| File | What it holds |
|---|---|
| `docs/design.md` | Architecture, detection design, event logic, engineering standards, corpus checklist — the plan of record (§ numbers are cited from code) |
| `docs/hardware.md` | Camera settings & bring-up · video path to OBS/TV · Stream Deck (living button map) · mic plan · TV & recalibration recipes |
| `docs/cli.md` | CLI reference — every command, flag, and the common recipes |
| `BACKLOG.md` | Punch list, queued features, accuracy idea bank |
| `CHANGELOG.md` | Dated checkpoint log + build decisions (append-only) |

This project supersedes an iPhone-based prototype
([grassten/pool-room](https://github.com/grassten/pool-room), archived) that
plateaued at ~90–95% shot-detection accuracy; its distilled lessons are
`docs/design.md` §2.3.

## License

[MIT](LICENSE)
