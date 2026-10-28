"""Step 1.3: air-gapped sandbox and immutable existing tests guard."""

import json
import sys
from pathlib import Path

import pytest

from hackproof.sandbox import (
    ImmutableTestGuard,
    LocalSubprocessRunner,
    RewardHackingDetectedError,
    SandboxExecutionResult,
)

PLAIN_TEST = "def test_ok():\n    assert 1 + 1 == 2\n"
ASSERT_RICH_TEST = (
    "def test_math():\n"
    "    assert 2 * 3 == 6\n"
    "    assert 'a' in 'abc'\n"
    "\n"
    "def test_unittest_style(self=None):\n"
    "    self.assertEqual(1, 1)\n"
)


def _write_suite(root: Path) -> dict[str, str]:
    (root / "test_alpha.py").write_text(PLAIN_TEST)
    nested = root / "nested"
    nested.mkdir()
    (nested / "test_beta.py").write_text(ASSERT_RICH_TEST)
    return {
        "test_alpha.py": PLAIN_TEST,
        "nested/test_beta.py": ASSERT_RICH_TEST,
    }


def test_baseline_lock_and_clean_pass(tmp_path: Path) -> None:
    _write_suite(tmp_path)
    baseline = ImmutableTestGuard.lock_test_baseline(tmp_path)
    assert set(baseline) == {"test_alpha.py", "nested/test_beta.py"}
    assert all(len(value) == 64 for value in baseline.values())
    assert ImmutableTestGuard.verify_test_integrity(tmp_path, baseline) == (True, [])


def test_lock_accepts_str_path(tmp_path: Path) -> None:
    _write_suite(tmp_path)
    baseline = ImmutableTestGuard.lock_test_baseline(str(tmp_path))
    assert len(baseline) == 2


