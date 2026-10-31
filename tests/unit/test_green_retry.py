"""Step 4.3c.1 (R9): green-feedback retry decision + report (offline, $0).

Covers ``benchmarks/run_benchmark.py`` retry helpers without live calls:
eligibility gates the single revision to iterable patch-content misses,
the condensed report carries failing names plus observed assertion
excerpts (never gold), and the miss→revision→blocks vector proves the
loop's data flow through a scripted builder. Live revision + re-eval
(``run_green_retry_oci/local``) execute on Wave 2, not in unit tests.
"""

from __future__ import annotations

import sys
from pathlib import Path

BENCH_DIR = Path(__file__).resolve().parent.parent.parent / "benchmarks"
sys.path.insert(0, str(BENCH_DIR))

import run_benchmark
from hackproof.debate.roles import architectural_builder
from run_benchmark import (
    GREEN_RETRY_TOTAL_CHARS,
    condense_green_failures,
    green_retry_eligible,
    retry_agent_index,
)

TASK_PY = {
    "id": "demo__pkg-1",
    "language": "python",
    "fail_to_pass": ["test_auth.py::test_login_none"],
    "pass_to_pass": ["test_auth.py::test_login_ok"],
}

JUNIT_PY = (
    b"<testsuite>"
    b'<testcase file="test_auth.py" classname="test_auth" '
    b'name="test_login_none">'
    b'<failure message="assert password is not None">'
    b"assert password is not None</failure></testcase>"
    b'<testcase file="test_auth.py" classname="test_auth" '
    b'name="test_login_ok">'
    b'<failure message="assert session == expected">'
    b"assert session == expected</failure></testcase>"
    b"</testsuite>"
)

RESOLUTION_PY = {
    "test_auth.py::test_login_none": ["test_auth.py::test_login_none"],
    "test_auth.py::test_login_ok": ["test_auth.py::test_login_ok"],
}

SCORING_MISS = {
    "resolved_functional": False,
    "f2p_not_fixed": ["test_auth.py::test_login_none"],
    "f2p_not_red_at_base": [],
    "p2p_broken": [],
    "f2p": {
        "test_auth.py::test_login_none": {"base": "failed", "agent": "failed"}
    },
    "p2p": {"test_auth.py::test_login_ok": {"base": "passed", "agent": "passed"}},
}

SCORING_P2P_ONLY = {
    "resolved_functional": False,
    "f2p_not_fixed": [],
    "f2p_not_red_at_base": [],
    "p2p_broken": ["test_auth.py::test_login_ok"],
    "f2p": {
        "test_auth.py::test_login_none": {"base": "failed", "agent": "passed"}
    },
    "p2p": {
        "test_auth.py::test_login_ok": {"base": "passed", "agent": "failed"}
    },
}


def test_eligible_on_f2p_not_fixed_and_p2p_broken() -> None:
    assert green_retry_eligible(SCORING_MISS) is True
    # Wave-1 2931-B shape: F2P fixed but a P2P broke — still iterable.
    assert green_retry_eligible(SCORING_P2P_ONLY) is True


def test_ineligible_on_resolved_unmeasured_broken_or_tamper() -> None:
    resolved = dict(SCORING_MISS, resolved_functional=True,
                    f2p_not_fixed=[])
    assert green_retry_eligible(resolved) is False
    unmeasured = dict(
        SCORING_MISS, f2p_not_red_at_base=["test_auth.py::test_login_none"]
    )
    assert green_retry_eligible(unmeasured) is False
    assert green_retry_eligible(SCORING_MISS, collect_broken=True) is False
    assert green_retry_eligible(SCORING_MISS, guard_clean=False) is False
    assert green_retry_eligible({}) is False


def test_condense_carries_names_transitions_and_excerpts() -> None:
    report = condense_green_failures(
        TASK_PY, JUNIT_PY, SCORING_MISS, RESOLUTION_PY
    )
    assert "[F2P] test_auth.py::test_login_none" in report
    assert "base=failed agent=failed" in report
    assert "assert password is not None" in report
    # The kept P2P is not a target: no entry, no excerpt.
    assert "test_login_ok" not in report


def test_condense_lists_p2p_broken_with_excerpt() -> None:
    report = condense_green_failures(
        TASK_PY, JUNIT_PY, SCORING_P2P_ONLY, RESOLUTION_PY
    )
    assert "[P2P] test_auth.py::test_login_ok" in report
    assert "base=passed agent=failed" in report
    assert "assert session == expected" in report


def test_condense_names_survive_unparseable_junit() -> None:
    report = condense_green_failures(
        TASK_PY, b"not xml at all", SCORING_MISS, RESOLUTION_PY
    )
    # Names come from scoring (harness truth), never invented, never lost.
    assert "test_auth.py::test_login_none" in report
    assert "(no failure message captured)" in report


def test_condense_total_cap_bounds_many_failures() -> None:
    many_f2p = [f"test_auth.py::test_case_{i:02d}" for i in range(20)]
    task = dict(TASK_PY, fail_to_pass=many_f2p)
    cases = (
        "<testsuite>"
        + "".join(
            f'<testcase file="test_auth.py" classname="test_auth" '
            f'name="test_case_{i:02d}">'
            f'<failure message="{"A" * 400}">{"A" * 400}</failure></testcase>'
            for i in range(20)
        )
        + "</testsuite>"
    ).encode("utf-8")
    scoring = {
        "resolved_functional": False,
        "f2p_not_fixed": many_f2p,
        "f2p_not_red_at_base": [],
        "p2p_broken": [],
        "f2p": {t: {"base": "failed", "agent": "failed"} for t in many_f2p},
        "p2p": {},
    }
    resolution = {t: [t] for t in many_f2p}
    report = condense_green_failures(task, cases, scoring, resolution)
    assert len(report) <= GREEN_RETRY_TOTAL_CHARS + len("\n…[report truncated]")
    assert "…[report truncated]" in report


