"""Asymmetric debate orchestrator (Step 2.2, Spark 1.3 + Wonder 1.0).

Runs the fixed 6-role loop for at most ``max_rounds`` rounds (hard cap 2
per the roadmap), early-terminating when the deterministic consensus
score reaches ``convergence_threshold``. Returns a
:class:`DebateConsensusReport` carrying the patch candidate, per-role
verdicts, rounds used, per-call token usage for the FinOps ledger, and
wall-clock evidence for the Gate 2 duration target (<= 8 s).

FinOps wiring: the orchestrator passes through already max_tokens-capped
clients (Spark 600 / Wonder 1500, enforced by ``MetaClient``) and
records per-call usage snapshots. Mock clients expose no usage, which is
recorded honestly as zeros.
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


def _snapshot_usage(role: str, client: Any) -> TokenUsage:
    """Capture one usage record; clients without usage report zeros."""
    model = str(getattr(client, "model", "unknown"))
    last = getattr(client, "last_usage", None)
    if isinstance(last, dict):
        prompt_tokens = int(last.get("prompt_tokens", 0))
        completion_tokens = int(last.get("completion_tokens", 0))
        total_tokens = int(last.get("total_tokens", 0))
    else:
        prompt_tokens = completion_tokens = total_tokens = 0
    return TokenUsage(
        role=role,
        model=model,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
    )


class DebateOrchestrator:
    """Six-role asymmetric debate loop with convergence gating."""

    def __init__(
        self,
        spark_client: _DebateClient,
        wonder_client: _DebateClient,
        max_rounds: int = 2,
        convergence_threshold: float = consensus_judge.CONVERGENCE_THRESHOLD,
    ) -> None:
        """Bind capped clients and convergence bounds.

Raises:
    ValueError: ``max_rounds < 1`` or threshold outside (0.0, 1.0].
        """
        if int(max_rounds) < 1:
            raise ValueError(f"max_rounds must be >= 1, got {max_rounds!r}")
        threshold = float(convergence_threshold)
        if not 0.0 < threshold <= 1.0:
            raise ValueError(
                f"convergence_threshold must be in (0.0, 1.0], "
                f"got {convergence_threshold!r}"
            )
        self._spark = spark_client
        self._wonder = wonder_client
        self._max_rounds = int(max_rounds)
        self._threshold = threshold

    @property
    def max_rounds(self) -> int:
        """Hard cap on debate rounds."""
        return self._max_rounds

    @property
    def convergence_threshold(self) -> float:
        """Consensus score closing the debate early."""
        return self._threshold

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
    ) -> DebateConsensusReport:
        """Execute the debate and return the consensus report."""
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
                ctx = {
                    "issue_description": issue_description,
                    "code_context": code_context,
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
            if round_score >= self._threshold:
                converged = True
                break

        consensus_text = "\n\n".join(
            f"[{role}] {verdict}" for role, verdict in rounds[-1].items()
        )
        patch_candidate = self._wonder.synthesize_patch(consensus_text)
        usage.append(_snapshot_usage("PatchSynthesis", self._wonder))

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
