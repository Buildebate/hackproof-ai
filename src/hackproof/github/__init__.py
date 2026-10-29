"""GitHub Native Artifacts: issue_auditor + pr_generator (Step 3.3)."""

from hackproof.github.issue_auditor import (
    COMMENT_MARKER,
    AuditTrace,
    IssueAuditor,
    IssueAuditorAPIError,
    IssueAuditorAuthError,
    IssueAuditorError,
)
from hackproof.github.pr_generator import (
    PRGenerator,
    VerificationPassport,
)

__all__ = [
    "COMMENT_MARKER",
    "AuditTrace",
    "IssueAuditor",
    "IssueAuditorAPIError",
    "IssueAuditorAuthError",
    "IssueAuditorError",
    "PRGenerator",
    "VerificationPassport",
]
