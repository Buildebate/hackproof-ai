"""EdgeScout debate role (Step 2.2, Muse Spark 1.3).

High-speed hostile boundary-condition generator: forces the
ArchitecturalBuilder to handle edge failures from line one.

``ROLE_SYSTEM_PROMPT`` is transmitted as a prompt prefix (not a transport
system override) because ``MetaClient`` pins byte-identical system
constants for prompt caching (FinOps, roadmap section 4). Live callers
that want a dedicated transport may alternatively construct
``MetaClient(model, system_prompt=ROLE_SYSTEM_PROMPT)`` per role.
"""

from __future__ import annotations

from typing import Any, Protocol

ROLE_NAME = "EdgeScout"
CLIENT_KIND = "spark"
ROLE_SYSTEM_PROMPT = (
    "You are EdgeScout, a hostile System One boundary-condition fuzzer. "
    "Output ONLY a numbered list of 5-7 concrete edge cases for the given "
    "issue (None/null, empty collections, type mismatches, off-by-one, "
    "unicode/empty-string, race conditions). No explanations, no prose."
)

__all__ = ["ROLE_NAME", "CLIENT_KIND", "ROLE_SYSTEM_PROMPT", "run", "parse_cases"]


class _Debater(Protocol):
    """Frozen client surface: MockMetaClient and MetaClient both satisfy it."""

    def debate_round(
        self, role: str, prompt: str, history: list[dict[str, Any]]
    ) -> str: ...


def run(client: _Debater, ctx: dict[str, Any]) -> str:
    """Generate hostile edge cases for the issue via one debate turn."""
    issue = str(ctx.get("issue_description", ""))
    code = str(ctx.get("code_context", ""))
    prompt = (
        f"{ROLE_SYSTEM_PROMPT}\n\nISSUE:\n{issue}\n\nCODE CONTEXT:\n{code}\n\n"
        f"ROUND: {int(ctx.get('round', 1))}"
    )
    return client.debate_round(ROLE_NAME, prompt, list(ctx.get("history", [])))


def parse_cases(verdict: str) -> list[str]:
    """Extract case lines (numbered or bulleted) from a scout verdict."""
    cases: list[str] = []
    for raw in verdict.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line[:2] in ("- ", "* ", "\u2022 "):
            cases.append(line[2:].strip())
            continue
        head, dot, rest = line.partition(".")
        if dot and head.strip().isdigit() and rest.strip():
            cases.append(rest.strip())
            continue
        head, paren, rest = line.partition(")")
        if paren and head.strip().isdigit() and rest.strip():
            cases.append(rest.strip())
    return [c for c in cases if c]
