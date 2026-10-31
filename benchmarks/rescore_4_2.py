"""Step 4.3a.3: offline rescore of Step 4.2 verdicts (measurement layer only).

Recomputes base/agent indexes + scoring from IMMUTABLE archived JUnit XML
(SHA-verified against recorded ``junit_sha256``) using the fixed bench_lib.
Original verdicts are preserved under ``superseded_4_2`` (provenance);
XML bytes are never touched.

Mechanical boundary (no judgment calls):
- base records rebuilt: measurable python (6 tasks) + TS 11589/11899 (R3).
  11739/11870 bases are verify-only (rebuild must equal recorded bytes).
- run records rescored: ONLY runs with archived agent JUnit (A-5414,
  A-10081, B-5414) + control B-7432 (must reproduce the verdict exactly).
- runs without agent JUnit: reported ``not-applicable``, untouched.
- Mode B flips additionally require recorded red-run + guard-clean.
  No receipt re-mint offline (caveat recorded; live confirmation in Wave 1).

Usage: ``python3 benchmarks/rescore_4_2.py [--apply]`` (default dry-run).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

BENCH_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BENCH_DIR))

from bench_lib import (  # noqa: E402
    index_pytest_cases,
    index_vitest_cases,
    load_manifest,
    parse_junit,
    remap_collected_index,
    score_task,
    task_by_id,
    utcnow_iso,
)
from run_benchmark import describe_failure  # noqa: E402

RESULTS = BENCH_DIR / "results"
PY_BASES = [
    "psf__requests-1142",
    "psf__requests-5414",
    "pytest-dev__pytest-10081",
    "pytest-dev__pytest-8399",
    "pytest-dev__pytest-5787",
    "pytest-dev__pytest-7432",
]
TS_BASES = ["vuejs__core-11589", "vuejs__core-11899"]
VERIFY_ONLY_BASES = ["vuejs__core-11739", "vuejs__core-11870"]
RESCORE_RUNS = [("A", "psf__requests-5414"), ("A", "pytest-dev__pytest-10081"), ("B", "psf__requests-5414")]
CONTROL_RUN = ("B", "pytest-dev__pytest-7432")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _manifest_files(task: dict) -> set[str]:
    return {
        test_id.split("::", 1)[0]
        for test_id in list(task.get("fail_to_pass", [])) + list(task.get("pass_to_pass", []))
    }


def rebuild_base(task: dict, base_record: dict, junit_bytes: bytes) -> tuple[dict, dict, list[str]]:
    """Return (base_index, id_resolution, unresolved_ids) from XML bytes."""
    if task["language"] == "python":
        index = index_pytest_cases(parse_junit(junit_bytes), known_files=_manifest_files(task))
        resolution = dict(base_record["id_resolution"])
    else:
        index = index_vitest_cases(parse_junit(junit_bytes))
        manifest_ids = list(task.get("fail_to_pass", [])) + list(task.get("pass_to_pass", []))
        resolution = {mid: ([mid] if mid in index else []) for mid in manifest_ids}
    base_index = remap_collected_index(resolution, index)
    unresolved = [mid for mid, hits in resolution.items() if not hits]
    return base_index, resolution, unresolved


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Offline 4.2 rescore (dry-run by default).")
    parser.add_argument("--apply", action="store_true", help="Write rescored records.")
    args = parser.parse_args(argv)

    manifest = load_manifest(BENCH_DIR / "datasets" / "tasks_manifest.json")
    rows: list[str] = []
    failures: list[str] = []
    pending_writes: list[tuple[Path, dict]] = []

    def note(ok: bool, text: str) -> None:
        rows.append(f"[{'OK' if ok else 'FAIL'}] {text}")
        if not ok:
            failures.append(text)

    # --- base records -----------------------------------------------------
    for task_id in PY_BASES + TS_BASES + VERIFY_ONLY_BASES:
        task = task_by_id(manifest, task_id)
        base_path = RESULTS / "results" / "base" / f"{task_id}.json"
        xml_path = RESULTS / "junit_reports" / "base" / f"{task_id}.xml"
        base_record = _load_json(base_path)
        junit_bytes = xml_path.read_bytes()
        if not base_record.get("junit_sha256") or _sha256(junit_bytes) != base_record["junit_sha256"]:
            note(False, f"base {task_id}: junit SHA mismatch (archive tampered?)")
            continue
        base_index, resolution, unresolved = rebuild_base(task, base_record, junit_bytes)
        if task_id in VERIFY_ONLY_BASES:
            same = base_index == base_record.get("base_index") and unresolved == base_record.get("unresolved_ids", [])
            note(same, f"base {task_id}: verify-only rebuild {'identical' if same else 'DRIFTED'}")
            continue
        changed = (
            base_index != base_record.get("base_index")
            or resolution != base_record.get("id_resolution")
            or unresolved != base_record.get("unresolved_ids", [])
        )
        note(True, f"base {task_id}: rebuilt ({len(base_index)} rows, {len(unresolved)} unresolved, changed={changed})")
        new_record = dict(base_record)
        new_record["superseded_4_2"] = {
            "base_index": base_record.get("base_index"),
            "id_resolution": base_record.get("id_resolution"),
            "unresolved_ids": base_record.get("unresolved_ids"),
            "ts": base_record.get("ts"),
        }
        new_record["base_index"] = base_index
        new_record["id_resolution"] = resolution
        new_record["unresolved_ids"] = unresolved
        new_record["rescore"] = {"tool": "benchmarks/rescore_4_2.py", "ts": utcnow_iso()}
        pending_writes.append((base_path, new_record))

    # --- run records ------------------------------------------------------
    for mode, task_id in RESCORE_RUNS + [CONTROL_RUN]:
        task = task_by_id(manifest, task_id)
        is_control = (mode, task_id) == CONTROL_RUN
        run_path = RESULTS / "results" / mode / f"{task_id}.json"
        xml_path = RESULTS / "junit_reports" / mode / f"{task_id}.xml"
        record = _load_json(run_path)
        if not xml_path.is_file():
            note(False, f"run {mode}/{task_id}: agent JUnit missing (not rescorable)")
            continue
        junit_bytes = xml_path.read_bytes()
        if not record.get("junit_sha256") or _sha256(junit_bytes) != record["junit_sha256"]:
            note(False, f"run {mode}/{task_id}: junit SHA mismatch (archive tampered?)")
            continue
        base_record = _load_json(RESULTS / "results" / "base" / f"{task_id}.json")
        if task["language"] == "python":
            base_index, resolution, _ = rebuild_base(
                task, base_record, (RESULTS / "junit_reports" / "base" / f"{task_id}.xml").read_bytes()
            )
            agent_full = index_pytest_cases(parse_junit(junit_bytes), known_files=_manifest_files(task))
        else:
            base_index, resolution, _ = rebuild_base(
                task, base_record, (RESULTS / "junit_reports" / "base" / f"{task_id}.xml").read_bytes()
            )
            agent_full = index_vitest_cases(parse_junit(junit_bytes))
        agent_index = remap_collected_index(resolution, agent_full)
        scoring = score_task(task, base_index, agent_index)
        functional = bool(scoring["resolved_functional"])
        if mode == "B":
            guard_clean = (record.get("guard") or {}).get("verdict") == "clean"
            red_ran = bool(record.get("junit_sha256"))
            resolved = functional and guard_clean and red_ran
            caveat = "rescore-resolved; machine receipt not re-minted offline (live confirmation in Wave 1)"
        else:
            resolved = functional
            caveat = ""
        new_status = "resolved" if resolved else "failed"
        new_reason = "" if resolved else describe_failure(scoring)
        old_status, old_reason = record.get("status"), record.get("reason")
        if is_control:
            same = new_status == old_status and new_reason == old_reason
            note(same, f"run {mode}/{task_id}: CONTROL {old_status}/{old_reason} -> {new_status}/{new_reason}")
            if not same:
                continue
        else:
            note(True, f"run {mode}/{task_id}: {old_status}/{old_reason} -> {new_status}/{new_reason or '(resolved)'}")
        new_record = dict(record)
        new_record["superseded_4_2"] = {
            "status": old_status,
            "reason": old_reason,
            "scoring": record.get("scoring"),
            "ts": record.get("ts"),
        }
        new_record["scoring"] = scoring
        new_record["status"] = new_status
        new_record["reason"] = new_reason
        new_record["rescore"] = {"tool": "benchmarks/rescore_4_2.py", "ts": utcnow_iso(), "caveat": caveat}
        pending_writes.append((run_path, new_record))

    print("\n".join(rows))
    print(f"--- dry_run={not args.apply} writes={len(pending_writes)} failures={len(failures)}")
    if failures:
        return 1
    if args.apply:
        for path, data in pending_writes:
            path.write_text(json.dumps(data, indent=1), encoding="utf-8")
        print(f"applied {len(pending_writes)} record updates")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
