# HackProof AI Benchmark — SUMMARY (Wave 3 Final, Step 12b.5 / Plan 44)

12 polyglot tasks (8 Python SWE-bench Verified + 4 TypeScript SWE-bench Multilingual),
two modes, OCI Ampere A1 (Docker + gVisor `runsc`, `--network=none`) over Tailnet.
Machine-readable companion: [`evidence_dossier.json`](evidence_dossier.json)
(reconciled against [`finops_ledger.json`](../finops_ledger.json) and both matrices).

## 1. Scoreboard

| Mode | Wave 1 | Wave 2 | Wave 3 | Goal |
|---|---|---|---|---|
| B (HackProof: guard → red → debate → patch → green + R9/R13 + W3 mechanisms) | 4/12 | 7/12 | **9/12** | 9/12 — **MET** |
| A (single-shot Wonder baseline) | 2/12 | **5/12** | not rerun (held) | comparison only |

History B: `0/12` raw 4.2 → `1/12` rescore → `4/12` W1 → `7/12` W2 →
`8/12` (12a.0 rescore 6028) → **`9/12` W3**.
History A: `0/12` raw 4.2 → `2/12` rescore → `2/12` W1 → **`5/12` W2** (held).
B is still **not** a superset story: `1142` now resolved in BOTH modes
(A already had it since W2); every W2 B-resolve held through W3 untouched.

## 2. Per-task verdicts (Wave 2 → Wave 3)

Mode B (`results/B/`, [`matrix_summary_B.json`](results/matrix_summary_B.json)):

| Task | W2 | W3 | Class |
|---|---|---|---|
| psf__requests-1142 | f2p-not-fixed:1 (retry broke collection) | **resolved** (green-v2 repair lap) | — |
| psf__requests-2931 | resolved | **resolved** (held) | — |
| psf__requests-5414 | resolved | **resolved** (held) | — |
| psf__requests-6028 | resolved (12a.0 rescore) | **resolved** (held) | — |
| pytest-dev__pytest-10081 | resolved | **resolved** (held) | — |
| pytest-dev__pytest-5787 | red-rejected (empty JUnit) | f2p-not-fixed:2 + p2p-broken:3 (red accepted, content miss) | genuine-hard content |
| pytest-dev__pytest-7432 | resolved | **resolved** (held) | — |
| pytest-dev__pytest-8399 | resolved | **resolved** (held) | — |
| vuejs__core-11589 | resolved | **resolved** (held) | — |
| vuejs__core-11739 | SEARCH-miss after 5× length rambles | f2p-not-fixed:1 (ramble stopped, 5 blocks landed) | content (1 F2P) |
| vuejs__core-11870 | resolved | **resolved** (held) | — |
| vuejs__core-11899 | f2p-not-fixed:1 (retry SEARCH-miss) | f2p-not-fixed:1 (revision no-blocks, first stands) | content (1 F2P) |

Mode A held at W2 (not rerun): resolved `1142`, `5414`, `11870`, `7432`, `8399`.

## 3. Integrity invariants (all hold)

- **Reward hacking 0%**: 0 violations across 22 evaluated patches (12 B + 10 A `clean`);
  no `None` guard verdicts remain in B (every run produced a patch); A holds 2 Nones from W2.
- **Receipts**: 8 minted live (`receipt_version 1.0`); 9th resolve (6028 rescore)
  carries no offline receipt per the 4.3a precedent — 8 receipts / 9 resolves.
- **Red**: B **12/12 accepted** (5787 fixed by the 12a.1 red path: Wonder
  resynthesis + full top-1 context + format guard; first-try accept).
- **Measurability**: 12/12 `done` in B (8 held + 4 fresh); no empty base index,
  no unresolved IDs. A matrix held from W2 (12/12).
- **Debate telemetry W3** (4 debates): rounds `{1: 2, 2: 2}`, converged 0,
  `ramble_stopped` 2 (5787, 11739), duration 535–1109 s, score 0.42–0.75.

## 4. FinOps (upper bounds; cache discounts unmeasured by design)

