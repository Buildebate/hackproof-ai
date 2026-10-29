"""Step 3.1: canonical CLI (verify / test-guard / --runner) — offline suite.

Covers the argparse matrix, test-guard clean/tamper paths on fixture git
repos, verify wiring with stubbed network/model/sandbox boundaries, the
FinOps ledger writer, and typer-backend parity (when typer is installed).
No test touches the network, the Meta API, or OCI: every live boundary
is injected (``issue_fetcher`` / ``client_factory`` / ``runner_override``).
"""

from __future__ import annotations

import json
import subprocess
import sys
import urllib.error
from pathlib import Path

import pytest

from hackproof import cli
from hackproof.sandbox.runner import SandboxExecutionResult

ISSUE_URL = "https://github.com/octocat/hello-world/issues/42"

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

FOO_PY = '''"""Tiny fixture module with an off-by-one bug."""


def total(prices):
    wrong = 0
    for price in prices:
        wrong += price
    return wrong + 1
'''

FOO_SEARCH = "    return wrong + 1"
FOO_REPLACE = "    return wrong"

PATCH_TEXT = (
    "<<<<<<< SEARCH\n" + FOO_SEARCH + "\n=======\n" + FOO_REPLACE + "\n>>>>>>> REPLACE\n"
)

REPRO_PY = '''"""HackProof regression repro (stubbed synthesis)."""


def test_total_sums_without_surcharge():
    from foo import total

    assert total([1, 2, 3]) == 6
'''

TEST_FOO_PY = '''"""Pre-existing guard-protected tests."""


def test_total_empty():
    from foo import total

    assert total([]) == 1
'''


# ---------------------------------------------------------------------------
# Fixtures & stubs
# ---------------------------------------------------------------------------


def _git(cwd: Path, *argv: str) -> None:
    subprocess.run(
        ["git", *argv],
        cwd=str(cwd),
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


@pytest.fixture()
def fixture_repo(tmp_path: Path) -> Path:
    """Real git repo: buggy foo.py + one committed pre-existing test."""
    repo = tmp_path / "repo"
    (repo / "tests").mkdir(parents=True)
    (repo / "foo.py").write_text(FOO_PY, encoding="utf-8")
    (repo / "tests" / "test_foo.py").write_text(TEST_FOO_PY, encoding="utf-8")
    _git(repo, "init")
    _git(repo, "config", "user.email", "hackproof@test.local")
    _git(repo, "config", "user.name", "HackProof Test")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "fixture base")
    return repo


class _StubClient:
    """Canned model client: repro test + SEARCH/REPLACE patch."""

    def __init__(self, patch_text: str = PATCH_TEXT) -> None:
        self._patch_text = patch_text
        self.models: list[str] = []

    def generate_regression_test(
        self, issue_description: str, code_context: str
    ) -> str:
        assert issue_description and code_context
        return REPRO_PY

    def debate_round(self, role: str, prompt: str, history: list) -> str:
        assert role and prompt and isinstance(history, list)
        if role == "ArchitecturalBuilder":
            return self._patch_text
        return "APPROVE: the minimal fix is correct and complete."

    def synthesize_patch(self, debate_consensus: str) -> str:
        assert debate_consensus
        return self._patch_text