def test_lock_rejects_missing_dir(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        ImmutableTestGuard.lock_test_baseline(tmp_path / "absent")


def test_detects_file_deletion(tmp_path: Path) -> None:
    _write_suite(tmp_path)
    baseline = ImmutableTestGuard.lock_test_baseline(tmp_path)
    (tmp_path / "test_alpha.py").unlink()
    with pytest.raises(RewardHackingDetectedError, match="deleted") as exc_info:
        ImmutableTestGuard.verify_test_integrity(tmp_path, baseline)
    assert "test_alpha.py" in str(exc_info.value)


def test_detects_file_modification(tmp_path: Path) -> None:
    _write_suite(tmp_path)
    baseline = ImmutableTestGuard.lock_test_baseline(tmp_path)
    with (tmp_path / "test_alpha.py").open("a") as handle:
        handle.write("# sneaky comment\n")
    with pytest.raises(RewardHackingDetectedError, match="modified") as exc_info:
        ImmutableTestGuard.verify_test_integrity(tmp_path, baseline)
    assert "test_alpha.py" in str(exc_info.value)


def test_detects_file_truncation(tmp_path: Path) -> None:
    _write_suite(tmp_path)
    baseline = ImmutableTestGuard.lock_test_baseline(tmp_path)
    (tmp_path / "nested" / "test_beta.py").write_text("")
    with pytest.raises(RewardHackingDetectedError, match="truncated"):
        ImmutableTestGuard.verify_test_integrity(tmp_path, baseline)


def test_detects_assertion_deletion(tmp_path: Path) -> None:
    original = _write_suite(tmp_path)
    baseline = ImmutableTestGuard.lock_test_baseline(tmp_path)
    tampered = ASSERT_RICH_TEST.replace("    assert 'a' in 'abc'\n", "")
    (tmp_path / "nested" / "test_beta.py").write_text(tampered)
    with pytest.raises(RewardHackingDetectedError):
        ImmutableTestGuard.verify_test_integrity(tmp_path, baseline)
    assert (
        ImmutableTestGuard.verify_no_assertion_tampering(
            original["nested/test_beta.py"], tampered
        )
        is False
    )


def test_new_files_are_out_of_scope(tmp_path: Path) -> None:
    _write_suite(tmp_path)
    baseline = ImmutableTestGuard.lock_test_baseline(tmp_path)
    (tmp_path / "test_brand_new.py").write_text(PLAIN_TEST)
    assert ImmutableTestGuard.verify_test_integrity(tmp_path, baseline) == (True, [])


def test_assertion_tampering_matrix() -> None:
    check = ImmutableTestGuard.verify_no_assertion_tampering
    assert check(PLAIN_TEST, PLAIN_TEST) is True
    assert check(PLAIN_TEST, PLAIN_TEST + "    assert True\n") is True
    assert check(ASSERT_RICH_TEST, PLAIN_TEST) is False
    assert (
        check(
            "import unittest\nself.assertEqual(1, 1)\nself.assertTrue(x)\n",
            "import unittest\nself.assertEqual(1, 1)\n",
        )
        is False
    )
    assert check(PLAIN_TEST, "def broken(:\n") is False
    assert check("def broken(:\n", PLAIN_TEST) is True


def test_clean_execution_exit_zero(tmp_path: Path) -> None:
    result = LocalSubprocessRunner().run_in_sandbox(
        [sys.executable, "-c", "print('sandbox-ok')"], cwd=tmp_path
    )
    assert isinstance(result, SandboxExecutionResult)
    assert result.exit_code == 0
    assert "sandbox-ok" in result.stdout
    assert result.timed_out is False
    assert result.duration_ms >= 0


def test_failing_command_reports_exit_code(tmp_path: Path) -> None:
    result = LocalSubprocessRunner().run_in_sandbox(
        [sys.executable, "-c", "raise SystemExit(3)"], cwd=tmp_path
    )
    assert result.exit_code == 3
    assert result.timed_out is False


def test_timeout_handling(tmp_path: Path) -> None:
    result = LocalSubprocessRunner().run_in_sandbox(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        cwd=tmp_path,
        timeout_sec=1,
    )
    assert result.timed_out is True
    assert result.duration_ms >= 1000
    assert result.duration_ms < 15000


def test_air_gapped_network_isolation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HTTP_PROXY", "http://proxy:8080")
    monkeypatch.setenv("https_proxy", "http://proxy:8080")
    monkeypatch.setenv("CUSTOM_ENDPOINT", "https://internal:9")
    monkeypatch.setenv("SERVICE_BASE_URL", "https://internal:9")
    monkeypatch.setenv("HACKPROOF_MARKER", "keep-me")
    probe = (
        "import json, os; print(json.dumps({k: v for k, v in os.environ.items() "
        "if 'PROXY' in k.upper() or k in "
        "('CUSTOM_ENDPOINT', 'SERVICE_BASE_URL', 'HACKPROOF_MARKER', 'HF_HUB_OFFLINE')}))"
    )
    scrubbed = LocalSubprocessRunner().run_in_sandbox(
        [sys.executable, "-c", probe], cwd=tmp_path, network_disabled=True
    )
    assert scrubbed.exit_code == 0
    child_env = json.loads(scrubbed.stdout)
    assert "HTTP_PROXY" not in child_env
    assert "https_proxy" not in child_env
    assert "CUSTOM_ENDPOINT" not in child_env
    assert "SERVICE_BASE_URL" not in child_env
    assert child_env["HACKPROOF_MARKER"] == "keep-me"
    assert child_env["HF_HUB_OFFLINE"] == "1"
    passthrough = LocalSubprocessRunner().run_in_sandbox(
        [sys.executable, "-c", probe], cwd=tmp_path, network_disabled=False
    )
    assert json.loads(passthrough.stdout)["HTTP_PROXY"] == "http://proxy:8080"


def test_runner_validates_inputs(tmp_path: Path) -> None:
    runner = LocalSubprocessRunner()
    with pytest.raises(ValueError, match="non-empty"):
        runner.run_in_sandbox([], cwd=tmp_path)
    with pytest.raises(ValueError, match="positive"):
        runner.run_in_sandbox([sys.executable], cwd=tmp_path, timeout_sec=0)
    with pytest.raises(FileNotFoundError):
        runner.run_in_sandbox([sys.executable], cwd=tmp_path / "absent")
