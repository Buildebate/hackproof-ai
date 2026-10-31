"""Deterministic workspace patcher (Step 2.3, ladder Step 4.3b.3/R6).

Applies ``SEARCH``/``REPLACE`` blocks inside an isolated ``git
worktree`` with zero line-number drift. Matching follows a
conservative ladder — exact, then whitespace-insensitive, then unique
fuzzy (threshold plus runner-up guard): a level with 0 matches falls
through to the next one, while 2+ matches at any level reject the
whole application and the file is left untouched (no partial
application, atomic revert on write failure). Total misses carry the
closest real region (bounded excerpt with line numbers) so the
single-retry loop can show the model ground truth instead of a bare
``0 matches``. Post-patch verification runs the full suite
(``pytest --junitxml`` / ``vitest --reporter=junit``) through the
injected sandbox runner (local default, ``RemoteSSHRunner`` for OCI).
"""

from __future__ import annotations

import difflib
import re
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

_WS_RUN = re.compile(r"\s+")
_FUZZY_THRESHOLD = 0.90
_FUZZY_MIN_CHARS = 12
_FUZZY_MIN_REGION_SCORE = 0.30
_REGION_CONTEXT_LINES = 8
_REGION_MAX_CHARS = 1500
_SUGGEST_CUTOFF = 0.6
_SUGGEST_RGLOB_CAP = 20000

