"""Stage 0 probe for issue #33: per-settle episode evidence from replays.

For every BallsSettled a corpus clip produces, report the engine-side
evidence the settle-time shot adjudication (#33) will read:

* every episode mover (``_episode_origin``): how far it actually traveled
  (origin -> last observed), and whether it is still present at the settle;
* every track death inside the episode, with its time;
* every arrival (present ball whose track was born after the episode latched):
  its post-birth travel and the dark gap between the nearest preceding
  same-episode death and its birth;
* the rearrangement's spatial spread — max pairwise distance across the
  involved balls' positions (Part C's racking-vs-break discriminator);
* whether a ShotTaken fired inside the episode.

The two populations under measurement (issue #33 "Measurements first"):
placement/retrieval settles must show arrivals dark for seconds and movers at
~0 mm; real shot settles must show a dwelled on-table mover with real travel.

Usage:
    uv run python scripts/settle_probe.py medium_shot soft_touch_0731 ...

Respects CUECLOCK_DETECT_CACHE the same way the corpus battery does (set it
to reuse stored detector output; unset replays cold).
"""

import argparse
import json
import os
import sys
from math import hypot
from pathlib import Path

import numpy as np

from cueclock.capture.clip import ClipReader
from cueclock.vision.calibration import TableCalibration
from cueclock.vision.events import BallsSettled, EventConfig, ShotTaken
from cueclock.vision.pipeline import ReferenceModel, VisionPipeline

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))  # `tests` is not a distributed package

# Clip -> empty-table reference pairing is owned by the corpus battery; import
# it rather than duplicate it (a wrong-era reference degrades detection
# silently — no error, just quietly missing or garbled events).
from tests.corpus.test_corpus_events import REFERENCES  # noqa: E402

CLIPS = Path("tests/clips")
CALIBRATION = Path("config/table_calibration.json")


def _reference_model(name: str, cache: dict) -> ReferenceModel:
    if name not in cache:
        reader = ClipReader(CLIPS / name)
        indices = np.linspace(0, len(reader) - 1, num=15, dtype=int)
        cache[name] = ReferenceModel.build(
            [reader.frame(int(i)) for i in indices], TableCalibration.load(CALIBRATION)
        )
    return cache[name]


def _detections(reader, clip_dir, pipeline, indices):
    """Detector output for indices — from disk when CUECLOCK_DETECT_CACHE is set."""
    cache_dir = os.environ.get("CUECLOCK_DETECT_CACHE")
    if not cache_dir:
        return [pipeline.detect(reader.frame(i)) for i in indices]
    from cueclock.capture.detect_cache import DetectionCache, cache_key

    store = DetectionCache(Path(cache_dir))
    key = cache_key(clip_dir, pipeline.reference, pipeline.detect_config, pipeline.churn_lag)
    hit = store.load(clip_dir, key, indices)
    if hit is not None:
        return hit
    computed = [pipeline.detect(reader.frame(i)) for i in indices]
    store.store(clip_dir, key, indices, computed)
    return computed


def _spread_mm(baseline, present, moved_mm_floor):
    """Positions of the involved balls (same greedy match as _match_rearrange),
    and their max pairwise distance — the settle-spread candidate for Part C."""
    dists = sorted(
        (hypot(p[0] - c[0], p[1] - c[1]), i, j)
        for i, p in enumerate(baseline)
        for j, c in enumerate(present)
    )
    used_p, used_c = set(), set()
    involved = []
    for d, i, j in dists:
        if i in used_p or j in used_c:
            continue
        used_p.add(i)
        used_c.add(j)
        if d > moved_mm_floor:
            involved.append(present[j])  # moved: where it ended up
    involved += [c for j, c in enumerate(present) if j not in used_c]  # appeared
    involved += [p for i, p in enumerate(baseline) if i not in used_p]  # vanished
    if len(involved) < 2:
        return 0.0, len(involved)
    spread = max(
        hypot(a[0] - b[0], a[1] - b[1]) for k, a in enumerate(involved) for b in involved[k + 1 :]
    )
    return spread, len(involved)


