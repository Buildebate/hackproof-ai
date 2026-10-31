"""Step 2.2: 6-role asymmetric debate protocol (orchestrator + roles)."""

from __future__ import annotations

from typing import Any

import pytest

from hackproof.debate import (
    ROLES,
    DebateOrchestrator,
)
from hackproof.debate.roles import (
    ROLE_ORDER,
    architectural_builder,
    consensus_judge,
    edge_scout,
    formal_adversary_critic,
    occams_razor_critic,
    signature_fact_checker,
)
from mocks.mock_meta_client import MockMetaClient

EXPECTED_ORDER = (
    "EdgeScout",
    "ArchitecturalBuilder",
    "OccamsRazorCritic",
    "SignatureFactChecker",
    "FormalAdversaryCritic",
    "ConsensusJudge",
)

ISSUE = "login crashes when password is None"
CODE = "def authenticate_user(name, password):\n    return check(name, password)\n"


class _ScriptedClient:
    """Deterministic debate client with captured prompts and fake usage."""

    def __init__(
        self,
        model: str = "muse-spark-1.3",
        verdicts: dict[str, str] | None = None,
        usage: dict[str, int] | None = None,
        syntheses: list[str] | None = None,
    ) -> None:
        self._model = model
        self._verdicts = dict(verdicts or {})
        self._usage = usage
        self._syntheses = list(syntheses) if syntheses is not None else None
        self.calls: list[tuple[str, str, list[dict[str, Any]]]] = []

    @property
    def model(self) -> str:
        return self._model

    @property
    def last_usage(self) -> dict[str, int] | None:
        return dict(self._usage) if self._usage is not None else None

    def debate_round(
        self, role: str, prompt: str, history: list[dict[str, Any]]
    ) -> str:
        self.calls.append((role, prompt, list(history)))
        return self._verdicts.get(role, f"{role}: neutral observation noted")

    def synthesize_patch(self, debate_consensus: str) -> str:
        self.calls.append(("synthesize_patch", debate_consensus, []))
        if self._syntheses is not None:
            if not self._syntheses:
                raise AssertionError("synthesize_patch called more than scripted")
            return self._syntheses.pop(0)
        return (
            "<<<<<<< SEARCH\n"
            "def authenticate_user(name, password):\n"
            "=======\n"
            "def authenticate_user(name, password):\n"
            "    if password is None:\n"
            '        raise ValueError("password required")\n'
            ">>>>>>> REPLACE\n"
        )


def _approve_verdicts() -> dict[str, str]:
    return {role: f"{role}: all checks green. APPROVE" for role in EXPECTED_ORDER}


# ---------------------------------------------------------------------------
# Pipeline structure
# ---------------------------------------------------------------------------


def test_roles_pipeline_order_and_client_kinds() -> None:
    assert ROLE_ORDER == EXPECTED_ORDER
    assert tuple(role.ROLE_NAME for role in ROLES) == EXPECTED_ORDER
    kinds = {role.ROLE_NAME: role.CLIENT_KIND for role in ROLES}
    assert kinds == {
        "EdgeScout": "spark",
        "ArchitecturalBuilder": "wonder",
        "OccamsRazorCritic": "spark",
        "SignatureFactChecker": "spark",
        "FormalAdversaryCritic": "wonder",
        "ConsensusJudge": "spark",
    }
    for role in ROLES:
        assert isinstance(role.ROLE_SYSTEM_PROMPT, str)
        assert len(role.ROLE_SYSTEM_PROMPT) > 40


def test_role_run_prefixes_system_prompt_and_routes_history() -> None:
    for role in ROLES:
        client = _ScriptedClient()
        history = [{"role": "EdgeScout", "text": "prior"}]
        verdict = role.run(
            client,
            {
                "issue_description": ISSUE,
                "code_context": CODE,
                "repo_structure": "invariants",
                "history": history,
                "round": 1,
                "patch_candidate": "patch",
            },
        )
        assert isinstance(verdict, str) and verdict
        called_role, prompt, seen_history = client.calls[0]
        assert called_role == role.ROLE_NAME
        assert prompt.startswith(role.ROLE_SYSTEM_PROMPT)
        assert seen_history == history


