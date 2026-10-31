"""Step 2.1 (Part B): synthesizer wiring + Red Baseline matrix (py + ts)."""

from pathlib import Path

import pytest

from hackproof.sandbox.runner import SandboxExecutionResult
from hackproof.synthesis import RedBaselineReport, RegressionTestSynthesizer
from mocks.mock_meta_client import MockMetaClient

PYTEST_RED_XML = """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest" errors="0" failures="1" skipped="0" tests="1">
<testcase classname="test_repro" name="test_repro_x" time="0.01">
<failure message="AssertionError: boom">assert False</failure>
</testcase></testsuite></testsuites>
"""

VITEST_RED_XML = """<?xml version="1.0" encoding="UTF-8"?>
<testsuites name="vitest" tests="1" failures="1" errors="0">
<testsuite name="repro.test.ts" tests="1" failures="1" errors="0">
<testcase name="repro works" classname="repro">
<failure message="AssertionError: expected true to be false">stack</failure>
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


class _FakeRunner:
    """Canned runner writing chosen JUnit to the requested report path."""

    def __init__(self, exit_code: int, junit_xml: str | None) -> None:
        self._exit_code = exit_code
        self._junit_xml = junit_xml
        self.calls: list[tuple[list[str], str]] = []

    def run_in_sandbox(self, cmd, cwd, timeout_sec=60, network_disabled=True):
        self.calls.append((list(cmd), str(cwd)))
        xml_path = self._junit_path(cmd)
        if self._junit_xml is not None and xml_path is not None:
            Path(xml_path).write_text(self._junit_xml, encoding="utf-8")
        return SandboxExecutionResult(
            exit_code=self._exit_code,
            stdout="",
            stderr="",
            duration_ms=3,
            timed_out=False,
        )

    @staticmethod
    def _junit_path(cmd: list[str]) -> str | None:
        for i, part in enumerate(cmd):
            if part == "--junitxml" and i + 1 < len(cmd):
                return cmd[i + 1]
            if part.startswith("--outputFile="):
                return part.split("=", 1)[1]
        return None


def _synth(runner: _FakeRunner) -> RegressionTestSynthesizer:
    return RegressionTestSynthesizer(client=MockMetaClient(), runner=runner)


def test_synthesize_python_and_typescript_files(tmp_path: Path) -> None:
    synth = _synth(_FakeRunner(1, PYTEST_RED_XML))
    py = synth.synthesize_test("issue", ["snippet()"], "repo/", "python", tmp_path)
    assert py.name.startswith("test_repro_") and py.suffix == ".py"
    assert "def test_repro_" in py.read_text()
    ts = synth.synthesize_test("issue", ["snippet()"], "repo/", "typescript", tmp_path)
    assert ts.name.startswith("repro-") and ts.name.endswith(".test.ts")
    assert py.name != ts.name


def test_client_wiring_passes_snippets(tmp_path: Path) -> None:
    client = MockMetaClient()
    synth = RegressionTestSynthesizer(client=client, runner=_FakeRunner(1, PYTEST_RED_XML))
    path = synth.synthesize_test("ISS", ["a()", "b()"], "STRUCT", "python", tmp_path)
    expected_context = "REPO STRUCTURE:\nSTRUCT\n\nSNIPPETS:\na()\n\nb()"
    assert path.read_text() == client.generate_regression_test("ISS", expected_context)


def test_red_accepted_python(tmp_path: Path) -> None:
    runner = _FakeRunner(1, PYTEST_RED_XML)
    report = _synth(runner).verify_red_baseline(tmp_path / "t.py", tmp_path, "python")
    assert isinstance(report, RedBaselineReport)
    assert report.accepted is True
    assert report.reason.startswith("red-accepted")
    assert (report.failures, report.errors, report.exit_code) == (1, 0, 1)
    argv = runner.calls[0][0]
    assert argv[0] == "pytest" and "--junitxml" in argv
    assert Path(report.junit_path).is_file()


def test_red_accepted_typescript(tmp_path: Path) -> None:
    runner = _FakeRunner(1, VITEST_RED_XML)
    synth = _synth(runner)
    test = synth.synthesize_test("i", "s", "r", "typescript", tmp_path)
    report = synth.verify_red_baseline(test, tmp_path, "typescript")
    assert report.accepted is True
    assert (report.failures, report.errors) == (1, 0)
    argv = runner.calls[0][0]
    assert argv[:2] == ["vitest", "run"]
    assert "--reporter=junit" in argv
    assert any(p.startswith("--outputFile=") for p in argv)
    assert str(test) in argv


def test_reject_pass_on_base(tmp_path: Path) -> None:
    runner = _FakeRunner(0, GREEN_XML)
    report = _synth(runner).verify_red_baseline(tmp_path / "t.py", tmp_path, "python")
    assert report.accepted is False
    assert report.reason.startswith("false-positive-pass")
    assert report.exit_code == 0


def test_reject_error_xml_and_exit_mismatch(tmp_path: Path) -> None:
    runner = _FakeRunner(2, ERROR_XML)
    report = _synth(runner).verify_red_baseline(tmp_path / "t.py", tmp_path, "python")
    assert report.accepted is False
    assert report.reason.startswith("broken-test-errors")
    assert report.errors == 1
    runner2 = _FakeRunner(0, PYTEST_RED_XML)  # exit 0 with failures = suspicious
    report2 = _synth(runner2).verify_red_baseline(tmp_path / "t.py", tmp_path, "python")
    assert report2.accepted is False


def test_reject_missing_and_unparseable_junit(tmp_path: Path) -> None:
    runner = _FakeRunner(1, None)
    report = _synth(runner).verify_red_baseline(tmp_path / "t.py", tmp_path, "python")
    assert report.accepted is False
    assert report.reason.startswith("junit-missing")
    runner2 = _FakeRunner(1, "not <xml")
    report2 = _synth(runner2).verify_red_baseline(tmp_path / "t2.py", tmp_path, "python")
    assert report2.accepted is False
    assert report2.reason.startswith("junit-unparseable")


def test_invalid_language_rejected(tmp_path: Path) -> None:
    synth = _synth(_FakeRunner(1, PYTEST_RED_XML))
    with pytest.raises(ValueError, match="unsupported language"):
        synth.synthesize_test("i", "s", "r", "go", tmp_path)
    with pytest.raises(ValueError, match="unsupported language"):
        synth.verify_red_baseline(tmp_path / "t.go", tmp_path, "go")


def test_default_runner_is_local_subprocess() -> None:
    from hackproof.sandbox.runner import LocalSubprocessRunner

    synth = RegressionTestSynthesizer(client=MockMetaClient())
    assert isinstance(synth._runner, LocalSubprocessRunner)


class _CaptureClient:
    """Client recording the issue text it was asked to address."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def generate_regression_test(
        self, issue_description: str, code_context: str
    ) -> str:
        self.calls.append((issue_description, code_context))
        return "def test_repro():\n    assert False, 'boom'\n"


def test_synthesize_feedback_appends_judge_reason(tmp_path: Path) -> None:
    """Step 4.3b.6 (R10): resynthesis carries the judge's verdict."""
    client = _CaptureClient()
    synth = RegressionTestSynthesizer(client=client)
    first = synth.synthesize_test("bug", "code", "repo/", "python", tmp_path)
    assert client.calls[0][0] == "bug"
    reason = "red run exited 0 (test passed on unpatched base)"
    second = synth.synthesize_test(
        "bug", "code", "repo/", "python", tmp_path, feedback=reason
    )
    assert "PREVIOUS ATTEMPT REJECTED" in client.calls[1][0]
    assert reason in client.calls[1][0]
    assert client.calls[1][0].startswith("bug\n\nPREVIOUS")
    # Retries never overwrite: the tag covers the feedback.
    assert first.name != second.name
    assert first.is_file() and second.is_file()