Ledger total **$78.48** (budget $150, stop-rule $110 not triggered);
dashboard truth pending founder reconciliation (books run above dashboard
by design — unmeasured cache discounts).
Waves: 4.2 `$14.93`, W1 `$19.61`, ablation `$2.75`, W2 `$21.76`,
**W3 `$18.59`** (1142 `$4.39` + 5787 `$5.60` + 11739 `$4.86` + 11899 `$3.74`).

**W3 envelope verdict: OVERRUN +$4.59 over $14.** Measured causes: green-v2
extra laps (1142 won on 22 calls; 5787 ran Wonder resynthesis + full debate +
repair lap), and bounded ramble spend (13 `length` truncations across W3,
capped by the stop rule instead of W2-class burns). Reprice applied with
zero drift; only the historic mixed-model 4.2 row stays unpriced
(upper-bounded $2.36).

Cumulative cost per B run: $65.98 / 36 runs ≈ **$1.83**; per B-resolved ≈ $7.33.
Gate 4 sub-$0.35/PR criterion is **not met** on measured upper bounds — gate stays open.

## 5. W3 mechanisms live effects (all four validated)

- **12a.0 R9-remap + 6028 rescore**: held — 6028 resolve intact, no receipt
  (precedent); no W3 rerun needed ($0).
- **12a.1 red path (5787)**: red accepted FIRST try (1916 bytes, exit 1) —
  no more empty-resynthesis rejections. Content genuinely hard (deep pytest
  internals): final `f2p-not-fixed:2 + p2p-broken:3`.
- **12a.2 green-v2 (1142/11899)**: 1142 collect-repair lap fired and the
  grounded revision flipped it to **resolved** (receipt minted live).
  11899 revision returned no blocks → first verdict honestly stands (no eval
  spent on an empty revision). Honest caveat: 5787's completed retry lap
  regressed P2P 1→3 while chasing F2P (verdict = last completed lap).
- **12a.3 anti-ramble + partial apply (11739)**: `ramble_stopped` after
  2 builder cutoffs (bounded $4.86 vs $4.69 unbounded W2 burn with zero
  eval); 5 blocks landed across 3 files (W2: whole file rejected on 1 miss);
  patch reached eval, missed by 1 F2P.

## 6. Wave 4 decision (plan criterion 12b.5: 12/12 → close; 10–11/12 → W4 ≤$6; ≤9/12 → root-cause, no blind W4)

Outcome **9/12 → GOAL MET, phase target reached; NO blind W4.**
All remaining misses are pure content-class with clean guard, accepted red,
and applied patches (5787 genuine-hard, 11739/11899 one F2P each); every
addressable mechanism from the W2 autopsy is built and live-validated 4/4.
A further wave without a new root-cause theory would re-roll stochastic
content at ~$4–5 per task with no addressable lever — forbidden by the plan.
W4 requires a fresh mechanism proposal + founder authorization.

## 7. Provenance

- Wave 2 bytes: commit `9d3d002` (W2 miss records superseded by W3 reruns, history intact).
- Wave 3 code: `5b3da6c` (wave label `bench4.3d-wave3`) + 12a.0–12a.3 mechanism commits.
- W3 run: `WAVE3_EXIT:0`, 4 fresh B results, 12-row B matrix via `--resume` pass.
- Test fallout fixed in-wave: 12a.3 offline vector pinned to the immutable
  committed W2 fixture (`tests/unit/fixtures/vuejs__core-11739.w2.patch_text.txt`,
  byte-identical to pre-W3 HEAD) after the live prompt it read was rotated
  by the rerun; suite 381 passed + 5 skipped, ruff clean.
- Plan: [`44-wave3-road-to-12-12-plan.md`](../../../docs/plans/completed/meta-global-ai-hackathon-2026/44-wave3-road-to-12-12-plan.md)
  (moved to `completed/` on step `12b.5` mint); honesty boundaries:
  [`ADR-029`](../../../docs/decisions/ADR-029-green-iteration-contour-and-synthesis-guardrail.md),
  [`ADR-030`](../../../docs/decisions/ADR-030-wave3-anti-ramble-and-partial-patch-application.md).
- Mode: `DEGRADED_SINGLE_AGENT` (self-attested; verification via deterministic tool
  exit codes, SHA-pinned JUnit, and public CI).
