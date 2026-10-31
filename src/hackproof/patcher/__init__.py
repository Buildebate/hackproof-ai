"""Deterministic patch application (Step 2.3)."""

from hackproof.patcher.workspace_patcher import (
    GREEN_JUNIT_NAME,
    SUPPORTED_LANGUAGES,
    GreenCheck,
    PatchApplicationError,
    SearchMatch,
    WorkspacePatcher,
    locate_search,
    suggest_path,
)

__all__ = [
    "WorkspacePatcher",
    "PatchApplicationError",
    "GreenCheck",
    "SearchMatch",
    "SUPPORTED_LANGUAGES",
    "GREEN_JUNIT_NAME",
    "locate_search",
    "suggest_path",
]
