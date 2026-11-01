"""Release dossier pins for Step 5.3 (Devpost submission).

- Monorepo tag v1.0.0 exists (annotated release tag).
- DEVPOST.md is present and carries final-format URLs.
- pyproject version == tag == PyPI (PyPI check needs network; skips offline).
- Tag subtree == public HEAD tree (needs gh + network; skips when
  unavailable; requires post-export state before it can go green).
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

PKG_ROOT = Path(__file__).resolve().parents[2]
MONO_ROOT = Path(__file__).resolve().parents[3]
TAG = "v1.0.0"


def _git(*args: str) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=MONO_ROOT, capture_output=True, text=True, timeout=60
    )
    assert proc.returncode == 0, f"git {' '.join(args)} failed: {proc.stderr[:300]}"
    return proc.stdout.strip()


def test_release_tag_exists() -> None:
    sha = _git("rev-parse", f"{TAG}^{{commit}}")
    assert re.fullmatch(r"[0-9a-f]{40}", sha), sha
    kind = _git("cat-file", "-t", TAG)
    assert kind == "tag", f"expected annotated tag, got {kind}"


def test_devpost_dossier_links() -> None:
    text = (PKG_ROOT / "DEVPOST.md").read_text(encoding="utf-8")
    for needle in (
        "github.com/Buildebate/hackproof-ai",
        "pypi.org/project/hackproof-ai",
        "youtube.com/watch?v=",
    ):
        assert needle in text, f"missing final link: {needle}"


def test_pyproject_version_matches_tag() -> None:
    text = (PKG_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    (version,) = re.findall(r'^version\s*=\s*"([^"]+)"', text, flags=re.M)
    assert version == TAG.lstrip("v"), f"pyproject {version} != tag {TAG}"


def test_pypi_version_matches_tag() -> None:
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen(
            "https://pypi.org/pypi/hackproof-ai/json", timeout=30
        ) as resp:
            data = json.load(resp)
    except (OSError, TimeoutError) as exc:
        pytest.skip(f"PyPI unreachable: {exc}")
    assert data["info"]["version"] == TAG.lstrip("v")


def test_tag_subtree_matches_public_head() -> None:
    if shutil.which("gh") is None:
        pytest.skip("gh CLI unavailable")
    try:
        out = subprocess.run(
            [
                "gh", "api", "repos/Buildebate/hackproof-ai/commits/main",
                "--jq", ".sha",
            ],
            capture_output=True, text=True, timeout=60,
        )
        if out.returncode != 0:
            pytest.skip(f"public repo unreachable: {out.stderr[:200]}")
        main_sha = out.stdout.strip()
        out2 = subprocess.run(
            [
                "gh", "api",
                f"repos/Buildebate/hackproof-ai/git/commits/{main_sha}",
                "--jq", ".tree.sha",
            ],
            capture_output=True, text=True, timeout=60,
        )
        if out2.returncode != 0:
            pytest.skip(f"public tree unreadable: {out2.stderr[:200]}")
        public_tree = out2.stdout.strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        pytest.skip(f"parity check unavailable: {exc}")
    tag_tree = _git("rev-parse", f"{TAG}:packages/hackproof-ai")
    assert tag_tree == public_tree, (
        f"tag subtree {tag_tree} != public HEAD tree {public_tree} "
        "(export required)"
    )