def probe(name, reader, pipeline: VisionPipeline, indices, clip_dir: Path):
    engine = pipeline._engine
    cfg: EventConfig = pipeline.event_config
    episode_start = None  # t when table_moving latched
    deaths = []  # (t, ball_id) inside the current episode
    fires = []  # ShotTaken t's inside the current episode
    prev_ids = set(engine._first_seen)
    was_moving = engine.table_moving
    frames = _detections(reader, clip_dir, pipeline, indices)
    results = []
    for i, (detections, churn, intrusions) in zip(indices, frames, strict=True):
        t = float(reader.timestamps[i])
        # Snapshot episode state BEFORE advance: a settling update() clears it.
        pre_origin = dict(engine._episode_origin)
        pre_last = dict(engine._episode_last)
        pre_first_seen = dict(engine._first_seen)
        pre_roster = set(engine._roster)
        pre_baseline = pipeline._settled_baseline

        events = pipeline.advance(t, detections, churn, intrusions)

        now_ids = set(engine._first_seen)
        for dead in prev_ids - now_ids:
            deaths.append((t, dead))
        prev_ids = now_ids
        if engine.table_moving and not was_moving:
            episode_start = t
            deaths = []
            fires = []
        was_moving = engine.table_moving
        fires += [e.t for e in events if isinstance(e, ShotTaken)]

        for e in events:
            if not isinstance(e, BallsSettled):
                continue
            summary = pipeline.last_settle_rearrange
            present = [(b.x_mm, b.y_mm) for b in pipeline.last_balls if b.present]
            present_ids = {b.ball_id for b in pipeline.last_balls if b.present}
            movers = []
            for bid, (ox, oy) in pre_origin.items():
                lx, ly = pre_last.get(bid, (ox, oy))
                movers.append(
                    {
                        "id": bid,
                        "travel_mm": round(hypot(lx - ox, ly - oy)),
                        "alive": bid in present_ids,
                        "roster": bid in pre_roster,
                        "born_t": round(pre_first_seen.get(bid, -1.0), 2),
                    }
                )
            arrivals = []
            for b in pipeline.last_balls:
                if not b.present:
                    continue
                born = pre_first_seen.get(b.ball_id)
                if born is None or episode_start is None or born <= episode_start:
                    continue
                bx, by = engine._birth_pos.get(b.ball_id, (b.x_mm, b.y_mm))
                prior_deaths = [dt for dt, _ in deaths if dt <= born]
                dark = born - (max(prior_deaths) if prior_deaths else episode_start)
                arrivals.append(
                    {
                        "id": b.ball_id,
                        "post_birth_travel_mm": round(hypot(b.x_mm - bx, b.y_mm - by)),
                        "dark_after_death_s": round(dark, 2),
                        "born_into_episode_s": round(born - episode_start, 2),
                    }
                )
            spread, n_inv = _spread_mm(pre_baseline or present, present, cfg.moving_enter_mm)
            results.append(
                {
                    "clip": name,
                    "settle_t": round(e.t, 2),
                    "episode_start": round(episode_start, 2) if episode_start else None,
                    "fires_in_episode": [round(ft, 2) for ft in fires],
                    "summary": None
                    if summary is None
                    else {
                        "moved": summary.moved,
                        "appeared": summary.appeared,
                        "vanished": summary.vanished,
                        "moved_mm": round(summary.moved_mm),
                    },
                    "movers": sorted(movers, key=lambda m: -m["travel_mm"]),
                    "arrivals": arrivals,
                    "deaths_in_episode": [{"t": round(dt, 2), "id": did} for dt, did in deaths],
                    "spread_mm": round(spread),
                    "spread_involved": n_inv,
                    # Issue #36: a bare `appeared` is uninterpretable without
                    # knowing how many balls each layout resolved. A break's
                    # appeared comes from the pack UN-MERGING (settled >>
                    # baseline); a hand sweep's comes from balls dying and
                    # being reborn at the same population. `None` baseline
                    # means the clip never had a quiet layout to anchor to —
                    # a degenerate measurement, not a zero.
                    "baseline_n": None if pre_baseline is None else len(pre_baseline),
                    "settled_n": len(present),
                }
            )
            episode_start = None
    return results


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("clips", nargs="*", help="corpus clip names under tests/clips/")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args()

    ref_cache: dict = {}
    all_results = []

    for name in args.clips:
        if not (CLIPS / name).exists():
            print(f"skip {name}: not recorded", file=sys.stderr)
            continue
        ref = REFERENCES.get(name, "empty_table")
        reader = ClipReader(CLIPS / name)
        pipeline = VisionPipeline(
            TableCalibration.load(CALIBRATION),
            _reference_model(ref, ref_cache),
            cadence_hz=reader.cadence_hz,
        )
        all_results += probe(name, reader, pipeline, reader.detect_indices(), CLIPS / name)

    if args.json:
        print(json.dumps(all_results, indent=1))
        return 0
    for r in all_results:
        s = r["summary"]
        stxt = (
            f"mv={s['moved']} ap={s['appeared']} vn={s['vanished']} mm={s['moved_mm']}"
            if s
            else "no summary"
        )
        print(
            f"\n{r['clip']} settle@{r['settle_t']}  episode@{r['episode_start']}  "
            f"[{stxt}]  fires={r['fires_in_episode']}  "
            f"spread={r['spread_mm']}mm/{r['spread_involved']} involved"
        )
        for m in r["movers"]:
            print(
                f"  mover id={m['id']:4d} travel={m['travel_mm']:5d}mm "
                f"alive={int(m['alive'])} roster={int(m['roster'])} born={m['born_t']}"
            )
        for a in r["arrivals"]:
            print(
                f"  arrival id={a['id']:4d} post_birth_travel={a['post_birth_travel_mm']:4d}mm "
                f"dark_after_death={a['dark_after_death_s']}s "
                f"born_into_episode={a['born_into_episode_s']}s"
            )
        if r["deaths_in_episode"]:
            ds = ", ".join(f"id={d['id']}@{d['t']}" for d in r["deaths_in_episode"])
            print(f"  deaths: {ds}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