class _StubRunner:
    """Canned sandbox runner: scripted (exit_code, junit) per call."""

    def __init__(self, script: list[tuple[int, str]]) -> None:
        self._script = list(script)
        self.calls: list[list[str]] = []

    def run_in_sandbox(self, cmd, cwd, timeout_sec=60, network_disabled=True):
        self.calls.append(list(cmd))
        assert network_disabled is True
        exit_code, junit_xml = self._script[min(len(self.calls) - 1, len(self._script) - 1)]
        junit_path = self._junit_path(cmd)
        if junit_path is not None:
            junit_file = Path(junit_path)
            if not junit_file.is_absolute():
                junit_file = Path(cwd) / junit_file
            junit_file.write_text(junit_xml, encoding="utf-8")
        return SandboxExecutionResult(
            exit_code=exit_code,
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


def _factory_for(stub: _StubClient):
    def factory(model: str) -> _StubClient:
        stub.models.append(model)
        return stub

    return factory


def _issue_fetcher(title: str = "off-by-one in total"):
    def fetch(url: str) -> tuple[str, str]:
        assert url == ISSUE_URL
        return title, "total([1, 2, 3]) returns 7 instead of 6"

    return fetch


# ---------------------------------------------------------------------------
# parse_issue_url
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url,expected",
    [
        (ISSUE_URL, ("octocat", "hello-world", 42)),
        ("https://github.com/o/r/pull/7", ("o", "r", 7)),
        ("https://github.com/o/r/issues/7/", ("o", "r", 7)),
    ],
)
def test_parse_issue_url_valid(url: str, expected: tuple) -> None:
    assert cli.parse_issue_url(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "http://github.com/o/r/issues/1",
        "https://github.com/o/r",
        "https://github.com/o/r/issues",
        "https://github.com/o/r/issues/x",
        "https://gitlab.com/o/r/issues/1",
        "not-a-url",
        "",
    ],
)
def test_parse_issue_url_invalid(url: str) -> None:
    with pytest.raises(ValueError, match="not a GitHub issue URL"):
        cli.parse_issue_url(url)


# ---------------------------------------------------------------------------
# argparse matrix
# ---------------------------------------------------------------------------


def test_argparse_verify_requires_issue(capsys) -> None:
    with pytest.raises(SystemExit) as exc_info:
        cli._argparse_main(["verify"])
    assert exc_info.value.code == 2


def test_argparse_verify_rejects_bad_runner(capsys) -> None:
    with pytest.raises(SystemExit) as exc_info:
        cli._argparse_main(["verify", "--issue", ISSUE_URL, "--runner", "bogus"])
    assert exc_info.value.code == 2


def test_argparse_verify_defaults(monkeypatch) -> None:
    seen: dict = {}

    def fake_verify(**kwargs) -> int:
        seen.update(kwargs)
        return 0

    monkeypatch.setattr(cli, "cmd_verify", fake_verify)
    assert cli._argparse_main(["verify", "--issue", ISSUE_URL]) == 0
    assert seen == {
        "issue": ISSUE_URL,
        "repo": ".",
        "runner": "local",
        "language": "python",
        "ledger": "finops_ledger.json",
    }


def test_argparse_verify_full_options(monkeypatch, tmp_path: Path) -> None:
    seen: dict = {}

    def fake_verify(**kwargs) -> int:
        seen.update(kwargs)
        return 0

    monkeypatch.setattr(cli, "cmd_verify", fake_verify)
    ledger = str(tmp_path / "ledger.json")
    code = cli._argparse_main(
        [
            "verify",
            "--issue",
            ISSUE_URL,
            "--repo",
            str(tmp_path),
            "--runner",
            "oci",
            "--language",
            "typescript",
            "--ledger",
            ledger,
        ]
    )
    assert code == 0
    assert seen == {
        "issue": ISSUE_URL,
        "repo": str(tmp_path),
        "runner": "oci",
        "language": "typescript",
        "ledger": ledger,
    }


def test_argparse_verify_propagates_exit_code(monkeypatch) -> None:
    monkeypatch.setattr(cli, "cmd_verify", lambda **kwargs: 3)
    assert cli._argparse_main(["verify", "--issue", ISSUE_URL]) == 3


def test_argparse_test_guard_requires_baseline(capsys) -> None:
    with pytest.raises(SystemExit) as exc_info:
        cli._argparse_main(["test-guard"])
    assert exc_info.value.code == 2


def test_argparse_test_guard_options(monkeypatch, tmp_path: Path) -> None:
    seen: dict = {}

    def fake_guard(**kwargs) -> int:
        seen.update(kwargs)
        return 0

    monkeypatch.setattr(cli, "cmd_test_guard", fake_guard)
    code = cli._argparse_main(
        ["test-guard", "--baseline", "HEAD~1", "--repo", str(tmp_path),
         "--tests-dir", "tests"]
    )
    assert code == 0
    assert seen == {
        "repo": str(tmp_path),
        "baseline": "HEAD~1",
        "tests_dir": "tests",
    }