# ---------------------------------------------------------------------------
# Full cycles with MockMetaClient ($0 cost)
# ---------------------------------------------------------------------------


def test_full_two_round_cycle_with_mocks() -> None:
    orchestrator = DebateOrchestrator(
        MockMetaClient("muse-spark-1.3"), MockMetaClient("muse-wonder-1.0")
    )
    report = orchestrator.run(ISSUE, CODE, repo_structure="no globals")

    assert report.rounds_used == 2
    assert report.converged is False
    assert report.convergence_score == pytest.approx(0.5)
    assert len(report.rounds) == 2
    assert list(report.verdicts) == list(EXPECTED_ORDER)
    for role, verdict in report.verdicts.items():
        assert f":{role}] mock-verdict-" in verdict
    assert "mock-patch-" in report.patch_candidate
    assert ">>>>>>> REPLACE" in report.patch_candidate
    # 6 roles x 2 rounds + 1 final patch synthesis.
    assert len(report.token_usage) == 13
    assert report.token_usage[-1].role == "PatchSynthesis"
    assert report.total_tokens == 0
    assert report.total_prompt_tokens == 0
    assert report.total_completion_tokens == 0
    assert report.duration_ms >= 0


def test_mock_cycle_is_deterministic() -> None:
    def _run() -> tuple[str, dict[str, str], float]:
        orchestrator = DebateOrchestrator(
            MockMetaClient("muse-spark-1.3"), MockMetaClient("muse-wonder-1.0")
        )
        report = orchestrator.run(ISSUE, CODE)
        return report.patch_candidate, dict(report.verdicts), report.convergence_score

    first, second = _run(), _run()
    assert first == second


def test_max_rounds_cap_binds_mock_cycle() -> None:
    orchestrator = DebateOrchestrator(
        MockMetaClient("muse-spark-1.3"),
        MockMetaClient("muse-wonder-1.0"),
        max_rounds=1,
    )
    report = orchestrator.run(ISSUE, CODE)
    assert report.rounds_used == 1
    assert report.converged is False
    assert len(report.token_usage) == 7


# ---------------------------------------------------------------------------
# Convergence paths
# ---------------------------------------------------------------------------


def test_early_convergence_on_unanimous_approve() -> None:
    spark = _ScriptedClient("muse-spark-1.3", _approve_verdicts())
    wonder = _ScriptedClient("muse-wonder-1.0", _approve_verdicts())
    report = DebateOrchestrator(spark, wonder).run(ISSUE, CODE)

    assert report.rounds_used == 1
    assert report.converged is True
    assert report.convergence_score == pytest.approx(1.0)
    assert all("APPROVE" in v for v in report.verdicts.values())
    assert ">>>>>>> REPLACE" in report.patch_candidate


def test_no_convergence_on_revise_verdicts() -> None:
    verdicts = {
        role: f"{role}: problems found. REVISE" for role in EXPECTED_ORDER
    }
    spark = _ScriptedClient("muse-spark-1.3", verdicts)
    wonder = _ScriptedClient("muse-wonder-1.0", verdicts)
    report = DebateOrchestrator(spark, wonder).run(ISSUE, CODE)

    assert report.rounds_used == 2
    assert report.converged is False
    assert report.convergence_score == pytest.approx(0.0)


def test_early_exit_on_five_sixths_plus_judge() -> None:
    """Step 4.3b.8 (R11): 5/6 with an approving judge closes round 1."""
    verdicts = dict(_approve_verdicts())
    verdicts["OccamsRazorCritic"] = "OccamsRazorCritic: too big. REVISE"
    spark = _ScriptedClient("muse-spark-1.3", verdicts)
    wonder = _ScriptedClient("muse-wonder-1.0", verdicts)
    report = DebateOrchestrator(spark, wonder).run(ISSUE, CODE)

    assert report.converged is True
    assert report.rounds_used == 1
    # 5/6 ≈ 0.833: the retired 0.85 threshold would have missed this.
    assert report.convergence_score == pytest.approx(5 / 6)


