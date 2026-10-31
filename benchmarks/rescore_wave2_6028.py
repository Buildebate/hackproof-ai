"""Step 12a.0: offline rescore of the 6028-B Wave 2 verdict (measurement only).

Recomputes base/agent indexes + scoring from IMMUTABLE archived JUnit XML
(SHA-verified against recorded ``junit_sha256``) using the fixed chain:
the retry-run JUnit is remapped with the retry run's OWN resolution
(``retry_agent_index``), not the first attempt's map. The live record went
``missing`` on an env-dependent path param (``B/`` vs ``B-green-retry/``
infix); the archived bytes score ``passed``.

Mechanical boundary (no judgment calls):
- ONLY run record rescored: B/psf__requests-6028 (+ control B-7432, which
  must reproduce ``resolved`` exactly). B-1142 is NOT rescorable: its
  archived collect streams were overwritten by the failed green-retry
  (exit 2, 0 IDs) while the XML is first-attempt — a mixed pair by
  construction. Retry streams get attempt-suffixed names in 12a.2.
- Base record untouched (already measurable).
- Mode B flip additionally requires recorded red-run + guard-clean.
  No receipt re-mint offline (precedent 4.3a; noted in dossier as 7
  receipts over 8 resolved).

Usage: ``python3 benchmarks/rescore_wave2_6028.py [--apply]`` (dry-run default).
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
    parser = argparse.ArgumentParser(description="Offline 6028-B rescore (dry-run by default).")
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
        run_path = RESULTS / "results" / "B" / f"{task_id}.json"
        record = _load_json(run_path)
        base_xml = RESULTS / "junit_reports" / "base" / f"{task_id}.xml"
        agent_xml = RESULTS / "junit_reports" / "B" / f"{task_id}.xml"
        base_record = _load_json(RESULTS / "results" / "base" / f"{task_id}.json")
        if not base_record.get("junit_sha256") or _sha256(base_xml.read_bytes()) != base_record["junit_sha256"]:
            note(False, f"{task_id}: base junit SHA mismatch (archive tampered?)")
            continue
        if not record.get("junit_sha256") or _sha256(agent_xml.read_bytes()) != record["junit_sha256"]:
            note(False, f"{task_id}: agent junit SHA mismatch (archive tampered?)")
            continue
        base_index = _reindex(base_xml, RESULTS / "collect_reports" / "base" / f"{task_id}.out", task)
        agent_index = _reindex(agent_xml, RESULTS / "collect_reports" / "B" / f"{task_id}.out", task)
        scoring = score_task(task, base_index, agent_index)
        functional = bool(scoring["resolved_functional"])
        guard_clean = (record.get("guard") or {}).get("verdict") == "clean"
        red_ran = bool(record.get("junit_sha256"))
        resolved = functional and guard_clean and red_ran
        new_status = "resolved" if resolved else "failed"
        new_reason = "" if resolved else describe_failure(scoring)
        old_status, old_reason = record.get("status"), record.get("reason")
        if is_control:
            same = new_status == old_status and new_reason == old_reason
            note(same, f"CONTROL B/{task_id}: {old_status}/{old_reason} -> {new_status}/{new_reason}")
            continue
        note(True, f"B/{task_id}: {old_status}/{old_reason} -> {new_status}/{new_reason or '(resolved)'}")
        if args.apply:
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
                "tool": "benchmarks/rescore_wave2_6028.py",
                "ts": utcnow_iso(),
                "caveat": "rescore-resolved; machine receipt not re-minted offline (precedent 4.3a)",
            }
            run_path.write_text(json.dumps(new_record, indent=1), encoding="utf-8")
            rows.append("applied 1 record update")

    print("\n".join(rows))
    print(f"--- dry_run={not args.apply} failures={len(failures)}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
