"""Run the shot-clock server: ``uv run python -m cueclock.server``."""

import argparse
from pathlib import Path

import uvicorn

from cueclock.server.app import create_app


def main() -> None:
    parser = argparse.ArgumentParser(description="cueclock shot-clock server")
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="bind address (default localhost; use 0.0.0.0 to control from another device)",
    )
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    # Same gitignored score, bracket and settings files `cueclock run` uses,
    # so a mid-match server restart keeps the scoreboard, the tournament and
    # the clock's settings.
    app = create_app(
        score_path=Path("config/score.json"),
        bracket_path=Path("config/bracket.json"),
        archive_dir=Path("logs"),
        settings_path=Path("config/settings.json"),
    )
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
