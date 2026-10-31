# Changelog

All notable user-facing changes to the `hackproof-ai` package are documented
in this file, per release.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

> Sprint trace (implementation steps, gates, TGRES receipts) lives in the
> monorepo log: `Buildebate/buildebate` → `CHANGELOG.md`.

## [Unreleased]

### Added
- Green-v2 bounded laps (Step 12a.2): collection-repair lap on
  collect exit=2 with traceback brief; second content iteration
  gated on strict F2P progress; revision prompt carries the applied
  diff; one R6 region retry per revision apply (ADR-029 §6.1
  amendment); lap-suffixed archives preserve first-attempt streams.
- Red-path hardening (Step 12a.1): red resynthesis escalated from
  Spark to Wonder; red synthesis sees the full top-1 file; empty
  resynthesis gets exactly one grounded format retry with a machine
  defect quote (5787-B returned 0 bytes) instead of a burned judge
  round; every attempt archived.
- R9 remap fix + 6028 rescore (Step 12a.0): retry-run JUnit is now
  remapped with the retry run's own resolution (`retry_agent_index`;
  TS keeps the first-attempt map); committed `rescore_wave2_6028.py`
  flips 6028-B to resolved (F2P 2/2, P2P 185/185) with superseded
  provenance and no offline receipt (4.3a precedent) — B stands at
  8/12. Also repaired Wave 2 fallout: Wave 1 debate prompts extracted
  byte-identical from history into `results_archive/wave1_prompts/`
  as immutable ablation inputs.
- Ledger correction to founder dashboard truth ($89.58 balance):
  -$5.07 priced reconciliation entry (upper-bound bias: unmeasured
  cache discounts + estimate rows), zero reprice drift; dossier and
  SUMMARY updated. R9 scoring bug found (retry JUnit remapped with
  stale resolution) — offline rescore flips 6028-B to resolved.
- Wave 2 final dossier (Steps 4.3c.2–4.3c.3): `benchmarks/SUMMARY.md` +
  `benchmarks/evidence_dossier.json` — honest B 7/12 / A 5/12 (goal 9/12
  not met), reconciled against ledger and both 12/12 matrices; Wave 2
  spend $21.76 vs $18 envelope (TS-ramble overrun, measured cause);
  Wave 3 decision recorded (CONDITIONAL GO, founder call).
- Benchmark dataset manifest (`benchmarks/datasets/tasks_manifest.json`):
  12 pinned SWE-bench tasks (8 Python + 4 TypeScript) with recorded dataset
  hashes and withheld golden tests for reproducible evaluation.
- Per-call `finish_reason` in usage records and the FinOps ledger, so
  truncated (`length`) completions are measured instead of silently lost.
- Known Meta rate card applied to the ledger (Wonder $4/$0.20/$20,
  Spark 1.3 Contributor $0.10/$0.002/$0.20 per 1M tokens): Step 4.2
  actuals $14.93 of $150 (upper bound, cache share unmeasured).
- Single grounded patch revision (Step 4.3b.3/R6): on total apply
  failure the builder gets exactly one retry against patcher ground
  truth (closest region / did-you-mean path); usage drained as
  `ArchitecturalBuilder-retry`, benchmark revisions archived to
  `patch_text_retry.txt`.
- Builder-only full top-1 file context (`build_code_context`, budgets
  48k/60k chars): the role writing SEARCH text sees the whole file,
  other roles keep the 12k head (bounded wave spend).
- Era-collect safety net (Step 4.3b.4/R7): one bounded retry of an
  empty base collect on `python:3.8-slim` (run-dir venv rebuilt, single
  retry, runtime pin + shared prep refresh, every step logged and
  recorded in the base `collect` block); collect stderr is now always
  archived to the transcript. Committed OCI probe
  `benchmarks/probe_collect.py` ($0, no LLM calls).
- Committed ledger repricer (Step 4.3b.9 code part):
  `benchmarks/reprice_ledger.py` prices unpriced entries from the
  ledger `pricing` rate card (dry-run by default, `--apply` to write).
  Priced rows are verified, never rewritten (drift >$0.01 fails loud);
  unknown/mixed-model rows receive a conservative `est_cost_upper_usd`
  and stay unpriced outside the total; the report breaks spend down by
  wave (`bench4.2` / `bench4.3-wave1` / reconciliation) for the Gate
  dossier.
