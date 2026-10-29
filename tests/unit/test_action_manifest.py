"""Step 3.2: lean composite GitHub Action + dogfood workflow manifests.

Parses ``action.yml`` and ``.github/workflows/verify.yml`` with
``yaml.safe_load`` and pins the security contract: required inputs and
steps exist in order, heavy retrieval backends are never installed on
PR runners, and the Meta API secret travels ONLY through ``env`` /
``with`` mappings — never printed, never embedded in shell.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

import hackproof

PACKAGE_ROOT = Path(__file__).resolve().parent.parent.parent
ACTION_PATH = PACKAGE_ROOT / "action.yml"
WORKFLOW_PATH = PACKAGE_ROOT / ".github" / "workflows" / "verify.yml"

FORBIDDEN_BACKENDS = ("torch", "contriever", "faiss")

_SECRET_PATTERNS = (
    re.compile(r"\$\{\{\s*inputs\.meta-api-key\s*\}\}"),
    re.compile(r"\$\{\{\s*secrets\.META_API_KEY\s*\}\}"),
)


def _load_action() -> dict:
    data = yaml.safe_load(ACTION_PATH.read_text(encoding="utf-8"))
    assert isinstance(data, dict), "action.yml must parse to a mapping"
    return data


def _load_workflow() -> dict:
    data = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
    assert isinstance(data, dict), "verify.yml must parse to a mapping"
    return data


def _walk_strings(node) -> list[str]:
    """Collect every scalar string in a parsed YAML tree."""
    found: list[str] = []
    if isinstance(node, str):
        return [node]
    if isinstance(node, dict):
        for key, value in node.items():
            found.extend(_walk_strings(key))
            found.extend(_walk_strings(value))
    elif isinstance(node, list):
        for item in node:
            found.extend(_walk_strings(item))
    return found


# ---------------------------------------------------------------------------
# action.yml: inputs contract
# ---------------------------------------------------------------------------


def test_action_inputs_contract() -> None:
    data = _load_action()
    inputs = data.get("inputs")
    assert isinstance(inputs, dict), "action.yml must declare inputs"
    assert inputs["meta-api-key"]["required"] is True
    assert inputs["issue-url"]["required"] is True
    assert inputs["runner-mode"]["default"] == "local"
    assert inputs["python-version"]["default"] == "3.11"
    assert "description" in inputs["meta-api-key"]


def test_action_branding_present() -> None:
    data = _load_action()
    branding = data.get("branding")
    assert isinstance(branding, dict)
    assert branding.get("icon"), "branding.icon must be set"
    assert branding.get("color"), "branding.color must be set"
    assert data.get("name"), "action name must be set"


# ---------------------------------------------------------------------------
# action.yml: steps in order
# ---------------------------------------------------------------------------


def test_action_steps_in_order() -> None:
    data = _load_action()
    runs = data.get("runs", {})
    assert runs.get("using") == "composite"
    steps = runs.get("steps")
    assert isinstance(steps, list) and len(steps) >= 3

    setup, install, verify = steps[0], steps[1], steps[2]
    assert setup.get("uses", "").startswith("actions/setup-python"), (
        "step 1 must provision Python via actions/setup-python"
    )
    assert "inputs.python-version" in str(setup.get("with", {})), (
        "setup-python must consume the python-version input"
    )

    install_run = str(install.get("run", ""))
    assert "pip install" in install_run and "hackproof-ai" in install_run, (
        "step 2 must pip-install the published package"
    )
    assert f"hackproof-ai>={hackproof.__version__}" in install_run, (
        "install step must pin the minimum version from this release"
    )

    verify_run = str(verify.get("run", ""))
    assert "hackproof verify" in verify_run
    assert "--issue" in verify_run and "inputs.issue-url" in verify_run
    assert "--runner" in verify_run and "inputs.runner-mode" in verify_run
    env = verify.get("env", {})
    assert env.get("META_API_KEY") == "${{ inputs.meta-api-key }}", (
        "the secret must reach the CLI ONLY through env"
    )


def test_action_installs_no_heavy_backends() -> None:
    data = _load_action()
    for text in _walk_strings(data.get("runs", {}).get("steps", [])):
        lowered = text.lower()
        for backend in FORBIDDEN_BACKENDS:
            assert backend not in lowered, (
                f"PR runners must not install {backend}: {text!r}"
            )


# ---------------------------------------------------------------------------
# Secret hygiene: never printed, never embedded outside allow-listed refs
# ---------------------------------------------------------------------------


def test_action_secret_never_printed() -> None:
    data = _load_action()
    for step in data.get("runs", {}).get("steps", []):
        run = str(step.get("run", ""))
        lowered = run.lower()
        if "echo" in lowered or "print" in lowered:
            assert "meta-api-key" not in lowered and "meta_api_key" not in lowered, (
                f"secret must never be printed: {run!r}"
            )
        for pattern in _SECRET_PATTERNS:
            for match in pattern.finditer(run):
                raise AssertionError(
                    f"secret ref must live in env/with, not shell: {match.group(0)!r}"
                )
    raw = ACTION_PATH.read_text(encoding="utf-8")
    assert "secrets.META_API_KEY" not in raw, (
        "composite action receives the secret via inputs, not secrets context"
    )


def test_action_secret_refs_only_allowlisted() -> None:
    raw = ACTION_PATH.read_text(encoding="utf-8")
    occurrences = raw.count("meta-api-key")
    allowlisted = len(_SECRET_PATTERNS[0].findall(raw))
    # Every mention is either the input declaration or the allow-listed ref.
    declarations = len(re.findall(r"(?m)^\s{2}meta-api-key:", raw))
    assert occurrences == allowlisted + declarations, (
        "meta-api-key may appear only in its declaration and ${{ inputs.meta-api-key }}"
    )
    assert allowlisted >= 1


# ---------------------------------------------------------------------------
# verify.yml: dogfood workflow
# ---------------------------------------------------------------------------


def test_workflow_triggers_on_pull_request() -> None:
    data = _load_workflow()
    triggers = data.get("on", data.get(True))
    if isinstance(triggers, dict):
        assert "pull_request" in triggers
    elif isinstance(triggers, list):
        assert "pull_request" in triggers
    else:
        assert triggers == "pull_request", f"unexpected trigger: {triggers!r}"


def test_workflow_uses_local_action_with_secret() -> None:
    data = _load_workflow()
    jobs = data.get("jobs", {})
    assert "verify" in jobs, "dogfood job must be named 'verify'"
    steps = jobs["verify"].get("steps", [])
    assert steps[0].get("uses", "").startswith("actions/checkout"), (
        "step 1 must check out the repo"
    )
    local = next(
        (step for step in steps if step.get("uses") == "./"),
        None,
    )
    assert local is not None, "workflow must use the local action via './'"
    with_block = local.get("with", {})
    assert with_block.get("meta-api-key") == "${{ secrets.META_API_KEY }}"
    issue_url = str(with_block.get("issue-url", ""))
    assert "github.repository" in issue_url, (
        "dogfood issue-url must address this repository"
    )
    assert "pull_request" in issue_url, (
        "dogfood issue-url must address the pull request number"
    )


def test_workflow_secret_never_printed() -> None:
    raw = WORKFLOW_PATH.read_text(encoding="utf-8")
    for lineno, line in enumerate(raw.splitlines(), start=1):
        lowered = line.lower()
        if "echo" in lowered or "print(" in lowered:
            assert "meta_api_key" not in lowered and "meta-api-key" not in lowered, (
                f"line {lineno} prints the secret: {line!r}"
            )
    occurrences = raw.count("META_API_KEY")
    allowlisted = len(_SECRET_PATTERNS[1].findall(raw))
    assert occurrences == allowlisted >= 1, (
        "secrets.META_API_KEY may appear only as ${{ secrets.META_API_KEY }}"
    )


def test_manifests_deterministic_and_terminated() -> None:
    for path in (ACTION_PATH, WORKFLOW_PATH):
        first = path.read_text(encoding="utf-8")
        assert first.endswith("\n"), f"{path.name} must end with a newline"
        assert yaml.safe_load(first) == yaml.safe_load(first), (
            f"{path.name} must parse deterministically"
        )
