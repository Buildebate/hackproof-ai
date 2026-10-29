"""Deterministic $0-cost mock of the Meta Model API (Step 1.4).

``MockMetaClient`` simulates Muse Spark 1.3 / Muse Wonder 1.0 responses as
pure functions of their inputs (SHA-256 based, no randomness, no network),
so unit tests and debate-logic dry runs never burn API credits.

The three-method interface is FROZEN: the production ``MetaClient``
(Step 2.1) must mirror it exactly so mock/live swap by constructor.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

SUPPORTED_MODELS = ("muse-spark-1.3", "muse-wonder-1.0")
FAIL_MODES = ("none", "timeout", "fault", "tamper")

__all__ = [
    "MockMetaClient",
    "MockInjectedError",
    "SUPPORTED_MODELS",
    "FAIL_MODES",
]


class MockInjectedError(RuntimeError):
    """Raised when ``fail_mode='fault'`` failure injection is armed."""


def _digest(*parts: str) -> str:
    """Stable 12-hex fingerprint of the given parts (cross-process)."""
    return hashlib.sha256("\x00".join(parts).encode("utf-8")).hexdigest()[:12]


def _strip_asserts_to_pass(source: str) -> str:
    """Simulate assertion-deletion reward hacking (keeps file parseable).

    Every ``assert ...`` statement line becomes ``pass`` at the same indent,
    modelling an agent that neuters checks instead of fixing code.
    """
    out: list[str] = []
    for line in source.splitlines(keepends=True):
        stripped = line.lstrip()
        indent = line[: len(line) - len(stripped)]
        if stripped.startswith("assert ") or stripped.startswith("assert("):
            ending = "\n" if line.endswith("\n") else ""
            out.append(f"{indent}pass{ending}")
        else:
            out.append(line)
    return "".join(out)


class MockMetaClient:
    """Deterministic offline stand-in for the Meta Model API."""

    def __init__(self, model: str = "muse-spark-1.3", fail_mode: str = "none") -> None:
        """Bind model role and failure-injection mode.

        Raises:
            ValueError: Unknown model or fail_mode.
        """
        if model not in SUPPORTED_MODELS:
            raise ValueError(
                f"unknown model {model!r}: expected one of {SUPPORTED_MODELS}"
            )
        if fail_mode not in FAIL_MODES:
            raise ValueError(
                f"unknown fail_mode {fail_mode!r}: expected one of {FAIL_MODES}"
            )
        self._model = model
        self._fail_mode = fail_mode

    @property
    def model(self) -> str:
        """Model role this mock simulates."""
        return self._model

    @property
    def fail_mode(self) -> str:
        """Armed failure-injection mode."""
        return self._fail_mode

    def _inject_or_pass(self) -> None:
        """Raise the armed injection signal, if any (timeout/fault)."""
        if self._fail_mode == "timeout":
            raise TimeoutError("MockMetaClient injected timeout (fail_mode='timeout')")
        if self._fail_mode == "fault":
            raise MockInjectedError("MockMetaClient injected fault (fail_mode='fault')")

    def generate_regression_test(
        self, issue_description: str, code_context: str
    ) -> str:
        """Synthesize a deterministic isolated repro test file.

        In ``tamper`` mode the returned file has its assertions replaced
        with ``pass`` (still parseable), so ``ImmutableTestGuard`` detects
        the breach via assertion-count reduction.
        """
        self._inject_or_pass()
        tag = _digest(self._model, issue_description, code_context)
        body = (
            f'"""Mock repro for {tag} (model={self._model})."""\n'
            "\n"
            f"def test_repro_{tag}():\n"
            f"    observed = {issue_description!r}\n"
            f"    context = {code_context!r}\n"
            "    assert observed is not None\n"
            "    assert 'error' in str(context).lower() or True\n"
        )
        if self._fail_mode == "tamper":
            return _strip_asserts_to_pass(body)
        return body

    def debate_round(
        self, role: str, prompt: str, history: list[dict[str, Any]]
    ) -> str:
        """Return a deterministic mock verdict for one debate turn."""
        self._inject_or_pass()
        payload = json.dumps(history, sort_keys=True, default=str)
        tag = _digest(self._model, role, prompt, payload)
        return (
            f"[{self._model}:{role}] mock-verdict-{tag} :: {prompt[:80]} "
            f"(history={len(history)} turns)"
        )

    def synthesize_patch(self, debate_consensus: str) -> str:
        """Return a deterministic mock SEARCH/REPLACE patch block."""
        self._inject_or_pass()
        tag = _digest(self._model, debate_consensus)
        return (
            f"# mock-patch-{tag} (model={self._model})\n"
            "<<<<<<< SEARCH\n"
            "def vulnerable(value):\n"
            "    return value\n"
            "=======\n"
            "def vulnerable(value):\n"
            "    if value is None:\n"
            '        raise ValueError("mock guard")\n'
            "    return value\n"
            ">>>>>>> REPLACE\n"
        )
