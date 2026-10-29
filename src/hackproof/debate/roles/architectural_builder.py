"""ArchitecturalBuilder debate role (Step 2.2, Muse Wonder 1.0, Active Focus).

Synthesizes the targeted fix strictly as atomic SEARCH/REPLACE blocks,
consumed downstream by the deterministic WorkspacePatcher (Step 2.3).

``ROLE_SYSTEM_PROMPT`` is transmitted as a prompt prefix (not a transport
system override) because ``MetaClient`` pins byte-identical system
constants for prompt caching (FinOps, roadmap section 4).
"""

from __future__ import annotations

from typing import Any, Protocol

ROLE_NAME = "ArchitecturalBuilder"
CLIENT_KIND = "wonder"
ROLE_SYSTEM_PROMPT = (
    "You are ArchitecturalBuilder, a System Two patch synthesizer in Active "
    "Focus mode. Output ONLY exact SEARCH/REPLACE blocks "
    "(<<<<<<< SEARCH ... ======= ... >>>>>>> REPLACE), one per hunk, no "
    "line numbers, no prose, no markdown fences."
)
SEARCH_MARKER = "<<<<<<< SEARCH"
SEPARATOR_MARKER = "======="
REPLACE_MARKER = ">>>>>>> REPLACE"

__all__ = [
    "ROLE_NAME",
    "CLIENT_KIND",
    "ROLE_SYSTEM_PROMPT",
    "SEARCH_MARKER",
    "SEPARATOR_MARKER",
    "REPLACE_MARKER",
    "run",
    "extract_blocks",
]


class _Debater(Protocol):
    """Frozen client surface: MockMetaClient and MetaClient both satisfy it."""

    def debate_round(
        self, role: str, prompt: str, history: list[dict[str, Any]]
    ) -> str: ...


def run(client: _Debater, ctx: dict[str, Any]) -> str:
    """Synthesize SEARCH/REPLACE blocks for the issue via one debate turn."""
    issue = str(ctx.get("issue_description", ""))
    code = str(ctx.get("code_context", ""))
    prompt = (
        f"{ROLE_SYSTEM_PROMPT}\n\nISSUE:\n{issue}\n\nCODE CONTEXT:\n{code}\n\n"
        f"ROUND: {int(ctx.get('round', 1))}"
    )
    return client.debate_round(ROLE_NAME, prompt, list(ctx.get("history", [])))


def extract_blocks(patch_text: str) -> list[tuple[str, str]]:
    """Parse complete SEARCH/REPLACE blocks into (search, replace) pairs.

Only structurally complete blocks (SEARCH marker, exactly one separator,
REPLACE marker) are returned; trailing incomplete fragments are ignored
so the WorkspacePatcher never receives a partial hunk.
"""
    blocks: list[tuple[str, str]] = []
    cursor = 0
    while True:
        start = patch_text.find(SEARCH_MARKER, cursor)
        if start == -1:
            return blocks
        end = patch_text.find(REPLACE_MARKER, start)
        if end == -1:
            return blocks
        body = patch_text[start + len(SEARCH_MARKER) : end]
        parts = body.split(SEPARATOR_MARKER)
        if len(parts) == 2:
            search, replace = parts[0].strip("\n"), parts[1].strip("\n")
            if search.strip() and replace.strip():
                blocks.append((search, replace))
        cursor = end + len(REPLACE_MARKER)