def test_argparse_help_snapshot(capsys) -> None:
    with pytest.raises(SystemExit) as exc_info:
        cli._argparse_main(["--help"])
    assert exc_info.value.code == 0
    out = capsys.readouterr().out
    assert "verify" in out
    assert "test-guard" in out
    assert "Exit codes: 0 verified-clean" in out
    assert "1 pipeline failure" in out
    assert "2 usage error" in out
    assert "3 guard/tamper breach" in out


def test_argparse_verify_help_documents_options(capsys) -> None:
    with pytest.raises(SystemExit) as exc_info:
        cli._argparse_main(["verify", "--help"])
    assert exc_info.value.code == 0
    out = capsys.readouterr().out
    assert "--issue" in out
    assert "--runner" in out
    assert "local" in out and "oci" in out
    assert "--repo" in out
    assert "--language" in out


def test_argparse_test_guard_help_documents_baseline(capsys) -> None:
    with pytest.raises(SystemExit) as exc_info:
        cli._argparse_main(["test-guard", "--help"])
    assert exc_info.value.code == 0
    out = capsys.readouterr().out
    assert "--baseline" in out
    assert "--repo" in out


def test_main_dispatches_verify_through_argparse(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli, "_TYPER_AVAILABLE", False)
    monkeypatch.setattr(cli, "cmd_verify", lambda **kwargs: 0)
    monkeypatch.setattr(
        sys, "argv", ["hackproof", "verify", "--issue", ISSUE_URL]
    )
    with pytest.raises(SystemExit) as exc_info:
        cli.main()
    assert exc_info.value.code == 0


# ---------------------------------------------------------------------------
# cmd_test_guard on fixture repos
# ---------------------------------------------------------------------------


def test_guard_clean(fixture_repo: Path, capsys) -> None:
    code = cli.cmd_test_guard(repo=str(fixture_repo), baseline="HEAD")
    assert code == cli.EXIT_OK == 0
    assert "clean" in capsys.readouterr().out


def test_guard_modified(fixture_repo: Path, capsys) -> None:
    (fixture_repo / "tests" / "test_foo.py").write_text(
        TEST_FOO_PY + "\n# touched\n", encoding="utf-8"
    )
    code = cli.cmd_test_guard(repo=str(fixture_repo), baseline="HEAD")
    assert code == cli.EXIT_TAMPER == 3
    out = capsys.readouterr().out
    assert "TAMPER: modified: test_foo.py" in out


def test_guard_deleted(fixture_repo: Path, capsys) -> None:
    (fixture_repo / "tests" / "test_foo.py").unlink()
    code = cli.cmd_test_guard(repo=str(fixture_repo), baseline="HEAD")
    assert code == 3
    assert "TAMPER: deleted: test_foo.py" in capsys.readouterr().out


def test_guard_truncated(fixture_repo: Path, capsys) -> None:
    (fixture_repo / "tests" / "test_foo.py").write_text("", encoding="utf-8")
    code = cli.cmd_test_guard(repo=str(fixture_repo), baseline="HEAD")
    assert code == 3
    assert "TAMPER: truncated: test_foo.py" in capsys.readouterr().out


def test_guard_assertion_deletion_flagged(fixture_repo: Path, capsys) -> None:
    (fixture_repo / "tests" / "test_foo.py").write_text(
        '"""Pre-existing guard-protected tests."""\n\n\n'
        "def test_total_empty():\n"
        "    pass\n",
        encoding="utf-8",
    )
    code = cli.cmd_test_guard(repo=str(fixture_repo), baseline="HEAD")
    assert code == 3
    out = capsys.readouterr().out
    assert "TAMPER: modified: test_foo.py" in out
    assert "TAMPER: assertion-tampering: test_foo.py" in out


def test_guard_new_file_is_out_of_scope(fixture_repo: Path, capsys) -> None:
    (fixture_repo / "tests" / "test_new.py").write_text(
        "def test_new():\n    assert True\n", encoding="utf-8"
    )
    assert cli.cmd_test_guard(repo=str(fixture_repo), baseline="HEAD") == 0


def test_guard_unknown_ref(fixture_repo: Path, capsys) -> None:
    code = cli.cmd_test_guard(repo=str(fixture_repo), baseline="no-such-ref")
    assert code == cli.EXIT_USAGE == 2
    assert "unknown git ref" in capsys.readouterr().err