- Wave attribution for benchmark runs: new ledger rows are labeled
  `bench4.3b-wave1:<task>:<mode>` (`BENCH_WAVE_LABEL`) instead of
  reusing the historic `bench4.2:` prefix.
- Wave 1 rerun results (Step 4.3b.9, minted): Mode B 4/12 resolved
  (10081/7432/8399/11870, TGRES receipts minted, guards clean),
  Mode A 2/12 (7432/8399); honest measurability 12/12 in both modes
  with fresh evidence bundles (2931 85/85 on era image 3.8, 6028 187
  indexed); red accepted 12/12; 0% reward hacking both modes.
  Wave spend $19.61 of $25 cap; ledger $40.45 / $150.
  Prior wave0 (4.2 + rescore) records preserved under
  `benchmarks/results_archive/wave0_4_2/`.
- Synthesis-discipline ablation (Step 4.3c.0, minted):
  `benchmarks/ablate_synthesis.py` replays the last builder turns of
  the 4 wave-1 no-blocks cases with/without the end-marker sentences
  (single difference proven by diff). Verdict: markers guilty — the
  A arm reproduces bare `REVISE` 3/4, the stripped B arm emits sane
  blocks for 5414 (2) and 5787 (1); 1142 is stochastic, 11739-B
  rambles to the cap (format guardrail still needed). Evidence table
  `benchmarks/evidence/synthesis_ablation_wave1.json`; 8 Wonder
  calls $2.75 of the $3 envelope.
- Green-feedback contour + synthesis guardrail (Step 4.3c.1, minted):
  one R9 revision (`revise_patch_from_green`, names + assert excerpts,
  no gold) + one re-eval in a fresh run dir for Mode B iterable
  misses (`green_retry_eligible`); R13 marker narrowing (builder
  prompt carries zero marker tokens), blocks-first + stop directives,
  and one grounded retry of empty synthesis (`PatchSynthesis-retry`).
  Wave-1 prompt frozen as `WAVE1_ROLE_SYSTEM_PROMPT` for replay
  fidelity. Honesty boundary: ADR-029.

### Fixed
- Wave attribution for Wave 2 (Step 4.3c.2): `BENCH_WAVE_LABEL`
  rotated to `bench4.3c-wave2`; `wave_of` buckets `waveN` and
  `ablation` honestly instead of folding every `bench4.3*` row
  into `bench4.3-wave1` (Wave 1 stays $19.61, ablation $2.75
  split out, zero drift).
- Marker-consensus debate exit (Step 4.3b.8/R11): the round closes
  on unanimous APPROVE, or 5/6 with an approving judge; markers
  count only as the verdict's last token and negated markers never
  count as approval.
  INTENTIONAL contract change: the 0.85 score threshold,
  `CONVERGENCE_THRESHOLD` and the `convergence_threshold`
  constructor arg are removed (the threshold never fired in
  practice); the score survives as a reporting field. EdgeScout and
  ArchitecturalBuilder prompts now require an end marker (block/case
  parsers ignore the trailing marker line).
- Evidence bundle per run (Step 4.3b.7/R12): full collect
  stdout/stderr, `pip freeze` of the measuring venv, and complete
  debate-round prompts (`debate_prompts.json`, recorded at the
  client boundary with zero product changes) are archived for every
  new run; records carry a self-describing `evidence` block and the
  auditor enforces it (grandfathered records skipped).
- Red-before-debates (Step 4.3b.6/R10): Mode B proves the red test
  on the pristine run dir BEFORE any debate spend; one resynthesis
  with the verbatim judge reason on rejection
  (`SparkRedSynthesis-retry`, `red_test_retry.txt` archive); a second
  rejection fails fast. The red prompt now hard-requires a failing
  assertion and forbids skip/skipif markers and empty bodies.
- ID resolution fallbacks (Step 4.3b.5/R8): path-suffix match
  (`/`-anchored, uniqueness-strict), pytest-8 long-string short-ID
  back-match (100-char threshold), and singleton exhaustion under
  equal case counts. `psf__requests-6028` now resolves 187/187
  (was: 4 unresolved); the OCI probe archives full collect output
  and the 214-ID listing is a sha-pinned regression fixture.
