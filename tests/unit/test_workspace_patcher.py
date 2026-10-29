"""Step 2.3: WorkspacePatcher + TGRES Provenance (incl. live red-to-green)."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

import pytest

from hackproof.patcher import (
    GREEN_JUNIT_NAME,
    PatchApplicationError,
    WorkspacePatcher,
)
from hackproof.provenance import TGRESProvenanceVerifier
from hackproof.sandbox.guards import RewardHackingDetectedError
from hackproof.sandbox.runner import (
    LocalSubprocessRunner,
    SandboxExecutionResult,
)

RED_XML = """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest" errors="0" failures="1" skipped="0" tests="1">
<testcase classname="test_repro" name="test_repro_x" time="0.01">
<failure message="AssertionError: boom">assert False</failure>
</testcase></testsuite></testsuites>
"""

GREEN_XML = """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest" errors="0" failures="0" skipped="0" tests="1">
<testcase classname="t" name="t" time="0.01" /></testsuite></testsuites>
"""

ERROR_XML = """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest" errors="1" failures="0" skipped="0" tests="1">
<testcase classname="t" name="t"><error message="SyntaxError: bad">trace</error>
</testcase></testsuite></testsuites>
"""

PATCH_TEXT = (
    "<<<<<<< SEARCH\n"
    "def vulnerable(value):\n"
    "    return value\n"
    "=======\n"
    "def vulnerable(value):\n"
    "    if value is None:\n"
    '        raise ValueError("none rejected")\n'
    "    return value\n"
    ">>>>>>> REPLACE\n"
)

VULNERABLE_SRC = "def vulnerable(value):\n    return value\n"
PATCHED_SRC = (
    "def vulnerable(value):\n"
    "    if value is None:\n"
    '        raise ValueError("none rejected")\n'
    "    return value\n"
)


class _FakeRunner:
    """Canned runner capturing argv and returning a scripted result."""

    def __init__(self, exit_code: int = 0, timed_out: bool = False) -> None:
        self._exit_code = exit_code
        self._timed_out = timed_out
        self.calls: list[tuple[list[str], str, int, bool]] = []

    def run_in_sandbox(self, cmd, cwd, timeout_sec=60, network_disabled=True):
        self.calls.append((list(cmd), str(cwd), timeout_sec, network_disabled))
        return SandboxExecutionResult(
            exit_code=self._exit_code,
            stdout="",
            stderr="",
            duration_ms=3,
            timed_out=self._timed_out,
        )


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=str(repo),
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """Real local git repository with one committed commit."""
    if shutil.which("git") is None:
        pytest.skip("git binary required for worktree tests")
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "hackproof@test.local")
    _git(repo, "config", "user.name", "HackProof Test")
    (repo / "app.py").write_text(VULNERABLE_SRC, encoding="utf-8")
    _git(repo, "add", "app.py")
    _git(repo, "commit", "-q", "-m", "base commit")
    return repo


# ---------------------------------------------------------------------------
# SEARCH/REPLACE application matrix
# ---------------------------------------------------------------------------


def test_apply_single_block_exact(tmp_path: Path) -> None:
    target = tmp_path / "app.py"
    target.write_text(VULNERABLE_SRC, encoding="utf-8")
    patcher = WorkspacePatcher(runner=_FakeRunner())

    out = patcher.apply_patch_text(tmp_path, "app.py", PATCH_TEXT)

    assert out == target
    assert target.read_text(encoding="utf-8") == PATCHED_SRC


def test_apply_multiple_blocks_sequential(tmp_path: Path) -> None:
    target = tmp_path / "m.py"
    target.write_text("a = 1\nb = 2\n", encoding="utf-8")
    patcher = WorkspacePatcher(runner=_FakeRunner())

    patcher.apply_blocks(tmp_path, "m.py", [("a = 1", "a = 10"), ("b = 2", "b = 20")])

    assert target.read_text(encoding="utf-8") == "a = 10\nb = 20\n"


def test_missing_search_rejected_without_touching_file(tmp_path: Path) -> None:
    target = tmp_path / "app.py"
    target.write_text(VULNERABLE_SRC, encoding="utf-8")
    patcher = WorkspacePatcher(runner=_FakeRunner())

    with pytest.raises(PatchApplicationError, match="0 matches"):
        patcher.apply_blocks(tmp_path, "app.py", [("def absent():", "x")])
    assert target.read_text(encoding="utf-8") == VULNERABLE_SRC


def test_duplicate_search_rejected_without_touching_file(tmp_path: Path) -> None:
    target = tmp_path / "dup.py"
    target.write_text("x = 1\nx = 1\n", encoding="utf-8")
    patcher = WorkspacePatcher(runner=_FakeRunner())

    with pytest.raises(PatchApplicationError, match="2 matches"):
        patcher.apply_blocks(tmp_path, "dup.py", [("x = 1", "x = 2")])
    assert target.read_text(encoding="utf-8") == "x = 1\nx = 1\n"


def test_whitespace_drifted_search_rejected(tmp_path: Path) -> None:
    target = tmp_path / "app.py"
    target.write_text(VULNERABLE_SRC, encoding="utf-8")
    patcher = WorkspacePatcher(runner=_FakeRunner())
    drifted = "def vulnerable(value): \n    return value\n"  # trailing space

    with pytest.raises(PatchApplicationError, match="0 matches"):
        patcher.apply_blocks(tmp_path, "app.py", [(drifted, "x")])
    assert target.read_text(encoding="utf-8") == VULNERABLE_SRC


def test_second_failing_hunk_leaves_no_partial_application(
    tmp_path: Path,
) -> None:
    target = tmp_path / "m.py"
    target.write_text("a = 1\nb = 2\n", encoding="utf-8")
    patcher = WorkspacePatcher(runner=_FakeRunner())

    with pytest.raises(PatchApplicationError, match="block 1"):
        patcher.apply_blocks(
            tmp_path, "m.py", [("a = 1", "a = 10"), ("zzz", "q")]
        )
    assert target.read_text(encoding="utf-8") == "a = 1\nb = 2\n"


def test_empty_inputs_rejected(tmp_path: Path) -> None:
    target = tmp_path / "app.py"
    target.write_text(VULNERABLE_SRC, encoding="utf-8")
    patcher = WorkspacePatcher(runner=_FakeRunner())

    with pytest.raises(PatchApplicationError, match="no SEARCH/REPLACE"):
        patcher.apply_blocks(tmp_path, "app.py", [])
    with pytest.raises(PatchApplicationError, match="no complete"):
        patcher.apply_patch_text(tmp_path, "app.py", "plain prose, no blocks")
    with pytest.raises(PatchApplicationError, match="empty SEARCH"):
        patcher.apply_blocks(tmp_path, "app.py", [("   ", "x")])
    with pytest.raises(FileNotFoundError, match="patch target"):
        patcher.apply_blocks(tmp_path, "missing.py", [("a", "b")])
    assert target.read_text(encoding="utf-8") == VULNERABLE_SRC


# ---------------------------------------------------------------------------
# Isolated worktree lifecycle (real git)
# ---------------------------------------------------------------------------


def test_worktree_isolates_and_cleans_up(git_repo: Path) -> None:
    patcher = WorkspacePatcher(runner=_FakeRunner())
    with patcher.isolated_worktree(git_repo) as worktree:
        assert worktree.is_dir()
        assert (worktree / "app.py").read_text(encoding="utf-8") == VULNERABLE_SRC
        patcher.apply_patch_text(worktree, "app.py", PATCH_TEXT)
        assert (worktree / "app.py").read_text(encoding="utf-8") == PATCHED_SRC
        # Base repository stays pristine while the worktree is patched.
        assert (git_repo / "app.py").read_text(encoding="utf-8") == VULNERABLE_SRC
        held = worktree

    assert not held.exists()
    listing = subprocess.run(
        ["git", "worktree", "list"],
        cwd=str(git_repo),
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    assert "hackproof-worktree-" not in listing.stdout


def test_worktree_removed_even_when_body_raises(git_repo: Path) -> None:
    patcher = WorkspacePatcher(runner=_FakeRunner())
    with pytest.raises(RuntimeError, match="boom"):
        with patcher.isolated_worktree(git_repo) as worktree:
            held = worktree
            raise RuntimeError("boom")
    assert not held.exists()


def test_worktree_rejects_non_repository(tmp_path: Path) -> None:
    if shutil.which("git") is None:
        pytest.skip("git binary required")
    patcher = WorkspacePatcher(runner=_FakeRunner())
    with pytest.raises(ValueError, match="not a git repository"):
        with patcher.isolated_worktree(tmp_path):
            pass  # pragma: no cover


def test_worktree_requires_git_binary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(shutil, "which", lambda *_a, **_k: None)
    patcher = WorkspacePatcher(runner=_FakeRunner())
    with pytest.raises(RuntimeError, match="git binary"):
        with patcher.isolated_worktree(tmp_path):
            pass  # pragma: no cover


# ---------------------------------------------------------------------------
# Post-patch verification hook
# ---------------------------------------------------------------------------


def test_green_command_matrix_and_pass_logic(tmp_path: Path) -> None:
    runner = _FakeRunner(exit_code=0)
    patcher = WorkspacePatcher(runner=runner)
    check = patcher.verify_post_patch(tmp_path, "python")

    assert runner.calls[0][0] == ["pytest", "-q", "--junitxml", GREEN_JUNIT_NAME]
    assert runner.calls[0][1] == str(tmp_path)
    assert runner.calls[0][3] is True
    assert check.passed is True
    assert check.exit_code == 0
    assert check.junit_path == str(tmp_path / GREEN_JUNIT_NAME)
    assert check.timed_out is False

    ts_runner = _FakeRunner(exit_code=0)
    ts_patcher = WorkspacePatcher(runner=ts_runner)
    ts_patcher.verify_post_patch(tmp_path, "typescript")
    assert ts_runner.calls[0][0] == [
        "vitest",
        "run",
        "--reporter=junit",
        f"--outputFile={GREEN_JUNIT_NAME}",
    ]

    failing = WorkspacePatcher(runner=_FakeRunner(exit_code=1))
    assert failing.verify_post_patch(tmp_path, "python").passed is False
    slow = WorkspacePatcher(runner=_FakeRunner(exit_code=0, timed_out=True))
    assert slow.verify_post_patch(tmp_path, "python").passed is False


def test_verify_post_patch_rejects_language(tmp_path: Path) -> None:
    patcher = WorkspacePatcher(runner=_FakeRunner())
    with pytest.raises(ValueError, match="unsupported language"):
        patcher.verify_post_patch(tmp_path, "go")


# ---------------------------------------------------------------------------
# Live red-to-green end-to-end (real git + real sandboxed pytest)
# ---------------------------------------------------------------------------


def test_live_red_to_green_in_worktree(git_repo: Path) -> None:
    if shutil.which("pytest") is None:
        pytest.skip("pytest binary required for live sandbox check")
    failing_test = (
        "from app import vulnerable\n"
        "\n"
        "def test_none_rejected():\n"
        "    try:\n"
        "        vulnerable(None)\n"
        "    except ValueError:\n"
        "        return\n"
        "    raise AssertionError('None was not rejected')\n"
    )
    (git_repo / "test_app.py").write_text(failing_test, encoding="utf-8")
    _git(git_repo, "add", "test_app.py")
    _git(git_repo, "commit", "-q", "-m", "red test")

    patcher = WorkspacePatcher(runner=LocalSubprocessRunner())
    with patcher.isolated_worktree(git_repo) as worktree:
        red = patcher.verify_post_patch(worktree, "python", timeout_sec=120)
        assert red.passed is False
        assert red.exit_code != 0
        red_xml = Path(red.junit_path).read_text(encoding="utf-8")
        assert "<failure" in red_xml

        patcher.apply_patch_text(worktree, "app.py", PATCH_TEXT)

        green = patcher.verify_post_patch(worktree, "python", timeout_sec=120)
        assert green.passed is True
        assert green.exit_code == 0
        assert Path(green.junit_path).is_file()


# ---------------------------------------------------------------------------
# TGRES provenance receipts
# ---------------------------------------------------------------------------


def test_mint_receipt_schema_and_digest() -> None:
    receipt = TGRESProvenanceVerifier.mint_receipt(
        base_commit="abc123",
        test_hash="t" * 64,
        patch_hash="p" * 64,
        red_junit_hash="r" * 64,
        green_junit_hash="g" * 64,
    )
    assert receipt["receipt_version"] == "1.0"
    assert receipt["base_commit"] == "abc123"
    stamp = datetime.fromisoformat(receipt["minted_at_utc"])
    assert stamp.tzinfo is not None
    body = {k: v for k, v in receipt.items() if k != "receipt_sha256"}
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
    assert (
        hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        == receipt["receipt_sha256"]
    )


def test_mint_receipt_rejects_empty_inputs() -> None:
    good = {
        "base_commit": "abc",
        "test_hash": "t",
        "patch_hash": "p",
        "red_junit_hash": "r",
        "green_junit_hash": "g",
    }
    for key in good:
        bad = dict(good)
        bad[key] = "  "
        with pytest.raises(ValueError, match=key):
            TGRESProvenanceVerifier.mint_receipt(**bad)


def test_verify_red_to_green_accepts_clean_proof(tmp_path: Path) -> None:
    assert (
        TGRESProvenanceVerifier.verify_red_to_green(RED_XML, 0, (True, []))
        is True
    )
    report = tmp_path / "red.xml"
    report.write_text(RED_XML, encoding="utf-8")
    assert (
        TGRESProvenanceVerifier.verify_red_to_green(report, 0, (True, []))
        is True
    )


def test_verify_red_to_green_negative_matrix() -> None:
    assert (
        TGRESProvenanceVerifier.verify_red_to_green(GREEN_XML, 0, (True, []))
        is False
    )
    assert (
        TGRESProvenanceVerifier.verify_red_to_green(ERROR_XML, 0, (True, []))
        is False
    )
    assert (
        TGRESProvenanceVerifier.verify_red_to_green(RED_XML, 1, (True, []))
        is False
    )
    with pytest.raises(ValueError, match="unparseable"):
        TGRESProvenanceVerifier.verify_red_to_green("not <xml", 0, (True, []))


def test_verify_red_to_green_missing_file(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="not produced"):
        TGRESProvenanceVerifier.verify_red_to_green(
            tmp_path / "absent.xml", 0, (True, [])
        )


def test_verify_red_to_green_raises_on_guard_breach() -> None:
    with pytest.raises(RewardHackingDetectedError, match="modified"):
        TGRESProvenanceVerifier.verify_red_to_green(
            RED_XML, 0, (False, ["modified: tests/unit/test_x.py"])
        )
    with pytest.raises(RewardHackingDetectedError):
        TGRESProvenanceVerifier.verify_red_to_green(
            RED_XML, 0, (True, ["deleted: tests/a.py"])
        )
    with pytest.raises(RewardHackingDetectedError):
        TGRESProvenanceVerifier.verify_red_to_green(RED_XML, 0, (False, []))


def test_verify_red_to_green_rejects_malformed_report() -> None:
    with pytest.raises(ValueError, match="guard_report"):
        TGRESProvenanceVerifier.verify_red_to_green(RED_XML, 0, "clean")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="guard_report"):
        TGRESProvenanceVerifier.verify_red_to_green(RED_XML, 0, (True, [], "x"))  # type: ignore[arg-type]
