# Changelog

All notable user-facing changes to the `hackproof-ai` package are documented
in this file, per release.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

> Sprint trace (implementation steps, gates, TGRES receipts) lives in the
> monorepo log: `Buildebate/buildebate` → `CHANGELOG.md`.

## [Unreleased]

## [0.2.0rc1] - 2026-10-29 (TestPyPI pre-release)

### Added
- Standalone CLI (`hackproof verify --issue <url> [--runner local|oci]`,
  `hackproof test-guard --baseline <ref>`, exit codes 0/1/2/3) with typer and
  argparse backends and a zero-dependency fallback.
- Dense semantic code retrieval (Contriever 768-dim + Faiss, CPU-first with
  offline and numpy fallbacks).
- Air-gapped sandbox runners (local subprocess + OCI gVisor over SSH) and the
  `ImmutableTestGuard` against AI reward hacking.
- Meta Model API client (`muse-spark-1.3` / `muse-wonder-1.0`, capped,
  retrying, Zero-Leak) plus an offline deterministic `MockMetaClient`.
- System One regression-test synthesis guarded by the Red Baseline Invariant.
- Six-role asymmetric debate protocol (Spark + Wonder, max 2 rounds,
  deterministic convergence scoring, FinOps token accounting).
- Deterministic `WorkspacePatcher` (exact SEARCH/REPLACE in isolated git
  worktrees) and TGRES provenance receipts with red-to-green machine proof.
- Lean composite GitHub Action (`action.yml`) + dogfood `verify.yml`
  workflow for pull requests.
- GitHub native audit artifacts: single live issue comment (5 collapsible
  sections), Verification Passport PR body, self-contained SVG trust badge.

[Unreleased]: https://github.com/Buildebate/hackproof-ai
[0.2.0rc1]: https://test.pypi.org/project/hackproof-ai/0.2.0rc1/