- `psf__requests-2931` pinned to `python:3.8-slim` (requests 2.9.0
  subclasses `collections.MutableMapping`, removed in 3.10): OCI probe
  collects exit 0, 168 IDs, 85/85 manifest IDs resolved (was: exit 2,
  0 IDs, 85 unresolved).
- `SEARCH` matching is now a conservative ladder (exact, then
  whitespace-insensitive, then unique fuzzy ≥0.90 with runner-up
  guard) instead of exact-only; misses carry the closest real region.
  INTENTIONAL contract change: whitespace-only drift now applies.
- Missing patch targets suggest the closest tracked path
  (did-you-mean) instead of a bare `FileNotFoundError`.
- Measurement void closed (Step 4.3a): pytest cases without the `file`
  attribute now resolve via `classname` fallback (465/465 archived cases,
  0 manifest-missing); module-level tests no longer get a phantom class
  segment; vitest names containing `>` (`ref<any[]>`, `>>>`) are kept
  verbatim (11589 79/79, 11899 26/26).
- Offline rescore `benchmarks/rescore_4_2.py`: A-5414, A-10081 and B-5414
  corrected failed → resolved (textbook red-to-green, 0 broken P2P);
  originals preserved under `superseded_4_2`; control B-7432 reproduced.
- `append_finops_entry` preserves unknown top-level ledger keys (rate
  card `pricing`, `cost_basis`) so Wave-1 appends keep the money books.
- `MetaClient` no longer stringifies a `null` completion into `"None"`:
  `content=null` (returned on `finish_reason=length`) yields `""`.
- Debate `TokenUsage` records now carry `finish_reason` per turn.
- Benchmark ledger is appended on infra-error paths too (no lost usage).
- Raised FinOps caps to measured reasoning needs (ADR-028): Spark 12288,
  Wonder 32768 completion tokens; client timeout 60s → 600s.

## [0.2.0rc1] - 2026-10-29 (TestPyPI pre-release)

### Added
- Standalone CLI (`hackproof verify --issue <url> [--runner local|oci]`,
  `hackproof test-guard --baseline <ref>`, exit codes 0/1/2/3) with typer and
  argparse backends and a zero-dependency fallback.
- Install extras (`hackproof-ai[cli]`, `hackproof-ai[retrieval]`,
  `hackproof-ai[test]`); requires Python ≥3.10; Apache-2.0 licensed.
- Dense semantic code retrieval (Contriever 768-dim + Faiss, CPU-first with
  offline and numpy fallbacks).
- Air-gapped sandbox runners (local subprocess + OCI gVisor over SSH) and the
  `ImmutableTestGuard` against AI reward hacking.
- Meta Model API client (`muse-spark-1.3` / `muse-wonder-1.0`, capped,
  retrying, Zero-Leak) plus an offline deterministic `MockMetaClient`.
- System One regression-test synthesis guarded by the Red Baseline Invariant.
- Six-role asymmetric debate protocol (Spark + Wonder, max 2 rounds,
  deterministic convergence scoring, FinOps token accounting).
- FinOps ledger (`finops_ledger.json`): per-call token usage from every run
  tracked against the $150 grant budget.
- Deterministic `WorkspacePatcher` (exact SEARCH/REPLACE in isolated git
  worktrees) and TGRES provenance receipts with red-to-green machine proof.
- Lean composite GitHub Action (`action.yml`) + dogfood `verify.yml`
  workflow for pull requests.
- GitHub native audit artifacts: single live issue comment (5 collapsible
  sections, offline dry-run mode), Verification Passport PR body,
  self-contained SVG trust badge.

### Fixed
- Test-runner stub now resolves relative JUnit report paths against the
  sandbox working directory instead of the repository root (plus a
  regression test); previously a stray `hackproof-green-junit.xml` file
  leaked into the repo root on CLI test runs.
- Removed an unused `json` import in `test_meta_client.py` (pre-existing
  F401); `ruff check` is clean across the whole package.

[Unreleased]: https://github.com/Buildebate/hackproof-ai
[0.2.0rc1]: https://test.pypi.org/project/hackproof-ai/0.2.0rc1/