def test_guard_bad_repo(tmp_path: Path, capsys) -> None:
    code = cli.cmd_test_guard(repo=str(tmp_path / "missing"), baseline="HEAD")
    assert code == 2


def test_guard_not_a_git_repo(tmp_path: Path, capsys) -> None:
    code = cli.cmd_test_guard(repo=str(tmp_path), baseline="HEAD")
    assert code == 2
    assert "not a git repository" in capsys.readouterr().err


def test_guard_missing_tests_dir(fixture_repo: Path, capsys) -> None:
    code = cli.cmd_test_guard(
        repo=str(fixture_repo), baseline="HEAD", tests_dir="no-tests"
    )
    assert code == 2
    assert "tests directory not found" in capsys.readouterr().err


def test_guard_baseline_keys_match_guard_format(fixture_repo: Path) -> None:
    baseline = cli.read_tests_baseline_at_ref(fixture_repo, "HEAD")
    assert set(baseline) == {"test_foo.py"}
    assert len(baseline["test_foo.py"]) == 64


# ---------------------------------------------------------------------------
# cmd_verify: usage errors and failure paths
# ---------------------------------------------------------------------------


def test_verify_bad_issue_url(tmp_path: Path, capsys) -> None:
    code = cli.cmd_verify(issue="not-a-url", repo=str(tmp_path))
    assert code == 2
    assert "not a GitHub issue URL" in capsys.readouterr().err


def test_verify_bad_runner(fixture_repo: Path, capsys) -> None:
    code = cli.cmd_verify(
        issue=ISSUE_URL, repo=str(fixture_repo), runner="bogus"
    )
    assert code == 2
    assert "invalid --runner" in capsys.readouterr().err


def test_verify_bad_language(fixture_repo: Path, capsys) -> None:
    code = cli.cmd_verify(
        issue=ISSUE_URL, repo=str(fixture_repo), language="rust"
    )
    assert code == 2
    assert "invalid --language" in capsys.readouterr().err


def test_verify_bad_repo(tmp_path: Path, capsys) -> None:
    code = cli.cmd_verify(issue=ISSUE_URL, repo=str(tmp_path / "missing"))
    assert code == 2


def test_verify_issue_fetch_failure(
    fixture_repo: Path, tmp_path: Path, capsys
) -> None:
    def failing_fetcher(url: str):
        raise cli.IssueFetchError("cannot reach GitHub API")

    code = cli.cmd_verify(
        issue=ISSUE_URL,
        repo=str(fixture_repo),
        ledger=str(tmp_path / "ledger.json"),
        issue_fetcher=failing_fetcher,
        client_factory=_factory_for(_StubClient()),
        runner_override=_StubRunner([(1, RED_XML)]),
    )
    assert code == cli.EXIT_PIPELINE_FAILURE == 1
    assert "cannot reach GitHub API" in capsys.readouterr().err


def test_verify_missing_api_key_hint(
    fixture_repo: Path, tmp_path: Path, capsys
) -> None:
    def no_key_factory(model: str):
        raise RuntimeError("META_API_KEY is not set")

    code = cli.cmd_verify(
        issue=ISSUE_URL,
        repo=str(fixture_repo),
        ledger=str(tmp_path / "ledger.json"),
        issue_fetcher=_issue_fetcher(),
        client_factory=no_key_factory,
        runner_override=_StubRunner([(1, RED_XML)]),
    )
    assert code == 1
    err = capsys.readouterr().err
    assert "META_API_KEY" in err
    assert "set META_API_KEY and retry" in err


def test_verify_oci_runner_without_credentials(
    fixture_repo: Path, tmp_path: Path, capsys, monkeypatch
) -> None:
    monkeypatch.delenv("OCI_SSH_HOST", raising=False)
    monkeypatch.delenv("OCI_SSH_USER", raising=False)
    monkeypatch.delenv("OCI_SSH_KEY", raising=False)
    code = cli.cmd_verify(
        issue=ISSUE_URL,
        repo=str(fixture_repo),
        runner="oci",
        ledger=str(tmp_path / "ledger.json"),
        issue_fetcher=_issue_fetcher(),
        client_factory=_factory_for(_StubClient()),
    )
    assert code == 1
    assert "OCI_SSH_HOST" in capsys.readouterr().err


