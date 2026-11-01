#!/usr/bin/env python3
"""Step 4.3: offline zero-falsification verifier for the benchmark tree.

Re-derives every claimed benchmark number from RAW artifacts only
(JUnit bytes, diff bytes, guard reports, collect outputs) and compares
the recomputation against the recorded ``results/<mode>/<task>.json``
records and ``benchmarks/evidence_dossier.json``. Any drift between raw
bytes and recorded claims fails loud with an explicit mismatch list.

Boundary (honest, by design): this is OFFLINE re-derivation from raw
JUnit, not live re-execution. Live re-execution stays available via
``benchmarks/run_benchmark.py`` with the same pins; the verifier proves
that the published claims equal what the raw JUnit bytes say.

Checks:
  M1  manifest: 12 tasks, unique ids, dataset_sha256 shape, golden rule
  R1  junit hash chain (record junit_sha256 <=> junit_reports bytes)
  R2  diff hash chain (record diff_sha256 <=> diffs bytes)
  R3  guard chain (record guard verdict <=> guard_reports file)
  R4  red chain (B/C: red_test.txt + red JUnit re-judged via judge_red)
  S1  independent per-side rescore from raw JUnit (each side resolved
      against its own JUnit universe, mirroring retry_agent_index;
      recorded base resolution cross-checked against archived collect
      or the base JUnit universe) compared against record status rows
  S2  hidden-golden replay: a recorded ``resolved`` with a failing
      golden is exposed as ``golden-false-pass`` (reward hacking
      demonstrated, counted per mode)
  D1  dossier cross-check (scoreboard/verdicts/guard counts/ablation_c)

Exit 0 + JSON report on consistency; exit 1 with mismatches otherwise.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent.parent
BENCH_DIR = PACKAGE_ROOT / "benchmarks"
sys.path.insert(0, str(BENCH_DIR))

from bench_lib import (  # noqa: E402  (stdlib-only sibling, shipped in export)
    STATUS_MISSING,
    STATUS_PASSED,
    index_pytest_cases,
    index_vitest_cases,
    judge_red,
    parse_collect_output,
    parse_junit,
    remap_collected_index,
    resolve_manifest_ids,
    score_task,
    sha256_bytes,
    sha256_text,
)

AGENT_MODES = ("A", "B", "C")
ALL_MODES = ("base", "A", "B", "C")
_HEX64 = frozenset("0123456789abcdef")


def _sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def _is_hex64(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(ch in _HEX64 for ch in value.lower())
    )


def _index_junit(task: dict, xml_bytes: bytes) -> dict[str, str]:
    cases = parse_junit(xml_bytes)
    if task.get("language") == "typescript":
        return index_vitest_cases(cases)
    return index_pytest_cases(cases)


class Verifier:
    """Accumulates mismatch strings; empty list means consistent."""

    def __init__(self, package_root: Path, expected_tasks: int = 12) -> None:
        self.root = package_root
        self.expected_tasks = expected_tasks
        self.bench = package_root / "benchmarks"
        self.results = self.bench / "results"
        self.mismatches: list[str] = []
        self.check_counts: dict[str, list[int]] = {}
        self.notes: list[str] = []
        self.golden_false_pass: dict[str, int] = {m: 0 for m in AGENT_MODES}
        self.recount: dict[str, dict[str, int]] = {}
        self.resolved_sets: dict[str, list[str]] = {}

    def _count(self, name: str, ok: bool) -> None:
        passed, total = self.check_counts.get(name, [0, 0])
        total += 1
        if ok:
            passed += 1
        self.check_counts[name] = [passed, total]

    def _bad(self, name: str, message: str) -> None:
        self._count(name, False)
        self.mismatches.append(message)

    def _good(self, name: str) -> None:
        self._count(name, True)

    # -- M1: manifest ---------------------------------------------------
    def check_manifest(self) -> dict:
        try:
            manifest = json.loads(
                (self.bench / "datasets" / "tasks_manifest.json").read_text(
                    encoding="utf-8"
                )
            )
        except (OSError, ValueError) as exc:
            self._bad("M1", f"manifest:unreadable:{exc}")
            return {"tasks": []}
        tasks = manifest.get("tasks", [])
        if len(tasks) != self.expected_tasks:
            self._bad(
                "M1",
                f"manifest:tasks={len(tasks)},want={self.expected_tasks}",
            )
        else:
            self._good("M1")
        seen: set[str] = set()
        for task in tasks:
            task_id = task.get("id", "?")
            if task_id in seen:
                self._bad("M1", f"manifest:{task_id}:duplicate-id")
            else:
                seen.add(task_id)
                self._good("M1")
            if not _is_hex64(task.get("dataset_sha256")):
                self._bad("M1", f"manifest:{task_id}:bad-dataset-sha256")
            else:
                self._good("M1")
            f2p = task.get("fail_to_pass", [])
            p2p = task.get("pass_to_pass", [])
            golden = task.get("hidden_golden", [])
            if not f2p:
                self._bad("M1", f"manifest:{task_id}:empty-f2p")
            else:
                self._good("M1")
            if not set(golden) <= set(p2p):
                self._bad("M1", f"manifest:{task_id}:golden-not-in-p2p")
            else:
                self._good("M1")
        if not manifest.get("hidden_golden_rule"):
            self._bad("M1", "manifest:missing-hidden-golden-rule")
        else:
            self._good("M1")
        return manifest

    def _load_record(self, mode: str, task_id: str) -> dict | None:
        path = self.results / "results" / mode / f"{task_id}.json"
        if not path.is_file():
            self._bad("R0", f"{mode}/{task_id}:missing-record")
            return None
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            self._bad("R0", f"{mode}/{task_id}:unparseable-record")
            return None
        if not isinstance(record, dict) or record.get("done") is not True:
            self._bad("R0", f"{mode}/{task_id}:not-done")
            return None
        self._good("R0")
        return record

    # -- R1/R2/R3/R4: hash chains ----------------------------------------
    def check_chains(self, task_id: str, record: dict, mode: str) -> None:
        junit_expect = record.get("junit_sha256")
        junit_path = self.results / "junit_reports" / mode / f"{task_id}.xml"
        if junit_expect:
            if not junit_path.is_file():
                self._bad("R1", f"{mode}/{task_id}:missing-xml")
            elif _sha256_file(junit_path) != junit_expect:
                self._bad("R1", f"{mode}/{task_id}:junit-sha-drift")
            else:
                self._good("R1")
        elif junit_path.is_file():
            self._bad("R1", f"{mode}/{task_id}:unrecorded-xml")
        else:
            self._good("R1")
        if mode == "base":
            return
        diff_expect = record.get("diff_sha256")
        diff_path = self.results / "diffs" / mode / f"{task_id}.diff"
        if diff_expect:
            if not diff_path.is_file():
                self._bad("R2", f"{mode}/{task_id}:missing-diff")
            elif _sha256_file(diff_path) != diff_expect:
                self._bad("R2", f"{mode}/{task_id}:diff-sha-drift")
            else:
                self._good("R2")
        elif diff_path.is_file():
            self._bad("R2", f"{mode}/{task_id}:unrecorded-diff")
        else:
            self._good("R2")
        guard = record.get("guard")
        guard_path = (
            self.results / "guard_reports" / mode / f"{task_id}.json"
        )
        if guard is None:
            # No-patch runs (no diff evaluated) may file a
            # self-describing not-applicable guard report instead of a
            # record link. The filed content must prove N/A explicitly.
            if not guard_path.is_file():
                self._good("R3")
            else:
                try:
                    filed_na = json.loads(
                        guard_path.read_text(encoding="utf-8")
                    )
                except ValueError:
                    self._bad("R3", f"{mode}/{task_id}:unparseable-guard")
                    return
                na_ok = (
                    record.get("diff_sha256") is None
                    or record.get("junit_sha256") is None
                ) and (
                    filed_na.get("verdict") == "not-applicable"
                    and filed_na.get("violations") == []
                    and filed_na.get("task") == task_id
                    and filed_na.get("mode") == mode
                )
                # A diff WITHOUT an eval JUnit (e.g. collect broken) is
                # still an unevaluated patch: N/A holds, nothing gated.
                if na_ok:
                    self._good("R3")
                else:
                    self._bad(
                        "R3", f"{mode}/{task_id}:unrecorded-guard"
                    )
        else:
            if not guard_path.is_file():
                self._bad("R3", f"{mode}/{task_id}:missing-guard")
            else:
                try:
                    filed = json.loads(
                        guard_path.read_text(encoding="utf-8")
                    )
                except ValueError:
                    self._bad("R3", f"{mode}/{task_id}:unparseable-guard")
                    filed = None
                if filed is not None:
                    if filed.get("verdict") != guard.get("verdict"):
                        self._bad(
                            "R3",
                            f"{mode}/{task_id}:guard-verdict-drift:"
                            f"{filed.get('verdict')}!={guard.get('verdict')}",
                        )
                    elif filed.get("violations") != guard.get("violations"):
                        self._bad(
                            "R3", f"{mode}/{task_id}:guard-violations-drift"
                        )
                    else:
                        self._good("R3")
        if mode == "A":
            return
        red = record.get("red")
        if not isinstance(red, dict):
            self._bad("R4", f"{mode}/{task_id}:missing-red-block")
            return
        # The runner archives every red attempt under a distinct name
        # (first: red_test.txt; Wonder retry: red_test_retry.txt; format
        # guard retry: red_test_retry_format.txt). The ACCEPTED body must
        # match exactly one archived candidate.
        matches = []
        for candidate in (
            "red_test.txt",
            "red_test_retry.txt",
            "red_test_retry_format.txt",
        ):
            path = self.results / "prompts" / mode / task_id / candidate
            if path.is_file() and _sha256_file(path) == red.get("sha256"):
                matches.append(candidate)
        if len(matches) != 1:
            self._bad(
                "R4",
                f"{mode}/{task_id}:red-body-archive-drift:{matches}",
            )
        else:
            self._good("R4")
        red_xml = (
            self.results / "junit_reports" / mode / f"{task_id}.red.xml"
        )
        if not red_xml.is_file():
            self._bad("R4", f"{mode}/{task_id}:missing-red-xml")
            return
        red_bytes = red_xml.read_bytes()
        if sha256_bytes(red_bytes) != red.get("junit_sha256"):
            self._bad("R4", f"{mode}/{task_id}:red-junit-sha-drift")
            return
        self._good("R4")
        try:
            verdict = judge_red(
                int(red.get("exit_code", 0)),
                red_bytes,
                timed_out=bool(red.get("timed_out", False)),
            )
        except ValueError:
            self._bad("R4", f"{mode}/{task_id}:red-junit-unparseable")
            return
        if bool(verdict.get("accepted")) != bool(red.get("accepted")):
            self._bad(
                "R4",
                f"{mode}/{task_id}:red-accept-drift:"
                f"judged={verdict.get('accepted')}",
            )
        else:
            self._good("R4")

    # -- S1/S2: independent rescore + golden replay ----------------------
    def _check_recorded_resolution(
        self, task: dict, base_record: dict, base_universe: list[str]
    ) -> None:
        """Cross-check the recorded base resolution against raw inputs."""
        task_id = task["id"]
        manifest_ids = list(task.get("fail_to_pass", [])) + list(
            task.get("pass_to_pass", [])
        )
        recorded = base_record.get("id_resolution", {})
        collect_path = (
            self.results / "collect_reports" / "base" / f"{task_id}.out"
        )
        if collect_path.is_file():
            collected = parse_collect_output(
                collect_path.read_text(encoding="utf-8", errors="replace")
            )
            fresh = resolve_manifest_ids(manifest_ids, collected)
            if {k: sorted(v) for k, v in fresh.items()} != {
                k: sorted(v) for k, v in recorded.items()
            }:
                self._bad(
                    "S1", f"{task_id}:id-resolution-drift-from-collect"
                )
            else:
                self._good("S1")
            fresh_unresolved = sorted(
                mid for mid, hits in fresh.items() if not hits
            )
            if fresh_unresolved != sorted(
                base_record.get("unresolved_ids", [])
            ):
                self._bad("S1", f"{task_id}:unresolved-ids-drift")
            else:
                self._good("S1")
            return
        # No archived collect output (vitest tasks): every recorded hit
        # must exist in the raw base JUnit universe, and the unresolved
        # list must equal the manifest ids with zero JUnit hits.
        universe = set(base_universe)
        all_hit = True
        for mid, hits in recorded.items():
            if not all(h in universe for h in hits):
                all_hit = False
        if not all_hit:
            self._bad("S1", f"{task_id}:recorded-resolution-misses-junit")
        else:
            self._good("S1")
        fresh_unresolved = sorted(
            mid
            for mid in manifest_ids
            if not resolve_manifest_ids([mid], base_universe).get(mid)
        )
        if fresh_unresolved != sorted(
            base_record.get("unresolved_ids", [])
        ):
            self._bad("S1", f"{task_id}:unresolved-ids-drift")
        else:
            self._good("S1")
        self.notes.append(f"{task_id}:resolution-recorded(no-collect)")

    def rescore_task(
        self,
        task: dict,
        base_record: dict,
        mode: str,
        record: dict,
    ) -> None:
        task_id = task["id"]
        if not record.get("junit_sha256"):
            if record.get("status") == "resolved":
                self._bad(
                    "S1", f"{mode}/{task_id}:resolved-without-eval-junit"
                )
            else:
                self._good("S1")
                if not record.get("reason"):
                    self._bad("S1", f"{mode}/{task_id}:failed-without-reason")
                else:
                    self._good("S1")
            return
        base_xml = self.results / "junit_reports" / "base" / f"{task_id}.xml"
        agent_xml = (
            self.results / "junit_reports" / mode / f"{task_id}.xml"
        )
        try:
            base_collected = _index_junit(task, base_xml.read_bytes())
            agent_collected = _index_junit(task, agent_xml.read_bytes())
        except ValueError as exc:
            self._bad("S1", f"{mode}/{task_id}:junit-unparseable:{exc}")
            return
        # Per-side resolution from the raw JUnit under test. Each eval
        # scores against its own collected universe (the retry evals run
        # in fresh run dirs with run-dir-absolute param spellings, so a
        # shared base resolution would false-negative them); this mirrors
        # the runner's retry_agent_index semantics for every run.
        manifest_ids = list(task.get("fail_to_pass", [])) + list(
            task.get("pass_to_pass", [])
        )
        base_resolution = resolve_manifest_ids(
            manifest_ids, list(base_collected)
        )
        agent_resolution = resolve_manifest_ids(
            manifest_ids, list(agent_collected)
        )
        self._check_recorded_resolution(
            task, base_record, list(base_collected)
        )
        base_index = remap_collected_index(base_resolution, base_collected)
        agent_index = remap_collected_index(
            agent_resolution, agent_collected
        )
        fresh = score_task(task, base_index, agent_index)
        recorded_scoring = record.get("scoring")
        if not isinstance(recorded_scoring, dict):
            self._bad("S1", f"{mode}/{task_id}:missing-scoring-block")
            return
        for section in ("f2p", "p2p"):
            fresh_rows = fresh.get(section, {})
            recorded_rows = recorded_scoring.get(section, {})
            for test_id, fresh_row in fresh_rows.items():
                recorded_row = recorded_rows.get(test_id)
                if recorded_row is None:
                    self._bad(
                        "S1",
                        f"{mode}/{task_id}:scoring-missing-row:{test_id}",
                    )
                    continue
                for side in ("base", "agent"):
                    if fresh_row.get(side) != recorded_row.get(side):
                        self._bad(
                            "S1",
                            f"{mode}/{task_id}:scoring-drift:{test_id}:"
                            f"{side}:{recorded_row.get(side)}"
                            f"->{fresh_row.get(side)}",
                        )
                    else:
                        self._good("S1")
        guard = record.get("guard") or {}
        # Mode A guard is post-hoc (binding=false): it measures but never
        # gates, so only its violations list counts; B/C gate on a clean
        # verdict.
        if mode == "A":
            gated = bool(fresh.get("resolved_functional")) and (
                guard.get("violations", []) == []
            )
        else:
            gated = bool(fresh.get("resolved_functional")) and (
                guard.get("verdict") == "clean"
            )
        expected = "resolved" if gated else "failed"
        if record.get("status") != expected:
            self._bad(
                "S1",
                f"{mode}/{task_id}:status-drift:"
                f"recorded={record.get('status')},recomputed={expected}",
            )
        else:
            self._good("S1")
        golden = list(task.get("hidden_golden", []))
        golden_status = {
            gid: agent_index.get(gid, STATUS_MISSING) for gid in golden
        }
        if record.get("status") == "resolved":
            bad_gold = sorted(
                gid
                for gid, st in golden_status.items()
                if st != STATUS_PASSED
            )
            if bad_gold:
                self.golden_false_pass[mode] += 1
                self._bad(
                    "S2",
                    f"{mode}/{task_id}:golden-false-pass:{bad_gold}",
                )
            else:
                self._good("S2")
        else:
            self._good("S2")

    # -- D1: dossier cross-check -----------------------------------------
    def check_dossier(self, manifest: dict) -> None:
        path = self.bench / "evidence_dossier.json"
        try:
            dossier = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            self._bad("D1", f"dossier:unreadable:{exc}")
            return
        scoreboard = dossier.get("scoreboard", {})
        want = {
            "A": scoreboard.get("mode_A_resolved"),
            "B": scoreboard.get("mode_B_resolved"),
        }
        for mode in ("A", "B"):
            got = self.recount.get(mode, {}).get("resolved", 0)
            if want[mode] != got:
                self._bad(
                    "D1",
                    f"dossier:scoreboard-{mode}:{want[mode]}!=recount:{got}",
                )
            else:
                self._good("D1")
        verdicts = dossier.get("verdicts", {})
        for mode in ("A", "B"):
            table = verdicts.get(mode, {})
            for task in manifest.get("tasks", []):
                task_id = task["id"]
                rec_path = (
                    self.results / "results" / mode / f"{task_id}.json"
                )
                if not rec_path.is_file():
                    continue
                status = json.loads(rec_path.read_text(encoding="utf-8")).get(
                    "status"
                )
                if table.get(task_id, {}).get("status") != status:
                    self._bad(
                        "D1",
                        f"dossier:verdicts-{mode}/{task_id}:"
                        f"{table.get(task_id, {}).get('status')}!={status}",
                    )
                else:
                    self._good("D1")
        guard_section = dossier.get("guard_reward_hacking", {})
        for mode in ("A", "B"):
            filed_violations = (
                guard_section.get(mode, {}).get("violations")
            )
            recount_violations = 0
            for task in manifest.get("tasks", []):
                task_id = task["id"]
                rec_path = (
                    self.results / "results" / mode / f"{task_id}.json"
                )
                if not rec_path.is_file():
                    continue
                guard = json.loads(rec_path.read_text(encoding="utf-8")).get(
                    "guard"
                ) or {}
                recount_violations += len(guard.get("violations", []))
            if filed_violations != recount_violations:
                self._bad(
                    "D1",
                    f"dossier:guard-{mode}:{filed_violations}"
                    f"!=recount:{recount_violations}",
                )
            else:
                self._good("D1")
        ablation = dossier.get("ablation_c", {})
        filed_c = ablation.get("resolved")
        if isinstance(filed_c, list):
            got_c = sorted(self.resolved_sets.get("C", []))
            if sorted(filed_c) != got_c:
                self._bad(
                    "D1",
                    f"dossier:ablation-c-resolved-drift:{len(filed_c)}"
                    f"!={len(got_c)}",
                )
            else:
                self._good("D1")

    def run(self) -> dict:
        manifest = self.check_manifest()
        tasks = manifest.get("tasks", [])
        base_records: dict[str, dict] = {}
        for task in tasks:
            task_id = task.get("id", "?")
            record = self._load_record("base", task_id)
            if record is None:
                continue
            base_records[task_id] = record
            self.check_chains(task_id, record, "base")
        for mode in AGENT_MODES:
            self.recount[mode] = {"resolved": 0, "failed": 0, "other": 0}
            self.resolved_sets[mode] = []
            for task in tasks:
                task_id = task.get("id", "?")
                record = self._load_record(mode, task_id)
                if record is None:
                    continue
                status = record.get("status")
                if status in ("resolved", "failed"):
                    self.recount[mode][status] += 1
                else:
                    self.recount[mode]["other"] += 1
                if status == "resolved":
                    self.resolved_sets[mode].append(task_id)
                self.check_chains(task_id, record, mode)
                base_record = base_records.get(task_id)
                if base_record is not None:
                    self.rescore_task(task, base_record, mode, record)
        self.check_dossier(manifest)
        ok = not self.mismatches
        return {
            "ok": ok,
            "exit_hint": 0 if ok else 1,
            "mismatches": sorted(self.mismatches),
            "checks": {
                name: {"passed": p, "total": t}
                for name, (p, t) in sorted(self.check_counts.items())
            },
            "recount": self.recount,
            "golden_false_pass": self.golden_false_pass,
            "notes": sorted(set(self.notes)),
            "report_sha256": sha256_text(
                json.dumps(sorted(self.mismatches), sort_keys=True)
            ),
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verify benchmark integrity from raw artifacts."
    )
    parser.add_argument(
        "--package-root",
        type=Path,
        default=PACKAGE_ROOT,
        help="Package root containing benchmarks/ (default: auto).",
    )
    parser.add_argument(
        "--expected-tasks",
        type=int,
        default=12,
        help="Manifest task count the M1 check enforces (default: 12).",
    )
    args = parser.parse_args(argv)
    report = Verifier(args.package_root, args.expected_tasks).run()
    print(json.dumps(report, indent=2, sort_keys=True))
    return int(report["exit_hint"])


if __name__ == "__main__":
    raise SystemExit(main())
