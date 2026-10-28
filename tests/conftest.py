"""Pytest bootstrap: make the src-layout package importable without install."""

import pathlib
import sys

_SRC = pathlib.Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(_SRC))
