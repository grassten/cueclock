"""Two-player match score for the wall-TV scoreboard.

Pure state + optional JSON persistence: with a ``path`` the score survives a
server restart mid-match (every mutation rewrites the file via a temp-file
rename; a missing or corrupt file just means defaults). With ``path=None``
the score is in-memory only — tests and bare dev servers never write into
the repo. No turn tracking, no game logic: the operator is the referee.
"""

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


@dataclass
class MatchScore:
    p1_name: str = "PLAYER 1"
    p1_score: int = 0
    p1_fargo: int | None = None  # None = no rating shown (unestablished / skipped)
    p2_name: str = "PLAYER 2"
    p2_score: int = 0
    p2_fargo: int | None = None
    race_to: int | None = None  # None = center segment hidden on the overlay
    path: Path | None = None  # None = in-memory only

    def __post_init__(self) -> None:
        if self.path is None or not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text())
            # Parse everything before assigning anything, so a half-valid
            # file can't half-load.
            p1_name = str(data["p1"]["name"])
            p1_score = max(0, int(data["p1"]["score"]))
            p2_name = str(data["p2"]["name"])
            p2_score = max(0, int(data["p2"]["score"]))
            p1_fargo = data["p1"].get("fargo")
            p1_fargo = int(p1_fargo) if p1_fargo is not None else None
            p2_fargo = data["p2"].get("fargo")
            p2_fargo = int(p2_fargo) if p2_fargo is not None else None
            race = data.get("race_to")
            race_to = int(race) if race is not None else None
        except (OSError, ValueError, KeyError, TypeError):
            return  # corrupt or half-written — keep defaults; next save rewrites
        self.p1_name, self.p1_score, self.p1_fargo = p1_name, p1_score, p1_fargo
        self.p2_name, self.p2_score, self.p2_fargo = p2_name, p2_score, p2_fargo
        self.race_to = race_to

    def as_dict(self) -> dict[str, object]:
        """The `score` object carried in every WS/state payload."""
        return {
            "p1": {"name": self.p1_name, "score": self.p1_score, "fargo": self.p1_fargo},
            "p2": {"name": self.p2_name, "score": self.p2_score, "fargo": self.p2_fargo},
            "race_to": self.race_to,
        }

    # ---------- mutations (each saves; the caller broadcasts) ----------

    def adjust(self, player: str, delta: int) -> None:
        """Add ``delta`` to a player's score, flooring at 0 (mis-press undo)."""
        if player == "p1":
            self.p1_score = max(0, self.p1_score + delta)
        elif player == "p2":
            self.p2_score = max(0, self.p2_score + delta)
        else:
            raise KeyError(player)
        self._save()

    def reset_scores(self) -> None:
        """Back to zero; names, ratings and race-to survive (next match, same players)."""
        self.p1_score = 0
        self.p2_score = 0
        self._save()

    def configure(self, updates: Mapping[str, object]) -> None:
        """Apply a partial config: only keys present change anything.

        ``p1_name`` / ``p2_name`` must be strings; ``race_to: null`` clears
        the race (hides the overlay's center segment). Non-string names and
        non-int race values are ignored rather than erroring — this feeds
        straight from the control panel.

        ``p1_fargo`` / ``p2_fargo`` (issue #27): int sets, null clears. A
        rating belongs to a name, so changing a name without sending a
        rating in the same update clears the old one — a stale rating under
        a new player is worse than no rating.
        """
        p1 = updates.get("p1_name")
        if isinstance(p1, str):
            if p1 != self.p1_name and "p1_fargo" not in updates:
                self.p1_fargo = None
            self.p1_name = p1
        p2 = updates.get("p2_name")
        if isinstance(p2, str):
            if p2 != self.p2_name and "p2_fargo" not in updates:
                self.p2_fargo = None
            self.p2_name = p2
        if "p1_fargo" in updates:
            fargo = updates["p1_fargo"]
            self.p1_fargo = fargo if isinstance(fargo, int) else None
        if "p2_fargo" in updates:
            fargo = updates["p2_fargo"]
            self.p2_fargo = fargo if isinstance(fargo, int) else None
        if "race_to" in updates:
            race = updates["race_to"]
            self.race_to = race if isinstance(race, int) else None
        self._save()

    def _save(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Atomic-enough: never leave a torn file where the loader looks.
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.as_dict(), indent=2) + "\n")
        tmp.replace(self.path)
