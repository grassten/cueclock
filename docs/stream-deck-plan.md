# Stream Deck — plan

The physical control surface for a deliberately headless system. The Mac mini lives
behind the TV; there is no keyboard, mouse, or monitor in reach of the table. Every
interaction the room needs — clock control, scores, scenes, music, corpus capture —
funnels through buttons that work with chalk on your hands.

## Why it earns its place (the sell)

1. **The clock's designed control surface.** The §2.1 API contract exists *because*
   of the Stream Deck (start/pause, reset, extension, ruleset switch). v1's foul
   handling is explicitly manual — "human calls the foul and hits reset" — so the
   button isn't a convenience, it's part of the design.
2. **The accuracy instrument** (accuracy-ideas §8): a ground-truth key pressed at
   every real shot plus a "that was wrong" key, with the server timestamping presses
   and auto-dumping the ring buffer to a labeled clip on every disagreement. This
   turns every practice session into corpus growth with zero extra table time — the
   single highest-leverage accuracy investment on the list. Beta-tester mode *is*
   this button.
3. **One-button rituals.** "Session start" = launch `cueclock run` + OBS scoreboard
   scene + playlist at preset volume, in one press (multi-actions are native).
   "Session end" reverses it. The room turns on like an appliance, not a workstation.
4. **Instant replay on the wall.** OBS's replay buffer + a save-and-switch-scene
   multi-action = watch your last shot on the TV seconds after it happens, no
   cueclock code required.

## Model choice (July 2026 lineup)

| Model | Layout | Price | Verdict |
|---|---|---|---|
| Mini / Neo | 6 / 8 keys | ~$80 / $100 | Too small once scores + music + scenes land |
| **MK.2** | **15 keys** | **~$150** | **The pick.** Enough keys with one or two folders; detachable stand leaves a flat slab that wall-mounts trivially; no knob surplus |
| Stream Deck + | 8 keys + 4 dials + touch strip | ~$200 | First pick until Steven's objections: 3 of 4 dials wasted, 8 keys thin, wedge body is the hardest to wall-mount |
| XL | 32 keys | ~$250 | The upgrade path if folder-hopping ever annoys — everything on one page |
| + XL (2026) | 36 keys + 6 dials | $350 | Overkill for a pool room |
| Pedal | 3 foot switches | ~$90 | **Sleeper add-on later:** foot-tap ground-truth marking while both hands are on the cue |

