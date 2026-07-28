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

The recording discipline and scenario shopping list live in
`docs/corpus-recording-checklist.md` (background: `docs/cv-app-rewrite.md` §2.3).
