# HackProof AI Benchmark — SUMMARY (Wave 2 Final, Step 4.4 / Plan 43)

12 polyglot tasks (8 Python SWE-bench Verified + 4 TypeScript SWE-bench Multilingual),
two modes, OCI Ampere A1 (Docker + gVisor `runsc`, `--network=none`) over Tailnet.
Machine-readable companion: [`evidence_dossier.json`](evidence_dossier.json)
(reconciled against [`finops_ledger.json`](../finops_ledger.json) and both matrices).

## 1. Scoreboard

| Mode | Wave 1 | Wave 2 | Goal |
|---|---|---|---|
| B (HackProof: guard → red → debate → patch → green + R9/R13) | 4/12 | **7/12** | 9/12 — NOT MET |
| A (single-shot Wonder baseline) | 2/12 | **5/12** | comparison only |

History B: `0/12` raw 4.2 → `1/12` rescore → `4/12` W1 → **`7/12` W2**.
History A: `0/12` raw 4.2 → `2/12` rescore → `2/12` W1 → **`5/12` W2**.
B leads on count but is **not** a superset of A: `1142` resolved in A, failed in B.

## 2. Per-task verdicts (Wave 1 → Wave 2)

Mode B (`results/B/`, [`matrix_summary_B.json`](results/matrix_summary_B.json)):

| Task | W1 | W2 | Class |
|---|---|---|---|
| psf__requests-1142 | no-blocks (length ramble) | f2p-not-fixed:1 (green-retry broke collection) | content |
| psf__requests-2931 | p2p-broken:1 | **resolved** | — |
| psf__requests-5414 | no-blocks (hallucinated file) | **resolved** | — |
| psf__requests-6028 | f2p-not-fixed:2 + p2p-broken:1 | p2p-broken:1 (R9 fixed both F2P) | content |
| pytest-dev__pytest-10081 | resolved | **resolved** (kept) | — |
| pytest-dev__pytest-5787 | no-blocks (hallucinated file) | red-rejected (empty JUnit after resynthesis) | red-quality |
| pytest-dev__pytest-7432 | resolved | **resolved** (kept, 1-round exit) | — |
| pytest-dev__pytest-8399 | resolved | **resolved** (kept, 1-round exit) | — |
| vuejs__core-11589 | f2p-not-fixed:1 | **resolved** | — |
| vuejs__core-11739 | retry-no-blocks | SEARCH-miss after 5× length rambles | ramble+match |
| vuejs__core-11870 | resolved | **resolved** (kept) | — |
| vuejs__core-11899 | guard-reject then SEARCH-miss | f2p-not-fixed:1 (green-retry SEARCH-miss) | content+match |

Mode A (`results/A/`, [`matrix_summary_A.json`](results/matrix_summary_A.json)):
resolved `1142`, `5414`, `11870` (new), `7432`, `8399` (kept);
misses `2931`/`6028` p2p-broken, `10081` f2p-not-fixed+p2p-broken, `5787` SEARCH-miss,
`11589`/`11899` f2p-not-fixed, `11739` no-blocks (last one standing).

## 3. Integrity invariants (all hold)

- **Reward hacking 0%**: 0 violations across 20 evaluated patches (10+10 `clean`);
  `None` guard verdicts only where no patch existed (5787/11739, both modes).
- **Receipts**: 7 minted, exactly the 7 B-resolved (`receipt_version 1.0`); 0 elsewhere.
- **Red**: B 11/12 accepted; 5787 rejected *before* debates (~$0.01, R10 gate saved ~$1.50).
- **Measurability**: 12/12 `done` both modes; no empty base index, no unresolved IDs.
- **R11 early exit live**: 7432 (unanimous, 1 round, 224 s) and 8399 (5/6+judge, 116 s).
- **Debate telemetry B** (11 debates): rounds `{1: 2, 2: 9}`, converged 3,
  duration p50 ≈ 449 s, p95 ≈ 734 s, score range 0.0–1.0.

## 4. FinOps (upper bounds; cache discounts unmeasured by design)

Ledger total **$64.96** (founder truth **$65.49** incl. $0.53 agent sessions);
budget $150, stop-rule $110 not triggered. Waves: 4.2 `$14.93`, W1 `$19.61`,
ablation `$2.75`, **W2 `$21.76`** (B `$17.78` + A `$3.98`).

**W2 envelope verdict: OVERRUN +$3.76 over $18** (incl. ablation).
Measured cause: TS ramble — `11739` $4.69 (5 truncations) + `11589` $4.66
(2 truncations) + `11899` $2.74 = 68% of B spend. Reprice applied with zero drift;
only the historic mixed-model 4.2 row stays unpriced (upper-bounded $2.36).

Cumulative cost per B run: $47.39 / 32 runs ≈ **$1.48**; per B-resolved ≈ $6.77.
Gate 4 sub-$0.35/PR criterion is **not met** on measured upper bounds — gate stays open.

## 5. R9 / R13 live effects (Wave 2 vs Wave 1)

- `no-blocks`: B 4/12 → **0/12**, A 8/12 → **1/12** — marker-strip (R13) cured synthesis collapse.
- Grounded synthesis retry: 0 firings — zero empty syntheses, guardrail held as backstop.
- Green retry (R9): 3 attempts (1142/6028/11899); 6028 F2P fixed 2→0; 0 full flips.
- Single-shot baseline gained +3 from blocks-first alone (1142/5414/11870).

## 6. Wave 3 decision (plan criterion 4.3c.2: 9/12 → stop, else decide)

Outcome **7/12 → founder decision required**. Recommendation: **CONDITIONAL GO** —
rerun the 5 B-misses, B-only (~$10–12, inside the $18 W3 envelope and $84 remainder);
2-of-5 flips reach 9/12. Iterability: 1142/6028/11899 MEDIUM (content),
11739 LOW without an anti-ramble mechanism, 5787 LOW without red-synthesis work.
A parity not repeated (transfer already proved 2/12 → 5/12).

## 7. Provenance

- Wave 1 bytes: commit `7f597e2` (W1 miss records superseded by W2 reruns, history intact).
- Wave 2 code: commit `3c6a81b` (wave label + honest wave buckets).
- Plan: [`43-fixrate-70-measurement-and-rerun-plan.md`](../../../docs/plans/completed/meta-global-ai-hackathon-2026/43-fixrate-70-measurement-and-rerun-plan.md)
  (moved to `completed/` on step `4.3c.3` mint); contour honesty boundary:
  [`ADR-029`](../../../docs/decisions/ADR-029-green-iteration-contour-and-synthesis-guardrail.md).
- Mode: `DEGRADED_SINGLE_AGENT` (self-attested; verification via deterministic tool
  exit codes, SHA-pinned JUnit, and public CI).
