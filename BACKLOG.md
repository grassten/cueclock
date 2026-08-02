# Backlog → the work board

Open work lives on the GitHub project board, not in this file:

### **https://github.com/users/grassten/projects/3**

Cards are issues on `grassten/cueclock`, so a checkpoint commit closes its own
backlog item — put `Closes #12` in the commit message and the card moves to Done
on push. That link is the whole reason the board beats a markdown list: there is
no second place to remember to update.

## How the board is organised

**Columns** (the `Status` field) — drag a card to transition it:

| Column | Means |
|---|---|
| Backlog | Not started, not next |
| Next | Short-term punch list |
| In progress | Being worked now |
| Awaiting live validation | Built and corpus-pinned; needs judgment at the table |
| Done | Landed — the detail lives in `CHANGELOG.md` |

**Labels** carry the categorisation, and they work everywhere the board doesn't
(issue list, `gh issue list --label`, the GitHub mobile app). Two orthogonal
axes, because "needs Steven at the table" is not a subject area:

- `area:accuracy` · `area:corpus` · `area:deck-ui` · `area:obs-display` ·
  `area:hardware` · `area:docs-infra`
- `needs:table-time` · `needs:purchase` · `needs:code-only`

**Saved views** — *Board* is everything; *At the table* is the
`needs:table-time` cut (the phone view: what can only be done standing at the
table); *Code only* is its complement.

```
gh issue list --label area:accuracy          # what's open on detection
gh issue list --label needs:table-time       # what needs a cue in hand
```

## What is not on the board

- **The accuracy idea bank** — `docs/accuracy-ideas.md`. Exploratory research
  notes (§1–§9), not work. An idea graduates to an issue when a live failure
  implicates it; that promotion is the only way a card gets created from it.
- **v1 scope exclusions** — `docs/design.md` §5 holds the standing YAGNI list
  (replay export from the ring buffer, break-speed measurement, match stats,
  16-ball identity, config hot-reload, auto-switch to final-10, match timer).
  It is a decision record, not a queue.
- **Landed work** — `CHANGELOG.md`, newest checkpoint first.
