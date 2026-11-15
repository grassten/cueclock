# Tests

- `unit/` — synthetic-input tests (no camera, no clips). Always run, everywhere,
  including CI.
- `corpus/` — event-grading tests that replay real recorded table footage. They
  **skip cleanly** when clips or the fitted local calibration are absent (fresh
  clones and CI have neither). Each clip is graded under the calibration its
  reference clip carries (`tests/clips/<reference>/table_calibration.json`,
  written by `cueclock recalibrate`), falling back to `config/`'s, so a
  recalibration does not silently re-grade older mounts.
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

**What a change costs, by the file it touches:**

| Editing | Cache | Iteration cost |
|---|---|---|
| `events.py` / `track.py` thresholds | **hits** | ~16 s |
| `detect.py` / `DetectConfig` | **misses by design** | ~9 min, unavoidable |
| anything outside `vision/` | n/a | unit suite, ~3 s |

The detector case is the design working, not a problem to route around: a
cached run must never grade stale detections.

`--detect-cache DIR` takes an explicit path, so several checkouts can share one
warm cache. That is safe to do concurrently — `store()` writes tmp+fsync+rename
with pid/uuid-unique temp names, and identical bytes make last-writer-wins
harmless.

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

**Commit only the goldens for clips you actually changed.** Some goldens are
deliberately stale, because `ground_truth()` reads them: a clip whose strike
fire was lost to a past regression keeps the *old* golden as the surviving
record of when the stroke happened, and the clock grade (#34) is scored against
it. Refreshing such a golden makes it agree with today's behaviour and
therefore *disagree* with `EXPECTED` — the clip then has no ground truth at
all, falls back to the hard count assertion, and goes red. That is what
`stroke_tug_strike_0801` did at checkpoint 87: a corpus-wide `--update-goldens`
turned a documented, understood residual into a failure. The run itself is
still the right diagnostic — just revert the files for clips your change did
not touch.

### Separating a real regression from a timing shift

Goldens are checked *before* the count assertions, so a threshold change tends
to turn everything red at once and the genuine breakages hide among timing
shifts. To split them, stash the goldens and regenerate — what stays red is a
count failure, i.e. an actual behaviour regression:

```sh
cp -R tests/goldens /tmp/goldens.base
uv run pytest tests/corpus -n 4 --detect-cache --update-goldens   # counts only
diff -r /tmp/goldens.base tests/goldens                           # what moved, and where
```

This is the loop the checkpoint 53–58 accuracy work ran on; a threshold move
that reported 42 red files was 6 real failures and 36 timing shifts, and there
is no way to tell them apart by eye.

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
| 2026-08-02, checkpoint 58 (406 tests) — **cold, the gate** | **3:27** |
| 2026-08-02, checkpoint 58, `--detect-cache` warm, corpus only | **16 s** |

Building the cache from empty costs roughly a cold run, once.

### Why you keep paying for cold runs, and how to stop

The key includes `detect.py`'s **file bytes** and every `DetectConfig` field
value. That is deliberate — it is what makes a cached run unable to grade stale
detections — but it has a consequence worth planning around: *any* edit to
`detect.py` invalidates all 59 entries, including a comment, and including
adding a feature behind a flag that is switched **off**, where the output is
bit-identical. The key is on the file, not on its behaviour, and it should stay
that way; behavioural keying is exactly how you end up silently grading stale
detections. So route around it instead.

`track.py` and `events.py` are deliberately **not** in the key. That is the
whole lever:

> **Land the detect-side change once, pay one cold run to warm the cache, then
> iterate everything downstream against it.**

Checkpoint 72's session is the worked example of getting this wrong. `detect.py`
was edited five separate times while `track.py` was being debugged in the same
loop, so every iteration paid a full cold battery. The bug actually under
investigation — a recovered core stealing a greedy match from a *moving* track,
which made a rolling ball read as still and fired a settle 1.7 s early — lived
entirely in `track.py`. With `detect.py` held still and the cache warm, that
debug loop is **~30 s instead of ~9 minutes**.

Two more things that cost real time that day:

- **Contention inflates the gate ~2.5×.** That cold battery reported 12:17
  against the ~4:51 baseline above, because two corpus replay sweeps were
  running alongside it. When you are checking wall time to confirm the corpus
  did not skip (see the worktree warning in `CLAUDE.md`), check what else is
  running before concluding anything from the number.
- **Re-running to read a failure.** Fixed at the source: golden diffs now report
  *every* difference rather than the first, render golden vs actual as an
  aligned table, and are written to `tests/goldens/.failures/<scenario>.txt`
  so a truncated capture never costs a rerun. See `corpus/goldens.py`.

### A red golden is a diff to read, not a verdict

It has three causes and they need different answers:

1. **A bug in the new code.** Decide from the table, not the pass/fail bit: is
   the new timing physically right? Checkpoint 72's settle moved 1.7 s
   *earlier*, which looks like an improvement — late settles are a real defect
   class — until you print ball speeds across the disputed window and find the
   table still moving at ~100 mm/s when the settle fired.
2. **A deliberate improvement.** Regenerate with `--update-goldens` and cite the
   moved timelines in the CHANGELOG entry.
3. **An over-specific assertion.** Widen the tolerance and say why. This is why
   `ball_id` is recorded but never asserted.

The failure mode to avoid is reflex in either direction: reverting on red throws
away the most informative artifact the battery produces, and regenerating on red
turns the net into a rubber stamp. Note that two of checkpoint 72's three diffs
were *same event, same time, different magnitude* — invisible as a pass/fail
count, and the reason goldens exist.

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
