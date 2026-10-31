"""ConsensusJudge debate role (Step 2.2, Muse Spark 1.3).

Convergence gate: prevents infinite argumentation by counting explicit
per-role verdict markers. Step 4.3b.8 (R11) replaced the 0.85 score
threshold (unreachable in practice — observed rounds scored 0.33-0.83
under substring matching, always burning both rounds) with an explicit
rule: the debate closes on unanimous APPROVE, or on all-but-one
APPROVE when the judge approves (an informed override of a lone
dissenter the judge has already seen).

Marker discipline (enforced by :func:`verdict_marker`): a marker
counts ONLY as the verdict's last whitespace-delimited token
(``APPROVE`` mid-text is prose, not a verdict), and negated markers
(``DO NOT APPROVE`` and kin) never count as approval.

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
APPROVE_MARKER = "APPROVE"
REVISE_MARKERS = ("REVISE", "REJECT")
_TRAILING_STRIP = " \t\r\n.!?;:'\""
_APPROVE_NEGATIONS = (
    "NOT APPROVE",
    "N'T APPROVE",
    "DONT APPROVE",
    "NEVER APPROVE",
    "CANNOT APPROVE",
    "CAN'T APPROVE",
    "CAN NOT APPROVE",
)
_REVISE_NEGATIONS = (
    "NOT REVISE",
    "N'T REVISE",
    "NOT REJECT",
    "N'T REJECT",
)

__all__ = [
    "ROLE_NAME",
    "CLIENT_KIND",
    "ROLE_SYSTEM_PROMPT",
    "APPROVE_MARKER",
    "REVISE_MARKERS",
    "run",
    "score",
    "verdict_marker",
    "approves",
    "consensus_reached",
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


def _normalized(verdict: str) -> str:
    """Upper-cased verdict with trailing whitespace/punctuation gone."""
    return verdict.strip().upper().rstrip(_TRAILING_STRIP).strip()


def _is_negated_approve(verdict: str) -> bool:
    """True when the verdict ends by explicitly withholding approval."""
    return _normalized(verdict).endswith(_APPROVE_NEGATIONS)


def verdict_marker(verdict: str) -> str | None:
    """End-position marker: ``APPROVE``, ``REVISE``, ``REJECT`` or None.

    Only the verdict's last whitespace-delimited token counts (after
    stripping trailing punctuation and wrapping quotes/brackets), so
    ``APPROVED``, ``DISAPPROVE`` and mid-text mentions never match.
    Negated markers (``DO NOT APPROVE`` and kin) never count.
    """
    text = _normalized(verdict)
    if not text:
        return None
    if text.endswith(_APPROVE_NEGATIONS):
        return None
    if text.endswith(_REVISE_NEGATIONS):
        return None
    token = text.rsplit(None, 1)[-1].strip("\"'()[]")
    if token == APPROVE_MARKER:
        return APPROVE_MARKER
    if token in REVISE_MARKERS:
        return token
    return None


def approves(verdict: str) -> bool:
    """True when the verdict ends with an unnegated APPROVE marker."""
    return verdict_marker(verdict) == APPROVE_MARKER


def _verdict_value(verdict: str) -> float:
    """Map one verdict to 1.0 (approve), 0.0 (revise) or 0.5 (neutral).

    An explicitly withheld approval (``DO NOT APPROVE``) is dissent
    (0.0), not abstention: it blocks consensus like a REVISE.
    """
    marker = verdict_marker(verdict)
    if marker == APPROVE_MARKER:
        return 1.0
    if marker in REVISE_MARKERS:
        return 0.0
    if _is_negated_approve(verdict):
        return 0.0
    return 0.5


def score(verdicts: Sequence[str]) -> float:
    """Compute the deterministic convergence score in [0.0, 1.0].

The score is the mean of per-verdict values; an empty round scores 0.0
(never converged). Reporting only since Step 4.3b.8 — the exit
decision is :func:`consensus_reached`.
"""
    values = [_verdict_value(v) for v in verdicts]
    if not values:
        return 0.0
    return sum(values) / len(values)


def consensus_reached(round_verdicts: dict[str, str]) -> bool:
    """True on unanimous APPROVE, or all-but-one with judge approval.

    Step 4.3b.8 (R11) exit rule: 6/6 closes immediately; 5/6 closes
    only when the dissenter is overruled by an approving judge (the
    judge runs last and has seen the dissent). Empty rounds never
    reach consensus.
    """
    total = len(round_verdicts)
    if total == 0:
        return False
    approvals = {
        name for name, verdict in round_verdicts.items()
        if approves(verdict)
    }
    if len(approvals) == total:
        return True
    return len(approvals) == total - 1 and ROLE_NAME in approvals
