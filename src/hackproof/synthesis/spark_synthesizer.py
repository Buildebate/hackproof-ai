"""System One regression test synthesizer (Step 2.1, Muse Spark).

Generates an isolated reproduction test through an injected client (mock in
unit tests, ``MetaClient("muse-spark-1.3")`` live) and enforces the Red
Baseline Invariant: the test MUST fail with an explicit assertion failure
on the unmodified base commit (parsed from JUnit XML), otherwise the
synthesis is rejected as a false positive or a broken test.
"""

from __future__ import annotations

import hashlib
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, Sequence

from hackproof.sandbox.runner import LocalSubprocessRunner, SandboxExecutionResult

SUPPORTED_LANGUAGES = ("python", "typescript")

__all__ = [
    "RegressionTestSynthesizer",
    "RedBaselineReport",
    "SUPPORTED_LANGUAGES",
]


class _TestGenerator(Protocol):
    """Frozen client surface: MockMetaClient and MetaClient both satisfy it."""

    def generate_regression_test(
        self, issue_description: str, code_context: str
    ) -> str: ...


class _Runner(Protocol):
    """Sandbox runner surface (local default, remote when --runner=oci)."""

    def run_in_sandbox(
        self,
        cmd: list[str],
        cwd: Path | str,
        timeout_sec: int = ...,
        network_disabled: bool = ...,
    ) -> SandboxExecutionResult: ...


@dataclass(frozen=True)
class RedBaselineReport:
    """Outcome of Red Baseline verification."""

    accepted: bool
    reason: str
    failures: int
    errors: int
    exit_code: int
    junit_path: str


def _digest(*parts: str) -> str:
    """Stable 12-hex fingerprint for file names."""
    return hashlib.sha256("\x00".join(parts).encode("utf-8")).hexdigest()[:12]


def _count_junit(xml_path: Path) -> tuple[int, int]:
    """Count <failure> and <error> nodes in a JUnit XML file.

    Raises:
        FileNotFoundError: Missing report.
        ValueError: Unparseable report.
    """
    if not xml_path.is_file():
        raise FileNotFoundError(f"JUnit report not produced: {xml_path}")
    try:
        root = ET.parse(xml_path).getroot()
    except ET.ParseError as exc:
        raise ValueError(f"JUnit report unparseable: {xml_path}") from exc
    failures = sum(1 for _ in root.iter("failure"))
    errors = sum(1 for _ in root.iter("error"))
    return failures, errors


class RegressionTestSynthesizer:
    """Spark-driven repro synthesis guarded by the Red Baseline Invariant."""

    def __init__(
        self,
        client: _TestGenerator,
        runner: _Runner | None = None,
    ) -> None:
        """Bind the generating client and the (injectable) sandbox runner."""
        self._client = client
        self._runner: _Runner = runner if runner is not None else LocalSubprocessRunner()

    def synthesize_test(
        self,
        issue_description: str,
        code_snippets: Sequence[str] | str,
        repo_structure: str,
        language: str,
        output_dir: Path | str,
        feedback: str | None = None,
    ) -> Path:
        """Generate the repro file and return its path.

        Args:
            issue_description: Bug report / failing behavior text.
            code_snippets: Localized code (Contriever/Faiss hits).
            repo_structure: Compact repo layout sketch for grounding.
            language: ``"python"`` or ``"typescript"``.
            output_dir: Directory receiving the repro file (created).
            feedback: Judge feedback from a rejected attempt, appended
                to the issue text (Step 4.3b.6/R10 resynthesis). The
                filename tag covers it, so retries never overwrite.

        Raises:
            ValueError: Unsupported language.
        """
        if language not in SUPPORTED_LANGUAGES:
            raise ValueError(
                f"unsupported language {language!r}: expected one of "
                f"{SUPPORTED_LANGUAGES}"
            )
        snippets = (
            code_snippets
            if isinstance(code_snippets, str)
            else "\n\n".join(code_snippets)
        )
        context = f"REPO STRUCTURE:\n{repo_structure}\n\nSNIPPETS:\n{snippets}"
        effective_issue = issue_description
        if feedback:
            effective_issue = (
                f"{issue_description}\n\nPREVIOUS ATTEMPT REJECTED BY "
                f"THE JUDGE — FIX IT:\n{feedback}"
            )
        content = self._client.generate_regression_test(
            effective_issue, context
        )
        tag = _digest(effective_issue, context, language)
        out_dir = Path(output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        filename = (
            f"test_repro_{tag}.py" if language == "python" else f"repro-{tag}.test.ts"
        )
        test_path = out_dir / filename
        test_path.write_text(content, encoding="utf-8")
        return test_path

    def _test_command(
        self, language: str, test_path: Path, junit_path: Path
    ) -> list[str]:
        """Build the per-language JUnit-producing test argv."""
        if language == "python":
            return ["pytest", "--junitxml", str(junit_path), str(test_path)]
        return [
            "vitest",
            "run",
            "--reporter=junit",
            f"--outputFile={junit_path}",
            str(test_path),
        ]

    def verify_red_baseline(
        self,
        test_path: Path | str,
        cwd: Path | str,
        language: str,
        timeout_sec: int = 120,
        network_disabled: bool = True,
    ) -> RedBaselineReport:
        """Run the repro test against base state and judge Red vs reject.

        The caller provides ``cwd`` at the UNMODIFIED base commit (worktree
        discipline lives with the orchestrator). Acceptance rule: exit code
        is non-zero AND JUnit shows >= 1 ``<failure>`` AND zero ``<error>``.
        Anything else (pass, syntax/import/compile breakage, missing or
        unparseable JUnit) is rejected with an explicit reason.

        Raises:
            ValueError: Unsupported language.
        """
        if language not in SUPPORTED_LANGUAGES:
            raise ValueError(
                f"unsupported language {language!r}: expected one of "
                f"{SUPPORTED_LANGUAGES}"
            )
        test_file = Path(test_path)
        junit_path = test_file.parent / f"{test_file.stem}-red.xml"
        result = self._runner.run_in_sandbox(
            self._test_command(language, test_file, junit_path),
            cwd=cwd,
            timeout_sec=timeout_sec,
            network_disabled=network_disabled,
        )
        try:
            failures, errors = _count_junit(junit_path)
        except FileNotFoundError:
            return RedBaselineReport(
                accepted=False,
                reason="junit-missing: runner produced no JUnit report",
                failures=0,
                errors=0,
                exit_code=result.exit_code,
                junit_path=str(junit_path),
            )
        except ValueError:
            return RedBaselineReport(
                accepted=False,
                reason="junit-unparseable: report is not valid XML",
                failures=0,
                errors=0,
                exit_code=result.exit_code,
                junit_path=str(junit_path),
            )
        if errors > 0:
            return RedBaselineReport(
                accepted=False,
                reason="broken-test-errors: JUnit shows errors, not assertion failures",
                failures=failures,
                errors=errors,
                exit_code=result.exit_code,
                junit_path=str(junit_path),
            )
        if result.exit_code == 0 or failures == 0:
            return RedBaselineReport(
                accepted=False,
                reason="false-positive-pass: test did not fail on base commit",
                failures=failures,
                errors=errors,
                exit_code=result.exit_code,
                junit_path=str(junit_path),
            )
        return RedBaselineReport(
            accepted=True,
            reason="red-accepted: explicit assertion failure on base commit",
            failures=failures,
            errors=errors,
            exit_code=result.exit_code,
            junit_path=str(junit_path),
        )