def test_no_exit_when_judge_dissents_or_two_dissent() -> None:
    """A dissenting judge (or two dissenters) forces round 2."""
    verdicts = dict(_approve_verdicts())
    verdicts["ConsensusJudge"] = "ConsensusJudge: blocked. REVISE"
    spark = _ScriptedClient("muse-spark-1.3", verdicts)
    wonder = _ScriptedClient("muse-wonder-1.0", verdicts)
    report = DebateOrchestrator(spark, wonder).run(ISSUE, CODE)
    assert report.converged is False
    assert report.rounds_used == 2

    verdicts = dict(_approve_verdicts())
    verdicts["OccamsRazorCritic"] = "OccamsRazorCritic: big. REVISE"
    verdicts["EdgeScout"] = "EdgeScout: vague. REVISE"
    spark = _ScriptedClient("muse-spark-1.3", verdicts)
    wonder = _ScriptedClient("muse-wonder-1.0", verdicts)
    report = DebateOrchestrator(spark, wonder).run(ISSUE, CODE)
    assert report.converged is False
    assert report.rounds_used == 2


def test_history_grows_monotonically_across_roles_and_rounds() -> None:
    spark = _ScriptedClient("muse-spark-1.3")
    wonder = _ScriptedClient("muse-wonder-1.0")
    DebateOrchestrator(spark, wonder).run(ISSUE, CODE)

    spark_lengths = [len(history) for _, _, history in spark.calls]
    wonder_lengths = [
        len(history)
        for role, _, history in wonder.calls
        if role != "synthesize_patch"
    ]
    # Spark roles: rounds at history sizes 0,2,3,5 then 6,8,9,11.
    assert spark_lengths == [0, 2, 3, 5, 6, 8, 9, 11]
    # Wonder roles: builder/adversary at 1,4 then 7,10.
    assert wonder_lengths == [1, 4, 7, 10]


# ---------------------------------------------------------------------------
# Validation + FinOps accounting
# ---------------------------------------------------------------------------


def test_constructor_rejects_invalid_bounds() -> None:
    spark = MockMetaClient("muse-spark-1.3")
    wonder = MockMetaClient("muse-wonder-1.0")
    with pytest.raises(ValueError, match="max_rounds"):
        DebateOrchestrator(spark, wonder, max_rounds=0)
    with pytest.raises(ValueError, match="max_rounds"):
        DebateOrchestrator(spark, wonder, max_rounds=-2)
    ok = DebateOrchestrator(spark, wonder)
    assert ok.max_rounds == 2


def test_token_usage_sums_per_call_records() -> None:
    usage = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
    spark = _ScriptedClient("muse-spark-1.3", _approve_verdicts(), usage)
    wonder = _ScriptedClient("muse-wonder-1.0", _approve_verdicts(), usage)
    report = DebateOrchestrator(spark, wonder).run(ISSUE, CODE)

    assert len(report.token_usage) == 7
    assert [u.role for u in report.token_usage] == [
        *EXPECTED_ORDER,
        "PatchSynthesis",
    ]
    assert {u.model for u in report.token_usage} == {
        "muse-spark-1.3",
        "muse-wonder-1.0",
    }
    assert report.total_prompt_tokens == 70
    assert report.total_completion_tokens == 35
    assert report.total_tokens == 105


def test_clients_without_usage_report_zeros() -> None:
    spark = _ScriptedClient("muse-spark-1.3", _approve_verdicts())
    wonder = _ScriptedClient("muse-wonder-1.0", _approve_verdicts())
    assert spark.last_usage is None
    report = DebateOrchestrator(spark, wonder).run(ISSUE, CODE)
    assert report.total_tokens == 0
    assert all(u.total_tokens == 0 for u in report.token_usage)


# ---------------------------------------------------------------------------
# Deterministic role helpers (no LLM involved)
# ---------------------------------------------------------------------------


