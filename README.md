# cueclock

Automated shot clock for Ultimate Pool (USA 8-ball) solo practice, driven by an overhead
machine-vision camera. A Basler ace 2 (1920×1200 @ 160 fps, global shutter) watches the table;
per-ball tracking in real table coordinates detects shots, settles, and scratches; a voice
keyword ("extension") adds time; the clock renders as an OBS overlay on a wall TV.

**Status: pre-hardware scaffold.** The camera is in transit; no application code yet. The
research and design docs in [`docs/`](docs/) are the plan of record — start with
[`cv-app-rewrite.md`](docs/cv-app-rewrite.md) (architecture + build order) and
[`basler-camera-setup.md`](docs/basler-camera-setup.md) (camera bring-up).

This project supersedes an iPhone-based prototype
([grassten/pool-room](https://github.com/grassten/pool-room), archived) that plateaued at
~90–95% shot-detection accuracy.

## Development

```bash
uv sync                      # create venv + install deps (incl. dev group)
uv run pre-commit install    # one-time: install git hooks
uv run pytest                # run tests
uv run ruff check .          # lint
uv run mypy                  # type-check
```

- Dependencies are managed with [uv](https://docs.astral.sh/uv/) — `uv add <pkg>`, never bare pip.
- Test clips (real table footage) are local-only and gitignored — see
  [`tests/clips/README.md`](tests/clips/README.md). Clip-based tests skip when fixtures are absent.

## License

[MIT](LICENSE)