**Decision (2026-07-28): MK.2.** Steven's read was right: the + sells three dials
he'd never touch while starving the key grid, and its fixed desk-wedge body is the
awkward one to wall-mount. The MK.2's stand detaches (it's the swappable-faceplate
model), leaving a flat-backed slab — Command strips or a ~$15–25 printed cradle
([Etsy print-on-demand](https://www.etsy.com/market/stream_deck_mount),
[DeckRack](https://www.deckrack.net/), sim-rig vendors) and it's on the wall.
Volume without a knob: macOS vol± keys repeat when held, or two preset-level keys
("practice 40%" / "loud"); the dial was a luxury. If the ground-truth workflow
sticks, add the Pedal so marking a real shot never takes a hand off the cue.

## Placement

It connects over **USB 2.0**, so a passive extension to ~16 ft is fine — it does
NOT have to sit at the TV. Options, best first:

1. Wall-mounted at the break end near the TV at hip/chest height — you're at that
   end constantly (racking, breaking), and it keeps the run short.
2. A small shelf wherever you naturally return between shots (seating/chalk spot),
   with a longer USB-C run along the wall.

Ships with a desk stand + ~5 ft cable; add a USB-C extension if it lands farther
from the mini.

## Button map (by page)

### Page 1 — Practice (default)
- **Clock toggle** (start/pause), **Reset**, **Extension** (+15 s)
- **Ground truth: shot** (press at every real shot — shadow-mode grading)
- **Flag: event wrong** (auto-dumps ring buffer to a labeled corpus clip)
- **Save last 15 s** (manual corpus capture: "that was interesting")
- **Instant replay** (OBS: save replay buffer → replay scene → back)
- **Play/pause** and **vol±** inline (15 keys leaves room — no page hop for music basics)
- **Match page** and **Music page** folder keys

### Page 2 — Match / drills
- **My score +1**, **Opponent +1** (or drill **Success** / **Miss** counters)
- **Undo score**, **Reset match**
- **Ruleset cycle** (ultimate_pool → wnt_9ball → final10; API-switchable today)
- **Foul → reset** (the v1 manual foul call)

### Page 3 — Music
- **Play/pause**, **Next**, playlist keys (**Pool Room**, one or two more)
- **Vol−**, **Vol+** (hold to repeat), **Mute**, preset-level keys ("practice 40%" / "loud")

### Page 4 — System
- **Session start** / **Session end** macros
- **OBS scenes:** scoreboard · replay · side-angle (official OBS plugin, native)
- **Health** (later: key face shows camera fps / clock state via API poll)

## Music: yes, and it's clean

The Mac mini pairs to the Bluetooth amp as a normal audio output (KEF Q150s don't
care who's talking). Music.app on the Mac with an Apple Music "Pool Room" playlist,
then:

- **Play/pause/next** — Stream Deck's native media-key actions drive Music.app.
- **Volume** — native vol± actions (hold to repeat) plus preset-level keys via
  `osascript -e 'set volume output volume 40'`; mute key likewise.
- **Playlist keys** — one `osascript` per key
  (`tell application "Music" to play playlist "Pool Room"`), via a shell/AppleScript
  action.
- **Now-playing on a key face** — community plugins can render track info to a key.

The phone stops being the music remote; the phone stays in your pocket. (The amp
just sees a Bluetooth source; if it multi-pairs, the phone still works as a guest
input.) One caveat: some BT amps need a re-pair nudge after power-off — worth one
session of testing; fallback is a cheap always-on USB DAC → amp line-in, which
also kills BT audio latency/dropout risk permanently.

## What exists today vs. what this creates

Works the day the Stream Deck arrives (no new code):
- Clock start/pause/reset/extension/ruleset — the §2.1 HTTP API is live
  (`http://localhost:8000/api/*`, via an HTTP-request plugin or `curl` shell action)
- OBS scene switching + instant replay (official Elgato OBS plugin)
- All music control (macOS-side, no cueclock involvement)
- Session start/stop macros (shell actions launching/killing `cueclock run`)

Build items it creates (priority order):
1. **Ring buffer + dump endpoint in `cueclock run`** — ✅ **LANDED 2026-07-29**
   (`capture/ring.py` + `POST /api/dump?label=<x>`): detect-cadence frames
   (stride 2 ≈ 78 Hz), default 12 s ≈ 2.2 GB RAM, saved via ClipWriter to
   `tests/clips/<label>/` on the T7. The flag/save keys are now one
   `curl -X POST 'http://localhost:8000/api/dump?label=flagged'` shell action.
2. **Ground-truth comparer** — server timestamps GT presses, aligns with emitted
   events, auto-dumps on disagreement (shadow-mode grading).
3. **Score state + API + overlay** — score keys need server endpoints and a
   scoreboard overlay element (small, pure additions to Hub/app).
4. (Later) drill counters / session stats — backlog until asked for, per house rules.

## Sources

Lineup/pricing (2026-07-28): [Elgato model guide](https://www.elgato.com/us/en/explorer/products/stream-deck/which-stream-deck-should-you-get/),
[Engadget on Stream Deck + XL](https://www.engadget.com/computing/elgatos-stream-deck--xl-adds-a-touch-strip-and-dials-to-an-already-massive-array-of-buttons-195200129.html),
[2026 roundups](https://alanspicer.com/best-stream-deck-2026/).
