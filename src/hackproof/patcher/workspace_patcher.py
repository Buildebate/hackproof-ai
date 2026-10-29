"""Deterministic workspace patcher (Step 2.3).

Applies exact ``SEARCH``/``REPLACE`` blocks inside an isolated ``git
worktree`` with zero line-number drift: every SEARCH must occur exactly
once in the target file, otherwise the whole application is rejected and
the file is left untouched (no partial application, atomic revert on
write failure). Post-patch verification runs the full suite
(``pytest --junitxml`` / ``vitest --reporter=junit``) through the
injected sandbox runner (local default, ``RemoteSSHRunner`` for OCI).
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from hackproof.debate.roles.architectural_builder import extract_blocks
from hackproof.sandbox.runner import LocalSubprocessRunner, SandboxExecutionResult

SUPPORTED_LANGUAGES = ("python", "typescript")
GREEN_JUNIT_NAME = "hackproof-green-junit.xml"
_GIT_TIMEOUT_SEC = 60

__all__ = [
    "SUPPORTED_LANGUAGES",
    "GREEN_JUNIT_NAME",
    "GreenCheck",
    "PatchApplicationError",
    "WorkspacePatcher",
]


class PatchApplicationError(Exception):
    """A SEARCH/REPLACE application was rejected (missing/ambiguous match)."""


@dataclass(frozen=True)
class GreenCheck:
    """Outcome of the post-patch full-suite verification run."""

    exit_code: int
    junit_path: str
    timed_out: bool
    passed: bool


class _Runner(Protocol):
    """Sandbox runner surface (local default, remote when --runner=oci)."""

    def run_in_sandbox(
        self,
        cmd: list[str],
        cwd: Path | str,
        timeout_sec: int = ...,
        network_disabled: bool = ...,
    ) -> SandboxExecutionResult: ...


def _git(argv: list[str], cwd: Path | str) -> subprocess.CompletedProcess[str]:
    """Run one git plumbing command, capturing text output."""
    return subprocess.run(
        ["git", *argv],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=_GIT_TIMEOUT_SEC,
    )


class WorkspacePatcher:
    """Exact-match patch application in isolated git worktrees."""

    def __init__(self, runner: _Runner | None = None) -> None:
        """Bind the sandbox runner (local air-gapped default)."""
        self._runner: _Runner = (
            runner if runner is not None else LocalSubprocessRunner()
        )

    @contextmanager
    def isolated_worktree(
        self, repo_path: Path | str, base_ref: str = "HEAD"
    ) -> Iterator[Path]:
        """Create a detached worktree for one task; remove it afterwards.

Args:
    repo_path: Existing git repository (worktrees anchor at its HEAD).
    base_ref: Ref to check out detached (default ``HEAD``).

Raises:
    RuntimeError: No ``git`` binary available.
    ValueError: ``repo_path`` is not a git repository.
    PatchApplicationError: Worktree creation failed.
        """
        if shutil.which("git") is None:
            raise RuntimeError(
                "git binary not found: isolated worktrees require git"
            )
        repo = Path(repo_path)
        probe = _git(["rev-parse", "--git-dir"], repo)
        if probe.returncode != 0:
            raise ValueError(f"not a git repository: {repo}")
        parent = Path(tempfile.mkdtemp(prefix="hackproof-worktree-"))
        worktree = parent / "wt"
        created = _git(
            ["worktree", "add", "--detach", str(worktree), base_ref], repo
        )
        if created.returncode != 0:
            shutil.rmtree(parent, ignore_errors=True)
            raise PatchApplicationError(
                f"git worktree add failed: {created.stderr.strip()}"
            )
        try:
            yield worktree
        finally:
            _git(["worktree", "remove", "--force", str(worktree)], repo)
            _git(["worktree", "prune"], repo)
            shutil.rmtree(parent, ignore_errors=True)

    def apply_blocks(
        self,
        worktree: Path | str,
        file_relpath: str,
        blocks: Sequence[tuple[str, str]],
    ) -> Path:
        """Apply validated SEARCH/REPLACE pairs to one file, atomically.

Every SEARCH must occur exactly once (0 or 2+ matches reject the
whole application). The file is written exactly once after all
blocks validate in memory, so a failing hunk never leaves a
partially patched file.

Raises:
    FileNotFoundError: Target file missing in the worktree.
    PatchApplicationError: Empty block list, empty SEARCH, or a
        missing/ambiguous match.
        """
        if not blocks:
            raise PatchApplicationError("no SEARCH/REPLACE blocks to apply")
        target = Path(worktree) / file_relpath
        if not target.is_file():
            raise FileNotFoundError(f"patch target not found: {target}")
        original = target.read_text(encoding="utf-8")
        content = original
        for index, (search, replace) in enumerate(blocks):
            if not search.strip():
                raise PatchApplicationError(
                    f"block {index}: empty SEARCH text"
                )
            occurrences = content.count(search)
            if occurrences == 0:
                raise PatchApplicationError(
                    f"block {index}: SEARCH not found (0 matches)"
                )
            if occurrences > 1:
                raise PatchApplicationError(
                    f"block {index}: ambiguous SEARCH "
                    f"({occurrences} matches)"
                )
            content = content.replace(search, replace, 1)
        try:
            target.write_text(content, encoding="utf-8")
        except Exception:
            target.write_text(original, encoding="utf-8")
            raise
        return target

    def apply_patch_text(
        self, worktree: Path | str, file_relpath: str, patch_text: str
    ) -> Path:
        """Parse debate patch text and apply its blocks to one file.

Raises:
    PatchApplicationError: No structurally complete blocks found.
        """
        blocks = extract_blocks(patch_text)
        if not blocks:
            raise PatchApplicationError(
                "patch text contains no complete SEARCH/REPLACE blocks"
            )
        return self.apply_blocks(worktree, file_relpath, blocks)

    def _green_command(self, language: str) -> list[str]:
        """Build the full-suite JUnit-producing argv (mirrors 2.1)."""
        if language == "python":
            return ["pytest", "-q", "--junitxml", GREEN_JUNIT_NAME]
        return [
            "vitest",
            "run",
            "--reporter=junit",
            f"--outputFile={GREEN_JUNIT_NAME}",
        ]

    def verify_post_patch(
        self,
        worktree: Path | str,
        language: str,
        timeout_sec: int = 300,
        network_disabled: bool = True,
    ) -> GreenCheck:
        """Run the full suite in the worktree and judge green vs red.

Args:
    worktree: Patched working tree (local path, or remote cwd for
        the OCI runner tier).
    language: ``"python"`` or ``"typescript"``.

Raises:
    ValueError: Unsupported language.
        """
        if language not in SUPPORTED_LANGUAGES:
            raise ValueError(
                f"unsupported language {language!r}: "
                f"expected one of {SUPPORTED_LANGUAGES}"
            )
        result = self._runner.run_in_sandbox(
            self._green_command(language),
            cwd=worktree,
            timeout_sec=timeout_sec,
            network_disabled=network_disabled,
        )
        return GreenCheck(
            exit_code=result.exit_code,
            junit_path=str(Path(worktree) / GREEN_JUNIT_NAME),
            timed_out=result.timed_out,
            passed=result.exit_code == 0 and not result.timed_out,
        )
