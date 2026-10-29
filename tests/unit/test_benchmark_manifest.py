"""Step 4.1: benchmark manifest schema + freeze-record validation (offline).

Validates ``benchmarks/datasets/tasks_manifest.json``: exactly 12 tasks,
the 8+4 language split, the Track 1/2 split, pinned refs, non-empty
hidden goldens (always a withheld subset of PASS_TO_PASS), and recorded
dataset hashes. No network access: the manifest is the frozen record.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

MANIFEST_PATH = (
    Path(__file__).resolve().parent.parent.parent
    / "benchmarks"
    / "datasets"
    / "tasks_manifest.json"
)

HEX40 = re.compile(r"^[0-9a-f]{40}$")
HEX64 = re.compile(r"^[0-9a-f]{64}$")
REQUIRED_FIELDS = {
    "id",
    "language",
    "runner",
    "track",
    "trap_kind",
    "repo",
    "base_ref",
    "dataset_sha256",
    "fail_to_pass",
    "pass_to_pass",
    "hidden_golden",
}
TRAP_KINDS = {"assertion-deletion", "ts-ignore", "mock-bypass", "trivial-stub"}


def _load() -> dict:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    assert isinstance(manifest, dict)
    assert isinstance(manifest.get("tasks"), list)
    return manifest


def test_exactly_twelve_tasks() -> None:
    assert len(_load()["tasks"]) == 12


def test_language_split_8_plus_4() -> None:
    tasks = _load()["tasks"]
    by_language = [task["language"] for task in tasks]
    assert by_language.count("python") == 8
    assert by_language.count("typescript") == 4
    assert set(by_language) == {"python", "typescript"}


def test_runner_matches_language() -> None:
    for task in _load()["tasks"]:
        expected = "pytest" if task["language"] == "python" else "vitest"
        assert task["runner"] == expected, task["id"]


def test_track_split_natural_8_trap_4() -> None:
    tasks = _load()["tasks"]
    natural = [t for t in tasks if t["track"] == "natural"]
    traps = [t for t in tasks if t["track"] == "trap"]
    assert len(natural) == 8
    assert len(traps) == 4
    assert sum(1 for t in natural if t["language"] == "python") == 6
    assert sum(1 for t in natural if t["language"] == "typescript") == 2
    assert sum(1 for t in traps if t["language"] == "python") == 2
    assert sum(1 for t in traps if t["language"] == "typescript") == 2


def test_trap_kinds_annotated() -> None:
    for task in _load()["tasks"]:
        if task["track"] == "natural":
            assert task["trap_kind"] is None, task["id"]
        else:
            assert task["trap_kind"] in TRAP_KINDS, task["id"]
    kinds = {t["trap_kind"] for t in _load()["tasks"] if t["track"] == "trap"}
    assert kinds == TRAP_KINDS, "all four trap kinds must be covered"


def test_required_fields_present() -> None:
    for task in _load()["tasks"]:
        missing = REQUIRED_FIELDS - set(task)
        assert not missing, f"{task.get('id')}: missing {missing}"


def test_ids_unique_and_official() -> None:
    tasks = _load()["tasks"]
    ids = [t["id"] for t in tasks]
    assert len(set(ids)) == 12
    for task in tasks:
        assert "__" in task["id"], task["id"]
        owner_repo = task["repo"]
        assert "/" in owner_repo and " " not in owner_repo
        assert task["id"].startswith(owner_repo.replace("/", "__") + "-"), task["id"]


def test_base_refs_pinned_and_hashed() -> None:
    for task in _load()["tasks"]:
        assert HEX40.match(task["base_ref"]), f"{task['id']}: base_ref not pinned"
        assert HEX64.match(task["dataset_sha256"]), f"{task['id']}: hash missing"
    hashes = [t["dataset_sha256"] for t in _load()["tasks"]]
    assert len(set(hashes)) == 12, "dataset hashes must be distinct per task"


def test_fail_to_pass_non_empty() -> None:
    for task in _load()["tasks"]:
        assert isinstance(task["fail_to_pass"], list) and task["fail_to_pass"], task["id"]
        assert all(isinstance(t, str) and t.strip() for t in task["fail_to_pass"])


def test_hidden_golden_withheld_subset() -> None:
    for task in _load()["tasks"]:
        golden = task["hidden_golden"]
        p2p = task["pass_to_pass"]
        assert isinstance(golden, list) and golden, f"{task['id']}: golden empty"
        assert set(golden) <= set(p2p), f"{task['id']}: golden not subset of P2P"
        assert len(golden) == min(3, len(p2p)), f"{task['id']}: golden rule violated"


def test_manifest_meta_pinned() -> None:
    manifest = _load()
    assert manifest["splits"] == {"python": 8, "typescript": 4, "natural": 8, "trap": 4}
    assert "hidden_golden_rule" in manifest
    assert "dataset_sha256_rule" in manifest
    sources = manifest["sources"]
    assert "SWE-bench_Verified" in sources["python"]
    assert "SWE-bench_Multilingual" in sources["typescript"]
    assert "78f471bf" in sources["python"] and "846e647b" in sources["typescript"]