__all__ = [
    "SUPPORTED_LANGUAGES",
    "GREEN_JUNIT_NAME",
    "GreenCheck",
    "PatchApplicationError",
    "SearchMatch",
    "WorkspacePatcher",
    "locate_search",
    "suggest_path",
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


@dataclass(frozen=True)
class SearchMatch:
    """One located SEARCH span: char offsets, ladder level, score."""

    start: int
    end: int
    level: str  # "exact" | "ws" | "fuzzy"
    score: float  # 1.0 for exact/ws, SequenceMatcher ratio for fuzzy


def _split_lines(content: str) -> tuple[list[str], list[int]]:
    """Split keeping line breaks plus the char offset of every line."""
    lines = content.splitlines(keepends=True)
    starts: list[int] = []
    offset = 0
    for line in lines:
        starts.append(offset)
        offset += len(line)
    return lines, starts


def _norm_line(line: str) -> str:
    """Collapse inner whitespace runs and strip ends (indent-insensitive)."""
    return _WS_RUN.sub(" ", line).strip()


def _window_span(
    lines: list[str], starts: list[int], at: int, nlines: int
) -> tuple[int, int]:
    """Char span of an n-line window, excluding the trailing line break."""
    last = lines[at + nlines - 1]
    stripped_len = len(last.rstrip("\r\n"))
    return starts[at], starts[at + nlines - 1] + stripped_len


def _format_region(
    lines: list[str], at: int, nlines: int, score: float
) -> str:
    """Numbered excerpt around a candidate window (bounded, retry fuel)."""
    first = max(0, at - _REGION_CONTEXT_LINES)
    last = min(len(lines), at + nlines + _REGION_CONTEXT_LINES)
    width = len(str(last))
    body_lines: list[str] = []
    for lineno in range(first, last):
        text = lines[lineno]
        if text.endswith("\n"):
            text = text[:-1]
        if text.endswith("\r"):
            text = text[:-1]
        body_lines.append(f"{lineno + 1:>{width}} | {text}")
    body = "\n".join(body_lines)
    header = (
        f"closest region (lines {at + 1}-{at + nlines}, "
        f"similarity {score:.2f}):"
    )
    rendered = f"{header}\n{body}"
    if len(rendered) > _REGION_MAX_CHARS:
        rendered = rendered[:_REGION_MAX_CHARS] + "\n[... truncated]"
    return rendered


def _miss_message(region: str | None) -> str:
    """Total-miss error text, always carrying ground truth when known."""
    message = (
        "SEARCH not found (0 matches at exact/ws/fuzzy>=0.90)"
    )
    if region is None:
        return message + "; no similar region found"
    return message + "\n" + region


def locate_search(content: str, search: str) -> SearchMatch:
    """Locate one SEARCH span via the exact/wider ladder.

    Levels: exact substring (historical rule), then
    whitespace-insensitive whole-line windows (reindentation, spacing,
    trailing whitespace), then a unique fuzzy window (SequenceMatcher
    ratio >= 0.90 with no runner-up above the threshold; searches
    shorter than 12 non-space chars skip fuzzy entirely). Every level
    keeps the exact-once rule: 2+ matches reject instead of guessing.

    Raises:
        PatchApplicationError: Blank SEARCH, an ambiguous match, or a
            total miss (the message carries the closest real region
            when one scores >= 0.30, bounded to ~1.5 KB).
    """
    if not search.strip():
        raise PatchApplicationError("empty SEARCH text")
    first = content.find(search)
    if first != -1:
        if content.find(search, first + 1) != -1:
            occurrences = content.count(search)
            raise PatchApplicationError(
                f"ambiguous SEARCH ({occurrences} matches)"
            )
        return SearchMatch(first, first + len(search), "exact", 1.0)
    lines, starts = _split_lines(content)
    wanted = search.splitlines()
    nlines = len(wanted)
    if nlines == 0 or nlines > len(lines):
        raise PatchApplicationError(_miss_message(None))
    norm_wanted = [_norm_line(line) for line in wanted]
    if not any(norm_wanted):
        raise PatchApplicationError("empty SEARCH text")
    norm_lines = [_norm_line(line) for line in lines]
    ws_hits = [
        at
        for at in range(len(lines) - nlines + 1)
        if norm_lines[at : at + nlines] == norm_wanted
    ]
    if len(ws_hits) > 1:
        raise PatchApplicationError(
            f"ambiguous SEARCH ({len(ws_hits)} "
            "whitespace-insensitive matches)"
        )
    if len(ws_hits) == 1:
        start, end = _window_span(lines, starts, ws_hits[0], nlines)
        return SearchMatch(start, end, "ws", 1.0)
    best = -1.0
    best_at = -1
    above = 0
    if len(search.strip()) >= _FUZZY_MIN_CHARS:
        target = "\n".join(norm_wanted)
        for at in range(len(lines) - nlines + 1):
            candidate = "\n".join(norm_lines[at : at + nlines])
            score = difflib.SequenceMatcher(None, target, candidate).ratio()
            if score >= _FUZZY_THRESHOLD:
                above += 1
            if score > best:
                best = score
                best_at = at
    if above > 1:
        raise PatchApplicationError(
            f"ambiguous SEARCH ({above} fuzzy matches above 0.90)"
        )
    if above == 1:
        start, end = _window_span(lines, starts, best_at, nlines)
        return SearchMatch(start, end, "fuzzy", best)
    region: str | None = None
    if best >= _FUZZY_MIN_REGION_SCORE and best_at >= 0:
        region = _format_region(lines, best_at, nlines, best)
    raise PatchApplicationError(_miss_message(region))


def _matches(content: str, search: str) -> bool:
    """True when the ladder locates ``search`` (routing probe)."""
    try:
        locate_search(content, search)
    except PatchApplicationError:
        return False
    return True


def _apply_to_text(
    original: str, blocks: Sequence[tuple[str, str]], label: str
) -> str:
    """Apply blocks to text in memory (shared by both apply paths)."""
    content = original
    for index, (search, replace) in enumerate(blocks):
        try:
            match = locate_search(content, search)
        except PatchApplicationError as exc:
            raise PatchApplicationError(
                f"{label}block {index}: {exc}"
            ) from exc
        content = content[: match.start] + replace + content[match.end :]
    return content


def suggest_path(root: Path | str, relpath: str) -> str | None:
    """Closest tracked path to a missing target (did-you-mean).

    Prefers ``git ls-files`` (fast, tracked only); falls back to a
    capped recursive walk for plain directories. Returns None when no
    candidate scores above the cutoff.
    """
    names: list[str] = []
    try:
        proc = _git(["ls-files"], root)
        if proc.returncode == 0:
            names = [line for line in proc.stdout.splitlines() if line]
    except Exception:
        names = []
    if not names:
        base = Path(root)
        try:
            count = 0
            for path in base.rglob("*"):
                if count >= _SUGGEST_RGLOB_CAP:
                    break
                try:
                    rel = path.relative_to(base)
                except ValueError:
                    continue
                if ".git" in rel.parts or not path.is_file():
                    continue
                names.append(rel.as_posix())
                count += 1
        except Exception:
            names = []
    if not names:
        return None
    matches = difflib.get_close_matches(
        relpath, names, n=1, cutoff=_SUGGEST_CUTOFF
    )
    return matches[0] if matches else None


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


_ROUTED_DENY_BASENAMES = frozenset(
    {
        "conftest.py",
        "pytest.ini",
        "setup.cfg",
        "pyproject.toml",
        "package.json",
        "tox.ini",
        "noxfile.py",
    }
)
_ROUTED_DENY_DIRS = (".git/", ".github/")


def _check_routed_target(file_relpath: str) -> str:
    """Validate a routed target path (escape + test-infra rails)."""
    if (
        not file_relpath
        or file_relpath.startswith("/")
        or ".." in Path(file_relpath).parts
    ):
        raise PatchApplicationError(f"refused: unsafe path {file_relpath!r}")
    lowered = file_relpath.replace("\\", "/")
    if lowered.startswith(_ROUTED_DENY_DIRS) or Path(lowered).name in _ROUTED_DENY_BASENAMES:
        raise PatchApplicationError(
            f"refused: test-infra path {file_relpath!r}"
        )
    return file_relpath


def _missing_target_error(worktree: Path | str, file_relpath: str) -> FileNotFoundError:
    """FileNotFoundError enriched with a did-you-mean suggestion."""
    target = Path(worktree) / file_relpath
    message = f"patch target not found: {target}"
    hint = suggest_path(worktree, file_relpath)
    if hint is not None:
        message += f" (did you mean: {hint}?)"
    return FileNotFoundError(message)


class WorkspacePatcher:
    """Ladder-match patch application in isolated git worktrees."""

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

Every SEARCH must locate exactly once via the exact/ws/fuzzy
ladder (a total miss or 2+ matches at any level reject the whole
application). The file is written exactly once after all blocks
validate in memory, so a failing hunk never leaves a partially
patched file.

Raises:
    FileNotFoundError: Target file missing in the worktree (carries a
        did-you-mean suggestion when one scores above the cutoff).
    PatchApplicationError: Empty block list, empty SEARCH, or a
        missing/ambiguous match (misses carry the closest region).
        """
        if not blocks:
            raise PatchApplicationError("no SEARCH/REPLACE blocks to apply")
        target = Path(worktree) / file_relpath
        if not target.is_file():
            raise _missing_target_error(worktree, file_relpath)
        original = target.read_text(encoding="utf-8")
        content = _apply_to_text(original, blocks, "")
        try:
            target.write_text(content, encoding="utf-8")
        except Exception:
            target.write_text(original, encoding="utf-8")
            raise
        return target

    def apply_routed_blocks(
        self,
        worktree: Path | str,
        routed: Sequence[tuple[str | None, str, str]],
        universe: Sequence[str] | None = None,
    ) -> dict[str, Path]:
        """Apply file-routed SEARCH/REPLACE blocks across files, atomically.

        Blocks carrying a header path apply to that file; headerless
        stragglers must locate in exactly one file of the routed set
        plus ``universe`` (caller-provided candidates). Every file
        validates fully in memory (ladder exact-once per block, same
        rule as :meth:`apply_blocks`) and all files are written only
        after every block validates — a failing hunk leaves the whole
        tree pristine.

        Raises:
            FileNotFoundError: A routed target file is missing (carries
                a did-you-mean suggestion when one scores above cutoff).
            PatchApplicationError: Empty input, unsafe/test-infra path,
                unroutable straggler, or a missing/ambiguous match.
        """
        if not routed:
            raise PatchApplicationError("no SEARCH/REPLACE blocks to apply")
        root = Path(worktree)
        search_space = list(
            dict.fromkeys([f for f, _, _ in routed if f] + list(universe or []))
        )
        resolved: list[tuple[str, str, str]] = []
        for position, (file_rel, search, replace) in enumerate(routed):
            if file_rel is not None:
                resolved.append(
                    (_check_routed_target(file_rel), search, replace)
                )
                continue
            if not search.strip():
                raise PatchApplicationError(
                    f"block {position}: empty SEARCH text"
                )
            hits = [
                candidate
                for candidate in search_space
                if (root / candidate).is_file()
                and _matches(
                    (root / candidate).read_text(encoding="utf-8"), search
                )
            ]
            if len(hits) != 1:
                raise PatchApplicationError(
                    f"block {position}: headerless SEARCH matches "
                    f"{len(hits)} files "
                    f"(need exactly 1 of {len(search_space)} searched)"
                )
            resolved.append((hits[0], search, replace))
        by_file: dict[str, list[tuple[str, str]]] = {}
        for file_rel, search, replace in resolved:
            by_file.setdefault(file_rel, []).append((search, replace))
        staged: dict[str, tuple[Path, str, str]] = {}
        for file_rel, blocks in by_file.items():
            target = root / file_rel
            if not target.is_file():
                raise _missing_target_error(worktree, file_rel)
            original = target.read_text(encoding="utf-8")
            content = _apply_to_text(original, blocks, f"{file_rel}: ")
            staged[file_rel] = (target, original, content)
        applied: dict[str, Path] = {}
        try:
            for file_rel, (target, _, content) in staged.items():
                target.write_text(content, encoding="utf-8")
                applied[file_rel] = target
        except Exception:
            for file_rel in applied:
                target, original, _ = staged[file_rel]
                target.write_text(original, encoding="utf-8")
            raise
        return applied

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
