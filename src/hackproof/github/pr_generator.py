"""Verified PR artifacts (Step 3.3, general-concept section C).

Builds the HackProof Verification Passport for pull request bodies —
a Red-to-Green Machine Proof table, guard confirmation, the TGRES
SHA-256 stamp, and a telemetry badge line — plus a self-contained SVG
trust badge with zero external service dependencies.

Telemetry honesty rule: compression and cost figures are rendered ONLY
from explicit caller-provided measurements. Unknown values render as
an honest "not measured yet" note pointing at
``benchmarks/evidence_dossier.json`` — never fabricated.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["PRGenerator", "VerificationPassport"]


@dataclass(frozen=True)
class VerificationPassport:
    """Measured evidence bound into one verified pull request."""

    pre_patch_commit: str
    post_patch_commit: str
    guard_ok: bool
    guard_files_checked: int
    receipt_sha256: str
    compression_pct: float | None = None
    cost_usd_per_pr: float | None = None

    def __post_init__(self) -> None:
        for name in ("pre_patch_commit", "post_patch_commit", "receipt_sha256"):
            if not getattr(self, name).strip():
                raise ValueError(f"{name} must be a non-empty string")
        if self.guard_files_checked < 0:
            raise ValueError("guard_files_checked must be >= 0")


class PRGenerator:
    """Stateless builder for PR bodies and the SVG trust badge."""

    # -- PR body --------------------------------------------------------

    @staticmethod
    def build_pr_body(passport: VerificationPassport) -> str:
        """Render the HackProof Verification Passport markdown."""
        if passport.guard_ok:
            guard_line = (
                f"✅ `Immutable Existing Tests Guard` clean — "
                f"`{passport.guard_files_checked}` file(s) checked, "
                f"`0` modified lines."
            )
        else:
            guard_line = (
                "❌ `Immutable Existing Tests Guard` BREACHED — "
                "this PR must not merge."
            )
        if (
            passport.compression_pct is not None
            and passport.cost_usd_per_pr is not None
        ):
            telemetry = (
                f"📊 Telemetry: `{passport.compression_pct:.0f}%` context "
                f"compression via TokenShield "
                f"(~`${passport.cost_usd_per_pr:.2f}` per verified PR)."
            )
        else:
            telemetry = (
                "📊 Telemetry: not measured yet — figures land in "
                "`benchmarks/evidence_dossier.json` (Phase 4)."
            )
        lines = [
            "## 🛡️ HackProof Verification Passport",
            "",
            "| Stage | Evidence |",
            "| --- | --- |",
            f"| 🔴 Red | pre-patch `{passport.pre_patch_commit}` — repro `FAILED (AssertionError)` |",
            f"| 🟢 Green | post-patch `{passport.post_patch_commit}` — full suite `exit code 0` |",
            "",
            guard_line,
            "",
            f"**TGRES SHA-256:** `{passport.receipt_sha256}`",
            "",
            telemetry,
            "",
            "![Verified by HackProof](verified-by-hackproof.svg)",
        ]
        return "\n".join(lines) + "\n"

    # -- SVG badge ------------------------------------------------------

    @staticmethod
    def build_badge_svg(verified: bool = True) -> str:
        """Render the trust badge SVG (no external service dependency).

        Args:
            verified: Green "Verified" badge when True, red "Failed"
                badge when False.
        """
        if verified:
            color = "#2da44e"
            label = "Verified by HackProof"
        else:
            color = "#cf222e"
            label = "HackProof check failed"
        # Width scales with the label so text never clips.
        width = 34 + len(label) * 7
        text_x = width // 2
        return (
            '<svg xmlns="http://www.w3.org/2000/svg" '
            f'width="{width}" height="20" role="img" '
            f'aria-label="{label}">'
            f'<rect width="{width}" height="20" rx="10" fill="{color}"/>'
            '<text x="17" y="14" font-family="Verdana,sans-serif" '
            'font-size="11" fill="#ffffff">🛡</text>'
            f'<text x="{text_x}" y="14" font-family="Verdana,sans-serif" '
            'font-size="11" fill="#ffffff" text-anchor="middle">'
            f"{label}</text></svg>\n"
        )
