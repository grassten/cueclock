"""Suite-wide pytest options.

Rootdir-level on purpose: `pytest_addoption` only runs from an *initial*
conftest, so `tests/corpus/conftest.py` would not register the flag for a
whole-suite run.
"""

import os

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--detect-cache",
        nargs="?",
        const="",
        default=None,
        metavar="DIR",
        help=(
            "reuse cached detector output for corpus replays, building it on a miss "
            "(default dir .detect-cache.nosync). Fast path for events.py/track.py work; "
            "checkpoint batteries run WITHOUT it. See tests/README.md."
        ),
    )


def pytest_configure(config: pytest.Config) -> None:
    # An env var is the transport rather than the option object: it reaches
    # pytest-xdist workers no matter how they are spawned, and the corpus
    # helpers that need it are plain functions, not fixtures.
    value = config.getoption("--detect-cache")
    if value is not None:
        from cueclock.capture.detect_cache import DEFAULT_CACHE_DIR

        os.environ["CUECLOCK_DETECT_CACHE"] = value or str(DEFAULT_CACHE_DIR)
