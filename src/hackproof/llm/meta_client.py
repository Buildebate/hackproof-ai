"""Production Meta Model API client (Step 2.1, System One/Two transport).

Speaks the OpenAI-compatible ``POST {base_url}/chat/completions`` contract
over stdlib ``urllib`` (zero new dependencies). The three-method interface
mirrors :class:`MockMetaClient` exactly so mock/live swap by constructor.

Security & FinOps rules (hard):
* Credentials come ONLY from ``META_BASE_URL`` / ``META_API_KEY`` env vars
  (constructor args override for tests). The key is never logged, never
  stored in objects, never embedded in exceptions (Zero-Leak).
* Per-model ``max_tokens`` caps are enforced at construction AND at request
  build time: Spark 600 / Wonder 1500. Any attempt to exceed raises.
* System prompts are byte-identical module constants (prompt caching).
  Debate roles (Step 2.2) may only substitute other module constants.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any, Callable

DEFAULT_BASE_URL = "https://api.meta.ai/v1"
DEFAULT_TIMEOUT_SEC = 60.0
DEFAULT_TEMPERATURE = 0.0

MODEL_MAX_TOKENS = {
    "muse-spark-1.3": 600,
    "muse-wonder-1.0": 1500,
}

SPARK_SYSTEM_PROMPT = (
    "You are Muse Spark, a fast System One coding guardrail. "
    "Answer with the minimal correct artifact only. No explanations, "
    "no markdown fences unless the artifact itself requires them."
)

WONDER_SYSTEM_PROMPT = (
    "You are Muse Wonder, a deep System Two reasoning engineer. "
    "Produce minimal, architecture-accurate artifacts. Prefer the smallest "
    "diff that fixes the issue. No explanations outside the artifact."
)

_MODEL_SYSTEM_PROMPTS = {
    "muse-spark-1.3": SPARK_SYSTEM_PROMPT,
    "muse-wonder-1.0": WONDER_SYSTEM_PROMPT,
}

_TRANSIENT_STATUSES = frozenset({408, 429, 500, 502, 503, 504})

__all__ = [
    "MetaClient",
    "MetaAPIError",
    "MetaAuthError",
    "MetaRateLimitError",
    "MetaTimeoutError",
    "MODEL_MAX_TOKENS",
    "SPARK_SYSTEM_PROMPT",
    "WONDER_SYSTEM_PROMPT",
    "DEFAULT_BASE_URL",
]


class MetaAPIError(RuntimeError):
    """Base class for Meta Model API failures (never carries secrets)."""


class MetaAuthError(MetaAPIError):
    """Missing credentials (constructor) or HTTP 401/403 (call path)."""


class MetaRateLimitError(MetaAPIError):
    """HTTP 429 rate limit (call path)."""


class MetaTimeoutError(MetaAPIError):
    """Network timeout after retry (call path)."""


def _urllib_transport(request: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """POST one chat request; return (status_code, parsed_json_body)."""
    payload = json.dumps(request["body"]).encode("utf-8")
    http_request = urllib.request.Request(
        request["url"],
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {request['api_key']}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(http_request, timeout=request["timeout"]) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read().decode("utf-8"))
        except Exception:
            body = {}
        return exc.code, body if isinstance(body, dict) else {}


class MetaClient:
    """Authenticated, capped, retrying Meta Model API client."""

    def __init__(
        self,
        model: str = "muse-spark-1.3",
        base_url: str | None = None,
        api_key: str | None = None,
        system_prompt: str | None = None,
        max_tokens: int | None = None,
        temperature: float = DEFAULT_TEMPERATURE,
        timeout_sec: float = DEFAULT_TIMEOUT_SEC,
        transport: Callable[[dict[str, Any]], tuple[int, dict[str, Any]]] | None = None,
    ) -> None:
        """Bind model, credentials, caps and transport.

        Raises:
            ValueError: Unknown model or max_tokens above the model cap.
            MetaAuthError: No API key from args or ``META_API_KEY``.
        """
        if model not in MODEL_MAX_TOKENS:
            raise ValueError(
                f"unknown model {model!r}: expected one of "
                f"{sorted(MODEL_MAX_TOKENS)}"
            )
        cap = MODEL_MAX_TOKENS[model]
        resolved_max = cap if max_tokens is None else max_tokens
        if resolved_max > cap:
            raise ValueError(
                f"max_tokens={resolved_max} exceeds {model} cap of {cap} "
                "(FinOps guardrail)"
            )
        if resolved_max <= 0:
            raise ValueError("max_tokens must be positive")
        resolved_key = api_key or os.environ.get("META_API_KEY")
        if not resolved_key:
            raise MetaAuthError(
                "META_API_KEY is not set: pass api_key= or export META_API_KEY "
                "(Step 1.0 provisions it into .env)"
            )
        self._model = model
        self._base_url = (base_url or os.environ.get("META_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
        self._api_key = resolved_key
        self._system_prompt = (
            _MODEL_SYSTEM_PROMPTS[model] if system_prompt is None else system_prompt
        )
        self._max_tokens = resolved_max
        self._temperature = temperature
        self._timeout_sec = timeout_sec
        self._transport = transport or _urllib_transport
        self._usage_log: list[dict[str, int]] = []

    def __repr__(self) -> str:  # Zero-Leak: key never rendered
        return (
            f"MetaClient(model={self._model!r}, base_url={self._base_url!r}, "
            f"max_tokens={self._max_tokens})"
        )

    @property
    def model(self) -> str:
        """Bound model id."""
        return self._model

    @property
    def system_prompt(self) -> str:
        """Active system prompt (a module constant by construction)."""
        return self._system_prompt

    @property
    def max_tokens(self) -> int:
        """Enforced per-request token cap."""
        return self._max_tokens

    @property
    def usage_log(self) -> list[dict[str, int]]:
        """Per-call token usage records (for the FinOps ledger)."""
        return list(self._usage_log)

    @property
    def last_usage(self) -> dict[str, int] | None:
        """Most recent usage record, or None before the first call."""
        return dict(self._usage_log[-1]) if self._usage_log else None

    def _messages(self, user_content: str, history: list[dict[str, Any]] | None = None) -> list[dict[str, str]]:
        """Assemble chat messages: system constant + history + user turn."""
        messages = [{"role": "system", "content": self._system_prompt}]
        for entry in history or []:
            role = entry.get("role", "user")
            text = entry.get("text", entry.get("content", ""))
            messages.append(
                {
                    "role": role if role in ("user", "assistant", "system") else "user",
                    "content": str(text),
                }
            )
        messages.append({"role": "user", "content": user_content})
        return messages

    def _post(self, messages: list[dict[str, str]]) -> str:
        """POST with one retry on transient failures; return content text."""
        request = {
            "url": f"{self._base_url}/chat/completions",
            "api_key": self._api_key,
            "timeout": self._timeout_sec,
            "body": {
                "model": self._model,
                "messages": messages,
                "max_tokens": self._max_tokens,
                "temperature": self._temperature,
            },
        }
        attempts = 0
        while True:
            attempts += 1
            try:
                status, body = self._transport(request)
            except TimeoutError as exc:
                if attempts <= 1:
                    continue
                raise MetaTimeoutError(
                    f"Meta API timed out after {attempts} attempts "
                    f"({self._timeout_sec}s each)"
                ) from exc
            except OSError as exc:
                if attempts <= 1:
                    continue
                raise MetaAPIError(f"Meta API transport failed: {exc}") from exc
            if status == 200:
                return self._extract_content(body)
            if status in (401, 403):
                raise MetaAuthError(
                    f"Meta API rejected credentials (HTTP {status}). "
                    "Check META_API_KEY / Step 1.0 provisioning."
                )
            if status == 429:
                raise MetaRateLimitError(
                    "Meta API rate limit hit (HTTP 429). Back off and retry."
                )
            if status in _TRANSIENT_STATUSES and attempts <= 1:
                continue
            detail = body.get("error", body) if isinstance(body, dict) else body
            raise MetaAPIError(
                f"Meta API request failed (HTTP {status}): {detail}"
            )

    def _extract_content(self, body: dict[str, Any]) -> str:
        """Pull completion text + usage out of a 200 response."""
        try:
            choices = body["choices"]
            content = choices[0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise MetaAPIError(
                f"Meta API returned malformed 200 payload: {sorted(body)}"
            ) from exc
        usage = body.get("usage") or {}
        record = {
            "prompt_tokens": int(usage.get("prompt_tokens", 0)),
            "completion_tokens": int(usage.get("completion_tokens", 0)),
            "total_tokens": int(usage.get("total_tokens", 0)),
        }
        self._usage_log.append(record)
        return str(content)

    def generate_regression_test(
        self, issue_description: str, code_context: str
    ) -> str:
        """Synthesize an isolated reproduction test file via the API."""
        user_content = (
            "Write ONE minimal isolated regression test file that reproduces "
            "this issue. Output ONLY the file content.\n\n"
            f"ISSUE:\n{issue_description}\n\nCODE CONTEXT:\n{code_context}"
        )
        return self._post(self._messages(user_content))

    def debate_round(
        self, role: str, prompt: str, history: list[dict[str, Any]]
    ) -> str:
        """Take one debate turn as the given role."""
        user_content = f"[{role}] {prompt}"
        return self._post(self._messages(user_content, history))

    def synthesize_patch(self, debate_consensus: str) -> str:
        """Synthesize a SEARCH/REPLACE patch from debate consensus."""
        user_content = (
            "Convert this debate consensus into exact SEARCH/REPLACE blocks. "
            "Output ONLY the blocks.\n\nCONSENSUS:\n" + debate_consensus
        )
        return self._post(self._messages(user_content))
