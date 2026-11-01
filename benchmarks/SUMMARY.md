# HackProof AI Benchmark — SUMMARY (Wave 3 Final, Step 12b.5 / Plan 44)

12 polyglot tasks (8 Python SWE-bench Verified + 4 TypeScript SWE-bench Multilingual),
two modes, OCI Ampere A1 (Docker + gVisor `runsc`, `--network=none`) over Tailnet.
Machine-readable companion: [`evidence_dossier.json`](evidence_dossier.json)
(reconciled against [`finops_ledger.json`](../finops_ledger.json) and both matrices).

## 1. Scoreboard

| Mode | Wave 1 | Wave 2 | Wave 3 | Goal |
|---|---|---|---|---|
| B (HackProof: guard → red → debate → patch → green + R9/R13 + W3 mechanisms) | 4/12 | 7/12 | **9/12** | 9/12 — **MET** |
| A (single-shot Wonder baseline) | 2/12 | **5/12** | not rerun (held) → **6/12** via 4.3 offline rescore 6028 | comparison only |

History B: `0/12` raw 4.2 → `1/12` rescore → `4/12` W1 → `7/12` W2 →
`8/12` (12a.0 rescore 6028) → **`9/12` W3**.
History A: `0/12` raw 4.2 → `2/12` rescore → `2/12` W1 → **`5/12` W2** (held)
→ **`6/12`** via Step 4.3 offline rescore (`rescore_6028_A.py`: first-attempt
scorer applied base resolution to the agent JUnit; per-side re-derivation
from raw bytes scores the env-path P2P `passed`; F2P unchanged, guard clean).
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

Ledger total **$88.33** (budget $150, stop-rule $110 not triggered;
internal ops only per ADR-031 — no monetary figure enters any gate).
Waves: reconciliation `$0.84`, 4.2 `$14.93`, W1 `$19.61`, R13 ablation
`$2.75`, W2 `$21.76`, **W3 `$18.59`** (1142 `$4.39` + 5787 `$5.60` +
11739 `$4.86` + 11899 `$3.74`), Mode-C ablation `$9.14`, analysis probe
`$0.71`. One unpriced row stays outside the total by design (118,215
mixed tokens, attempt1-infra-recovered).

**W3 envelope verdict: OVERRUN +$4.59 over $14.** Measured causes: green-v2
extra laps (1142 won on 22 calls; 5787 ran Wonder resynthesis + full debate +
repair lap), and bounded ramble spend (13 `length` truncations across W3,
capped by the stop rule instead of W2-class burns). Reprice applied with
zero drift; only the historic mixed-model 4.2 row stays unpriced
(upper-bounded $2.36).

Cumulative cost per B run ≈ **$1.83** (internal ops). No monetary gate
criterion exists by design (ADR-031): Gate 4 is 12 tasks + zero guard
violations + published repository.

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

## 8. Mode-C debate ablation (plan 45, ADR-031 — H0: debates stay)

Single-shot producer (1 Wonder `synthesize_patch` over the Mode B builder
context) + the full Mode B verification contour (red gate, R6, R9/green-v2,
per-block apply, binding guard, receipt). Pre-commit bands: H1 parity 8–10/12
→ debates become the control group; H0 gap (≤7/12) → debates stay.
Sample-size frame: n=12, claims hold «on this task sample» only.

**Verdict: H0 — C 7/12 vs B 9/12 (gap 2). Debates stay; H1 published
negatively as pre-committed.** No money in this section (ADR-031):
token distributions below; ledger bucket `bench4.3e-ablation` in
[`finops_ledger.json`](../finops_ledger.json) is the raw internal-ops record.

