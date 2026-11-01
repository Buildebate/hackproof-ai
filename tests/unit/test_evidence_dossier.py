"""Step 4.4: evidence dossier schema + independent metric recomputation.

Every number the dossier files is re-derived here from RAW artifacts
(run records, raw JUnit via the Step 4.3 verifier, raw debate.json,
finops ledger entries) and compared EXACTLY. Hand-typed drift fails
loud; the TokenShield figure is pinned to honest N/A (no telemetry
spans cover the benchmark runs — inventing a percentage would be a
surrogate).
"""

from __future__ import annotations

import json
import math
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent.parent.parent
BENCH_DIR = PACKAGE_ROOT / "benchmarks"
RESULTS_DIR = BENCH_DIR / "results"
DOSSIER_PATH = BENCH_DIR / "evidence_dossier.json"
LEDGER_PATH = PACKAGE_ROOT / "finops_ledger.json"
VERIFY_SCRIPT = PACKAGE_ROOT / "scripts" / "verify_benchmark_integrity.py"

TASK_IDS = [
    "psf__requests-1142",
    "psf__requests-2931",
    "psf__requests-5414",
    "psf__requests-6028",
    "pytest-dev__pytest-10081",
    "pytest-dev__pytest-5787",
    "pytest-dev__pytest-7432",
    "pytest-dev__pytest-8399",
    "vuejs__core-11589",
    "vuejs__core-11739",
    "vuejs__core-11870",
    "vuejs__core-11899",
]


def _load_dossier() -> dict:
    return json.loads(DOSSIER_PATH.read_text(encoding="utf-8"))


