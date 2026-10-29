"""Live GitHub issue audit trace (Step 3.3, general-concept section C).

Posts exactly ONE structured comment per issue and UPDATES it in place
through pipeline stages (find-by-marker, edit-in-place). The body schema
is fixed: a hidden marker line, a status header, and five collapsible
``<details>`` sections — Localization, Test Immutability Guard, Red
Verification, Asymmetric Debate, Green Verification.

The GitHub token comes ONLY from the environment (``GITHUB_TOKEN`` by
default) and never appears in messages, tracebacks, URLs, or bodies
(Zero-Leak). ``dry_run=True`` renders the markdown without any API
call, for unit tests and offline demos.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable

COMMENT_MARKER = "<!-- hackproof-audit -->"
_API_BASE = "https://api.github.com"
_TIMEOUT_SEC = 30

__all__ = [
    "COMMENT_MARKER",
    "AuditTrace",
    "IssueAuditor",
    "IssueAuditorAPIError",
    "IssueAuditorAuthError",
    "IssueAuditorError",
]


class IssueAuditorError(RuntimeError):
    """Base class for audit-comment failures (never carries the token)."""


class IssueAuditorAuthError(IssueAuditorError):
    """Missing token (constructor path) or HTTP 401/403 (call path)."""


class IssueAuditorAPIError(IssueAuditorError):
    """Non-auth GitHub API failure, with the HTTP status attached."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class AuditTrace:
    """Structured evidence rendered into the single audit comment."""

    issue_url: str
    status: str = "in-progress"
    localization_files: tuple[str, ...] = ()
    localization_ms: int = 0
    guard_digest: str = ""
    guard_modified_lines: int = 0
    guard_files_checked: int = 0
    red_failures: int = 0
    red_junit_ref: str = ""
    debate_rounds: int = 0
    debate_seconds: float = 0.0
    green_exit_code: int | None = None
    receipt_sha256: str = ""


# Transport: (method, url, body, token) -> (status_code, parsed_json).
# The token travels as a separate argument so it can never leak into
# logged URLs or bodies.
Transport = Callable[[str, str, dict | None, str], tuple[int, dict | list]]


def _urllib_transport(
    method: str, url: str, body: dict | None, token: str
) -> tuple[int, dict | list]:
    """Default GitHub REST transport over stdlib urllib."""
    payload = None
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "hackproof-ai",
        "Authorization": f"Bearer {token}",
    }
    if body is not None:
        payload = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=payload, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT_SEC) as response:
            raw = response.read().decode("utf-8")
            parsed = json.loads(raw) if raw.strip() else {}
            return response.status, parsed
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read().decode("utf-8", "replace")
            message = str(json.loads(detail).get("message", detail))[:200]
        except (ValueError, OSError):
            message = f"HTTP {exc.code}"
        raise IssueAuditorAPIError(
            f"GitHub API {method} failed: {message}", status=exc.code
        ) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise IssueAuditorAPIError(f"GitHub API unreachable: {exc}") from exc


