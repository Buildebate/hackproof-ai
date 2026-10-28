"""Immutable test guard: SHA-256 baseline lock against AI reward hacking.

The guard snapshots every test file before task execution and proves
afterwards that zero existing tests were modified, deleted, truncated, or
had assertions removed. Any breach raises :class:`RewardHackingDetectedError`.
"""

from __future__ import annotations

import ast
import hashlib
from dataclasses import dataclass
from pathlib import Path

_EXCLUDED_DIRS = {"__pycache__", ".pytest_cache", ".git", ".hg", ".svn"}
_EXCLUDED_SUFFIXES = {".pyc", ".pyo"}

__all__ = ["RewardHackingDetectedError", "ImmutableTestGuard"]


class RewardHackingDetectedError(Exception):
    """Raised when an existing test file was tampered with.

    Attributes:
        violations: Human-readable per-file violation details, e.g.
            ``"modified: tests/unit/test_x.py"``.
    """

    def __init__(self, violations: list[str]) -> None:
        self.violations = list(violations)
        detail = "; ".join(self.violations) if self.violations else "unknown"
        super().__init__(f"Reward hacking detected: {detail}")


@dataclass(frozen=True)
class _AssertionCounts:
    asserts: int
    assert_calls: int


def _iter_test_files(root: Path) -> list[Path]:
    """All regular files under root, minus caches and VCS metadata."""
    found: list[Path] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        if path.suffix in _EXCLUDED_SUFFIXES:
            continue
        if any(part in _EXCLUDED_DIRS for part in path.relative_to(root).parts):
            continue
        found.append(path)
    return found


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _assertion_counts(source: str) -> _AssertionCounts | None:
    """Count ``assert`` statements and ``assert*`` call sites, else None."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    asserts = 0
    assert_calls = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Assert):
            asserts += 1
        elif isinstance(node, ast.Call):
            func = node.func
            name = ""
            if isinstance(func, ast.Name):
                name = func.id
            elif isinstance(func, ast.Attribute):
                name = func.attr
            if "assert" in name.lower():
                assert_calls += 1
    return _AssertionCounts(asserts=asserts, assert_calls=assert_calls)


class ImmutableTestGuard:
    """Stateless SHA-256 guard over a test directory."""

    @staticmethod
    def lock_test_baseline(test_dir: Path | str) -> dict[str, str]:
        """Hash every test file; keys are POSIX relative paths."""
        root = Path(test_dir)
        if not root.exists():
            raise FileNotFoundError(f"test_dir does not exist: {root}")
        if not root.is_dir():
            raise NotADirectoryError(f"test_dir is not a directory: {root}")
        return {
            path.relative_to(root).as_posix(): _sha256_file(path)
            for path in _iter_test_files(root)
        }

    @staticmethod
    def verify_no_assertion_tampering(pre_content: str, post_content: str) -> bool:
        """True unless assertion counts were reduced or broken.

        Returns False when the post content no longer parses while the
        pre content did. Unparseable pre content yields True: there is
        nothing attributable to judge.
        """
        pre = _assertion_counts(pre_content)
        if pre is None:
            return True
        post = _assertion_counts(post_content)
        if post is None:
            return False
        return post.asserts >= pre.asserts and post.assert_calls >= pre.assert_calls

    @staticmethod
    def verify_test_integrity(
        test_dir: Path | str, baseline: dict[str, str]
    ) -> tuple[bool, list[str]]:
        """Verify zero existing test files changed; raise on any breach.

        Returns ``(True, [])`` when clean. Raises
        :class:`RewardHackingDetectedError` detailing every deleted,
        truncated, or modified file. Newly added files are out of scope:
        the guard protects pre-existing tests.
        """
        root = Path(test_dir)
        current = ImmutableTestGuard.lock_test_baseline(root)
        violations: list[str] = []
        for relative, expected_hash in sorted(baseline.items()):
            actual_hash = current.get(relative)
            if actual_hash is None:
                violations.append(f"deleted: {relative}")
                continue
            if actual_hash == expected_hash:
                continue
            try:
                size = (root / relative).stat().st_size
            except OSError:
                size = -1
            if size == 0:
                violations.append(f"truncated: {relative}")
            else:
                violations.append(f"modified: {relative}")
        if violations:
            raise RewardHackingDetectedError(violations)
        return True, []