def _verifier_report() -> dict:
    proc = subprocess.run(
        [sys.executable, str(VERIFY_SCRIPT)],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout[-2000:]
    report = json.loads(proc.stdout)
    assert report["ok"] is True
    assert report["mismatches"] == []
    return report


def test_dossier_schema() -> None:
    dossier = _load_dossier()
    for key in (
        "scoreboard",
        "verdicts",
        "guard_reward_hacking",
        "metrics_4_4",
        "debate_telemetry_B_final",
        "finops",
        "ablation_c",
        "provenance",
    ):
        assert key in dossier, f"missing dossier section: {key}"
    scoreboard = dossier["scoreboard"]
    assert scoreboard["denominator"] == 12
    assert isinstance(scoreboard["mode_A_resolved"], int)
    assert isinstance(scoreboard["mode_B_resolved"], int)
    metrics = dossier["metrics_4_4"]
    for key in (
        "true_fix_rate",
        "reward_hacking_rate",
        "token_usage_mode_B",
        "tokenshield_compression_pct",
    ):
        assert key in metrics, f"missing metrics_4_4 block: {key}"
    tele = dossier["debate_telemetry_B_final"]
    assert tele["debates_run"] == tele["duration_ms"]["n"]
    assert tele["measurement_conditions"]


def test_true_fix_rate_matches_raw_recount() -> None:
    dossier = _load_dossier()
    report = _verifier_report()
    filed = dossier["metrics_4_4"]["true_fix_rate"]
    for mode in ("A", "B", "C"):
        resolved = report["recount"][mode]["resolved"]
        assert filed[mode] == f"{resolved}/12", mode
    assert dossier["scoreboard"]["mode_A_resolved"] == 6
    assert dossier["scoreboard"]["mode_B_resolved"] == 9
    assert dossier["ablation_c"]["resolved"] is not None
    assert len(dossier["ablation_c"]["resolved"]) == 7


def test_reward_hacking_rate_recomputed_from_raw() -> None:
    dossier = _load_dossier()
    report = _verifier_report()
    filed = dossier["metrics_4_4"]["reward_hacking_rate"]
    evaluated = 0
    for mode in ("A", "B", "C"):
        violations = 0
        for task_id in TASK_IDS:
            record = json.loads(
                (
                    RESULTS_DIR / "results" / mode / f"{task_id}.json"
                ).read_text(encoding="utf-8")
            )
            guard = record.get("guard") or {}
            if record.get("guard") is not None:
                evaluated += 1
            violations += len(guard.get("violations", []))
        breaches = violations + report["golden_false_pass"][mode]
        resolved = report["recount"][mode]["resolved"]
        assert breaches == 0, mode
        assert filed[mode] == f"0/{resolved}", mode
    assert evaluated == 32  # 12 B + 10 A + 10 C (no-patch runs excluded)


def test_token_usage_recomputed_from_ledger() -> None:
    dossier = _load_dossier()
    ledger = json.loads(LEDGER_PATH.read_text(encoding="utf-8"))
    total_tokens = 0
    buckets: dict[str, int] = defaultdict(int)
    for entry in ledger["entries"]:
        if not entry.get("priced"):
            continue
        parts = entry.get("issue", "").split(":")
        if len(parts) > 1 and parts[-1] == "B" and entry.get("total_tokens"):
            total_tokens += entry["total_tokens"]
            buckets[parts[0]] += entry["total_tokens"]
    filed = dossier["metrics_4_4"]["token_usage_mode_B"]
    assert filed["total_tokens"] == total_tokens
    assert filed["resolved"] == 9
    assert filed["tokens_per_resolved"] == round(total_tokens / 9)
    assert filed["buckets"] == dict(sorted(buckets.items()))
    assert filed["informational_only"] is True


def test_ledger_total_and_budget() -> None:
    dossier = _load_dossier()
    ledger = json.loads(LEDGER_PATH.read_text(encoding="utf-8"))
    recomputed = round(
        sum(
            e.get("est_cost_usd", 0.0)
            for e in ledger["entries"]
            if e.get("priced")
        ),
        2,
    )
    assert ledger["total_spent_usd"] == recomputed
    assert ledger["total_spent_usd"] <= 150.0
    assert ledger["total_spent_usd"] <= 110.0  # stop-rule guard
    assert dossier["finops"]["ledger_total_spent_usd"] == recomputed
    assert dossier["finops"]["budget_usd"] == 150.0
    waves: dict[str, float] = defaultdict(float)
    for entry in ledger["entries"]:
        if entry.get("priced"):
            waves[entry.get("issue", "").split(":")[0]] += entry.get(
                "est_cost_usd", 0.0
            )
    assert dossier["finops"]["waves"] == {
        k: round(v, 2) for k, v in sorted(waves.items())
    }
    unpriced = [e for e in ledger["entries"] if not e.get("priced")]
    assert len(unpriced) == 1
    assert unpriced[0]["total_tokens"] == 118215


def test_debate_latency_recomputed_from_raw() -> None:
    dossier = _load_dossier()
    durations: list[int] = []
    rounds: Counter[int] = Counter()
    scores: list[float] = []
    for path in sorted((RESULTS_DIR / "prompts" / "B").glob("*/debate.json")):
        record = json.loads(path.read_text(encoding="utf-8"))
        durations.append(record["duration_ms"])
        rounds[record["rounds_used"]] += 1
        scores.append(record["convergence_score"])
    durations.sort()
    count = len(durations)
    assert count == 12
    filed = dossier["debate_telemetry_B_final"]
    assert filed["debates_run"] == count
    assert filed["rounds_hist"] == {str(k): rounds[k] for k in sorted(rounds)}
    block = filed["duration_ms"]
    assert block["n"] == count
    assert block["min"] == durations[0]
    assert block["p50"] == durations[math.ceil(0.5 * count) - 1]
    assert block["p95"] == durations[math.ceil(0.95 * count) - 1]
    assert block["max"] == durations[-1]
    assert filed["score_min"] == round(min(scores), 4)
    assert filed["score_max"] == round(max(scores), 4)


def test_tokenshield_compression_honest_na() -> None:
    dossier = _load_dossier()
    block = dossier["metrics_4_4"]["tokenshield_compression_pct"]
    assert block["value"] is None
    assert "N/A" in block["reason"]
