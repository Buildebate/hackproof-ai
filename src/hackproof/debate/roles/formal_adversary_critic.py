"""FormalAdversaryCritic debate role (Step 2.2, Muse Wonder 1.0, Active Memory).

Deep semantic auditor: hunts hidden regressions and architectural
invariant violations while keeping repository invariants in context.

``ROLE_SYSTEM_PROMPT`` is transmitted as a prompt prefix (not a transport
system override) because ``MetaClient`` pins byte-identical system
constants for prompt caching (FinOps, roadmap section 4).
"""

from __future__ import annotations

from typing import Any, Protocol

ROLE_NAME = "FormalAdversaryCritic"
CLIENT_KIND = "wonder"
ROLE_SYSTEM_PROMPT = (
    "You are FormalAdversaryCritic, a System Two semantic auditor in Active "
    "Memory mode. Audit the patch against the repo invariants for hidden "
    "regressions and side effects. Output a bulleted finding list, then end "
    "with exactly one marker word: APPROVE or REVISE."
)

__all__ = [
    "ROLE_NAME",
    "CLIENT_KIND",
    "ROLE_SYSTEM_PROMPT",
    "run",
    "parse_findings",
]


class _Debater(Protocol):
    """Frozen client surface: MockMetaClient and MetaClient both satisfy it."""

    def debate_round(
        self, role: str, prompt: str, history: list[dict[str, Any]]
    ) -> str: ...


def run(client: _Debater, ctx: dict[str, Any]) -> str:
    """Audit the current patch candidate via one debate turn."""
    patch = str(ctx.get("patch_candidate", ""))
    invariants = str(ctx.get("repo_structure", ""))
    prompt = (
        f"{ROLE_SYSTEM_PROMPT}\n\nPATCH:\n{patch or '(no patch yet)'}\n\n"
        f"REPO_INVARIANTS:\n{invariants or '(none provided)'}\n\n"
        f"ROUND: {int(ctx.get('round', 1))}"
    )
    return client.debate_round(ROLE_NAME, prompt, list(ctx.get("history", [])))


def parse_findings(verdict: str) -> list[str]:
    """Extract finding lines (numbered or bulleted) from an audit verdict."""
    findings: list[str] = []
    for raw in verdict.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line[:2] in ("- ", "* ", "\u2022 "):
            findings.append(line[2:].strip())
            continue
        head, dot, rest = line.partition(".")
        if dot and head.strip().isdigit() and rest.strip():
            findings.append(rest.strip())
            continue
        head, paren, rest = line.partition(")")
        if paren and head.strip().isdigit() and rest.strip():
            findings.append(rest.strip())
    return [f for f in findings if f]
