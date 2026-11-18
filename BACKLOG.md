# Backlog → the issue list

Open work lives in the GitHub issues, not in this file:

### **https://github.com/grassten/cueclock/issues**

A checkpoint commit closes its own backlog item: put `Closes #12` in the commit
message and the issue closes on push. That link is the whole reason issues beat
a markdown list: there is no second place to remember to update.

## How it is organised

**Labels** carry the categorisation. Two orthogonal axes, because "needs Steven
at the table" is not a subject area:

- `area:accuracy` · `area:corpus` · `area:deck-ui` · `area:obs-display` ·
  `area:hardware` · `area:docs-infra`
- `needs:table-time` · `needs:purchase` · `needs:code-only`

```
gh issue list --label area:accuracy          # what's open on detection
gh issue list --label needs:table-time       # what needs a cue in hand
```

The GitHub project board this file used to point at is no longer used
(2026-11-17).

## What is not in the issues

- **Speculative accuracy ideas.** Retired 2026-08-02 with
  `docs/accuracy-ideas.md`. Accuracy work is now derived from evidence:
  a session journal or a recorded clip implicates a specific failure, and that becomes an issue. Nothing gets worked on the
  strength of a hypothesis alone.
- **v1 scope exclusions** — `docs/design.md` §5 holds the standing YAGNI list
  (replay export, break-speed measurement, match stats,
  config hot-reload, auto-switch to final-10, match timer). 16-ball identity
  left the list on 2026-11-17: it was built as the ball list (#54, design §13).
  It is a decision record, not a queue.
- **Landed work** — `CHANGELOG.md`, newest checkpoint first.
