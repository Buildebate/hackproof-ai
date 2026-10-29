"""Step 3.3: GitHub native audit artifacts — offline suite.

Covers the single-comment audit trace schema (general-concept section C),
in-place update logic over a mocked GitHub transport, the Verification
Passport PR body, and the self-contained SVG trust badge. No test touches
the network: live calls go through an injected stub transport, and
dry-run mode performs zero calls by construction.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET

import pytest

from hackproof.github import (
    COMMENT_MARKER,
    AuditTrace,
    IssueAuditor,
    IssueAuditorAPIError,
    IssueAuditorAuthError,
    PRGenerator,
    VerificationPassport,
)

TRACE = AuditTrace(
    issue_url="https://github.com/octocat/hello-world/issues/42",
    status="verified-clean",
    localization_files=("foo.py", "bar.py"),
    localization_ms=12,
    guard_digest="ab" * 32,
    guard_modified_lines=0,
    guard_files_checked=4,
    red_failures=1,
    red_junit_ref="red-junit.xml",
    debate_rounds=1,
    debate_seconds=6.4,
    green_exit_code=0,
    receipt_sha256="cd" * 32,
)


class _StubTransport:
    """Canned GitHub REST transport recording every call."""

    def __init__(self, script: list[tuple[int, dict | list]]) -> None:
        self._script = list(script)
        self.calls: list[tuple[str, str, dict | None]] = []
        self.tokens: list[str] = []

    def __call__(self, method, url, body, token):
        self.calls.append((method, url, body))
        self.tokens.append(token)
        assert "secret" not in url.lower(), "token must not leak into URLs"
        assert token, "token must be supplied to the transport"
        index = min(len(self.calls) - 1, len(self._script) - 1)
        return self._script[index]


def _auditor(
    script: list[tuple[int, dict | list]],
    monkeypatch: pytest.MonkeyPatch,
    token: str = "test-token",
    dry_run: bool = False,
) -> tuple[IssueAuditor, _StubTransport]:
    monkeypatch.setenv("GITHUB_TOKEN", token)
    transport = _StubTransport(script)
    return IssueAuditor(dry_run=dry_run, transport=transport), transport


# ---------------------------------------------------------------------------
# Comment body schema (general-concept section C)
# ---------------------------------------------------------------------------


def test_marker_is_first_line() -> None:
    body = IssueAuditor.render_comment(TRACE)
    assert body.splitlines()[0] == COMMENT_MARKER
    assert body.count(COMMENT_MARKER) == 1


def test_all_five_sections_present() -> None:
    body = IssueAuditor.render_comment(TRACE)
    for title in (
        "🔍 Localization",
        "🛡️ Test Immutability Guard",
        "🔴 Red Verification",
        "⚔️ Asymmetric Debate",
        "🟢 Green Verification",
    ):
        assert title in body, f"missing section: {title}"
    assert body.count("<details>") == 5
    assert body.count("</details>") == 5


def test_sections_collapsible_with_summary() -> None:
    body = IssueAuditor.render_comment(TRACE)
    assert body.count("<summary>") == 5


def test_evidence_values_interpolated() -> None:
    body = IssueAuditor.render_comment(TRACE)
    assert "`foo.py`" in body and "`bar.py`" in body
    assert "`12 ms`" in body
    assert "ab" * 32 in body
    assert "`0` modified lines" in body
    assert "`4` checked files" in body
    assert "FAILED (AssertionError)" in body
    assert "`1` failure(s)" in body
    assert "`red-junit.xml`" in body
    assert "`1` round(s)" in body
    assert "`6.4 s`" in body
    assert "`exit code 0`" in body
    assert "cd" * 32 in body
    assert "VERIFIED CLEAN" in body


def test_status_variants() -> None:
    in_progress = IssueAuditor.render_comment(
        AuditTrace(issue_url=TRACE.issue_url, status="in-progress")
    )
    assert "in progress" in in_progress
    assert "_pending_" in in_progress
    failed = IssueAuditor.render_comment(
        AuditTrace(issue_url=TRACE.issue_url, status="failed")
    )
    assert "FAILED" in failed


def test_render_deterministic() -> None:
    assert IssueAuditor.render_comment(TRACE) == IssueAuditor.render_comment(TRACE)
    assert IssueAuditor.render_comment(TRACE).endswith("\n")


# ---------------------------------------------------------------------------
# post_or_update_comment over a mocked transport
# ---------------------------------------------------------------------------


def test_creates_comment_when_marker_absent(monkeypatch) -> None:
    auditor, transport = _auditor(
        [
            (200, [{"id": 1, "body": "unrelated comment"}]),
            (201, {"id": 101, "html_url": "https://example.invalid/c/101"}),
        ],
        monkeypatch,
    )
    result = auditor.post_or_update_comment("octocat", "hello-world", 42, TRACE)
    assert result["id"] == 101
    assert [method for method, _, _ in transport.calls] == ["GET", "POST"]
    _, post_url, post_body = transport.calls[1]
    assert post_url.endswith("/repos/octocat/hello-world/issues/42/comments")
    assert post_body is not None and COMMENT_MARKER in post_body["body"]
    assert "secret" not in str(transport.calls).lower()
    assert transport.tokens == ["test-token", "test-token"]


def test_updates_comment_in_place_when_marker_found(monkeypatch) -> None:
    existing = COMMENT_MARKER + "\nold body"
    auditor, transport = _auditor(
        [
            (200, [{"id": 7, "body": "noise"}, {"id": 9, "body": existing}]),
            (200, {"id": 9, "html_url": "https://example.invalid/c/9"}),
        ],
        monkeypatch,
    )
    result = auditor.post_or_update_comment("octocat", "hello-world", 42, TRACE)
    assert result["id"] == 9
    assert [method for method, _, _ in transport.calls] == ["GET", "PATCH"]
    _, patch_url, patch_body = transport.calls[1]
    assert patch_url.endswith("/repos/octocat/hello-world/issues/comments/9")
    assert patch_body is not None and "VERIFIED CLEAN" in patch_body["body"]


def test_dry_run_makes_zero_calls(monkeypatch) -> None:
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    transport = _StubTransport([])
    auditor = IssueAuditor(dry_run=True, transport=transport)
    result = auditor.post_or_update_comment("o", "r", 1, TRACE)
    assert result["dry_run"] is True
    assert COMMENT_MARKER in result["body"]
    assert transport.calls == []


def test_missing_token_raises_auth_error(monkeypatch) -> None:
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    auditor = IssueAuditor(transport=_StubTransport([]))
    with pytest.raises(IssueAuditorAuthError, match="GITHUB_TOKEN is not set"):
        auditor.post_or_update_comment("o", "r", 1, TRACE)


def test_http_401_maps_to_auth_error(monkeypatch) -> None:
    auditor, _ = _auditor([(401, {"message": "Bad credentials"})], monkeypatch)
    with pytest.raises(IssueAuditorAuthError, match="HTTP 401"):
        auditor.post_or_update_comment("o", "r", 1, TRACE)


def test_http_500_maps_to_api_error_with_status(monkeypatch) -> None:
    auditor, _ = _auditor([(500, {"message": "boom"})], monkeypatch)
    with pytest.raises(IssueAuditorAPIError) as exc_info:
        auditor.post_or_update_comment("o", "r", 1, TRACE)
    assert exc_info.value.status == 500


def test_malformed_comment_response_rejected(monkeypatch) -> None:
    auditor, _ = _auditor([(200, []), (201, [])], monkeypatch)
    with pytest.raises(IssueAuditorAPIError, match="malformed"):
        auditor.post_or_update_comment("o", "r", 1, TRACE)


def test_zero_leak_on_transport_failure(monkeypatch) -> None:
    def failing_transport(method, url, body, token):
        raise IssueAuditorAPIError("GitHub API unreachable: offline")

    monkeypatch.setenv("GITHUB_TOKEN", "ghp-live-secret-value")
    auditor = IssueAuditor(transport=failing_transport)
    with pytest.raises(IssueAuditorAPIError) as exc_info:
        auditor.post_or_update_comment("o", "r", 1, TRACE)
    assert "ghp-live-secret-value" not in str(exc_info.value)


def test_custom_token_env_name(monkeypatch) -> None:
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setenv("MY_GH_TOKEN", "custom-token")
    transport = _StubTransport(
        [(200, []), (201, {"id": 3, "html_url": "https://example.invalid/c/3"})]
    )
    auditor = IssueAuditor(github_token_env="MY_GH_TOKEN", transport=transport)
    assert auditor.post_or_update_comment("o", "r", 1, TRACE)["id"] == 3
    assert transport.tokens == ["custom-token", "custom-token"]


# ---------------------------------------------------------------------------
# Verification Passport PR body
# ---------------------------------------------------------------------------


def _passport(**overrides) -> VerificationPassport:
    fields = {
        "pre_patch_commit": "aaa111",
        "post_patch_commit": "bbb222",
        "guard_ok": True,
        "guard_files_checked": 6,
        "receipt_sha256": "ee" * 32,
        "compression_pct": 78.0,
        "cost_usd_per_pr": 0.28,
    }
    fields.update(overrides)
    return VerificationPassport(**fields)


def test_passport_header_and_table() -> None:
    body = PRGenerator.build_pr_body(_passport())
    assert "## 🛡️ HackProof Verification Passport" in body
    assert "pre-patch `aaa111`" in body
    assert "post-patch `bbb222`" in body
    assert "exit code 0" in body
    assert "| Stage | Evidence |" in body


def test_passport_guard_and_receipt() -> None:
    body = PRGenerator.build_pr_body(_passport())
    assert "Immutable Existing Tests Guard" in body
    assert "`6` file(s) checked" in body
    assert "`0` modified lines" in body
    assert "ee" * 32 in body


def test_passport_guard_breach_blocks_merge() -> None:
    body = PRGenerator.build_pr_body(_passport(guard_ok=False))
    assert "BREACHED" in body
    assert "must not merge" in body


def test_passport_telemetry_line_with_measurements() -> None:
    body = PRGenerator.build_pr_body(_passport())
    assert "`78%`" in body
    assert "~`$0.28`" in body


def test_passport_telemetry_honest_when_unmeasured() -> None:
    body = PRGenerator.build_pr_body(
        _passport(compression_pct=None, cost_usd_per_pr=None)
    )
    assert "not measured yet" in body
    assert "evidence_dossier.json" in body
    assert "%" not in body.split("Telemetry:")[1].split("\n")[0].replace(
        "not measured yet", ""
    )


def test_passport_rejects_empty_evidence() -> None:
    with pytest.raises(ValueError, match="pre_patch_commit"):
        _passport(pre_patch_commit="  ")
    with pytest.raises(ValueError, match="receipt_sha256"):
        _passport(receipt_sha256="")
    with pytest.raises(ValueError, match="guard_files_checked"):
        _passport(guard_files_checked=-1)


# ---------------------------------------------------------------------------
# SVG trust badge
# ---------------------------------------------------------------------------


def test_badge_svg_is_valid_xml() -> None:
    svg = PRGenerator.build_badge_svg()
    root = ET.fromstring(svg)
    assert root.tag.endswith("svg")
    assert svg.endswith("\n")


def test_badge_verified_and_failed_variants() -> None:
    verified = PRGenerator.build_badge_svg(verified=True)
    assert "Verified by HackProof" in verified
    assert "#2da44e" in verified
    failed = PRGenerator.build_badge_svg(verified=False)
    assert "failed" in failed.lower()
    assert "#cf222e" in failed
    ET.fromstring(failed)


def test_badge_has_no_external_references() -> None:
    svg = PRGenerator.build_badge_svg()
    assert "http" not in svg.replace("http://www.w3.org/2000/svg", "")
    assert "href" not in svg


def test_badge_deterministic() -> None:
    assert PRGenerator.build_badge_svg() == PRGenerator.build_badge_svg()
    assert PRGenerator.build_badge_svg(False) == PRGenerator.build_badge_svg(False)
