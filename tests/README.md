# Tests

- `unit/` — synthetic-input tests (no camera, no clips). Always run, everywhere,
  including CI.
- `corpus/` — event-grading tests that replay real recorded table footage. They
  **skip cleanly** when clips or the fitted local calibration are absent (fresh
  clones and CI have neither).
- `clips` — a local, gitignored symlink to wherever the clip corpus lives.
  **Clips are local fixtures, never published.** Code and docs always use the
  `tests/clips` path, so relocating the corpus only means repointing the symlink.

On Steven's machine the corpus lives on the T7 external drive:

```sh
ln -s /Volumes/T7/pool_room_clips tests/clips
```

If the drive isn't mounted the symlink dangles and corpus tests skip — that's by
design. `cueclock record` writes through the same symlink, so mount the drive
before recording.

The full corpus battery streams ~90 GB off the T7; run it with 4 workers:

```sh
uv run pytest -n 4
```

Measured 2026-08-01 (36 scenarios): serial 6:40 · `-n 4` 3:57 · `-n 8` 4:57.
Four workers hit the T7's sequential-read ceiling; more just adds contention
(and lands on the M2's efficiency cores), so `-n 4` is the number — it isn't
the pytest-xdist default (`-n auto` picks 8 here and is slower). Each test
builds its own pipeline, so workers share no state and results are identical
to a serial run.

The recording discipline and scenario shopping list live in
`docs/design.md` §9 (background: the §2.3 lessons).
