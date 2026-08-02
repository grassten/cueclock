# cueclock — Project Brief

Automated shot clock for Ultimate Pool (USA 8-ball) solo practice. Steven is training
competitively without a human shot-clock operator. An overhead **Basler ace 2 a2A1920-160ucBAS**
(1920×1200 @ 160 fps, global shutter, Kowa LM4NCL 3.5 mm lens) watches the table; the app
detects shots/settles/scratches and drives an OBS browser-source overlay on a wall TV.
Runs on the Mac mini M2 that also runs OBS (HDMI straight to the TV).

This is a **from-scratch rewrite** of an archived iPhone prototype
([grassten/pool-room](https://github.com/grassten/pool-room)) that plateaued at ~90–95%
accuracy. Read the prototype's distilled lessons before touching detection logic —
they're in `docs/design.md` §2.3 and must not be re-learned the hard way.

## Current status

**Rig live (July 2026).** Camera mounted + calibrated, wall TV + OBS installed, Stream
Deck phase 1 running. Accuracy hardening is the ongoing work: live failures get Flagged
(ring-buffer dump → labeled clip), triaged offline, and promoted to graded corpus
scenarios. Live execution status: **`CHANGELOG.md`** (check the newest checkpoint first;
append a checkpoint entry there in every checkpoint commit). Open work: **`BACKLOG.md`**.

## Plan of record — the docs

| File | What it settles |
|---|---|
| `docs/design.md` | Architecture, detection design, event logic, engineering standards, build order, resolved decisions, corpus checklist. **§ numbers are cited from code — never renumber; append.** |
| `docs/hardware.md` | Camera assembly/bring-up/locked settings (Part A) · display path debayer → undistort → Syphon → OBS → TV (Part B) · Stream Deck (Part C — **living button map: must match `deck/layout.py` in the same commit**) · mic plan (Part D) · TV + post-bump recalibration recipes (Part E) |
| `docs/cli.md` | CLI reference: commands, flags, recipes — **must match `cli.py` in the same commit** |
| `BACKLOG.md` | Punch list, queued features, accuracy idea bank |
| `CHANGELOG.md` | Append-only dated checkpoints + build decisions (pre-2026-07-31 entries cite the old doc layout; originals archived in `~/Documents/cueclock-docs-archive/`) |
| `README.md` | Public-facing overview, launch commands, OBS sources — keep the launch command here current |

Key design commitments (rationale + sources in `docs/design.md` — don't relitigate casually):
- Classical segmentation against an empty-table reference; **no neural detector** in v1.
- Track all balls; events from **per-ball velocities in table millimeters** (homography).
- Detection runs on **distorted** pixels; only coordinates get undistorted. Full-frame
  remap exists only on the display path.
- Strike = conjunction (stationary dwell + velocity impulse + coherent trajectory),
  guarded by other-ball motion within kiss range (carom guard v2, checkpoint 23).
  Settle = per-ball displacement windows with hysteresis; junk tracks under 70%
  observed don't judge table state. Scratch = track lost near a pocket zone, keyed
  on last *observed* position.
- The vision core is **pure** — `(frame, timestamp) in → events out`, no I/O, no clocks,
  no threads — so recorded-clip replay tests stay deterministic.
- **No blind threshold tuning** — every constant is anchored to a measurement from the
  corpus; that discipline is how the old app's plateau gets avoided.

## Dev workflow

- **uv only.** `uv sync`, `uv run <cmd>`, `uv add <pkg>` (`--group dev` for tooling).
  Never bare `pip install`. (The old project's pipenv rule is dead.) The venv really
  lives at `.venv.nosync/` with `.venv` as a local symlink — iCloud syncs Desktop and
  stamps `UF_HIDDEN` on files, which makes CPython ≥3.11 silently skip `.pth` files
  and break the editable install (CHANGELOG checkpoint 13). If the venv is ever
  rebuilt, recreate the symlink, not a real `.venv/` directory.
- **ruff** lints and formats; **mypy --strict** on `src/`; **pytest** for tests.
  All three run in CI and (except mypy) in pre-commit — `uv run pre-commit install` once.
- Clip-based tests **skip** when `tests/clips/` fixtures are absent (clips are local-only,
  gitignored, never published). Unit tests must always run everywhere. The corpus lives
  at `/Volumes/T7/pool_room_clips/` (4 TB external SSD, ExFAT) with `tests/clips` as a
  local gitignored symlink — always use the symlink path in code and docs. T7 unmounted
  ⇒ symlink dangles ⇒ corpus tests skip; mount it before recording or corpus runs.
- Principles Steven has named explicitly: **KISS, YAGNI, DRY.** This repo will be
  open-sourced — write for a stranger reading it. v1 scope is clock + events + overlays
  (clock + scoreboard) + API + match mode (rack context, scores) + voice "extension";
  replay export, break speed, stats, and 16-ball identity are backlog until asked for.

## Hardware / environment facts

- Mac mini M2 (Mac14,3, 24 GB), macOS 26.x, is both dev and production. OBS 32.1.1
  installed (includes the Syphon Client fix — the 32.0.x regression is past).
- pylon Viewer GUI doesn't exist on Apple Silicon; camera verification goes through
  `scripts/live_view.py` (`docs/hardware.md` §A3). The pypylon wheel streams the camera
  without a system pylon install.
- Camera's 8-bit Bayer format is **BayerRG8**; the OpenCV↔GenICam naming mismatch is
  **resolved**: decode with `cv2.COLOR_BayerBG2BGR` (verified against the blue cloth
  2026-07-27 — see `docs/hardware.md` §A3).
- The iPhone side angle runs through **Camo** over USB (Camo Studio + the phone app,
  manual focus/exposure/WB), owned by OBS directly. It replaced Continuity Camera on
  2026-08-01 — wireless, no manual controls, and it died mid-session once.
- Stream Deck hits the HTTP API; keep the `/api/*` contract from `docs/design.md` §2.1.
  **Never install the Elgato Stream Deck app** — it would claim exclusive HID access
  and starve the `cueclock deck` daemon.
