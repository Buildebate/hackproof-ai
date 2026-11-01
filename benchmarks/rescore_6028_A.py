"""Step 4.3: offline rescore of the 6028-A Wave 2 verdict (measurement only).

Caught by ``scripts/verify_benchmark_integrity.py`` (S1 status-drift):
the first-attempt scorer remaps the agent JUnit with the BASE run's
resolution (``run_benchmark.py:3617``), so an env-dependent path param
(``base/`` vs ``A/`` run-dir infix) scores ``missing`` -> ``p2p-broken``
even though the archived A JUnit carries the test as ``passed`` under
its own run-dir spelling. The retry path (``retry_agent_index``) already
uses per-side resolution; one-shot Mode A never gets a retry, so the
artifact stood as ``failed/p2p-broken:1``.

This script recomputes base/agent indexes + scoring from IMMUTABLE
archived JUnit XML (SHA-verified against recorded ``junit_sha256``)
using each side's OWN collect output — the exact 12a.0 precedent chain
(``rescore_wave2_6028.py`` for Mode B).

Mechanical boundary (no judgment calls):
- ONLY run record rescored: A/psf__requests-6028 (+ control A-7432, which
  must reproduce ``resolved`` with identical scoring).
- Base record untouched. F2P rows must already be fixed in the raw A
  JUnit (no F2P movement allowed by a rescore).
- Mode A flip additionally requires recorded eval JUnit + post-hoc
  guard with zero violations (binding=false by design; measures only).

Usage: ``python3 benchmarks/rescore_6028_A.py [--apply]`` (dry-run default).
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
    load_manifest,
    parse_collect_output,
    parse_junit,
    remap_collected_index,
    resolve_manifest_ids,
    score_task,
    task_by_id,
    utcnow_iso,
)
from run_benchmark import describe_failure  # noqa: E402

RESULTS = BENCH_DIR / "results"
TASK_ID = "psf__requests-6028"
CONTROL_ID = "pytest-dev__pytest-7432"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _reindex(xml_path: Path, collect_path: Path, task: dict) -> dict[str, str]:
    junit_bytes = xml_path.read_bytes()
    full_index = index_pytest_cases(parse_junit(junit_bytes))
    collected = parse_collect_output(collect_path.read_text(encoding="utf-8"))
    mids = task["fail_to_pass"] + task["pass_to_pass"]
    resolution = resolve_manifest_ids(mids, collected)
    # 12a.0 fixed chain: each run remapped with its OWN resolution.
    return remap_collected_index(resolution, full_index)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="6028-A rescore (dry-run by default).")
    parser.add_argument("--apply", action="store_true", help="Write rescored record.")
    args = parser.parse_args(argv)

    manifest = load_manifest(BENCH_DIR / "datasets" / "tasks_manifest.json")
    rows: list[str] = []
    failures: list[str] = []

    def note(ok: bool, text: str) -> None:
        rows.append(f"[{'OK' if ok else 'FAIL'}] {text}")
        if not ok:
            failures.append(text)

    for task_id, is_control in ((TASK_ID, False), (CONTROL_ID, True)):
        task = task_by_id(manifest, task_id)
        run_path = RESULTS / "results" / "A" / f"{task_id}.json"
        record = _load_json(run_path)
        base_xml = RESULTS / "junit_reports" / "base" / f"{task_id}.xml"
        agent_xml = RESULTS / "junit_reports" / "A" / f"{task_id}.xml"
        base_record = _load_json(RESULTS / "results" / "base" / f"{task_id}.json")
        if not base_record.get("junit_sha256") or _sha256(base_xml.read_bytes()) != base_record["junit_sha256"]:
            note(False, f"{task_id}: base junit SHA mismatch (archive tampered?)")
            continue
        if not record.get("junit_sha256") or _sha256(agent_xml.read_bytes()) != record["junit_sha256"]:
            note(False, f"{task_id}: agent junit SHA mismatch (archive tampered?)")
            continue
        base_index = _reindex(base_xml, RESULTS / "collect_reports" / "base" / f"{task_id}.out", task)
        agent_index = _reindex(agent_xml, RESULTS / "collect_reports" / "A" / f"{task_id}.out", task)
        scoring = score_task(task, base_index, agent_index)
        functional = bool(scoring["resolved_functional"])
        guard_clean = (record.get("guard") or {}).get("violations", None) == []
        eval_ran = bool(record.get("junit_sha256"))
        resolved = functional and guard_clean and eval_ran
        new_status = "resolved" if resolved else "failed"
        new_reason = "" if resolved else describe_failure(scoring)
        old_status, old_reason = record.get("status"), record.get("reason")
        if is_control:
            same = (
                new_status == old_status
                and new_reason == old_reason
                and scoring == record.get("scoring")
            )
            note(same, f"CONTROL A/{task_id}: {old_status}/{old_reason} -> {new_status}/{new_reason}")
            continue
        old_f2p = {
            tid: (row.get("base"), row.get("agent"))
            for tid, row in (record.get("scoring") or {}).get("f2p", {}).items()
        }
        new_f2p = {
            tid: (row.get("base"), row.get("agent"))
            for tid, row in scoring.get("f2p", {}).items()
        }
        note(
            old_f2p == new_f2p,
            f"A/{task_id}: F2P rows unchanged by rescore ({len(new_f2p)} rows)",
        )
        note(True, f"A/{task_id}: {old_status}/{old_reason} -> {new_status}/{new_reason or '(resolved)'}")
        if args.apply and not failures:
            new_record = dict(record)
            new_record["superseded_wave2"] = {
                "status": old_status,
                "reason": old_reason,
                "scoring": record.get("scoring"),
                "ts": record.get("ts"),
            }
            new_record["scoring"] = scoring
            new_record["status"] = new_status
            new_record["reason"] = new_reason
            new_record["rescore"] = {
                "tool": "benchmarks/rescore_6028_A.py",
                "ts": utcnow_iso(),
                "caveat": "first-attempt scorer used base resolution on agent JUnit; rescored per-side (12a.0 chain)",
            }
            run_path.write_text(json.dumps(new_record, indent=1) + "\n", encoding="utf-8")
            rows.append("applied 1 record update")

    print("\n".join(rows))
    print(f"--- dry_run={not args.apply} failures={len(failures)}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