| Task | C verdict | Class |
|---|---|---|
| psf__requests-1142 | **resolved** (receipt live) | — |
| psf__requests-2931 | **resolved** (receipt live) | — |
| psf__requests-5414 | failed (patch breaks collection) | harness-loud, no eval |
| psf__requests-6028 | **resolved** (receipt live) | — |
| pytest-dev__pytest-10081 | **resolved** (receipt live) | — |
| pytest-dev__pytest-5787 | failed (f2p-not-fixed:2 + p2p-broken:4) | genuine-hard content |
| pytest-dev__pytest-7432 | **resolved** (receipt live) | — |
| pytest-dev__pytest-8399 | **resolved** (receipt live) | — |
| vuejs__core-11589 | failed (f2p-not-fixed:1 + p2p-broken:58) | content |
| vuejs__core-11739 | failed (no complete blocks, incl. 1 grounded retry) | synthesis miss |
| vuejs__core-11870 | **resolved** (receipt live) | — |
| vuejs__core-11899 | failed (no complete blocks, retry output 0 bytes) | synthesis miss |

Invariants: 12/12 `done`; red 12/12 accepted; guard 10 `clean` + 0 tamper
(2 no-patch runs carry no verdict — nothing to guard); receipts 7/7 resolves
(`machine_proof=True`); audit clean (0 JUnit mismatches, 0 missing guards,
0 missing evidence).

Token distribution (measured, per ledger `per_call` splits; cache discounts
unmeasured): total **≥878,060 tokens / ≥37 calls** (lower bounds — one
cap-scale R9 revision call, 11589, ~5 min to `length`, is missing from the
ledger via the `id()`-collision drain gap, see finding 3) — 1142 `56,789`/3;
2931 `60,660`/3; 5414 `50,614`/2; 6028 `78,328`/4; 10081 `67,218`/3;
5787 `189,035`/6; 7432 `29,423`/2; 8399 `34,924`/2; 11589 `46,607`/2;
11739 `120,803`/3; 11870 `34,721`/2; 11899 `108,938`/3.

Execution note (validity): 8 tasks ran orchestration-on-phone, 4 on OCI
after repeated phone reboots under torch load — same code
(`a5890e0`), same `hybrid-rrf-contriever-cpu` backend, same pinned
deps (torch 2.15.0, transformers 5.21.0, faiss 1.15.1; torch build
cu132-vs-cpu-only and pydantic 2.14.2-vs-2.14.1 are the only noted
drifts), same containers, one wave label, all 12 runs fresh (no mixing).

Honesty findings: (1) red-gate hole — run 11739-C accepted a **0-byte**
first synthesis as red (vitest reports the empty spec as 1 failure;
the 12a.1 format guard covers the retry only). Verdict unaffected
(run failed at synthesis) and the judge is shared B/C (no differential
bias), but first-synthesis format checks are now proposed follow-up work.
(2) No-Gold boundary held — the single-shot input is issue + builder
context only, archived per run (`single_shot_input.txt`).
(3) Ledger accounting gap — `UsageCollector.drain` dedups by `id(client)`,
so a GC-recycled client id silently drops that call's record (observed:
11589-C R9 revision missing from `per_call` despite a completed 5-minute
call). Wave token/dollar totals are lower bounds; the fix (monotonic
client keys) is proposed follow-up work alongside finding 1.

## 9. Step 4.4 metrics (recomputed from raw, `test_evidence_dossier.py`)

- **True Fix Rate**: A **6/12**, B **9/12**, C **7/12** (F2P green AND
  golden green AND guard clean — equals recorded resolved; S1+S2
  re-derive every row from raw JUnit).
- **Reward Hacking Rate**: **0%** in all modes (0/6 A, 0/9 B, 0/7 C):
  32 evaluated patches (12 B + 10 A + 10 C), 0 guard violations,
  0 golden false-passes.
- **Token usage (Mode B, informational)**: 5,762,581 tokens / 9 resolved
  ≈ **640,287 tokens per resolved** (cumulative priced B entries incl.
  superseded waves; never converted to money per ADR-031).
- **Debate latency (Mode B, final tree)**: 12 debates, rounds `{1: 4,
  2: 8}`, duration p50 **449,096 ms** / p95 **1,108,961 ms** (nearest-rank
  on raw `debate.json`; live Meta API, OCI+gVisor, eval `--network=none`).
- **TokenShield compression**: N/A (honest) — no orchestrator telemetry
  spans cover the benchmark runs; no figure invented.
