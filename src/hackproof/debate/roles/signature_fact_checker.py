"""SignatureFactChecker debate role (Step 2.2, Muse Spark 1.3).

Hallucination firewall: verifies function names and import paths named in
the patch against the localized code context (Contriever/Faiss hits).
Unknown references are computed deterministically and embedded into the
debate prompt so the verdict is grounded in measured evidence.

``ROLE_SYSTEM_PROMPT`` is transmitted as a prompt prefix (not a transport
system override) because ``MetaClient`` pins byte-identical system
constants for prompt caching (FinOps, roadmap section 4).
"""

from __future__ import annotations

import re
from typing import Any, Protocol

ROLE_NAME = "SignatureFactChecker"
CLIENT_KIND = "spark"
ROLE_SYSTEM_PROMPT = (
    "You are SignatureFactChecker, a System One API hallucination firewall. "
    "Given the patch and the measured unknown-reference list, reply with "
    "APPROVE when every referenced name resolves, or REVISE naming the "
    "hallucinated symbols. End your verdict with exactly one marker word: "
    "APPROVE or REVISE."
)
_DEF_RE = re.compile(r"^\s*def\s+([A-Za-z_]\w*)\s*\(", re.MULTILINE)
_FROM_IMPORT_RE = re.compile(r"^\s*from\s+([\w.]+)\s+import\s+", re.MULTILINE)
_PLAIN_IMPORT_RE = re.compile(r"^\s*import\s+([\w., \t]+)", re.MULTILINE)

__all__ = [
    "ROLE_NAME",
    "CLIENT_KIND",
    "ROLE_SYSTEM_PROMPT",
    "run",
    "check_references",
]


class _Debater(Protocol):
    """Frozen client surface: MockMetaClient and MetaClient both satisfy it."""

    def debate_round(
        self, role: str, prompt: str, history: list[dict[str, Any]]
    ) -> str: ...


def check_references(patch_text: str, code_context: str) -> list[str]:
    """Return patch-referenced names missing from the code context.

Collects ``def`` names and imported module paths from the patch and
reports the sorted names with no substring occurrence in the localized
code context. An empty list means every reference resolves.
"""
    referenced: set[str] = set(_DEF_RE.findall(patch_text))
    referenced.update(_FROM_IMPORT_RE.findall(patch_text))
    for group in _PLAIN_IMPORT_RE.findall(patch_text):
        for chunk in group.split(","):
            name = chunk.strip().split(" as ")[0].strip()
            if name:
                referenced.add(name)
    return sorted(name for name in referenced if name not in code_context)


def run(client: _Debater, ctx: dict[str, Any]) -> str:
    """Fact-check the current patch candidate via one debate turn."""
    patch = str(ctx.get("patch_candidate", ""))
    code = str(ctx.get("code_context", ""))
    unknown = check_references(patch, code)
    prompt = (
        f"{ROLE_SYSTEM_PROMPT}\n\nPATCH:\n{patch or '(no patch yet)'}\n\n"
        f"UNKNOWN_REFERENCES: {unknown if unknown else '[]'}\n\n"
        f"ROUND: {int(ctx.get('round', 1))}"
    )
    return client.debate_round(ROLE_NAME, prompt, list(ctx.get("history", [])))
