"""Asymmetric debate orchestrator (Step 2.2, Spark 1.3 + Wonder 1.0).

Runs the fixed 6-role loop for at most ``max_rounds`` rounds (hard cap 2
per the roadmap), early-terminating on explicit marker consensus
(unanimous APPROVE, or all-but-one with judge approval — Step 4.3b.8,
which retired the unreachable 0.85 score threshold). Returns a
:class:`DebateConsensusReport` carrying the patch candidate, per-role
verdicts, rounds used, per-call token usage for the FinOps ledger, and
wall-clock evidence for the Gate 2 duration target (<= 8 s).

FinOps wiring: the orchestrator passes through already max_tokens-capped
clients (Spark 12288 / Wonder 32768 per ADR-028, enforced by
``MetaClient``) and
records per-call usage snapshots. Mock clients expose no usage, which is
recorded honestly as zeros.

Step 4.3c.1 (R13): a synthesis emitting zero complete blocks is
retried exactly once with a grounded format-violation note
(``PatchSynthesis-retry`` usage); the second output wins as-is.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Protocol

from hackproof.debate.roles import ROLES, architectural_builder, consensus_judge

__all__ = [
    "DebateConsensusReport",
    "DebateOrchestrator",
    "TokenUsage",
]


class _DebateClient(Protocol):
    """Frozen client surface: MockMetaClient and MetaClient both satisfy it."""

    def debate_round(
        self, role: str, prompt: str, history: list[dict[str, Any]]
    ) -> str: ...

    def synthesize_patch(self, debate_consensus: str) -> str: ...


@dataclass(frozen=True)
class TokenUsage:
    """Per-call token accounting for one debate turn (FinOps ledger)."""

    role: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    finish_reason: str | None = None


@dataclass(frozen=True)
class DebateConsensusReport:
    """Outcome of one asymmetric debate run."""

    patch_candidate: str
    verdicts: dict[str, str]
    rounds: tuple[dict[str, str], ...]
    rounds_used: int
    converged: bool
    convergence_score: float
    token_usage: tuple[TokenUsage, ...] = ()
    total_prompt_tokens: int = 0
    total_completion_tokens: int = 0
    total_tokens: int = 0
    duration_ms: int = 0


def _synthesis_violation(patch_text: str, finish_reason: str | None) -> str:
    """Grounded format-violation note for the single synthesis retry.

    Step 4.3c.1 (R13): quotes machine counts from the failed output —
    never a guessed diagnosis — so the retry addresses the observed
    defect (empty response, missing markers, empty SEARCH/REPLACE).
    """
    search_hits = patch_text.count(architectural_builder.SEARCH_MARKER)
    sep_hits = patch_text.count(architectural_builder.SEPARATOR_MARKER)
    replace_hits = patch_text.count(architectural_builder.REPLACE_MARKER)
    finish = finish_reason or "unknown"
    return (
        "\n\nFORMAT VIOLATION IN YOUR PREVIOUS SYNTHESIS "
        "(ground truth, fix it): 0 complete SEARCH/REPLACE blocks "
        f"(chars={len(patch_text)}, SEARCHx{search_hits}, "
        f"separatorx{sep_hits}, REPLACEx{replace_hits}, "
        f"finish_reason={finish}). Output ONLY complete blocks, blocks "
        "FIRST, one repo-relative file path header per block, non-empty "
        "SEARCH and REPLACE."
    )


def _snapshot_usage(role: str, client: Any) -> TokenUsage:
    """Capture one usage record; clients without usage report zeros."""
    model = str(getattr(client, "model", "unknown"))
    last = getattr(client, "last_usage", None)
    if isinstance(last, dict):
        prompt_tokens = int(last.get("prompt_tokens", 0))
        completion_tokens = int(last.get("completion_tokens", 0))
        total_tokens = int(last.get("total_tokens", 0))
        finish_reason = last.get("finish_reason")
        finish_reason = str(finish_reason) if finish_reason else None
    else:
        prompt_tokens = completion_tokens = total_tokens = 0
        finish_reason = None
    return TokenUsage(
        role=role,
        model=model,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
        finish_reason=finish_reason,
    )


class DebateOrchestrator:
    """Six-role asymmetric debate loop with convergence gating."""

    def __init__(
        self,
        spark_client: _DebateClient,
        wonder_client: _DebateClient,
        max_rounds: int = 2,
    ) -> None:
        """Bind capped clients and the round cap.

