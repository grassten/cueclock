"""Scaffold sanity check — replaced by real suites as modules land."""

import cueclock


def test_version() -> None:
    assert cueclock.__version__