def test_consensus_score_matrix() -> None:
    # Step 4.3b.8 (R11): end-position markers only; negations dissent.
    assert consensus_judge.score([]) == 0.0
    assert consensus_judge.score(["all green. APPROVE"]) == pytest.approx(1.0)
    assert consensus_judge.score(["approve."]) == pytest.approx(1.0)
    assert consensus_judge.score(["(APPROVE)"]) == pytest.approx(1.0)
    assert consensus_judge.score(["x APPROVE y"]) == pytest.approx(0.5)
    assert consensus_judge.score(["needs REVISE"]) == pytest.approx(0.0)
    assert consensus_judge.score(["REJECT this"]) == pytest.approx(0.5)
    assert consensus_judge.score(["plain note"]) == pytest.approx(0.5)
    assert consensus_judge.score(["DO NOT APPROVE"]) == pytest.approx(0.0)
    assert consensus_judge.score(["APPROVED"]) == pytest.approx(0.5)
    assert consensus_judge.score(["DISAPPROVE"]) == pytest.approx(0.5)
    assert consensus_judge.score(["DO NOT REVISE"]) == pytest.approx(0.5)
    mixed = ["A APPROVE", "B REVISE", "C neutral"]
    assert consensus_judge.score(mixed) == pytest.approx(0.5)


def test_consensus_reached_matrix() -> None:
    """Step 4.3b.8 (R11): 6/6, or 5/6 with an approving judge."""
    full = {role: f"{role}: ok. APPROVE" for role in EXPECTED_ORDER}
    assert consensus_judge.consensus_reached(full) is True
    five = dict(full)
    five["OccamsRazorCritic"] = "OccamsRazorCritic: big. REVISE"
    assert consensus_judge.consensus_reached(five) is True
    judge_out = dict(full)
    judge_out["ConsensusJudge"] = "ConsensusJudge: blocked. REVISE"
    assert consensus_judge.consensus_reached(judge_out) is False
    judge_neg = dict(full)
    judge_neg["ConsensusJudge"] = "ConsensusJudge: DO NOT APPROVE"
    assert consensus_judge.consensus_reached(judge_neg) is False
    four = dict(five)
    four["EdgeScout"] = "EdgeScout: vague. REVISE"
    assert consensus_judge.consensus_reached(four) is False
    assert consensus_judge.consensus_reached({}) is False
    assert consensus_judge.verdict_marker("final word: APPROVE") == "APPROVE"
    assert consensus_judge.verdict_marker("DO NOT APPROVE") is None
    assert consensus_judge.approves("ok. APPROVE") is True
    assert consensus_judge.approves("ok. DO NOT APPROVE") is False


PATCH_SMALL = (
    "<<<<<<< SEARCH\n"
    "def f(v):\n"
    "    return v\n"
    "=======\n"
    "def f(v):\n"
    "    if v is None:\n"
    '        raise ValueError("x")\n'
    "    return v\n"
    ">>>>>>> REPLACE\n"
)


def test_occam_assessment_budget_and_branching() -> None:
    small = occams_razor_critic.assess_patch(PATCH_SMALL, minimal_added_lines=2)
    assert small.added_lines == 4
    assert small.branching_points >= 1
    assert small.over_budget is False

    bloated = "<<<<<<< SEARCH\na\n=======\n" + "x = 1\n" * 10 + ">>>>>>> REPLACE\n"
    big = occams_razor_critic.assess_patch(bloated, minimal_added_lines=2)
    assert big.added_lines == 10
    assert big.over_budget is True

    plain = occams_razor_critic.assess_patch("a = 1\nb = 2\n", 1)
    assert plain.added_lines == 2
    assert plain.over_budget is False

    branching = occams_razor_critic.assess_patch(
        "if a and b:\n    for x in y:\n        pass\n", 1
    )
    assert branching.branching_points >= 3


def test_signature_check_reports_only_unknown_names() -> None:
    patch = (
        "def authenticate_user(name, password):\n"
        "    return check(name, password)\n"
        "from os import path\n"
        "import sys, json\n"
        "def hallucinated_handler():\n"
        "    pass\n"
    )
    code = (
        "def authenticate_user(name, password): ...\n"
        "from os import path\n"
        "import sys\n"
    )
    assert signature_fact_checker.check_references(patch, code) == [
        "hallucinated_handler",
        "json",
    ]
    assert signature_fact_checker.check_references(patch, patch) == []