Raises:
    ValueError: ``max_rounds < 1``.
        """
        if int(max_rounds) < 1:
            raise ValueError(f"max_rounds must be >= 1, got {max_rounds!r}")
        self._spark = spark_client
        self._wonder = wonder_client
        self._max_rounds = int(max_rounds)

    @property
    def max_rounds(self) -> int:
        """Hard cap on debate rounds."""
        return self._max_rounds

    def _client_for(self, role_module: Any) -> _DebateClient:
        """Route a role to its bound Spark/Wonder client."""
        if role_module.CLIENT_KIND == "wonder":
            return self._wonder
        return self._spark

    def run(
        self,
        issue_description: str,
        code_context: str,
        repo_structure: str = "",
        builder_code_context: str | None = None,
    ) -> DebateConsensusReport:
        """Execute the debate and return the consensus report.

        ``builder_code_context`` (Step 4.3b.3/R6) enriches ONLY the
        ArchitecturalBuilder turns (the role that writes SEARCH text)
        with the full top-1 file; every other role keeps the short
        head so wave token spend stays bounded. None preserves the
        legacy behavior (one context for all roles).
        """
        started = time.perf_counter()
        history: list[dict[str, Any]] = []
        usage: list[TokenUsage] = []
        rounds: list[dict[str, str]] = []
        patch_candidate = ""
        converged = False
        round_score = 0.0

        for round_no in range(1, self._max_rounds + 1):
            round_verdicts: dict[str, str] = {}
            for role_module in ROLES:
                client = self._client_for(role_module)
                role_context = code_context
                if (
                    role_module is architectural_builder
                    and builder_code_context is not None
                ):
                    role_context = builder_code_context
                ctx = {
                    "issue_description": issue_description,
                    "code_context": role_context,
                    "repo_structure": repo_structure,
                    "history": history,
                    "round": round_no,
                    "patch_candidate": patch_candidate,
                }
                verdict = role_module.run(client, ctx)
                usage.append(_snapshot_usage(role_module.ROLE_NAME, client))
                round_verdicts[role_module.ROLE_NAME] = verdict
                history.append(
                    {"role": role_module.ROLE_NAME, "text": verdict}
                )
                if role_module is architectural_builder:
                    patch_candidate = verdict
            round_score = consensus_judge.score(
                list(round_verdicts.values())
            )
            rounds.append(round_verdicts)
            if consensus_judge.consensus_reached(round_verdicts):
                converged = True
                break

        consensus_text = "\n\n".join(
            f"[{role}] {verdict}" for role, verdict in rounds[-1].items()
        )
        patch_candidate = self._wonder.synthesize_patch(consensus_text)
        first_usage = _snapshot_usage("PatchSynthesis", self._wonder)
        usage.append(first_usage)
        if not architectural_builder.extract_blocks(patch_candidate):
            retry_input = consensus_text + _synthesis_violation(
                patch_candidate, first_usage.finish_reason
            )
            patch_candidate = self._wonder.synthesize_patch(retry_input)
            usage.append(
                _snapshot_usage("PatchSynthesis-retry", self._wonder)
            )

        duration_ms = int((time.perf_counter() - started) * 1000)
        return DebateConsensusReport(
            patch_candidate=patch_candidate,
            verdicts=dict(rounds[-1]),
            rounds=tuple(rounds),
            rounds_used=len(rounds),
            converged=converged,
            convergence_score=round_score,
            token_usage=tuple(usage),
            total_prompt_tokens=sum(u.prompt_tokens for u in usage),
            total_completion_tokens=sum(u.completion_tokens for u in usage),
            total_tokens=sum(u.total_tokens for u in usage),
            duration_ms=duration_ms,
        )