def test_condense_vitest_ids_resolve() -> None:
    task = {
        "id": "demo__pkg-2",
        "language": "typescript",
        "fail_to_pass": ["auth.spec.ts > login > rejects none"],
        "pass_to_pass": [],
    }
    junit = (
        b"<testsuite>"
        b'<testcase file="auth.spec.ts" classname="auth.spec.ts" '
        b'name="auth.spec.ts &gt; login &gt; rejects none">'
        b'<failure message="expected null to be defined">'
        b"expected null to be defined</failure></testcase>"
        b"</testsuite>"
    )
    scoring = {
        "resolved_functional": False,
        "f2p_not_fixed": ["auth.spec.ts > login > rejects none"],
        "f2p_not_red_at_base": [],
        "p2p_broken": [],
        "f2p": {
            "auth.spec.ts > login > rejects none": {
                "base": "failed",
                "agent": "failed",
            }
        },
        "p2p": {},
    }
    resolution = {
        "auth.spec.ts > login > rejects none": [
            "auth.spec.ts > login > rejects none"
        ]
    }
    report = condense_green_failures(task, junit, scoring, resolution)
    assert "auth.spec.ts > login > rejects none" in report
    assert "expected null to be defined" in report


class _ScriptedBuilder:
    """Single-shot builder client capturing the green revision prompt."""

    def __init__(self, revision: str) -> None:
        self._revision = revision
        self.calls: list[tuple[str, str]] = []

    def debate_round(self, role: str, prompt: str, history: list) -> str:
        self.calls.append((role, prompt))
        return self._revision


REVISION_PATCH = (
    "src/auth.py\n"
    "<<<<<<< SEARCH\n"
    "def login(password):\n"
    "    return check(password)\n"
    "=======\n"
    "def login(password):\n"
    "    if password is None:\n"
    '        raise ValueError("password required")\n'
    "    return check(password)\n"
    ">>>>>>> REPLACE\n"
)


def test_miss_to_revision_vector_first_miss_then_blocks() -> None:
    # First patch missed: F2P still red -> eligible for exactly one retry.
    assert green_retry_eligible(SCORING_MISS) is True
    report = condense_green_failures(
        TASK_PY, JUNIT_PY, SCORING_MISS, RESOLUTION_PY
    )
    client = _ScriptedBuilder(REVISION_PATCH)
    out = architectural_builder.revise_patch_from_green(
        client,
        issue_description="login crashes when password is None",
        code_context="def login(password):\n    return check(password)\n",
        previous_patch="PREVIOUS-PATCH",
        green_report=report,
    )
    assert out == REVISION_PATCH
    assert len(client.calls) == 1
    role, prompt = client.calls[0]
    assert role == "ArchitecturalBuilder"
    # The feedback names the failing test and its observed assertion.
    assert "test_auth.py::test_login_none" in prompt
    assert "assert password is not None" in prompt
    # The revision parses into a routed block aimed at the miss.
    routed = architectural_builder.extract_routed_blocks(out)
    assert len(routed) == 1
    assert routed[0][0] == "src/auth.py"


def test_run_benchmark_exports_retry_helpers() -> None:
    assert callable(run_benchmark.run_green_revision)
    assert callable(run_benchmark.run_green_retry_oci)
    assert callable(run_benchmark.run_green_retry_local)
    assert callable(retry_agent_index)


# Step 12a.0: the retry-run JUnit index must be remapped with the FRESH
# retry-run resolution. First-attempt keys carry the first run dir infix;
# the retry dir infix differs, so the stale map mislabels env-dependent
# (absolute-path param) IDs as missing (observed: 6028-B P2P).
MID_PARAM = "tests/test_u.py::T::test_p[/f.py]"
FIRST_COLLECTED = "tests/test_u.py::T::test_p[/runs/B/tests/f.py]"
RETRY_COLLECTED = "tests/test_u.py::T::test_p[/runs/B-green-retry/tests/f.py]"


def test_retry_remap_uses_fresh_resolution_for_python() -> None:
    stale = {MID_PARAM: [FIRST_COLLECTED]}
    fresh = {MID_PARAM: [RETRY_COLLECTED]}
    full_index = {RETRY_COLLECTED: "passed"}
    # The stale first-attempt map reproduces the live bug (missing).
    assert retry_agent_index(
        {"language": "python"}, stale, stale, full_index
    )[MID_PARAM] == "missing"
    # The fresh retry-run map resolves the same JUnit bytes to passed.
    assert retry_agent_index(
        {"language": "python"}, fresh, stale, full_index
    )[MID_PARAM] == "passed"


def test_retry_remap_keeps_first_attempt_map_for_typescript() -> None:
    mid = "a.spec.ts > suite > case"
    base = {mid: [mid]}
    full_index = {mid: "passed"}
    assert retry_agent_index(
        {"language": "typescript"}, {}, base, full_index
    )[mid] == "passed"