def test_builder_extracts_only_complete_blocks() -> None:
    two = (
        "<<<<<<< SEARCH\nA1\n=======\nB1\n>>>>>>> REPLACE\n"
        "noise line\n"
        "<<<<<<< SEARCH\nA2\n=======\nB2\n>>>>>>> REPLACE\n"
    )
    blocks = architectural_builder.extract_blocks(two)
    assert blocks == [("A1", "B1"), ("A2", "B2")]

    assert architectural_builder.extract_blocks("no markers here") == []
    incomplete = "<<<<<<< SEARCH\nA\n=======\nB\n(no end marker)"
    assert architectural_builder.extract_blocks(incomplete) == []
    missing_sep = "<<<<<<< SEARCH\nA\n>>>>>>> REPLACE\n"
    assert architectural_builder.extract_blocks(missing_sep) == []


def test_builder_extracts_routed_blocks_with_file_headers() -> None:
    # Step 4.3b.1 (R4): real header spellings from B-11739 patch_text.txt.
    text = (
        "packages/shared/src/normalizeProp.ts\n"
        "<<<<<<< SEARCH\nS1\n=======\nR1\n>>>>>>> REPLACE\n"
        "noise line\n"
        "packages/runtime-core/src/helpers/useCssVars.ts\n"
        "<<<<<<< SEARCH\nS2\n=======\nR2\n>>>>>>> REPLACE\n"
        "Fix:\n"
        "<<<<<<< SEARCH\nS3\n=======\nR3\n>>>>>>> REPLACE\n"
    )
    assert architectural_builder.extract_routed_blocks(text) == [
        ("packages/shared/src/normalizeProp.ts", "S1", "R1"),
        ("packages/runtime-core/src/helpers/useCssVars.ts", "S2", "R2"),
        (None, "S3", "R3"),
    ]
    assert architectural_builder.extract_routed_blocks("no markers here") == []
    assert architectural_builder.extract_routed_blocks(
        "./src/app.py\n<<<<<<< SEARCH\nS\n=======\nR\n>>>>>>> REPLACE\n"
    ) == [("src/app.py", "S", "R")]
    # Traversal survives parsing intact so the patcher can refuse it loudly.
    assert architectural_builder.extract_routed_blocks(
        "../evil.py\n<<<<<<< SEARCH\nS\n=======\nR\n>>>>>>> REPLACE\n"
    ) == [("../evil.py", "S", "R")]


def test_scout_parses_numbered_and_bullet_cases() -> None:
    verdict = (
        "1. None password crashes\n"
        "2) empty string bypasses check\n"
        "- unicode name breaks layout\n"
        "* race on concurrent login\n"
        "plain prose line\n"
    )
    assert edge_scout.parse_cases(verdict) == [
        "None password crashes",
        "empty string bypasses check",
        "unicode name breaks layout",
        "race on concurrent login",
    ]
    assert edge_scout.parse_cases("no list here") == []
    assert edge_scout.parse_cases("") == []


def test_adversary_parses_findings() -> None:
    verdict = "- breaks retry invariant\n2. leaks session token\nAPPROVE"
    assert formal_adversary_critic.parse_findings(verdict) == [
        "breaks retry invariant",
        "leaks session token",
    ]
    assert formal_adversary_critic.parse_findings("") == []


# ---------------------------------------------------------------------------
# Step 4.3b.3 (R6): verbatim directive, grounded retry, builder-only context
# ---------------------------------------------------------------------------


def test_builder_prompt_carries_verbatim_directive() -> None:
    prompt = architectural_builder.ROLE_SYSTEM_PROMPT
    assert "byte-for-byte" in prompt
    assert "never reconstruct code from memory" in prompt


