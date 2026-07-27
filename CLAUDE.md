# cueclock — Project Brief

Automated shot clock for Ultimate Pool (USA 8-ball) solo practice. Steven is training
competitively without a human shot-clock operator. An overhead **Basler ace 2 a2A1920-160ucBAS**
(1920×1200 @ 160 fps, global shutter, Kowa LM4NCL 3.5 mm lens) watches the table; the app
detects shots/settles/scratches and drives an OBS browser-source overlay on a wall TV.
Runs on the Mac mini M2 that also runs OBS (HDMI straight to the TV).

This is a **from-scratch rewrite** of an archived iPhone prototype
([grassten/pool-room](https://github.com/grassten/pool-room)) that plateaued at ~90–95%
accuracy. Read the prototype's distilled lessons before touching detection logic —
they're in `docs/cv-app-rewrite.md` §2.3 and must not be re-learned the hard way.

## Current status

**Pre-hardware build (July 2026).** Camera is in transit. Repo structure, tooling, and
design docs exist; camera-free modules are landing now. Build order: `docs/cv-app-rewrite.md`
§7; live execution status: **`docs/progress.md`** (check it first, update it at every
checkpoint commit).

## Plan of record — docs/

| Doc | What it settles |
|---|---|
| `docs/cv-app-rewrite.md` | Architecture, detection design, event logic, engineering standards, build order, resolved decisions |
| `docs/basler-camera-setup.md` | Camera assembly, pylon/pypylon install, bring-up, production settings |
| `docs/video-output-pipeline.md` | Display path: debayer → undistort → Syphon → OBS → TV |
| `docs/*.pdf` | Hardware build sheet + original project spec (background) |

Key design commitments (rationale + sources in the docs — don't relitigate casually):
- Classical segmentation against an empty-table reference; **no neural detector** in v1.
- Track all balls; events from **per-ball velocities in table millimeters** (homography).
- Detection runs on **distorted** pixels; only coordinates get undistorted. Full-frame
  remap exists only on the display path.
- Strike = conjunction (stationary dwell + velocity impulse + coherent trajectory).
  Settle = per-ball speed floor with hysteresis. Scratch = track lost near a pocket zone.
- The vision core is **pure** — `(frame, timestamp) in → events out`, no I/O, no clocks,
  no threads — so recorded-clip replay tests stay deterministic.

## Dev workflow

- **uv only.** `uv sync`, `uv run <cmd>`, `uv add <pkg>` (`--group dev` for tooling).
  Never bare `pip install`. (The old project's pipenv rule is dead.)
- **ruff** lints and formats; **mypy --strict** on `src/`; **pytest** for tests.
  All three run in CI and (except mypy) in pre-commit — `uv run pre-commit install` once.
- Clip-based tests **skip** when `tests/clips/` fixtures are absent (clips are local-only,
  gitignored, never published). Unit tests must always run everywhere.
- Principles Steven has named explicitly: **KISS, YAGNI, DRY.** This repo will be
  open-sourced — write for a stranger reading it. v1 scope is clock + events + overlay +
  API; replay export, break speed, stats, and 16-ball identity are backlog until asked for.

## Hardware / environment facts

- Mac mini M2 (Mac14,3, 24 GB), macOS 26.x, is both dev and production. OBS 32.1.1
  installed (includes the Syphon Client fix — the 32.0.x regression is past).
- pylon Camera Software Suite must be installed system-wide for pypylon to work
  (arm64-native, but **pylon Viewer GUI doesn't exist on Apple Silicon** — verification
  goes through the pypylon live-view script in `docs/basler-camera-setup.md` §5).
- Camera's 8-bit Bayer format is **BayerRG8**; watch the OpenCV↔GenICam Bayer-constant
  naming mismatch (sanity-check against the blue cloth).
- iPhone Continuity Camera remains a wireless side-angle source, owned by OBS directly.
- Stream Deck hits the HTTP API; keep the `/api/*` contract from `docs/cv-app-rewrite.md` §2.1.
