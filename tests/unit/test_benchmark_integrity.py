"""Step 4.3: integrity verifier contract (real tree + tamper fixtures).

Pins ``scripts/verify_benchmark_integrity.py``: exit 0 with zero
mismatches on the real benchmark tree, exit 1 with an explicit mismatch
on every tamper class (JUnit bytes, diff bytes, status flip, golden
false-pass). Tamper fixtures are minimal synthetic trees (1 task,
``--expected-tasks 1``) so the negative paths stay hermetic and fast.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT = PACKAGE_ROOT / "scripts" / "verify_benchmark_integrity.py"

TASK_ID = "demo__pkg-1"
F2P = "test_x.py::test_fix"
P2P = "test_x.py::test_keep"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _junit(rows: list[tuple[str, str]]) -> bytes:
    cases = []
    for name, outcome in rows:
        if outcome == "passed":
            cases.append(
                f'<testcase classname="test_x" name="{name}"'
                f' file="test_x.py" />'
            )
        else:
            cases.append(
                f'<testcase classname="test_x" name="{name}"'
                f' file="test_x.py">'
                f"<failure message=\"assert\">boom</failure></testcase>"
            )
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        f"<testsuite tests=\"{len(rows)}\">"
        + "".join(cases)
        + "</testsuite>"
    ).encode()


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=1), encoding="utf-8")


def _red_block(red_body: bytes, red_xml: bytes) -> dict:
    return {
        "rel": "test_repro_deadbeef.py",
        "sha256": _sha(red_body),
        "exit_code": 1,
        "timed_out": False,
        "accepted": True,
        "reason": "red accepted (1 failure(s), 0 errors)",
        "failures": 1,
        "errors": 0,
        "junit_bytes": len(red_xml),
        "junit_sha256": _sha(red_xml),
    }


def build_fixture(root: Path) -> dict[str, Path]:
    """Build a minimal consistent tree; return key artifact paths."""
    bench = root / "benchmarks"
    results = bench / "results"
    manifest = {
        "version": "fixture-1",
        "hidden_golden_rule": "first p2p",
        "dataset_sha256_rule": "fixture",
        "tasks": [
            {
                "id": TASK_ID,
                "language": "python",
                "repo": "demo/pkg",
                "base_ref": "0" * 40,
                "dataset_sha256": "ab" * 32,
                "fail_to_pass": [F2P],
                "pass_to_pass": [P2P],
                "hidden_golden": [P2P],
            }
        ],
    }
    _write_json(bench / "datasets" / "tasks_manifest.json", manifest)

    base_xml = _junit([("test_fix", "failed"), ("test_keep", "passed")])
    agent_xml = _junit([("test_fix", "passed"), ("test_keep", "passed")])
    red_body = b"def test_repro():\n    assert False\n"
    red_xml = _junit([("test_repro", "failed")])
    diff_bytes = b"diff --git a/x.py b/x.py\n"
    resolution = {F2P: [F2P], P2P: [P2P]}

    _write_json(
        results / "results" / "base" / f"{TASK_ID}.json",
        {
            "task": TASK_ID,
            "mode": "base",
            "done": True,
            "junit_sha256": _sha(base_xml),
            "id_resolution": resolution,
            "unresolved_ids": [],
            "base_index": {F2P: "failed", P2P: "passed"},
        },
    )
    (results / "junit_reports" / "base" / f"{TASK_ID}.xml").parent.mkdir(
        parents=True, exist_ok=True
    )
    (results / "junit_reports" / "base" / f"{TASK_ID}.xml").write_bytes(
        base_xml
    )

    scoring = {
        "f2p": {F2P: {"base": "failed", "agent": "passed"}},
        "p2p": {P2P: {"base": "passed", "agent": "passed", "golden": "yes"}},
    }
    for mode, binding in (("A", False), ("B", True)):
        guard = {
            "task": TASK_ID,
            "mode": mode,
            "binding": binding,
            "verdict": "clean",
            "violations": [],
            "note": "fixture",
        }
        record: dict = {
            "task": TASK_ID,
            "mode": mode,
            "done": True,
            "status": "resolved",
            "reason": "",
            "junit_sha256": _sha(agent_xml),
            "diff_sha256": _sha(diff_bytes),
            "scoring": scoring,
            "guard": guard,
            "eval_exit": 0,
            "eval_timed_out": False,
        }
        if mode == "B":
            record["red"] = _red_block(red_body, red_xml)
        else:
            record["red"] = None
        _write_json(results / "results" / mode / f"{TASK_ID}.json", record)
        _write_json(
            results / "guard_reports" / mode / f"{TASK_ID}.json", guard
        )
        junit_path = results / "junit_reports" / mode / f"{TASK_ID}.xml"
        junit_path.parent.mkdir(parents=True, exist_ok=True)
        junit_path.write_bytes(agent_xml)
        diff_path = results / "diffs" / mode / f"{TASK_ID}.diff"
        diff_path.parent.mkdir(parents=True, exist_ok=True)
        diff_path.write_bytes(diff_bytes)
    prompt_dir = results / "prompts" / "B" / TASK_ID
    prompt_dir.mkdir(parents=True, exist_ok=True)
    (prompt_dir / "red_test.txt").write_bytes(red_body)
    (results / "junit_reports" / "B" / f"{TASK_ID}.red.xml").write_bytes(
        red_xml
    )

    # Mode C no-patch shape (mirrors 11739/11899-C): red accepted, then
    # no synthesis blocks, hence no eval artifacts and no record guard.
    _write_json(
        results / "results" / "C" / f"{TASK_ID}.json",
        {
            "task": TASK_ID,
            "mode": "C",
            "done": True,
            "status": "failed",
            "reason": "no blocks",
            "guard": None,
            "red": _red_block(red_body, red_xml),
        },
    )
    prompt_c = results / "prompts" / "C" / TASK_ID
    prompt_c.mkdir(parents=True, exist_ok=True)
    (prompt_c / "red_test.txt").write_bytes(red_body)
    (results / "junit_reports" / "C" / f"{TASK_ID}.red.xml").parent.mkdir(
        parents=True, exist_ok=True
    )
    (results / "junit_reports" / "C" / f"{TASK_ID}.red.xml").write_bytes(
        red_xml
    )
    _write_json(
        bench / "evidence_dossier.json",
        {
            "scoreboard": {"mode_A_resolved": 1, "mode_B_resolved": 1},
            "verdicts": {
                "A": {TASK_ID: {"status": "resolved"}},
                "B": {TASK_ID: {"status": "resolved"}},
            },
            "guard_reward_hacking": {
                "A": {"violations": 0},
                "B": {"violations": 0},
            },
            "ablation_c": {"resolved": []},
        },
    )
    return {
        "b_junit": results / "junit_reports" / "B" / f"{TASK_ID}.xml",
        "b_diff": results / "diffs" / "B" / f"{TASK_ID}.diff",
        "b_record": results / "results" / "B" / f"{TASK_ID}.json",
    }


def run_verifier(root: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--package-root",
            str(root),
            "--expected-tasks",
            "1",
            *extra,
        ],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def test_verifier_accepts_consistent_fixture(tmp_path: Path) -> None:
    build_fixture(tmp_path)
    proc = run_verifier(tmp_path)
    assert proc.returncode == 0, proc.stderr + proc.stdout
    report = json.loads(proc.stdout)
    assert report["ok"] is True
    assert report["mismatches"] == []
    assert report["recount"]["B"]["resolved"] == 1


def test_verifier_rejects_tampered_junit(tmp_path: Path) -> None:
    paths = build_fixture(tmp_path)
    # Flip a passing case into a failure (keeps XML valid).
    blob = paths["b_junit"].read_bytes().replace(
        b'file="test_x.py" />', b'file="test_x.py"><failure message="x">y</failure></testcase>', 1
    )
    paths["b_junit"].write_bytes(blob)
    proc = run_verifier(tmp_path)
    assert proc.returncode == 1
    report = json.loads(proc.stdout)
    assert report["ok"] is False
    assert any("junit-sha-drift" in m for m in report["mismatches"])


def test_verifier_rejects_tampered_diff(tmp_path: Path) -> None:
    paths = build_fixture(tmp_path)
    paths["b_diff"].write_bytes(b"tampered diff\n")
    proc = run_verifier(tmp_path)
    assert proc.returncode == 1
    report = json.loads(proc.stdout)
    assert any("diff-sha-drift" in m for m in report["mismatches"])


def test_verifier_rejects_status_flip(tmp_path: Path) -> None:
    paths = build_fixture(tmp_path)
    record = json.loads(paths["b_record"].read_text(encoding="utf-8"))
    record["status"] = "failed"
    record["reason"] = "hand-edited"
    paths["b_record"].write_text(json.dumps(record), encoding="utf-8")
    proc = run_verifier(tmp_path)
    assert proc.returncode == 1
    report = json.loads(proc.stdout)
    assert any("status-drift" in m for m in report["mismatches"])


def test_verifier_exposes_golden_false_pass(tmp_path: Path) -> None:
    paths = build_fixture(tmp_path)
    # Golden test fails in raw JUnit, but the record still claims resolved
    # (hash chain re-sealed so only the S2 replay can catch it).
    evil_xml = _junit([("test_fix", "passed"), ("test_keep", "failed")])
    paths["b_junit"].write_bytes(evil_xml)
    record = json.loads(paths["b_record"].read_text(encoding="utf-8"))
    record["junit_sha256"] = _sha(evil_xml)
    record["scoring"]["p2p"][P2P]["agent"] = "passed"
    paths["b_record"].write_text(json.dumps(record), encoding="utf-8")
    proc = run_verifier(tmp_path)
    assert proc.returncode == 1
    report = json.loads(proc.stdout)
    assert any("golden-false-pass" in m for m in report["mismatches"])


def test_verifier_accepts_real_tree() -> None:
    proc = subprocess.run(
        [sys.executable, str(SCRIPT)],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout[-2000:]
    report = json.loads(proc.stdout)
    assert report["ok"] is True
    assert report["mismatches"] == []
    assert report["recount"]["A"] == {
        "resolved": 6,
        "failed": 6,
        "other": 0,
    }
    assert report["recount"]["B"] == {
        "resolved": 9,
        "failed": 3,
        "other": 0,
    }
    assert report["recount"]["C"] == {
        "resolved": 7,
        "failed": 5,
        "other": 0,
    }
    assert report["golden_false_pass"] == {"A": 0, "B": 0, "C": 0}