def test_revise_patch_builds_grounded_retry_prompt() -> None:
    client = _ScriptedClient(
        model="muse-wonder-1.0",
        verdicts={"ArchitecturalBuilder": "REVISED-PATCH"},
    )
    out = architectural_builder.revise_patch(
        client,
        issue_description="ISSUE-TEXT",
        code_context="CODE-CONTEXT",
        previous_patch="PREVIOUS-PATCH",
        failure_report="FAILURE-REGION",
    )
    assert out == "REVISED-PATCH"
    assert len(client.calls) == 1
    role, prompt, history = client.calls[0]
    assert role == "ArchitecturalBuilder"
    assert history == []
    for needle in (
        "FAILURE-REGION",
        "PREVIOUS-PATCH",
        "ISSUE-TEXT",
        "CODE-CONTEXT",
        "byte-for-byte",
        "ONLY corrected SEARCH/REPLACE blocks",
    ):
        assert needle in prompt


def test_orchestrator_scopes_full_context_to_builder() -> None:
    spark = _ScriptedClient(model="muse-spark-1.3")
    wonder = _ScriptedClient(model="muse-wonder-1.0")
    orchestrator = DebateOrchestrator(
        spark, wonder, max_rounds=1
    )
    orchestrator.run(
        issue_description=ISSUE,
        code_context="SHORT-HEAD",
        builder_code_context="FULL-TOP1-FILE",
    )
    prompts: dict[str, str] = {}
    for client in (spark, wonder):
        for role, prompt, _history in client.calls:
            if role != "synthesize_patch":
                prompts.setdefault(role, prompt)
    assert set(prompts) == set(EXPECTED_ORDER)
    assert "FULL-TOP1-FILE" in prompts["ArchitecturalBuilder"]
    assert "SHORT-HEAD" not in prompts["ArchitecturalBuilder"]
    for role, prompt in prompts.items():
        if role != "ArchitecturalBuilder":
            assert "FULL-TOP1-FILE" not in prompt


def test_orchestrator_default_context_unchanged_for_all_roles() -> None:
    spark = _ScriptedClient(model="muse-spark-1.3")
    wonder = _ScriptedClient(model="muse-wonder-1.0")
    orchestrator = DebateOrchestrator(
        spark, wonder, max_rounds=1
    )
    orchestrator.run(issue_description=ISSUE, code_context="SHORT-HEAD")
    seen = [
        prompt
        for client in (spark, wonder)
        for role, prompt, _history in client.calls
        if role == "ArchitecturalBuilder"
    ]
    assert len(seen) == 1
    assert "SHORT-HEAD" in seen[0]


# ---------------------------------------------------------------------------
# Step 4.3b.8 (R11): end-position markers on verdict roles;
# Step 4.3c.1 (R13): builder narrowed out of marker scope
# ---------------------------------------------------------------------------


def test_verdict_roles_require_end_marker_builder_forbids_it() -> None:
    # Step 4.3c.1 (R13): markers stay scoped to the five verdict roles;
    # the builder prompt carries no marker tokens at all (the 4.3c.0
    # ablation proved they divert it into bare verdict-tokens).
    for role in (
        edge_scout,
        occams_razor_critic,
        signature_fact_checker,
        formal_adversary_critic,
        consensus_judge,
    ):
        assert "exactly one marker word" in role.ROLE_SYSTEM_PROMPT
        assert "APPROVE" in role.ROLE_SYSTEM_PROMPT
        assert "REVISE" in role.ROLE_SYSTEM_PROMPT
    prompt = architectural_builder.ROLE_SYSTEM_PROMPT
    assert "exactly one marker word" not in prompt
    assert "APPROVE" not in prompt
    assert "REVISE" not in prompt
    # The builder's verbatim directive survives the marker removal.
    assert "byte-for-byte" in prompt
    assert "never reconstruct code from memory" in prompt
    # Blocks-first + stop directives (truncation + anti-ramble).
    assert "blocks FIRST" in prompt
    assert "Stop immediately after the last" in prompt


def test_builder_extract_ignores_trailing_marker() -> None:
    plain = architectural_builder.extract_routed_blocks(PATCH_SMALL)
    marked = architectural_builder.extract_routed_blocks(
        PATCH_SMALL + "APPROVE\n"
    )
    assert len(plain) == 1
    assert marked == plain


