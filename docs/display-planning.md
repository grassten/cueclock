# Wall display — planning

Plan for the TV on the break-end wall: what it shows, how big, which panel, and how
it's mounted. Decisions here feed the OBS scene work (multiple scenes, scoreboard-first)
that replaces the original "table feed on the wall full-time" idea.

## Requirements (settled with Steven, 2026-07-28)

- **Location:** centered on the break-end wall; wide-open wall, no width/height
  constraints. Mac mini M2 drives it over HDMI directly (it also runs OBS).
- **Viewing distance:** 11–14 ft from the primary reading position (shooting from the
  far end toward the wall).
- **Primary content:** big scoreboard / shot clock scene — **a mostly static image,
  on for hours per session.** This dominates the panel choice (see burn-in below).
- **Secondary content:** on-demand OBS scenes with the table/side-angle video feed
  (replays, drill review), future practice-data scenes (stats, drill timers), and
  occasional YouTube tutorials. **No movie/TV watching** (clarified 2026-07-28) —
  HDR/cinema picture quality carries no weight in the panel choice.
- **Stated top concern:** glare from the table's perimeter LED light frame, which
  hangs over the table directly in front of this wall.
- **Budget:** $600+, more if it earns it.

## Size at 11–14 ft

Horizontal field of view by screen size (16:9):

| Size | Screen width | FOV @ 11 ft | FOV @ 14 ft |
|---|---|---|---|
| 55" | 47.9" | ~21° | ~16° |
| 65" | 56.7" | ~24° | ~19° |
| 75" | 65.4" | ~28° | ~22° |

Reference points: ~20–25° is the comfortable "normal TV" zone; SMPTE's cinema
recommendation is 30°+. Reading:

- **55"** is fine as a pure scoreboard (digits can still be a foot tall) but sits at
  the small end for general viewing from 14 ft.
- **65"** lands in the comfort zone across the whole 11–14 ft range — the safe pick.
- **75"** is justified mainly by the general-viewing use; at this distance it's
  "pleasantly big," not overwhelming, but it roughly doubles the price tier and
  weight for a marginal scoreboard benefit.

