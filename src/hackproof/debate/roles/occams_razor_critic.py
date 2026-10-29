"""OccamsRazorCritic debate role (Step 2.2, Muse Spark 1.3).

Anti-overengineering gate: prunes builder output that balloons a minimal
guard clause into boilerplate. The verdict is LLM-produced, but the
underlying measurement (added-line budget + branching heuristic) is a
deterministic local computation so the gate cannot be reward-hacked by
a persuasive-sounding verdict.

``ROLE_SYSTEM_PROMPT`` is transmitted as a prompt prefix (not a transport
system override) because ``MetaClient`` pins byte-identical system
constants for prompt caching (FinOps, roadmap section 4).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from hackproof.debate.roles.architectural_builder import (
    REPLACE_MARKER,
    SEARCH_MARKER,
    SEPARATOR_MARKER,
)

ROLE_NAME = "OccamsRazorCritic"
CLIENT_KIND = "spark"
ROLE_SYSTEM_PROMPT = (
    "You are OccamsRazorCritic, a System One simplicity enforcer. Given the "
    "patch and its measured size/complexity, reply with APPROVE if the fix "
    "is minimal, or REVISE with the exact lines to delete. End your verdict "
    "with exactly one marker word: APPROVE or REVISE."
)
LINE_BUDGET_MULTIPLIER = 3
_BRANCH_KEYWORDS = frozenset(
    {
        "if ",
        "elif ",
        "else:",
        "else ",
        "for ",
        "while ",
        " and ",
        " or ",
        "except",
        "case ",
        "catch",
        "&&",
        "||",
        "? ",
        " ?",
    }
)

__all__ = [
    "ROLE_NAME",
    "CLIENT_KIND",
    "ROLE_SYSTEM_PROMPT",
    "LINE_BUDGET_MULTIPLIER",
    "OccamAssessment",
    "run",
    "assess_patch",
]


class _Debater(Protocol):
    """Frozen client surface: MockMetaClient and MetaClient both satisfy it."""

    def debate_round(
        self, role: str, prompt: str, history: list[dict[str, Any]]
    ) -> str: ...


@dataclass(frozen=True)
class OccamAssessment:
    """Deterministic size/complexity measurement of a patch candidate."""

    added_lines: int
    branching_points: int
    over_budget: bool


def _added_lines(patch_text: str) -> list[str]:
    """Collect non-empty added lines from REPLACE sections (or whole text)."""
    if SEARCH_MARKER in patch_text and REPLACE_MARKER in patch_text:
        collected: list[str] = []
        cursor = 0
        while True:
            start = patch_text.find(SEPARATOR_MARKER, cursor)
            if start == -1:
                break
            end = patch_text.find(REPLACE_MARKER, start)
            if end == -1:
                break
            section = patch_text[start + len(SEPARATOR_MARKER) : end]
            collected.extend(
                line for line in section.splitlines() if line.strip()
            )
            cursor = end + len(REPLACE_MARKER)
        return collected
    return [line for line in patch_text.splitlines() if line.strip()]


def assess_patch(patch_text: str, minimal_added_lines: int) -> OccamAssessment:
    """Measure a patch against the minimal-diff line budget.

Args:
    patch_text: Builder output (SEARCH/REPLACE blocks or plain diff text).
    minimal_added_lines: Expected added-line count of the minimal fix.

The patch is over budget when its added lines exceed
``LINE_BUDGET_MULTIPLIER`` times the minimal count.
"""
    added = _added_lines(patch_text)
    branching = sum(
        1
        for line in added
        for keyword in _BRANCH_KEYWORDS
        if keyword in line
    )
    budget = LINE_BUDGET_MULTIPLIER * max(int(minimal_added_lines), 1)
    return OccamAssessment(
        added_lines=len(added),
        branching_points=branching,
        over_budget=len(added) > budget,
    )


def run(client: _Debater, ctx: dict[str, Any]) -> str:
    """Critique the current patch candidate via one debate turn."""
    patch = str(ctx.get("patch_candidate", ""))
    minimal = int(ctx.get("minimal_added_lines", 2))
    assessment = assess_patch(patch, minimal)
    prompt = (
        f"{ROLE_SYSTEM_PROMPT}\n\nPATCH:\n{patch or '(no patch yet)'}\n\n"
        f"MEASURED: added_lines={assessment.added_lines} "
        f"branching_points={assessment.branching_points} "
        f"over_budget={assessment.over_budget}\n\n"
        f"ROUND: {int(ctx.get('round', 1))}"
    )
    return client.debate_round(ROLE_NAME, prompt, list(ctx.get("history", [])))
