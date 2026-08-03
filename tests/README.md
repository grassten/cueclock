# Tests

- `unit/` — synthetic-input tests (no camera, no clips). Always run, everywhere,
  including CI.
- `corpus/` — event-grading tests that replay real recorded table footage. They
  **skip cleanly** when clips or the fitted local calibration are absent (fresh
  clones and CI have neither).
- `clips` — a local, gitignored symlink to wherever the clip corpus lives.
  **Clips are local fixtures, never published.** Code and docs always use the
  `tests/clips` path, so relocating the corpus only means repointing the symlink.
- `goldens/` — one committed JSON timeline per corpus clip. Unlike the clips
  they grade, goldens **are** committed: they are small text, and they keep the
  record of what the engine did even in a clone with no pixels. See
  [Golden timelines](#golden-timelines).

On Steven's machine the corpus lives on the T7 external drive:

```sh
ln -s /Volumes/T7/pool_room_clips tests/clips
```

If the drive isn't mounted the symlink dangles and corpus tests skip — that's by
design. `cueclock record` writes through the same symlink, so mount the drive
before recording.

## Running the battery

Two tiers. **Cold is the default and the gate; cached is the iteration loop.**

```sh
uv run pytest -n 4                  # cold — full fidelity. Run before every commit.
uv run pytest -n 4 --detect-cache   # cached — for events.py / track.py iteration.
```

Always `-n 4`, never `-n auto`: the M2 has exactly 4 performance cores, and 8
workers spill onto the efficiency cores and land slower.

### Why two tiers

The battery replays ~53,000 frames per run. Per frame the detector costs
~5.9 ms and the tracker + event engine — the part that actually changes while
accuracy is being tuned — costs 0.08 ms. `--detect-cache` stores the seam
between them (`VisionPipeline.detect` → `advance`), so a rerun skips both the
T7 read and the detector: ~21 MB of cached detections stand in for ~134 GB of
frames. See `src/cueclock/capture/detect_cache.py` for the key design.

The cache key covers everything that can change a detection — `detect.py`'s
bytes, `DetectConfig`, a content hash of the three reference arrays, clip
identity, and the OpenCV/NumPy versions. Edit the detector and every entry
misses, so a cached run cannot silently grade stale detections. It *cannot*
cover environment drift below those versions, which is why **cold stays the
default and checkpoint batteries run cold**. Cache dir `.detect-cache.nosync/`
(gitignored); delete it freely, it rebuilds.

`--detect-cache` does nothing for a `DetectConfig` sweep — every grid point is
a new key and a full cold run. It accelerates event/tracker work only.

## Golden timelines

`EXPECTED` in `corpus/test_corpus_events.py` asserts event *counts*. Counts are
blind to timing — and that blindness was measured, not assumed:

> **Perturbing `settle_dwell_s` from 0.5 to 0.6 moved 33 of 53 corpus
> timelines. The count assertions caught one.** (2026-08-02)

So every clip also carries a golden: its full event list (type, timestamp,
speed, strike path) committed as `goldens/<scenario>.json`. The comparison runs
inside `test_corpus_clip`, on the events that test already replayed — goldens
cost no extra wall time.

```sh
uv run pytest tests/corpus -n 4 --update-goldens   # rewrite from this run
```

**Then read the diff.** A golden diff is the measurement: it is the only place
that shows what a threshold move did to the rest of the corpus. Regenerating
without reading it is the same as not having goldens, and a CHANGELOG entry for
a detection change should cite which timelines moved.

Tolerances and the rationale for *not* asserting `ball_id` are in
`corpus/goldens.py`; `unit/test_goldens.py` covers the comparator itself, so it
runs everywhere including CI.

### Measured

All `-n 4`, same machine. 53 scenarios except where noted.

| | wall |
|---|---|
| 2026-08-01, 36 scenarios, memmap reader (serial 6:40) | 3:57 |
| 2026-08-02, memmap reader | 7:48 |
| 2026-08-02, `pread` reader | 5:05 |
| 2026-08-02, checkpoint 52 baseline (358 tests) | 3:50 |
| 2026-08-02, + reference/calibration reuse — **cold, the gate** | **4:51** |
| 2026-08-02, `--detect-cache` warm, corpus only | **12 s** |
| 2026-08-02, `--detect-cache` warm, full suite | **21 s** |

Building the cache from empty costs roughly a cold run, once.

Per frame, measured the same day: raw-frame read 2.4 ms (`pread`) or 11.1 ms
(`memmap`), `detect_balls` 5.5 ms, churn 0.4 ms, tracker + engine **0.08 ms**.
That last number is the whole argument for the cache — and for keeping
`--detect-cache` out of the checkpoint path, since everything above it goes
unexercised on a cached run.

The 2026-08-01 note that "four workers hit the T7's sequential-read ceiling"
is obsolete: that ceiling was the reader's own `np.memmap`, which faulted in
at 4 KB granularity (207 MB/s vs 958 MB/s for whole-frame `pread`). With
`ClipReader` reading properly the cold battery is CPU-bound, not I/O-bound.

Each test builds its own pipeline, so workers share no state and results are
identical to a serial run.

The recording discipline and scenario shopping list live in
`docs/design.md` §9 (background: the §2.3 lessons).
