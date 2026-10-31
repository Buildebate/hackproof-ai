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
    "Focus mode. Output exact SEARCH/REPLACE blocks "
    "(<<<<<<< SEARCH ... ======= ... >>>>>>> REPLACE), one per hunk, no "
    "line numbers, no prose, no markdown fences. Prefix each block with "
    "its repo-relative target file path on its own line. Copy every "
    "SEARCH block byte-for-byte from the shown CODE CONTEXT (exact "
    "indentation and punctuation); never reconstruct code from memory. "
    "After the blocks, end your verdict with exactly one marker word on "
    "its own final line: APPROVE if the patch is complete, or REVISE if "
    "the context is insufficient to write one."
)
ROUTED_PATH_SUFFIXES = (".py", ".ts", ".tsx", ".js", ".jsx", ".mts", ".cts")
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
    "extract_routed_blocks",
    "revise_patch",
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


def revise_patch(
    client: _Debater,
    *,
    issue_description: str,
    code_context: str,
    previous_patch: str,
    failure_report: str,
) -> str:
    """Single-retry patch revision after a deterministic apply failure.

    Step 4.3b.3 (R6): exactly one extra builder turn. ``failure_report``
    is the patcher error verbatim — it already carries the closest
    real region (or a did-you-mean path), so the model revises against
    ground truth instead of guessing again. Callers must invoke this
    at most once per patch and drain the extra usage into the ledger.
    """
    prompt = (
        f"{ROLE_SYSTEM_PROMPT}\n\nISSUE:\n{issue_description}\n\n"
        f"CODE CONTEXT:\n{code_context}\n\n"
        "YOUR PREVIOUS PATCH FAILED TO APPLY. Do not explain; output "
        "ONLY corrected SEARCH/REPLACE blocks (same format, file path "
        "headers, byte-for-byte SEARCH from the context/region below).\n\n"
        f"FAILURE (patcher verdict, ground truth attached):\n{failure_report}\n\n"
        f"PREVIOUS PATCH:\n{previous_patch}"
    )
    return client.debate_round(ROLE_NAME, prompt, [])


def _iter_block_spans(patch_text: str) -> list[tuple[str, str, str]]:
    """Yield (gap, search, replace) for each structurally complete block.

    ``gap`` is the raw text between the previous REPLACE marker (or the
    start of input) and this block's SEARCH marker; callers mine it for
    routing headers. Incomplete trailing fragments are ignored.
    """
    spans: list[tuple[str, str, str]] = []
    cursor = 0
    gap_start = 0
    while True:
        start = patch_text.find(SEARCH_MARKER, cursor)
        if start == -1:
            return spans
        end = patch_text.find(REPLACE_MARKER, start)
        if end == -1:
            return spans
        body = patch_text[start + len(SEARCH_MARKER) : end]
        parts = body.split(SEPARATOR_MARKER)
        if len(parts) == 2:
            search, replace = parts[0].strip("\n"), parts[1].strip("\n")
            if search.strip() and replace.strip():
                spans.append((patch_text[gap_start:start], search, replace))
        cursor = end + len(REPLACE_MARKER)
        gap_start = cursor
    return spans


def _header_of_gap(gap: str) -> str | None:
    """Target path from inter-block text, or None when absent.

    A header is the last non-empty gap line iff it is a single token
    carrying ``/`` or a source suffix (``src/foo.py``); prose, bare
    words (``Fix:``) and marker fragments never route a block. A
    leading ``a/`` (unified-diff style) is stripped.
    """
    lines = [line.strip() for line in gap.splitlines() if line.strip()]
    if not lines:
        return None
    candidate = lines[-1]
    if any(ch.isspace() for ch in candidate):
        return None
    if SEARCH_MARKER in candidate or SEPARATOR_MARKER in candidate or REPLACE_MARKER in candidate:
        return None
    if "/" not in candidate and not candidate.endswith(ROUTED_PATH_SUFFIXES):
        return None
    while candidate.startswith("./"):
        candidate = candidate[2:]
    candidate = candidate.lstrip("/")
    # NOTE: ".." segments are NOT normalized here; the patcher rejects
    # them explicitly (silently rewriting traversal would mask attacks).
    return candidate or None


def extract_blocks(patch_text: str) -> list[tuple[str, str]]:
    """Parse complete SEARCH/REPLACE blocks into (search, replace) pairs.

Only structurally complete blocks (SEARCH marker, exactly one separator,
REPLACE marker) are returned; trailing incomplete fragments are ignored
so the WorkspacePatcher never receives a partial hunk.
"""
    return [(search, replace) for _, search, replace in _iter_block_spans(patch_text)]


def extract_routed_blocks(
    patch_text: str,
) -> list[tuple[str | None, str, str]]:
    """Parse blocks into (file, search, replace) triples.

    ``file`` is the header path when the block carries one, else None
    (legacy headerless block, routed by unique match downstream).
    """
    return [
        (_header_of_gap(gap), search, replace)
        for gap, search, replace in _iter_block_spans(patch_text)
    ]
