"""Pytest bootstrap: make the src-layout package importable without install."""

import pathlib
import sys

_SRC = pathlib.Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(_SRC))


def pytest_addoption(parser):
    """Register --live: opt-in flag for real Meta API smoke tests."""
    parser.addoption(
        "--live",
        action="store_true",
        default=False,
        help="run live Meta API tests (burns grant credits; off by default)",
    )
