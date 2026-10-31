"""Step 4.3b.9: price unpriced FinOps ledger entries from the ledger rate card.

Live benchmark runs append honestly-unpriced token entries (``priced: false``,
``est_cost_usd: 0.0``); the Step 4.2 pricing pass was an ad-hoc one-liner that
was never committed. This committed script makes the pricing step reproducible:
it reads rates from ``ledger["pricing"]`` (founder rate card, USD/1M tokens),
prices every entry whose per-call model splits are fully known, and recomputes
``total_spent_usd`` with the exact rule ``append_finops_entry`` uses (rounded
sum of priced entries).

Honesty rules (Zero-Falsification):
- Entries with ``priced: true`` (including ``account-reconciliation``) are
  NEVER rewritten; priced entries with known splits are only *verified*
  (recomputed cost must match within 1 cent, else reported as drift).
- Entries with unknown/mixed models or missing token splits cannot be priced
  exactly: they receive a conservative upper bound (``est_cost_upper_usd``,
  all tokens at the highest output rate in the card) and STAY unpriced,
  excluded from the total. The upper bound is reported, never folded in.
- Cache discounts are unmeasured by the client, so every priced figure is an
  explicit upper estimate (input/output list rates, no cache split).

Usage: ``python3 benchmarks/reprice_ledger.py [--ledger PATH] [--apply]``
(default dry-run: report only, ledger untouched).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

BENCH_DIR = Path(__file__).resolve().parent
PKG_ROOT = BENCH_DIR.parent
DEFAULT_LEDGER = PKG_ROOT / "finops_ledger.json"

VERIFY_TOLERANCE_USD = 0.01


def load_ledger(path: Path) -> dict:
    """Load and sanity-check the ledger file (fail loud on bad shape)."""
    try:
        ledger = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise SystemExit(f"reprice: cannot read ledger {path}: {exc}")
    except ValueError as exc:
        raise SystemExit(f"reprice: ledger {path} is not valid JSON: {exc}")
    if not isinstance(ledger, dict):
        raise SystemExit(f"reprice: ledger {path} root must be an object")
    if not isinstance(ledger.get("entries"), list):
        raise SystemExit(f"reprice: ledger {path} has no 'entries' list")
    pricing = ledger.get("pricing")
    if not isinstance(pricing, dict) or not pricing:
        raise SystemExit(
            f"reprice: ledger {path} has no 'pricing' rate card; "
            "refusing to invent rates"
        )
    return ledger


def rate_for(pricing: dict, model: object) -> tuple[float, float] | None:
    """Return (input_rate, output_rate) per 1M tokens, or None if unknown."""
    if not isinstance(model, str):
        return None
    card = pricing.get(model)
    if not isinstance(card, dict):
        return None
    try:
        return (float(card["input"]), float(card["output"]))
    except (KeyError, TypeError, ValueError):
        return None


def max_output_rate(pricing: dict) -> float:
    """Highest output rate in the card (conservative upper-bound basis)."""
    best = 0.0
    for card in pricing.values():
        if isinstance(card, dict):
            try:
                best = max(best, float(card["output"]))
            except (KeyError, TypeError, ValueError):
                continue
    if best <= 0.0:
        raise SystemExit("reprice: rate card has no usable output rates")
    return best


def price_entry(entry: dict, pricing: dict) -> dict:
    """Price one entry; never mutates (caller applies on --apply).

    Returns ``{"action": ...}`` with action in:
    - ``"keep-priced"``: already priced (cost optionally verified);
    - ``"priced"``: newly priced exactly from per-call splits;
    - ``"upper-bound"``: unpriceable exactly; conservative bound computed.
    """
    if entry.get("priced"):
        return {"action": "keep-priced", "cost": entry.get("est_cost_usd", 0.0)}
    calls = entry.get("per_call")
    if not isinstance(calls, list) or not calls:
        return {"action": "upper-bound", "reason": "no per_call splits"}
    total = 0.0
    for call in calls:
        if not isinstance(call, dict):
            return {"action": "upper-bound", "reason": "malformed per_call row"}
        rates = rate_for(pricing, call.get("model"))
        if rates is None:
            return {
                "action": "upper-bound",
                "reason": f"unknown model {call.get('model')!r}",
            }
        try:
            prompt = int(call.get("prompt_tokens", 0))
            completion = int(call.get("completion_tokens", 0))
        except (TypeError, ValueError):
            return {
                "action": "upper-bound",
                "reason": f"bad token split in {call.get('model')!r}",
            }
        if prompt < 0 or completion < 0:
            return {
                "action": "upper-bound",
                "reason": f"negative token split in {call.get('model')!r}",
            }
        in_rate, out_rate = rates
        total += (prompt * in_rate + completion * out_rate) / 1_000_000
    return {"action": "priced", "cost": total}


def verify_priced_entry(entry: dict, pricing: dict) -> dict | None:
    """Recompute a priced entry with known splits; return drift report or None.

    Returns None when the entry cannot be re-verified (unknown models — e.g.
    reconciliation rows) or when it matches within tolerance.
    """
    if not entry.get("priced"):
        return None
    calls = entry.get("per_call")
    if not isinstance(calls, list) or not calls:
        return None
    probe = dict(entry)
    probe["priced"] = False
    outcome = price_entry(probe, pricing)
    if outcome["action"] != "priced":
        return None
    try:
        recorded = float(entry.get("est_cost_usd", 0.0))
    except (TypeError, ValueError):
        return {
            "issue": entry.get("issue"),
            "recorded": entry.get("est_cost_usd"),
            "recomputed": outcome["cost"],
            "drift": None,
        }
    drift = abs(recorded - float(outcome["cost"]))
    if drift <= VERIFY_TOLERANCE_USD:
        return None
    return {
        "issue": entry.get("issue"),
        "recorded": recorded,
        "recomputed": outcome["cost"],
        "drift": drift,
    }


def wave_of(issue: object) -> str:
    """Wave bucket for Gate cost attribution (prefix before task/mode)."""
    if not isinstance(issue, str):
        return "other"
    if issue.startswith("account-reconciliation"):
        return "account-reconciliation"
    if issue.startswith("bench4.2:"):
        return "bench4.2"
    if issue.startswith("bench4.3"):
        parts = issue.split(":")
        if len(parts) > 1 and parts[1] == "ablation":
            return "bench4.3-ablation"
        match = re.search(r"wave(\d+)", parts[0])
        if match:
            return f"bench4.3-wave{match.group(1)}"
        return "bench4.3-wave1"
    return "other"


def reprice_ledger(ledger: dict, apply: bool) -> dict:
    """Run the pricing pass; mutate ``ledger`` only when ``apply`` is True."""
    pricing = ledger["pricing"]
    ceiling = max_output_rate(pricing)
    report: dict[str, object] = {
        "priced": [],
        "upper_bound": [],
        "drift": [],
        "waves": {},
    }
    for entry in ledger["entries"]:
        if not isinstance(entry, dict):
            continue
        issue = entry.get("issue", "?")
        if entry.get("priced"):
            drift = verify_priced_entry(entry, pricing)
            if drift is not None:
                report["drift"].append(drift)  # type: ignore[union-attr]
            continue
        outcome = price_entry(entry, pricing)
        if outcome["action"] == "priced":
            cost = float(outcome["cost"])
            report["priced"].append({"issue": issue, "cost": cost})  # type: ignore[union-attr]
            if apply:
                entry["est_cost_usd"] = cost
                entry["priced"] = True
        else:
            try:
                total_tokens = int(entry.get("total_tokens", 0))
            except (TypeError, ValueError):
                total_tokens = 0
            bound = total_tokens * ceiling / 1_000_000
            row = {
                "issue": issue,
                "reason": outcome.get("reason"),
                "total_tokens": total_tokens,
                "upper_usd": bound,
            }
            report["upper_bound"].append(row)  # type: ignore[union-attr]
            if apply:
                entry["est_cost_upper_usd"] = bound
                entry["cost_note"] = (
                    f"unpriceable exactly ({outcome.get('reason')}); "
                    f"upper bound ${bound:.2f} at Wonder-output rate, "
                    "excluded from priced total"
                )
    if apply:
        total = 0.0
        for entry in ledger["entries"]:
            if isinstance(entry, dict) and entry.get("priced"):
                try:
                    total += float(entry.get("est_cost_usd", 0.0))
                except (TypeError, ValueError):
                    continue
        ledger["total_spent_usd"] = round(total, 2)
    waves: dict[str, dict[str, float]] = {}
    for entry in ledger["entries"]:
        if not isinstance(entry, dict):
            continue
        bucket = waves.setdefault(
            wave_of(entry.get("issue")), {"priced_usd": 0.0, "upper_usd": 0.0}
        )
        if entry.get("priced"):
            try:
                bucket["priced_usd"] += float(entry.get("est_cost_usd", 0.0))
            except (TypeError, ValueError):
                continue
        else:
            ub = entry.get("est_cost_upper_usd")
            if isinstance(ub, (int, float)):
                bucket["upper_usd"] += float(ub)
            else:
                for row in report["upper_bound"]:  # type: ignore[union-attr]
                    if row["issue"] == entry.get("issue"):
                        bucket["upper_usd"] += float(row["upper_usd"])
                        break
    report["waves"] = waves
    report["total_spent_usd"] = ledger.get("total_spent_usd")
    report["budget_usd"] = ledger.get("budget_usd", 150.0)
    return report


def render_report(report: dict, applied: bool) -> str:
    """Human-readable pricing report (stdout + transcript paste)."""
    lines = [
        f"reprice: {'APPLIED' if applied else 'DRY-RUN'} "
        f"spent=${report['total_spent_usd']} / "
        f"budget=${report['budget_usd']}",
    ]
    for row in report["priced"]:  # type: ignore[union-attr]
        lines.append(f"  priced ${row['cost']:.4f} :: {row['issue']}")
    for row in report["upper_bound"]:  # type: ignore[union-attr]
        lines.append(
            f"  UPPER-BOUND ${row['upper_usd']:.2f} ({row['total_tokens']} tok, "
            f"{row['reason']}) :: {row['issue']}"
        )
    for row in report["drift"]:  # type: ignore[union-attr]
        lines.append(
            f"  DRIFT recorded={row['recorded']} recomputed={row['recomputed']} "
            f":: {row['issue']}"
        )
    lines.append("  waves:")
    waves = report["waves"]
    assert isinstance(waves, dict)
    for wave in sorted(waves):
        bucket = waves[wave]
        lines.append(
            f"    {wave}: priced=${bucket['priced_usd']:.2f} "
            f"upper-unpriced=${bucket['upper_usd']:.2f}"
        )
    if not report["priced"] and not report["upper_bound"] and not report["drift"]:
        lines.append("  no changes: all entries priced and verified")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point; exit 1 on drift (Gate must see it loud)."""
    parser = argparse.ArgumentParser(
        description="Price unpriced FinOps entries from the ledger rate card."
    )
    parser.add_argument("--ledger", default=str(DEFAULT_LEDGER))
    parser.add_argument(
        "--apply", action="store_true", help="write pricing into the ledger"
    )
    args = parser.parse_args(argv)
    path = Path(args.ledger)
    ledger = load_ledger(path)
    report = reprice_ledger(ledger, apply=args.apply)
    if args.apply:
        path.write_text(json.dumps(ledger, indent=2), encoding="utf-8")
    print(render_report(report, applied=args.apply))
    if report["drift"]:
        print("reprice: PRICED-ENTRY DRIFT detected (see above); failing loud")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
