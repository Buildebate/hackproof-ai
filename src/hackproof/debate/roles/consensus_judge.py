"""ConsensusJudge debate role (Step 2.2, Muse Spark 1.3).

Convergence gate: prevents infinite argumentation by scoring each round.
The score is a deterministic function of per-role verdict markers
(``APPROVE`` / ``REVISE`` / ``REJECT``), so convergence can never be
faked by a verbose but non-committal verdict. The debate closes when
``score >= 0.85`` (equivalent to divergence ``D < 0.15``).

``ROLE_SYSTEM_PROMPT`` is transmitted as a prompt prefix (not a transport
system override) because ``MetaClient`` pins byte-identical system
constants for prompt caching (FinOps, roadmap section 4).
"""

from __future__ import annotations

from typing import Any, Protocol, Sequence

ROLE_NAME = "ConsensusJudge"
CLIENT_KIND = "spark"
ROLE_SYSTEM_PROMPT = (
    "You are ConsensusJudge, a System One convergence gate. Review the "
    "round verdicts and reply with one line: CONVERGED when every role "
    "approved, otherwise DIVERGED with the blocking role name. End your "
    "verdict with exactly one marker word: APPROVE or REVISE."
)
CONVERGENCE_THRESHOLD = 0.85
APPROVE_MARKER = "APPROVE"
REVISE_MARKERS = ("REVISE", "REJECT")

__all__ = [
    "ROLE_NAME",
    "CLIENT_KIND",
    "ROLE_SYSTEM_PROMPT",
    "CONVERGENCE_THRESHOLD",
    "APPROVE_MARKER",
    "REVISE_MARKERS",
    "run",
    "score",
]


class _Debater(Protocol):
    """Frozen client surface: MockMetaClient and MetaClient both satisfy it."""

    def debate_round(
        self, role: str, prompt: str, history: list[dict[str, Any]]
    ) -> str: ...


def run(client: _Debater, ctx: dict[str, Any]) -> str:
    """Judge round convergence via one debate turn."""
    prompt = (
        f"{ROLE_SYSTEM_PROMPT}\n\nROUND: {int(ctx.get('round', 1))}\n"
        "Judge the verdicts accumulated in the debate history."
    )
    return client.debate_round(ROLE_NAME, prompt, list(ctx.get("history", [])))


def _verdict_value(verdict: str) -> float:
    """Map one verdict to 1.0 (approve), 0.0 (revise) or 0.5 (neutral)."""
    upper = verdict.upper()
    if APPROVE_MARKER in upper:
        return 1.0
    if any(marker in upper for marker in REVISE_MARKERS):
        return 0.0
    return 0.5


def score(verdicts: Sequence[str]) -> float:
    """Compute the deterministic convergence score in [0.0, 1.0].

The score is the mean of per-verdict values; an empty round scores 0.0
(never converged).
"""
    values = [_verdict_value(v) for v in verdicts]
    if not values:
        return 0.0
    return sum(values) / len(values)
