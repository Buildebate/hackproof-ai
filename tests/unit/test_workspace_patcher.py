"""Step 2.3: WorkspacePatcher + TGRES Provenance (incl. live red-to-green)."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

import pytest

from hackproof.debate.roles.architectural_builder import (
    extract_blocks,
    extract_routed_blocks,
    revise_patch,
)
from hackproof.patcher import (
    GREEN_JUNIT_NAME,
    PatchApplicationError,
    WorkspacePatcher,
    locate_search,
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


def test_apply_routed_blocks_multi_file_success(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.py").write_text("y = 2\n", encoding="utf-8")
    patcher = WorkspacePatcher(runner=_FakeRunner())

    applied = patcher.apply_routed_blocks(
        tmp_path,
        [("a.py", "x = 1\n", "x = 10\n"), ("sub/b.py", "y = 2\n", "y = 20\n")],
    )

    assert sorted(applied) == ["a.py", "sub/b.py"]
    assert (tmp_path / "a.py").read_text(encoding="utf-8") == "x = 10\n"
    assert (tmp_path / "sub" / "b.py").read_text(encoding="utf-8") == "y = 20\n"


def test_apply_routed_blocks_atomic_across_files(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("y = 2\n", encoding="utf-8")
    patcher = WorkspacePatcher(runner=_FakeRunner())

    with pytest.raises(PatchApplicationError, match="block 0: SEARCH not found"):
        patcher.apply_routed_blocks(
            tmp_path,
            [("a.py", "x = 1\n", "x = 10\n"), ("b.py", "NOPE\n", "y = 20\n")],
        )

    assert (tmp_path / "a.py").read_text(encoding="utf-8") == "x = 1\n"
    assert (tmp_path / "b.py").read_text(encoding="utf-8") == "y = 2\n"


# ---------------------------------------------------------------------------
# Step 12a.3: opt-in per-block partial application (atomic default intact)
# ---------------------------------------------------------------------------


def test_partial_applies_hits_skips_misses(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text(
        "one = 1\ntwo = 2\nthree = 3\n", encoding="utf-8"
    )
    (tmp_path / "b.py").write_text("y = 2\n", encoding="utf-8")
    patcher = WorkspacePatcher()
    reports: list[dict] = []

    applied = patcher.apply_routed_blocks(
        tmp_path,
        [
            ("a.py", "one = 1\n", "one = 10\n"),
            ("a.py", "NOPE\n", "two = 20\n"),
            ("a.py", "three = 3\n", "three = 30\n"),
            ("b.py", "NOPE-B\n", "y = 20\n"),
        ],
        partial=True,
        report=reports,
    )

    assert sorted(applied) == ["a.py"]
    assert (tmp_path / "a.py").read_text(encoding="utf-8") == (
        "one = 10\ntwo = 2\nthree = 30\n"
    )
    assert (tmp_path / "b.py").read_text(encoding="utf-8") == "y = 2\n"
    assert len(reports) == 1
    assert reports[0]["applied"] == {"a.py": [0, 2]}
    missed = reports[0]["missed"]
    assert [m["position"] for m in missed] == [1, 3]
    assert missed[0]["file"] == "a.py"
    assert "SEARCH not found" in missed[0]["error"]
    assert missed[1]["file"] == "b.py"


def test_partial_total_miss_raises_and_writes_nothing(
    tmp_path: Path,
) -> None:
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    patcher = WorkspacePatcher()
    reports: list[dict] = []

    with pytest.raises(PatchApplicationError, match="no block matches"):
        patcher.apply_routed_blocks(
            tmp_path,
            [("a.py", "NOPE\n", "x = 2\n")],
            partial=True,
            report=reports,
        )

    assert (tmp_path / "a.py").read_text(encoding="utf-8") == "x = 1\n"
    assert len(reports) == 1
    assert reports[0]["applied"] == {}
    assert [m["position"] for m in reports[0]["missed"]] == [0]


def test_partial_unsafe_block_never_writes(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    patcher = WorkspacePatcher()
    reports: list[dict] = []

    applied = patcher.apply_routed_blocks(
        tmp_path,
        [
            ("../evil.py", "x", "y"),
            ("a.py", "x = 1\n", "x = 10\n"),
        ],
        partial=True,
        report=reports,
    )

    assert sorted(applied) == ["a.py"]
    assert (tmp_path / "a.py").read_text(encoding="utf-8") == "x = 10\n"
    assert not (tmp_path.parent / "evil.py").exists()
    missed = reports[0]["missed"]
    assert [m["position"] for m in missed] == [0]
    assert missed[0]["file"] == "../evil.py"


def test_partial_offline_vector_real_11739_patch(tmp_path: Path) -> None:
    """Step 12a.3: the real B-11739 W2 patch through the partial path.

    Offline and deterministic: the archived 7-block patch is parsed
    for real, a fixture worktree embeds every SEARCH region except
    block 5 (stale), and partial application must land 6 blocks while
    reporting block 5 as addressable with closest-region ground truth.

    Wave-rotation safety (12b.4): the input is the immutable committed
    fixture extracted byte-identical from the pre-Wave-3 HEAD
    (``5b3da6c:.../prompts/B/vuejs__core-11739/patch_text.txt``) — the
    live ``results/prompts`` tree is overwritten by every wave rerun,
    so reading it here broke determinism (Wave 3: 5-block
    ramble-stopped patch). Absence fails loud (committed file).
    """
    patch_path = (
        Path(__file__).resolve().parent
        / "fixtures"
        / "vuejs__core-11739.w2.patch_text.txt"
    )
    assert patch_path.is_file(), f"committed W2 fixture missing: {patch_path}"
    blocks = extract_routed_blocks(patch_path.read_text(encoding="utf-8"))
    assert len(blocks) == 7
    files = sorted({rel for rel, _, _ in blocks})
    assert files == [
        "packages/runtime-dom/src/helpers/useCssVars.ts",
        "packages/runtime-dom/src/modules/style.ts",
    ]
    stale = 5
    for rel in files:
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        parts = [
            f"// --- anchor {pos} ---\n{search}"
            for pos, (f, search, _) in enumerate(blocks)
            if f == rel and pos != stale
        ]
        target.write_text("\n".join(parts) + "\n", encoding="utf-8")
    reports: list[dict] = []
    applied = WorkspacePatcher().apply_routed_blocks(
        tmp_path, blocks, partial=True, report=reports
    )
    assert sorted(applied) == files
    missed = reports[0]["missed"]
    assert [m["position"] for m in missed] == [stale]
    assert missed[0]["file"] == (
        "packages/runtime-dom/src/helpers/useCssVars.ts"
    )
    assert "SEARCH not found" in missed[0]["error"]
    assert "closest region" in missed[0]["error"]
    landed = sum(len(v) for v in reports[0]["applied"].values())
    assert landed == 6


def test_apply_routed_blocks_refuses_unsafe_targets(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    patcher = WorkspacePatcher(runner=_FakeRunner())

    with pytest.raises(PatchApplicationError, match="unsafe path"):
        patcher.apply_routed_blocks(tmp_path, [("../evil.py", "x", "y")])
    with pytest.raises(PatchApplicationError, match="test-infra"):
        patcher.apply_routed_blocks(tmp_path, [("conftest.py", "x", "y")])
    with pytest.raises(FileNotFoundError, match="patch target not found"):
        patcher.apply_routed_blocks(tmp_path, [("missing.py", "x", "y")])

    assert (tmp_path / "a.py").read_text(encoding="utf-8") == "x = 1\n"


def test_apply_routed_blocks_routes_unique_straggler(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("y = 2\n", encoding="utf-8")
    patcher = WorkspacePatcher(runner=_FakeRunner())

    applied = patcher.apply_routed_blocks(
        tmp_path,
        [("a.py", "x = 1\n", "x = 10\n"), (None, "y = 2\n", "y = 20\n")],
        universe=["a.py", "b.py"],
    )

    assert sorted(applied) == ["a.py", "b.py"]
    assert (tmp_path / "b.py").read_text(encoding="utf-8") == "y = 20\n"


def test_apply_routed_blocks_rejects_ambiguous_straggler(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("same = 0\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("same = 0\n", encoding="utf-8")
    patcher = WorkspacePatcher(runner=_FakeRunner())

    with pytest.raises(PatchApplicationError, match="2 files"):
        patcher.apply_routed_blocks(
            tmp_path,
            [("a.py", "same = 0\n", "same = 1\n"), (None, "same = 0\n", "same = 2\n")],
            universe=["a.py", "b.py"],
        )


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


def test_whitespace_only_drift_applies_at_ws_level(tmp_path: Path) -> None:
    # Step 4.3b.3 (R6) INTENTIONAL CONTRACT CHANGE (founder-approved
    # plan): the exact-only rule used to reject trailing-space drift;
    # the ladder now applies whitespace-only drift at the ws level.
    # Content drift is still rejected (see the miss/ambiguity tests).
    target = tmp_path / "app.py"
    target.write_text(VULNERABLE_SRC, encoding="utf-8")
    patcher = WorkspacePatcher(runner=_FakeRunner())
    drifted = "def vulnerable(value): \n    return value\n"  # trailing space
    match = locate_search(VULNERABLE_SRC, drifted)
    assert match.level == "ws"
    patcher.apply_blocks(
        tmp_path,
        "app.py",
        [(drifted, "def vulnerable(value):\n    return value")],
    )
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


# ---------------------------------------------------------------------------
# Step 4.3b.3 (R6): matching ladder, region-bearing misses, single retry
# ---------------------------------------------------------------------------

# Byte-exact excerpts of pristine HEAD bytes (sources: archived
# prompts/A/*/patch_text.txt + /tmp/hp_bench_work/_clones HEAD blobs).
ARCHIVED_SEARCH_1142 = (
    "        else:\n"
    "            self.headers['Content-Length'] = '0'"
)
EXCERPT_1142 = (
    "            length = False\n"
    "\n"
    "        if is_stream:\n"
    "            body = data\n"
    "\n"
    "            if files:\n"
    "                raise NotImplementedError("
    "'Streamed bodies and files are mutually exclusive.')\n"
    "\n"
    "            if length:\n"
    "                self.headers['Content-Length'] = length\n"
    "            else:\n"
    "                self.headers['Transfer-Encoding'] = 'chunked'\n"
    "        # Check if file, fo, generator, iterator.\n"
    "        # If not, run through normal process.\n"
    "\n"
    "        else:\n"
    "            # Multi-part file uploads.\n"
    "            if files:\n"
    "                (body, content_type) = self._encode_files(files, data)\n"
)
# Verbatim span copied from the shown region (lines 9-10 of the excerpt);
# the REPLACE side is a simulation artifact of the retry double.
REVISED_SEARCH_1142 = (
    "            if length:\n"
    "                self.headers['Content-Length'] = length"
)
REVISED_REPLACE_1142 = (
    "            if length:\n"
    "                self.headers['Content-Length'] = str(length)"
)

ARCHIVED_SEARCH_7432 = (
    "    if item.config.option.runxfail:\n"
    "        pass  # don't interfere\n"
    '    elif call.when == "call":'
)
EXCERPT_7432 = (
    "    # unittest special case, see setting of unexpectedsuccess_key\n"
    '    if unexpectedsuccess_key in item._store and rep.when == "call":\n'
    "        reason = item._store[unexpectedsuccess_key]\n"
    "        if reason:\n"
    '            rep.longrepr = "Unexpected success: {}".format(reason)\n'
    "        else:\n"
    '            rep.longrepr = "Unexpected success"\n'
    '        rep.outcome = "failed"\n'
    "    elif item.config.option.runxfail:\n"
    "        pass  # don't interfere\n"
    "    elif call.excinfo and isinstance(call.excinfo.value, xfail.Exception):\n"
    "        assert call.excinfo.value.msg is not None\n"
    '        rep.wasxfail = "reason: " + call.excinfo.value.msg\n'
    '        rep.outcome = "skipped"\n'
    "    elif not rep.skipped and xfailed:\n"
    "        if call.excinfo:\n"
    "            raises = xfailed.raises\n"
    "            if raises is not None and not isinstance(call.excinfo.value, raises):\n"
    '                rep.outcome = "failed"\n'
    "            else:\n"
)
REVISED_SEARCH_7432 = (
    "    elif item.config.option.runxfail:\n"
    "        pass  # don't interfere\n"
    "    elif call.excinfo and isinstance(call.excinfo.value, xfail.Exception):"
)
REVISED_REPLACE_7432 = (
    "    elif item.config.option.runxfail:\n"
    "        rep.outcome = 'skipped'  # revised against shown region\n"
    "    elif call.excinfo and isinstance(call.excinfo.value, xfail.Exception):"
)


class _VerbatimDouble:
    """Retry double: returns one canned revision, captures prompts."""

    def __init__(self, revised_patch: str) -> None:
        self._revised = revised_patch
        self.calls: list[tuple[str, str]] = []

    def debate_round(self, role: str, prompt: str, history: list) -> str:
        self.calls.append((role, prompt))
        assert history == []
        return self._revised


def _revision_patch(search: str, replace: str) -> str:
    return (
        "<<<<<<< SEARCH\n" + search + "\n=======\n" + replace + "\n>>>>>>> REPLACE\n"
    )


def test_locate_search_exact_level_unchanged() -> None:
    match = locate_search(VULNERABLE_SRC, "    return value\n")
    assert (match.start, match.level, match.score) == (23, "exact", 1.0)
    assert VULNERABLE_SRC[match.start : match.end] == "    return value\n"
    with pytest.raises(PatchApplicationError, match=r"ambiguous SEARCH \(2"):
        locate_search("same\nsame\n", "same\n")


def test_ladder_ws_level_applies_reindented_search(tmp_path: Path) -> None:
    target = tmp_path / "app.py"
    target.write_text(VULNERABLE_SRC, encoding="utf-8")
    reindented = "def vulnerable(value):\n        return value\n"
    assert VULNERABLE_SRC.count(reindented) == 0
    match = locate_search(VULNERABLE_SRC, reindented)
    assert match.level == "ws"
    patcher = WorkspacePatcher(runner=_FakeRunner())
    patcher.apply_blocks(
        tmp_path,
        "app.py",
        [(reindented, "def vulnerable(value):\n    return value\n    # touched")],
    )
    assert target.read_text(encoding="utf-8") == (
        "def vulnerable(value):\n    return value\n    # touched\n"
    )


def test_ladder_fuzzy_level_applies_single_token_typo(tmp_path: Path) -> None:
    content = (
        "def compute_total(items):\n"
        "    total = sum(items)\n"
        "    return total\n"
    )
    typo = (
        "def compute_total(items):\n"
        "    total = sum(items)\n"
        "    return totals"
    )
    assert content.count(typo) == 0
    match = locate_search(content, typo)
    assert match.level == "fuzzy"
    assert match.score >= 0.90
    target = tmp_path / "calc.py"
    target.write_text(content, encoding="utf-8")
    patcher = WorkspacePatcher(runner=_FakeRunner())
    patcher.apply_blocks(
        tmp_path,
        "calc.py",
        [
            (
                typo,
                "def compute_total(items):\n"
                "    total = sum(items)\n"
                "    return int(total)",
            )
        ],
    )
    assert target.read_text(encoding="utf-8") == (
        "def compute_total(items):\n"
        "    total = sum(items)\n"
        "    return int(total)\n"
    )


def test_ladder_ambiguous_fuzzy_refused(tmp_path: Path) -> None:
    content = (
        'processed_result_alpha = compute_weighted_total(input_items, mode="alpha")\n'
        "store_processed_result(processed_result_alphx)\n"
        'processed_result_alpha = compute_weighted_total(input_items, mode="alpha")\n'
        "store_processed_result(processed_result_alphy)\n"
    )
    search = (
        'processed_result_alpha = compute_weighted_total(input_items, mode="alpha")\n'
        "store_processed_result(processed_result_alphz)"
    )
    assert content.count(search) == 0
    with pytest.raises(PatchApplicationError, match="ambiguous SEARCH"):
        locate_search(content, search)
    target = tmp_path / "dup.py"
    target.write_text(content, encoding="utf-8")
    patcher = WorkspacePatcher(runner=_FakeRunner())
    with pytest.raises(PatchApplicationError, match="fuzzy matches above"):
        patcher.apply_blocks(tmp_path, "dup.py", [(search, "x")])
    assert target.read_text(encoding="utf-8") == content


def test_ladder_fuzzy_skipped_for_tiny_search() -> None:
    with pytest.raises(PatchApplicationError, match="no similar region"):
        locate_search("abcdefg\n", "abcx")


def test_miss_carries_closest_region_7432() -> None:
    assert EXCERPT_7432.count(ARCHIVED_SEARCH_7432) == 0
    with pytest.raises(PatchApplicationError) as excinfo:
        locate_search(EXCERPT_7432, ARCHIVED_SEARCH_7432)
    message = str(excinfo.value)
    assert "SEARCH not found (0 matches" in message
    assert "closest region" in message
    assert "similarity 0.71" in message
    assert "elif item.config.option.runxfail:" in message
    assert "pass  # don't interfere" in message
    assert " 9 | " in message


@pytest.mark.parametrize(
    "excerpt, archived, revised_search, revised_replace",
    [
        (EXCERPT_1142, ARCHIVED_SEARCH_1142, REVISED_SEARCH_1142, REVISED_REPLACE_1142),
        (EXCERPT_7432, ARCHIVED_SEARCH_7432, REVISED_SEARCH_7432, REVISED_REPLACE_7432),
    ],
)
def test_retry_loop_applies_after_region_shown(
    tmp_path: Path,
    excerpt: str,
    archived: str,
    revised_search: str,
    revised_replace: str,
) -> None:
    """Step 4.3b.3 acceptance: A-1142 / A-7432 apply via one retry.

    The archived SEARCH (byte-exact debate output) honestly misses;
    the miss carries the real region; the double plays an instructed
    model copying its corrected SEARCH verbatim from that region; the
    re-extracted block applies exactly once. Assertions target the
    machinery (single retry, region in the retry prompt, byte-exact
    landing), not model cognition.
    """
    assert excerpt.count(archived) == 0
    assert excerpt.count(revised_search) == 1
    target = tmp_path / "target.py"
    target.write_text(excerpt, encoding="utf-8")
    patcher = WorkspacePatcher(runner=_FakeRunner())
    with pytest.raises(PatchApplicationError) as excinfo:
        patcher.apply_blocks(tmp_path, "target.py", [(archived, "revised")])
    failure = str(excinfo.value)
    assert "closest region" in failure
    double = _VerbatimDouble(_revision_patch(revised_search, revised_replace))
    revised_text = revise_patch(
        double,
        issue_description="issue",
        code_context=excerpt,
        previous_patch=_revision_patch(archived, "revised"),
        failure_report=failure,
    )
    assert len(double.calls) == 1
    role, prompt = double.calls[0]
    assert role == "ArchitecturalBuilder"
    assert "closest region" in prompt
    assert revised_search.splitlines()[0].strip() in prompt
    blocks = extract_blocks(revised_text)
    assert len(blocks) == 1
    patcher.apply_blocks(tmp_path, "target.py", blocks)
    expected = excerpt.replace(revised_search, revised_replace, 1)
    assert target.read_text(encoding="utf-8") == expected
    assert revised_replace.splitlines()[-1] in expected


def test_missing_target_suggests_closest_path(tmp_path: Path) -> None:
    # Step 4.3b.3 (R6): the 4.3b.2 ghost-path lesson
    # (runtime-core vs runtime-dom useCssVars.ts) is now loud.
    real_dir = tmp_path / "packages" / "runtime-dom" / "src" / "helpers"
    real_dir.mkdir(parents=True)
    (real_dir / "useCssVars.ts").write_text("export {}\n", encoding="utf-8")
    patcher = WorkspacePatcher(runner=_FakeRunner())
    ghost = "packages/runtime-core/src/helpers/useCssVars.ts"
    try:
        patcher.apply_blocks(tmp_path, ghost, [("a", "b")])
    except FileNotFoundError as exc:
        message = str(exc)
    else:  # pragma: no cover - the target genuinely does not exist
        raise AssertionError("expected FileNotFoundError")
    assert "patch target not found" in message
    assert (
        "did you mean: packages/runtime-dom/src/helpers/useCssVars.ts?"
        in message
    )