By the numbers alone, 65" is the comfort-zone floor and 75" the best fit for
general viewing. **Decided 2026-07-28: 55"** — Steven judged it the right
proportional/aesthetic fit for the space, and the viewing distance fine in
practice. The scoreboard use is unaffected at any of these sizes (digits can be
a foot tall on a 55"); the FOV math only ever argued about movie-night immersion,
which lost to how the room looks.

## Glare: the perimeter LED is the enemy

The LED frame is a large, bright emitter hanging between the viewer and the screen
wall — close to the worst case for specular reflections. Three levers, in order of
effectiveness per dollar:

1. **Downward tilt (~5–10°) on the mount.** Reflections obey angle-in = angle-out; a
   slight tilt dumps the LED frame's reflection toward the floor instead of the
   viewer's eye line. Costs ~$30 more for a tilting mount and works with any TV.
   Mount the screen center around standing eye level (~62–66" to center) so the tilt
   doesn't distort the viewing geometry.
2. **True matte panel.** Matte coatings diffuse the LED frame's mirror image into a
   faint haze instead of a sharp hot spot. **Marketing warning (researched
   2026-07-28):** "anti-glare" splits into two genuinely different technologies —
   true matte films (Samsung "Glare Free" on QN90F/Frame, Hisense "Hi-Matte" on
   CanvasTV, TCL "Ultra Matte" on NXTFRAME) actually diffuse a point source, while
   anti-reflective coatings on glossy panels (Hisense U8QG "Anti-Reflection Pro",
   TCL QM8K) only dim the mirror image — a bright LED frame stays visible as a
   defined reflection. Reviewers called glare the U8QG's "one glaring issue"
   ([Tom's Guide](https://www.tomsguide.com/tvs/the-hisense-u8qg-test-results-reveal-mind-blowing-brightness-but-theres-one-glaring-issue-with-this-tv));
   Rtings' [direct-reflections methodology](https://www.rtings.com/tv/tests/picture-quality/direct-reflections)
   documents the same glossy-vs-matte split.
3. **Raw brightness.** A high-brightness mini-LED panel keeps the *content* far
   brighter than the residual reflection, which is what makes reflections
   unnoticeable in practice. Best combined with (2); brightness alone fights a sharp
   specular highlight rather than removing it — that's the U8QG/QM8K strategy, and
   reviewers found it insufficient against direct light sources with dark content
   (our scoreboard scene is deliberately dark-background).

## Burn-in rules out OLED

The scoreboard scene is a static, high-contrast image displayed for hours, session
after session — the textbook OLED burn-in workload. The best anti-glare panels on
the market are Samsung's matte OLEDs, and they are still the wrong tool here.
**LCD (QLED/mini-LED) only.** LCD image retention is minor and recoverable; no scene
design gymnastics (pixel shift, logo dimming, screensavers) are needed to protect
the panel.

If a future scene redesign ever makes the display mostly-video, this constraint can
be revisited; until then it's settled.

## Candidate models (researched 2026-07-28, US street prices)

| Model | Panel | Glare answer | 55" | 65" | 75" | Notes |
|---|---|---|---|---|---|---|
| **Samsung QN90F** (2025) | mini-LED, ~2000+ nits | **True matte** (Glare Free) | ~$1,200–1,500 | ~$1,600 | ~$2,200–3,000 | The only high-brightness + true-matte + no-burn-in combination. Set HDMI input to "PC" for 4:4:4 text. |
| **Hisense CanvasTV** (S7N '24 / S7SG '26) | QLED, ~550 nits, no local dimming | **True matte** (Hi-Matte) | ~$650–700 | ~$700–900 | ~$1,100–1,300 | Art TV; flush wall mount included. Great static scoreboard, mediocre HDR video punch. |
| **Hisense U8QG** (2025) | mini-LED, up to ~5,000 nits | AR coating on gloss — **weak vs. direct light** | ~$700–750 | ~$950–1,000 | ~$1,300–1,400 | Best picture/$. Has USB-C DisplayPort input (handy for the Mac mini). Glare is its reviewed weakness. |
| **TCL NXTFRAME/A300W** | QLED, modest nits | **True matte** (Ultra Matte) | ~$630 | ~$800 | — | CanvasTV's twin from TCL; same trade-offs. |
| **TCL QM8K/QM8L** ('25/'26) | mini-LED, very bright | AR on gloss, rated below Hisense's | — | ~$1,000–1,500 | ~$1,450–2,000 | Value mini-LED play; same glare story as U8QG. |
| ~~Samsung S95F/S95H OLED~~ | QD-OLED | Best-in-class matte | ~$1,600 | ~$2,200–3,400 | — | **Disqualified: burn-in.** Rtings' longevity data puts hours-daily static scoreboards squarely in the confirmed burn-in zone. |

Also considered and dropped: **Samsung The Frame Pro** (matte mini-LED art TV,
65" ~$1,400–2,000) — its Wireless One Connect box puts a wireless hop between
the HDMI source and the panel, the wrong architecture for a live clock display.
The standard Frame has wired HDMI but is a dim QLED without local dimming — at
that point the CanvasTV does the same job for half the price.

Ranked for this room (glare is the stated #1 concern; scoreboard is hours-static):

1. **Samsung QN90F 65" (~$1,600)** — true matte *and* bright *and* burn-in-immune;
   the one TV where all three requirements meet. This is where "$600+, more if it
   earns it" earns it.
2. **Hisense CanvasTV 65" (~$700–900)** — genuinely matte at half the price; a
   static scoreboard needs neither 2,000 nits nor local dimming. Compromise is
   HDR/video quality for the general-viewing use.
3. **Hisense U8QG 65" (~$1,000)** — only if the LED frame's reflection proves
   tolerable in practice (tilt mount helps); otherwise its brightness advantage is
   spent fighting the exact problem the matte panels just don't have.

At 75", the matte options are CanvasTV 75" (~$1,100–1,300) or QN90F 75"
(~$2,200+). Full source list from the 2026-07-28 research sweep: Rtings
bright-room + direct-reflections pages, Tom's Guide / TechRadar / 9to5Google
U8QG reviews, TechRadar QN90F, TrustedReviews / CNN Underscored CanvasTV,
ecoustics QM8L, AVS Forum S95F/S95H, Rtings longevity test coverage.

## Where this landed (2026-07-28)

Budget reality: Steven ruled out the $1,600 tier — QN90F stays a research record,
not a candidate. Size settled at **55"** (proportions/aesthetics in the space).

The U8QG was briefly the front-runner on a tilt-mount argument: the LED frame
hangs near eye level, so its sharp reflection lands in a narrow band that ~3–4°
of downward tilt pushes off the top edge (each degree of tilt shifts the
reflected scene two degrees). Steven then spotted the flaw: **tilting the screen
down points its reflection field at the lit table itself** — the largest,
brightest diffuse object in the room — so a glossy panel trades a sharp hot spot
for a broad blue-table ghost across dark content. And with movie watching
explicitly out of scope, the U8QG's HDR firepower buys nothing this room needs.

**Decision: Hisense CanvasTV 55" (S7N ~$650 clearance / 2026 S7SG ~$700).**
The matte panel is the one property that actually solves the stated #1 problem,
and the "art TV" positioning is not a mismatch — an art TV is engineered to
display a static image for hours under ambient light, which is *exactly* the
scoreboard workload. The art cosplay features (Art Mode, flush UltraSlim mount,
bezel frames) are separable extras; what's being bought is the only sub-$700
true-matte 55" panel on the market. Verified for this install (2026-07-28):

- **Standard VESA 400×200 (M6, 9.5–11.5 mm)** — the included flush mount is
  optional; a low-profile tilting mount works and creates the gap needed to
  mount the **Mac mini behind the TV** (M2 mini is 1.4" tall; keep it in open
  air, not boxed in). Cable track carries TV + mini power down to the outlet.
  ([seekingtech mount specs](https://seekingtech.com/hisense-s7-and-s7n-canvas-tv-wall-mount-specs/))
- **Proper 4:4:4 chroma, sharp fonts, ~23 ms PC-mode input lag** — fine as a
  Mac-driven text/scoreboard display. Reviewers note only the thinnest letters
  on dark backgrounds soften — irrelevant at scoreboard digit sizes.
  ([choose.tv S7NQ review](https://www.choose.tv/us/tv/reviews/hisense/hisense-s7nq-canvas-tv_AEQ),
  [Rtings CanvasTV 2024](https://www.rtings.com/tv/reviews/hisense/canvastv-qled-2024))
- Tilt is now optional (matte diffuses the table's reflection into faint haze),
  but a tilting mount is still the pragmatic pick: a few degrees of margin plus
  the Mac-mini pocket for ~$30–50.

Fallback if the CanvasTV's picture disappoints in person: U8QG 55" (~$700–750),
accepting the glossy-panel table ghost.

**Ordered 2026-07-28 (Amazon):** Hisense 55S7SG (2026 CanvasTV) $697.99 ·
ECHOGEAR tilting mount (VESA 400×200 ✓, ~2.5" profile, tilts for glare) $39.99 ·
Delamu 13 ft cable raceway (power drop to outlet) $14.99. Power plan: strip
mounts behind the TV, one strip cord down the raceway; TV + Mac mini + T7 all
live behind the panel. Open item before install day: **measure the camera-USB
ceiling run** — camera above table center → ceiling → down to behind the TV is
roughly 12–14 ft with slack (~4+ m); if the current cable is 3 m it won't reach.
Passive USB 3.0 Micro-B (screw-lock) is reliable to 5 m at this camera's
360 MB/s; beyond that it must be an active cable.

## Mounting / install notes

- Screen center ~62–66" from the floor; tilting mount, 5–10° down.
- The TV install disturbs the camera rig ⇒ triggers the **post-install
  recalibration checklist** in `progress.md` (level camera, re-focus, re-fit
  undistort, re-set OBS crop, then §4.3 ChArUco calibration).
- Judge the OBS LUT/grade on the actual TV, not the Mac monitor
  (`progress.md` post-install list item 6).
- Mac mini → TV is a straight HDMI run; pick a mount location that leaves the
  cable reachable for the planned cable track.

## OBS scene plan (sketch — to be built at the TV)

1. **Scoreboard (default):** shot clock huge (readable at 14 ft ⇒ digits 10"+ —
   trivial on 65"), frame/score, extension indicators, subtle event flashes
   (shot / settle / scratch). Dark background: hides LCD blooming, minimizes the
   screen lighting up the room, and any static-content concerns.
2. **Replay / review:** table feed (rectified) large + scoreboard strip. On-demand
   via Stream Deck scene-switch (OBS hotkeys or obs-websocket).
3. **Practice data (future):** session stats, drill timers — backlog until asked.

The old idea of the full-time table feed is dropped: nobody needs to watch the
table they're standing at; the feed earns wall space only in replay/review scenes.