def test_scout_parse_ignores_trailing_marker() -> None:
    verdict = "1. None input\n2. empty string\nAPPROVE"
    assert edge_scout.parse_cases(verdict) == ["None input", "empty string"]


# ---------------------------------------------------------------------------
# Step 4.3c.1 (R13): single grounded retry on empty synthesis
# ---------------------------------------------------------------------------


def test_synthesis_retry_recovers_blocks_on_empty_first() -> None:
    spark = _ScriptedClient(model="muse-spark-1.3")
    wonder = _ScriptedClient(
        model="muse-wonder-1.0", syntheses=["REVISE", PATCH_SMALL]
    )
    orchestrator = DebateOrchestrator(spark, wonder, max_rounds=1)
    report = orchestrator.run(issue_description=ISSUE, code_context=CODE)
    assert report.patch_candidate == PATCH_SMALL
    synth_calls = [c for c in wonder.calls if c[0] == "synthesize_patch"]
    assert len(synth_calls) == 2
    assert "FORMAT VIOLATION" in synth_calls[1][1]
    assert "0 complete SEARCH/REPLACE blocks" in synth_calls[1][1]
    roles = [u.role for u in report.token_usage]
    assert "PatchSynthesis" in roles
    assert "PatchSynthesis-retry" in roles


def test_synthesis_retry_quotes_truncation_and_stops_after_one() -> None:
    spark = _ScriptedClient(model="muse-spark-1.3")
    wonder = _ScriptedClient(
        model="muse-wonder-1.0",
        syntheses=["", ""],
        usage={
            "prompt_tokens": 10,
            "completion_tokens": 32768,
            "total_tokens": 32778,
            "finish_reason": "length",
        },
    )
    orchestrator = DebateOrchestrator(spark, wonder, max_rounds=1)
    report = orchestrator.run(issue_description=ISSUE, code_context=CODE)
    # Second output wins as-is: no third call is scripted, so a third
    # attempt would raise instead of passing silently.
    assert report.patch_candidate == ""
    synth_calls = [c for c in wonder.calls if c[0] == "synthesize_patch"]
    assert len(synth_calls) == 2
    assert "finish_reason=length" in synth_calls[1][1]
    assert "chars=0" in synth_calls[1][1]


def test_synthesis_without_empty_first_runs_no_retry() -> None:
    spark = _ScriptedClient(model="muse-spark-1.3")
    wonder = _ScriptedClient(
        model="muse-wonder-1.0", syntheses=[PATCH_SMALL]
    )
    orchestrator = DebateOrchestrator(spark, wonder, max_rounds=1)
    report = orchestrator.run(issue_description=ISSUE, code_context=CODE)
    assert report.patch_candidate == PATCH_SMALL
    synth_calls = [c for c in wonder.calls if c[0] == "synthesize_patch"]
    assert len(synth_calls) == 1
    roles = [u.role for u in report.token_usage]
    assert "PatchSynthesis-retry" not in roles


# ---------------------------------------------------------------------------
# Step 4.3c.1 (R9): green-feedback revision prompt
# ---------------------------------------------------------------------------


def test_revise_patch_from_green_builds_grounded_prompt() -> None:
    client = _ScriptedClient(
        model="muse-wonder-1.0",
        verdicts={"ArchitecturalBuilder": "REVISED-PATCH"},
    )
    out = architectural_builder.revise_patch_from_green(
        client,
        issue_description="ISSUE-TEXT",
        code_context="CODE-CONTEXT",
        previous_patch="PREVIOUS-PATCH",
        green_report="GREEN-REPORT",
    )
    assert out == "REVISED-PATCH"
    assert len(client.calls) == 1
    role, prompt, history = client.calls[0]
    assert role == "ArchitecturalBuilder"
    assert history == []
    for needle in (
        "GREEN-REPORT",
        "PREVIOUS-PATCH",
        "ISSUE-TEXT",
        "CODE-CONTEXT",
        "WAS APPLIED BUT THE TESTS STILL FAIL",
        "ONLY corrected SEARCH/REPLACE blocks",
        "do not touch test files",
    ):
        assert needle in prompt
    assert "APPROVE" not in prompt
    assert "REVISE" not in prompt