class IssueAuditor:
    """Single-comment live audit trace with in-place stage updates."""

    def __init__(
        self,
        github_token_env: str = "GITHUB_TOKEN",
        dry_run: bool = False,
        transport: Transport | None = None,
    ) -> None:
        """Bind the token source, mode, and transport.

        The token itself is read lazily on the first live call, so
        dry-run rendering never requires credentials.
        """
        self._token_env = github_token_env
        self._dry_run = dry_run
        self._transport = transport or _urllib_transport

    @property
    def dry_run(self) -> bool:
        """True when rendering without any API call."""
        return self._dry_run

    def _token(self) -> str:
        token = os.environ.get(self._token_env, "")
        if not token:
            raise IssueAuditorAuthError(
                f"{self._token_env} is not set; export it or use dry_run=True"
            )
        return token

    # -- rendering ----------------------------------------------------

    @staticmethod
    def render_comment(trace: AuditTrace) -> str:
        """Render the exact section-C body schema for ``trace``.

        Pure and deterministic: the same trace always yields the same
        markdown, byte for byte.
        """
        if trace.status == "verified-clean":
            status_line = "✅ **HackProof verdict: VERIFIED CLEAN**"
        elif trace.status == "failed":
            status_line = "❌ **HackProof verdict: FAILED**"
        else:
            status_line = "⏳ **HackProof verification in progress…**"
        files = "\n".join(f"- `{name}`" for name in trace.localization_files) or "- _pending_"
        green = (
            f"`exit code {trace.green_exit_code}`"
            if trace.green_exit_code is not None
            else "_pending_"
        )
        receipt = f"`{trace.receipt_sha256}`" if trace.receipt_sha256 else "_pending_"
        lines = [
            COMMENT_MARKER,
            f"## 🛡️ HackProof Audit Trace — [{trace.issue_url}]({trace.issue_url})",
            "",
            status_line,
            "",
            "<details>",
            "<summary>🔍 Localization</summary>",
            "",
            f"Files localized in `{trace.localization_ms} ms`:",
            files,
            "</details>",
            "",
            "<details>",
            "<summary>🛡️ Test Immutability Guard</summary>",
            "",
            f"SHA-256 baseline digest `{trace.guard_digest or 'pending'}` — "
            f"`{trace.guard_modified_lines}` modified lines "
            f"across `{trace.guard_files_checked}` checked files.",
            "</details>",
            "",
            "<details>",
            "<summary>🔴 Red Verification</summary>",
            "",
            f"Synthesized regression test `FAILED (AssertionError)` "
            f"with `{trace.red_failures}` failure(s); "
            f"JUnit ref `{trace.red_junit_ref or 'pending'}`.",
            "</details>",
            "",
            "<details>",
            "<summary>⚔️ Asymmetric Debate</summary>",
            "",
            f"`{trace.debate_rounds}` round(s) between Muse Spark 1.3 and "
            f"Muse Wonder 1.0 in `{trace.debate_seconds:.1f} s` (measured).",
            "</details>",
            "",
            "<details>",
            "<summary>🟢 Green Verification</summary>",
            "",
            f"Deterministic `SEARCH / REPLACE` patch applied; "
            f"full suite {green}.",
            f"TGRES receipt {receipt}.",
            "</details>",
        ]
        return "\n".join(lines) + "\n"

    # -- live API -----------------------------------------------------

    def _call(
        self, method: str, url: str, body: dict | None = None
    ) -> dict | list:
        status, parsed = self._transport(method, url, body, self._token())
        if status in (401, 403):
            raise IssueAuditorAuthError(
                f"GitHub API denied access (HTTP {status}); "
                f"check {self._token_env} scopes"
            )
        if status >= 400:
            raise IssueAuditorAPIError(
                f"GitHub API {method} failed with HTTP {status}", status=status
            )
        return parsed

    def _find_comment_id(self, owner: str, repo: str, number: int) -> int | None:
        url = (
            f"{_API_BASE}/repos/{owner}/{repo}/issues/{number}"
            f"/comments?per_page=100"
        )
        comments = self._call("GET", url)
        if not isinstance(comments, list):
            return None
        for comment in comments:
            if not isinstance(comment, dict):
                continue
            if COMMENT_MARKER in str(comment.get("body", "")):
                comment_id = comment.get("id")
                if isinstance(comment_id, int):
                    return comment_id
        return None

    def post_or_update_comment(
        self, owner: str, repo: str, number: int, trace: AuditTrace
    ) -> dict:
        """Create the audit comment or update it in place by marker.

        Returns the comment payload (``id`` + ``html_url``). In dry-run
        mode returns ``{"dry_run": True, "body": ...}`` and performs
        zero transport calls.
        """
        body = self.render_comment(trace)
        if self._dry_run:
            return {"dry_run": True, "body": body}
        existing_id = self._find_comment_id(owner, repo, number)
        if existing_id is None:
            url = f"{_API_BASE}/repos/{owner}/{repo}/issues/{number}/comments"
            result = self._call("POST", url, {"body": body})
        else:
            url = f"{_API_BASE}/repos/{owner}/{repo}/issues/comments/{existing_id}"
            result = self._call("PATCH", url, {"body": body})
        if not isinstance(result, dict):
            raise IssueAuditorAPIError("GitHub API returned a malformed comment")
        return result
