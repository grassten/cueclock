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
    p2_name: str = "PLAYER 2"
    p2_score: int = 0
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
            race = data.get("race_to")
            race_to = int(race) if race is not None else None
        except (OSError, ValueError, KeyError, TypeError):
            return  # corrupt or half-written — keep defaults; next save rewrites
        self.p1_name, self.p1_score = p1_name, p1_score
        self.p2_name, self.p2_score = p2_name, p2_score
        self.race_to = race_to

    def as_dict(self) -> dict[str, object]:
        """The `score` object carried in every WS/state payload."""
        return {
            "p1": {"name": self.p1_name, "score": self.p1_score},
            "p2": {"name": self.p2_name, "score": self.p2_score},
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
        """Back to zero; names and race-to survive (next match, same players)."""
        self.p1_score = 0
        self.p2_score = 0
        self._save()

    def configure(self, updates: Mapping[str, object]) -> None:
        """Apply a partial config: only keys present change anything.

        ``p1_name`` / ``p2_name`` must be strings; ``race_to: null`` clears
        the race (hides the overlay's center segment). Non-string names and
        non-int race values are ignored rather than erroring — this feeds
        straight from the control panel.
        """
        p1 = updates.get("p1_name")
        if isinstance(p1, str):
            self.p1_name = p1
        p2 = updates.get("p2_name")
        if isinstance(p2, str):
            self.p2_name = p2
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