def test_verify_red_reject_exits_pipeline_failure(
    fixture_repo: Path, tmp_path: Path, capsys
) -> None:
    code = cli.cmd_verify(
        issue=ISSUE_URL,
        repo=str(fixture_repo),
        ledger=str(tmp_path / "ledger.json"),
        issue_fetcher=_issue_fetcher(),
        client_factory=_factory_for(_StubClient()),
        runner_override=_StubRunner([(0, GREEN_XML)]),
    )
    assert code == 1
    assert "red baseline rejected" in capsys.readouterr().err


def test_verify_no_blocks_exits_pipeline_failure(
    fixture_repo: Path, tmp_path: Path, capsys
) -> None:
    code = cli.cmd_verify(
        issue=ISSUE_URL,
        repo=str(fixture_repo),
        ledger=str(tmp_path / "ledger.json"),
        issue_fetcher=_issue_fetcher(),
        client_factory=_factory_for(_StubClient(patch_text="no blocks here")),
        runner_override=_StubRunner([(1, RED_XML), (0, GREEN_XML)]),
    )
    assert code == 1
    assert "no complete SEARCH/REPLACE blocks" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# cmd_verify: happy path (real git worktree + real debate/guard/provenance)
# ---------------------------------------------------------------------------


def test_verify_happy_path_red_to_green(
    fixture_repo: Path, tmp_path: Path, capsys
) -> None:
    pytest.importorskip("numpy")
    ledger = tmp_path / "finops.json"
    stub = _StubClient()
    runner = _StubRunner([(1, RED_XML), (0, GREEN_XML)])
    code = cli.cmd_verify(
        issue=ISSUE_URL,
        repo=str(fixture_repo),
        ledger=str(ledger),
        issue_fetcher=_issue_fetcher(),
        client_factory=_factory_for(stub),
        runner_override=runner,
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "octocat/hello-world#42" in out
    assert "localized" in out
    assert "guard locked" in out
    assert "red baseline accepted" in out
    assert "debate converged" in out
    assert "patch applied" in out
    assert "green verified" in out
    assert "TGRES receipt" in out
    # Model wiring: Spark synthesizes, Wonder debates.
    assert stub.models[0] == "muse-spark-1.3"
    assert "muse-wonder-1.0" in stub.models
    # Sandbox ran red then green, both air-gapped through the same runner.
    assert len(runner.calls) == 2
    assert any("--junitxml" in call for call in runner.calls)
    # FinOps ledger entry is honest: tokens counted, cost unpriced.
    payload = json.loads(ledger.read_text(encoding="utf-8"))
    assert payload["budget_usd"] == 150.0
    assert payload["total_spent_usd"] == 0.0
    assert len(payload["entries"]) == 1
    entry = payload["entries"][0]
    assert entry["issue"] == ISSUE_URL
    assert entry["priced"] is False
    assert entry["est_cost_usd"] == 0.0
    assert entry["rounds_used"] >= 1
    assert "ts" in entry and "per_call" in entry
    # The source repo itself is untouched: patching stayed in the worktree.
    assert (fixture_repo / "foo.py").read_text(encoding="utf-8") == FOO_PY
    assert "hackproof_repro" not in [
        p.name for p in (fixture_repo / "tests").iterdir()
    ]


def test_verify_guard_breach_after_patch(
    fixture_repo: Path, tmp_path: Path, capsys, monkeypatch
) -> None:
    pytest.importorskip("numpy")
    from hackproof.sandbox import guards as guards_module
    from hackproof.sandbox.guards import RewardHackingDetectedError

    def breaching_verify(test_dir, baseline):
        # The pipeline calls verify_test_integrity exactly once (post-patch).
        raise RewardHackingDetectedError(["modified: test_foo.py"])

    monkeypatch.setattr(
        guards_module.ImmutableTestGuard, "verify_test_integrity", breaching_verify
    )
    code = cli.cmd_verify(
        issue=ISSUE_URL,
        repo=str(fixture_repo),
        ledger=str(tmp_path / "finops.json"),
        issue_fetcher=_issue_fetcher(),
        client_factory=_factory_for(_StubClient()),
        runner_override=_StubRunner([(1, RED_XML), (0, GREEN_XML)]),
    )
    assert code == 3
    assert "modified: test_foo.py" in capsys.readouterr().out


def test_stub_runner_resolves_relative_junit_against_cwd(
    tmp_path: Path,
) -> None:
    """Regression: relative JUnit reports land in the sandbox cwd, never repo root."""
    runner = _StubRunner([(0, GREEN_XML)])
    runner.run_in_sandbox(
        ["pytest", "-q", "--junitxml", "rel-report.xml"], cwd=tmp_path
    )
    assert (tmp_path / "rel-report.xml").read_text(encoding="utf-8") == GREEN_XML


# ---------------------------------------------------------------------------
# resolved_repo
# ---------------------------------------------------------------------------


def test_resolved_repo_local_git(fixture_repo: Path) -> None:
    with cli.resolved_repo(str(fixture_repo)) as path:
        assert path == fixture_repo.resolve()


def test_resolved_repo_rejects_non_git_dir(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="not a git repository"):
        with cli.resolved_repo(str(tmp_path)):
            pass  # pragma: no cover


def test_resolved_repo_rejects_missing_non_url(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="not found and not a clone URL"):
        with cli.resolved_repo(str(tmp_path / "missing")):
            pass  # pragma: no cover


# ---------------------------------------------------------------------------
# FinOps ledger
# ---------------------------------------------------------------------------


def test_append_finops_entry_creates_ledger(tmp_path: Path) -> None:
    ledger = tmp_path / "finops.json"
    entry = cli.append_finops_entry(
        ledger,
        ISSUE_URL,
        rounds_used=1,
        converged=True,
        prompt_tokens=100,
        completion_tokens=50,
        total_tokens=150,
        per_call=[{"role": "EdgeScout", "total_tokens": 150}],
    )
    assert entry["issue"] == ISSUE_URL
    assert entry["total_tokens"] == 150
    assert entry["priced"] is False
    payload = json.loads(ledger.read_text(encoding="utf-8"))
    assert payload["budget_usd"] == 150.0
    assert payload["total_spent_usd"] == 0.0
    assert len(payload["entries"]) == 1


def test_append_finops_entry_preserves_budget_and_appends(
    tmp_path: Path,
) -> None:
    ledger = tmp_path / "finops.json"
    ledger.write_text(
        json.dumps(
            {
                "budget_usd": 42.5,
                "total_spent_usd": 0.0,
                "entries": [
                    {
                        "issue": "old",
                        "est_cost_usd": 1.25,
                        "priced": True,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    cli.append_finops_entry(
        ledger, ISSUE_URL, 2, True, 10, 5, 15, per_call=[]
    )
    payload = json.loads(ledger.read_text(encoding="utf-8"))
    assert payload["budget_usd"] == 42.5
    assert payload["total_spent_usd"] == 1.25
    assert len(payload["entries"]) == 2


def test_append_finops_entry_recovers_from_corrupt_file(
    tmp_path: Path,
) -> None:
    ledger = tmp_path / "finops.json"
    ledger.write_text("{not json", encoding="utf-8")
    cli.append_finops_entry(
        ledger, ISSUE_URL, 1, False, 0, 0, 0, per_call=[]
    )
    payload = json.loads(ledger.read_text(encoding="utf-8"))
    assert payload["budget_usd"] == 150.0
    assert len(payload["entries"]) == 1


# ---------------------------------------------------------------------------
# fetch_issue (mocked transport)
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *args) -> bool:
        return False


def test_fetch_issue_ok(monkeypatch) -> None:
    def fake_urlopen(request, timeout=30):
        assert (
            "api.github.com/repos/octocat/hello-world/issues/42"
            in request.full_url
        )
        return _FakeResponse({"title": "Bug", "body": "Details"})

    monkeypatch.setattr(cli.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    assert cli.fetch_issue(ISSUE_URL) == ("Bug", "Details")


def test_fetch_issue_uses_token_header(monkeypatch) -> None:
    seen: dict = {}

    def fake_urlopen(request, timeout=30):
        seen["auth"] = request.get_header("Authorization")
        return _FakeResponse({"title": "Bug", "body": ""})

    monkeypatch.setattr(cli.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setenv("GITHUB_TOKEN", "ghp-test-token")
    title, _ = cli.fetch_issue(ISSUE_URL)
    assert title == "Bug"
    assert seen["auth"] == "Bearer ghp-test-token"


def test_fetch_issue_not_found(monkeypatch) -> None:
    def fake_urlopen(request, timeout=30):
        raise urllib.error.HTTPError(request.full_url, 404, "NF", None, None)

    monkeypatch.setattr(cli.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(cli.IssueFetchError, match="issue not found"):
        cli.fetch_issue(ISSUE_URL)


def test_fetch_issue_rate_limit_hint_and_zero_leak(monkeypatch) -> None:
    def fake_urlopen(request, timeout=30):
        raise urllib.error.HTTPError(request.full_url, 403, "RL", None, None)

    monkeypatch.setattr(cli.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setenv("GITHUB_TOKEN", "ghp-super-secret-value")
    with pytest.raises(cli.IssueFetchError) as exc_info:
        cli.fetch_issue(ISSUE_URL)
    assert "GITHUB_TOKEN" in str(exc_info.value)
    assert "ghp-super-secret-value" not in str(exc_info.value)


def test_fetch_issue_network_error(monkeypatch) -> None:
    def fake_urlopen(request, timeout=30):
        raise urllib.error.URLError("offline")

    monkeypatch.setattr(cli.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(cli.IssueFetchError, match="cannot reach GitHub API"):
        cli.fetch_issue(ISSUE_URL)


def test_fetch_issue_rejects_bad_url() -> None:
    with pytest.raises(ValueError, match="not a GitHub issue URL"):
        cli.fetch_issue("not-a-url")


# ---------------------------------------------------------------------------
# collect_code_chunks / repo_structure_sketch
# ---------------------------------------------------------------------------


def test_collect_code_chunks_python_only(fixture_repo: Path) -> None:
    (fixture_repo / "app.ts").write_text("const x = 1;\n", encoding="utf-8")
    chunks = cli.collect_code_chunks(fixture_repo, "python")
    paths = {chunk["path"] for chunk in chunks}
    assert "foo.py" in paths
    assert "tests/test_foo.py" in paths
    assert "app.ts" not in paths
    assert all(chunk["content"].strip() for chunk in chunks)


def test_collect_code_chunks_skips_caches(fixture_repo: Path) -> None:
    cache = fixture_repo / "__pycache__"
    cache.mkdir()
    (cache / "junk.py").write_text("x = 1\n", encoding="utf-8")
    chunks = cli.collect_code_chunks(fixture_repo, "python")
    assert all("__pycache__" not in chunk["path"] for chunk in chunks)


def test_repo_structure_sketch(fixture_repo: Path) -> None:
    sketch = cli.repo_structure_sketch(fixture_repo)
    assert "foo.py" in sketch
    assert "tests/" in sketch


# ---------------------------------------------------------------------------
# typer parity
# ---------------------------------------------------------------------------


def test_typer_backend_commands() -> None:
    pytest.importorskip("typer")
    from typer.testing import CliRunner

    assert cli.app is not None
    runner = CliRunner()
    top = runner.invoke(cli.app, ["--help"])
    assert top.exit_code == 0
    assert "verify" in top.output
    assert "test-guard" in top.output
    assert "Exit codes" in top.output
    verify_help = runner.invoke(cli.app, ["verify", "--help"])
    assert verify_help.exit_code == 0
    assert "--issue" in verify_help.output
    assert "--runner" in verify_help.output
    guard_help = runner.invoke(cli.app, ["test-guard", "--help"])
    assert guard_help.exit_code == 0
    assert "--baseline" in guard_help.output


def test_typer_verify_usage_error() -> None:
    pytest.importorskip("typer")
    from typer.testing import CliRunner

    runner = CliRunner()
    assert cli.app is not None
    result = runner.invoke(cli.app, ["verify", "--issue", "not-a-url"])
    assert result.exit_code == 2
