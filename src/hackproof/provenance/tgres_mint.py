"""TGRES provenance receipts and red-to-green verification (Step 2.3).

Mints SHA-256 evidence receipts binding base commit, test, patch, and
both JUnit reports; and enforces the Red-to-Green Machine Proof: the
pre-patch report MUST be Red (assertion failure, zero errors — the same
rule as the Step 2.1 Red Baseline Invariant), the post-patch suite MUST
exit 0, and pre-existing tests MUST be 100% unchanged per the
``ImmutableTestGuard`` report. A guard breach raises
:class:`RewardHackingDetectedError`; any other violation returns False.
"""

from __future__ import annotations

import hashlib
import json
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

from hackproof.sandbox.guards import RewardHackingDetectedError

RECEIPT_VERSION = "1.0"

__all__ = ["RECEIPT_VERSION", "TGRESProvenanceVerifier"]


def _count_junit(red_junit_xml: Path | str) -> tuple[int, int]:
    """Count <failure> and <error> nodes in a JUnit report.

Args:
    red_junit_xml: Report file path (``Path``) or raw XML text (``str``).

Raises:
    FileNotFoundError: Report file missing.
    ValueError: Report unparseable.
    """
    if isinstance(red_junit_xml, Path):
        if not red_junit_xml.is_file():
            raise FileNotFoundError(
                f"JUnit report not produced: {red_junit_xml}"
            )
        raw = red_junit_xml.read_text(encoding="utf-8")
        label = str(red_junit_xml)
    else:
        raw = red_junit_xml
        label = "<red_junit_xml>"
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise ValueError(f"JUnit report unparseable: {label}") from exc
    failures = sum(1 for _ in root.iter("failure"))
    errors = sum(1 for _ in root.iter("error"))
    return failures, errors


class TGRESProvenanceVerifier:
    """Stateless minting and verification of red-to-green evidence."""

    @staticmethod
    def mint_receipt(
        base_commit: str,
        test_hash: str,
        patch_hash: str,
        red_junit_hash: str,
        green_junit_hash: str,
    ) -> dict[str, str]:
        """Mint a SHA-256 evidence receipt with a UTC timestamp.

Raises:
    ValueError: Any evidence input is empty.
        """
        inputs = {
            "base_commit": base_commit,
            "test_hash": test_hash,
            "patch_hash": patch_hash,
            "red_junit_hash": red_junit_hash,
            "green_junit_hash": green_junit_hash,
        }
        for name, value in inputs.items():
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"receipt input {name!r} must be non-empty")
        payload: dict[str, str] = {
            "receipt_version": RECEIPT_VERSION,
            **inputs,
            "minted_at_utc": datetime.now(timezone.utc).isoformat(),
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        payload["receipt_sha256"] = hashlib.sha256(
            canonical.encode("utf-8")
        ).hexdigest()
        return payload

    @staticmethod
    def verify_red_to_green(
        red_junit_xml: Path | str,
        green_exit_code: int,
        guard_report: tuple[bool, list[str]],
    ) -> bool:
        """Verify the full red-to-green machine proof.

Args:
    red_junit_xml: Pre-patch JUnit report (file ``Path`` or XML text).
    green_exit_code: Post-patch suite exit code (must be 0).
    guard_report: ``ImmutableTestGuard`` outcome ``(ok, violations)``.

Returns:
    True only when the pre-patch report is Red (``failures >= 1``
    AND ``errors == 0``), the post-patch exit code is 0, and the
    guard report is clean.

Raises:
    RewardHackingDetectedError: Guard report shows tampering.
    ValueError: Malformed guard report or unparseable JUnit.
    FileNotFoundError: JUnit report file missing.
        """
        if (
            not isinstance(guard_report, (tuple, list))
            or len(guard_report) != 2
            or not isinstance(guard_report[0], bool)
        ):
            raise ValueError(
                "guard_report must be (ok: bool, violations: list[str])"
            )
        ok, violations = guard_report
        if not ok or violations:
            raise RewardHackingDetectedError(
                list(violations) if violations else ["guard report negative"]
            )
        failures, errors = _count_junit(red_junit_xml)
        if failures < 1 or errors > 0:
            return False
        return int(green_exit_code) == 0
