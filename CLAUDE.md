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
Deck phase 1 running. Accuracy hardening is the ongoing work, driven by the session
journals (`cueclock report`, `session-stats`) and graded against the recorded-clip
corpus; new clips are deliberate `cueclock record` takes (the Flag key and its
in-RAM ring were removed in checkpoint 114, #55). Live execution status: **`CHANGELOG.md`** (check the newest checkpoint first;
append a checkpoint entry there in every checkpoint commit). Open work: the
**[project board](https://github.com/users/grassten/projects/3)** — cards are issues on
`grassten/cueclock`, so a checkpoint commit closes its own item with `Closes #N`. Filter
by `area:*` / `needs:*` labels; `BACKLOG.md` explains the layout.

## Plan of record — the docs

| File | What it settles |
|---|---|
| `docs/design.md` | Architecture, detection design, event logic, engineering standards, build order, resolved decisions, corpus checklist. **§ numbers are cited from code — never renumber; append.** |
| `docs/hardware.md` | Camera assembly/bring-up/locked settings (Part A) · display path debayer → undistort → Syphon → OBS → TV (Part B) · Stream Deck (Part C — **living button map: must match `deck/layout.py` in the same commit**) · mic plan (Part D) · TV + post-bump recalibration recipes (Part E) |
| `docs/cli.md` | CLI reference: commands, flags, recipes — **must match `cli.py` in the same commit** |
| `BACKLOG.md` | Pointer to the work board + how it's organised (columns, labels, views). Not a task list — don't add items here, open an issue. |
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

- **iCloud evicts anything it thinks is idle, and launchd daemons cannot get it
  back.** `config/` and `models/` are symlinks to `config.nosync/` /
  `models.nosync/` for the same reason `.venv` points at `.venv.nosync`. After a
  power cycle on 2026-09-01 every file in both was *dataless*, and a background
  process reading one gets `OSError` **EDEADLK — "Resource deadlock avoided"**
  (materialisation is off for launchd jobs), which silently killed TV control,
  the Syphon rebind, the TV projector and the Vosk model while the logs said
  only "no usable tv.json". Check with `find config models -type f -flags
  +dataless`; anything listed means a symlink was replaced by a real directory.
  The rest of the repo lives in iCloud too: on 2026-11-01 `src/`, `tests/`,
  `scripts/`, `logs/` and most of `.git/` came back dataless after a reboot, so the
  deck daemon and its `run` child died on `import` (a terminal could still read
  them, which is why it looked intermittent). The repo root now carries Finder's
  "Keep Downloaded" flag (`xattr -w 'com.apple.fileprovider.pinned#PX' 1 .`), which
  new files inherit. If EDEADLK returns, check `find . -type f -flags +dataless`
  and read the files from a terminal to bring them back; a fresh clone in iCloud
  needs the flag set again.
  Never recreate either as a real directory inside `~/Desktop`.
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
- The corpus battery has **two tiers** (`tests/README.md`): `uv run pytest -n 4` is cold,
  full-fidelity, ~9 min on an otherwise idle M2 (measured 2026-08-11 over 62 clips /
  526 tests; it was 8:37 at 57 clips) — **the gate, run it before every commit that
  touches detection**. `--detect-cache` reuses stored detector output for a ~30 s run —
  the iteration loop for
  `events.py`/`track.py`, which is where nearly all accuracy work happens. Editing
  `detect.py` or `DetectConfig` invalidates every entry by design, so a cached run can
  never grade stale detections.
- **A fresh git worktree is not a working repo here, and it fails silently.** `tests/clips`
  (→ the T7), `config/` (the mount calibration), `logs/`, `models/` and `.venv` are all
  *gitignored*, so a new worktree has none of them: every corpus test skips and the battery
  reports green having graded nothing — an agent can honestly say "357 passed" while
  validating zero clips. **Always check the test count and wall time**: cold is ~9 min and
  ~526 tests, cached ~30 s; much smaller or faster means the corpus skipped. The working
  recipe (three parallel agents ran on it 2026-08-11 — keep worktrees *outside* `~/Desktop`,
  since iCloud is what forced `.venv.nosync` in the first place):

  ```sh
  git worktree add ~/cueclock-wt/<name> -b wt/<name>
  cd ~/cueclock-wt/<name>
  ln -sfn /Volumes/T7/pool_room_clips tests/clips
  for d in config logs models; do ln -sfn ~/Desktop/cueclock/$d $d; done
  uv sync   # a real .venv is fine here — outside Desktop there is no iCloud to hide .pth
  ./.venv/bin/python -m pytest tests -n 4 \
      --detect-cache=/Users/steven/Desktop/cueclock/.detect-cache.nosync
  ```

  Two traps inside that recipe. **The `tests` positional is mandatory** — without it pytest
  parses the cache path as a positional *before* `tests/conftest.py` registers the option,
  roots itself at the main checkout and dies with "unrecognized arguments: --detect-cache".
  And **share one absolute cache root**: `.detect-cache.nosync/` is per-root, so an unshared
  worktree starts cold. Sharing is safe by design — the key covers `detect.py`,
  `DetectConfig` and the reference digest, deliberately not `events.py`/`track.py`, and
  entries land by atomic rename, so concurrent agents cannot corrupt each other.
- Principles Steven has named explicitly: **KISS, YAGNI, DRY.** This repo will be
  open-sourced — write for a stranger reading it. v1 scope is clock + events + overlays
  (clock + scoreboard) + API + match mode (rack context, scores) + voice "extension";
  replay export, break speed, stats, and 16-ball identity are backlog until asked for.

## Hardware / environment facts

- Mac mini M2 (Mac14,3, 24 GB), macOS 26.x, is both dev and production. OBS **32.2.1**
  installed (includes the Syphon Client fix — the 32.0.x regression is past).
- pylon Viewer GUI doesn't exist on Apple Silicon; camera verification goes through
  `scripts/live_view.py` (`docs/hardware.md` §A3). The pypylon wheel streams the camera
  without a system pylon install.
- Camera's 8-bit Bayer format is **BayerRG8**; the OpenCV↔GenICam naming mismatch is
  **resolved**: decode with `cv2.COLOR_BayerBG2BGR` (verified against the blue cloth
  2026-07-27 — see `docs/hardware.md` §A3).
- The side angle is an **Insta360 Link 2C Pro** on its own USB-C port, owned by OBS
  directly, and its built-in mic is the voice listener's input (since 2026-11-12,
  `docs/hardware.md` Part F). It replaced the iPhone-over-Camo setup (2026-08-01 to
  2026-11-12), which had replaced Continuity Camera. OBS must use frame rate
  `60000240/1000000`: a plain 60 silently falls back to 50 fps (§B3). Its settings
  live in `config/sidecam.json` and the deck re-applies them at session start
  (`cueclock sidecam`). **Never leave Insta360 Link Controller open during play**:
  its live preview cost detection ~45 Hz and OBS 3.6% of its frames (checkpoint 108).
- Stream Deck hits the HTTP API; keep the `/api/*` contract from `docs/design.md` §2.1.
  **Never install the Elgato Stream Deck app** — it would claim exclusive HID access
  and starve the `cueclock deck` daemon.
