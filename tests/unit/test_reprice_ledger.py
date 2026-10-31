"""Step 4.3b.9: FinOps ledger repricing from the ledger rate card (offline).

Covers ``benchmarks/reprice_ledger.py`` with tmp-ledger fixtures (pricing
vectors, not fabricated spend): exact pricing of known model splits,
conservative upper bounds for mixed/unknown entries, priced-entry
immutability, drift detection, and wave attribution. A final test audits
the REAL ``finops_ledger.json`` in dry-run mode: it passes on whatever
entries exist and fails only on genuine inconsistency (priced drift or an
unpriceable priced entry).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

BENCH_DIR = Path(__file__).resolve().parent.parent.parent / "benchmarks"
sys.path.insert(0, str(BENCH_DIR))

import reprice_ledger
import run_benchmark
from reprice_ledger import (
    load_ledger,
    price_entry,
    reprice_ledger as run_reprice,
    verify_priced_entry,
    wave_of,
)

REAL_LEDGER = BENCH_DIR.parent / "finops_ledger.json"

RATE_CARD = {
    "source": "test fixture",
    "unit": "USD per 1M tokens",
    "muse-wonder-1.0": {
        "billed_as": "Muse Wonder",
        "input": 4.0,
        "cached_input": 0.2,
        "output": 20.0,
    },
    "muse-spark-1.3": {
        "billed_as": "Muse Spark 1.3 Contributor",
        "input": 0.1,
        "cached_input": 0.002,
        "output": 0.2,
    },
}


def _ledger(entries: list[dict]) -> dict:
    return {
        "budget_usd": 150.0,
        "total_spent_usd": 0.0,
        "pricing": dict(RATE_CARD),
        "entries": entries,
    }


def _unpriced(issue: str, calls: list[dict], total: int = 0) -> dict:
    return {
        "ts": "2026-10-31T00:00:00+00:00",
        "issue": issue,
        "rounds_used": 0,
        "converged": False,
        "prompt_tokens": sum(c.get("prompt_tokens", 0) for c in calls),
        "completion_tokens": sum(c.get("completion_tokens", 0) for c in calls),
        "total_tokens": total or sum(c.get("total_tokens", 0) for c in calls),
        "est_cost_usd": 0.0,
        "priced": False,
        "per_call": calls,
    }


def test_price_entry_known_mixed_split_is_exact() -> None:
    entry = _unpriced(
        "bench4.3b-wave1:task:B",
        [
            {
                "role": "ArchitecturalBuilder-single",
                "model": "muse-wonder-1.0",
                "prompt_tokens": 3185,
                "completion_tokens": 1500,
                "total_tokens": 4685,
            },
            {
                "role": "red",
                "model": "muse-spark-1.3",
                "prompt_tokens": 1000,
                "completion_tokens": 500,
                "total_tokens": 1500,
            },
        ],
    )
    outcome = price_entry(entry, RATE_CARD)
    assert outcome["action"] == "priced"
    # Wonder: 3185*4 + 1500*20 = 42740; Spark: 1000*0.1 + 500*0.2 = 200.
    assert outcome["cost"] == pytest.approx((42740 + 200) / 1_000_000)


def test_price_entry_unknown_model_gets_upper_bound_only() -> None:
    entry = _unpriced(
        "bench4.2:pytest-dev__pytest-7432:B:attempt1-infra-recovered",
        [
            {
                "role": "debate-aggregate-from-transcript",
                "model": "mixed",
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 118215,
            }
        ],
        total=118215,
    )
    outcome = price_entry(entry, RATE_CARD)
    assert outcome["action"] == "upper-bound"
    assert "mixed" in str(outcome["reason"])


def test_price_entry_missing_splits_gets_upper_bound() -> None:
    assert price_entry({"priced": False}, RATE_CARD)["action"] == "upper-bound"
    entry = _unpriced("bench4.3b-wave1:x:A", [])
    assert price_entry(entry, RATE_CARD)["action"] == "upper-bound"


def test_reprice_apply_prices_and_recomputes_total(tmp_path: Path) -> None:
    ledger = _ledger(
        [
            _unpriced(
                "bench4.3b-wave1:task:A",
                [
                    {
                        "role": "r",
                        "model": "muse-spark-1.3",
                        "prompt_tokens": 1000,
                        "completion_tokens": 500,
                        "total_tokens": 1500,
                    }
                ],
            ),
            {
                "ts": "2026-10-30T16:30:24+00:00",
                "issue": "account-reconciliation: pre-4.2 phases $5.91",
                "total_tokens": 0,
                "est_cost_usd": 5.91,
                "priced": True,
            },
        ]
    )
    report = run_reprice(ledger, apply=True)
    assert len(report["priced"]) == 1
    assert ledger["entries"][0]["priced"] is True
    assert ledger["entries"][0]["est_cost_usd"] == pytest.approx(0.0002)
    # Reconciliation row untouched; total follows the append rule (round 2).
    assert ledger["entries"][1]["est_cost_usd"] == 5.91
    assert ledger["total_spent_usd"] == round(5.91 + 0.0002, 2)
    assert report["waves"]["bench4.3-wave1"]["priced_usd"] == pytest.approx(
        0.0002
    )


def test_reprice_dry_run_leaves_ledger_untouched() -> None:
    ledger = _ledger(
        [_unpriced("bench4.3b-wave1:t:B", [{"model": "mixed", "total_tokens": 10}], total=10)]
    )
    report = run_reprice(ledger, apply=False)
    assert len(report["upper_bound"]) == 1
    assert ledger["entries"][0].get("priced") is False
    assert "est_cost_upper_usd" not in ledger["entries"][0]


def test_reprice_upper_bound_uses_max_output_rate(tmp_path: Path) -> None:
    ledger = _ledger(
        [
            _unpriced(
                "bench4.2:task:B:attempt1",
                [{"model": "mixed", "total_tokens": 118215}],
                total=118215,
            )
        ]
    )
    report = run_reprice(ledger, apply=True)
    row = report["upper_bound"][0]
    assert row["upper_usd"] == pytest.approx(118215 * 20.0 / 1_000_000)
    assert ledger["entries"][0]["priced"] is False
    assert "excluded from priced total" in ledger["entries"][0]["cost_note"]
    assert ledger["total_spent_usd"] == 0.0


def test_verify_priced_entry_flags_tampered_cost() -> None:
    entry = {
        "issue": "bench4.2:task:A",
        "est_cost_usd": 999.0,
        "priced": True,
        "per_call": [
            {
                "model": "muse-spark-1.3",
                "prompt_tokens": 1000,
                "completion_tokens": 500,
            }
        ],
    }
    drift = verify_priced_entry(entry, RATE_CARD)
    assert drift is not None
    assert drift["issue"] == "bench4.2:task:A"
    assert drift["drift"] == pytest.approx(999.0 - 0.0002)


def test_verify_priced_entry_accepts_honest_cost() -> None:
    entry = {
        "issue": "bench4.2:task:A",
        "est_cost_usd": 0.0002,
        "priced": True,
        "per_call": [
            {
                "model": "muse-spark-1.3",
                "prompt_tokens": 1000,
                "completion_tokens": 500,
            }
        ],
    }
    assert verify_priced_entry(entry, RATE_CARD) is None


def test_load_ledger_refuses_missing_rate_card(tmp_path: Path) -> None:
    path = tmp_path / "ledger.json"
    path.write_text(json.dumps({"entries": []}), encoding="utf-8")
    with pytest.raises(SystemExit):
        load_ledger(path)


def test_wave_of_buckets() -> None:
    assert wave_of("bench4.2:task:A") == "bench4.2"
    assert wave_of("bench4.3b-wave1:task:B") == "bench4.3-wave1"
    assert wave_of("account-reconciliation: x") == "account-reconciliation"
    assert wave_of(None) == "other"


def test_wave_label_roundtrip_between_writer_and_repricer() -> None:
    label = run_benchmark.BENCH_WAVE_LABEL
    assert label == "bench4.3b-wave1"
    assert wave_of(f"{label}:psf__requests-1142:B") == "bench4.3-wave1"


class _StubUsage:
    def __init__(self) -> None:
        self.per_call = [
            {
                "role": "ArchitecturalBuilder-single",
                "model": "muse-wonder-1.0",
                "prompt_tokens": 3185,
                "completion_tokens": 1500,
                "total_tokens": 4685,
            }
        ]
        self.prompt_tokens = 3185
        self.completion_tokens = 1500
        self.total_tokens = 4685


class _StubTee:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def log(self, message: str) -> None:
        self.lines.append(message)


def test_ledger_append_run_writes_wave_label_unpriced(tmp_path: Path) -> None:
    ledger_path = tmp_path / "ledger.json"
    ledger_path.write_text(
        json.dumps(
            {
                "budget_usd": 150.0,
                "total_spent_usd": 5.91,
                "pricing": dict(RATE_CARD),
                "entries": [
                    {
                        "issue": "account-reconciliation: x",
                        "est_cost_usd": 5.91,
                        "priced": True,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    run_benchmark.ledger_append_run(
        ledger_path, "psf__requests-5414", "A", _StubUsage(), 0, False,
        _StubTee(),
    )
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    assert len(ledger["entries"]) == 2
    row = ledger["entries"][1]
    assert row["issue"] == "bench4.3b-wave1:psf__requests-5414:A"
    assert row["priced"] is False
    assert row["est_cost_usd"] == 0.0
    # Unpriced append must not move the priced total (repricer does that).
    assert ledger["total_spent_usd"] == 5.91
    # And the repricer must pick the row up exactly afterwards.
    report = run_reprice(ledger, apply=True)
    assert len(report["priced"]) == 1
    assert ledger["entries"][1]["priced"] is True
    assert ledger["entries"][1]["est_cost_usd"] == pytest.approx(0.04274)


def test_real_ledger_dry_run_is_consistent() -> None:
    if not REAL_LEDGER.exists():
        pytest.skip("no live ledger in this checkout")
    ledger = load_ledger(REAL_LEDGER)
    report = run_reprice(ledger, apply=False)
    assert report["drift"] == []
    # The only honestly-unpriceable row is the 4.2 mixed-token recovery.
    for row in report["upper_bound"]:
        assert "mixed" in str(row["reason"]) or "model" in str(row["reason"])
