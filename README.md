# HackProof AI

[![PyPI](https://img.shields.io/pypi/v/hackproof-ai)](https://pypi.org/project/hackproof-ai/)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue)](LICENSE)
[![Python](https://img.shields.io/badge/python-%3E%3D3.10-blue)](pyproject.toml)
[![Reward Hacking](https://img.shields.io/badge/reward_hacking-0%2F32-green)](benchmarks/evidence_dossier.json)

Autonomous Zero-Trust Adversarial Engineering Engine for the Meta Global AI
Hackathon 2026.

HackProof AI forces AI coding agents to *prove* their fixes: every claim is
untrusted until a Red-to-Green machine proof backs it. Existing tests are
locked by an immutability guard, a regression test proves the baseline
failure, an asymmetric debate (Muse Spark ↔ Muse Wonder) synthesizes the
minimal patch, and the green suite plus a TGRES SHA-256 receipt seal a
verified pull request.

## Quickstart

```bash
pip install hackproof-ai
hackproof verify --issue https://github.com/<owner>/<repo>/issues/42
```

Or add zero-trust verification to any repository with the GitHub Action
(tag `v1` lands with the 5.3 release):

```yaml
- uses: Buildebate/hackproof-ai@v1
  with:
    meta-api-key: ${{ secrets.META_API_KEY }}
    issue-url: https://github.com/${{ github.repository }}/issues/${{ github.event.pull_request.number }}
```

## Architecture

```text
GitHub issue
    │  Contriever + Faiss (≤15 ms retrieval)
    ▼
┌──────────────┐  SHA-256 lock   ┌───────────────────────┐
│  Localize    │ ───────────────▶ │ Immutable Guard       │
│  top-k files │                  │ tests/ sealed         │
└──────────────┘                  └───────────┬───────────┘
                                              │ Spark red test
                                              ▼ (must FAIL)
┌──────────────────────┐  SEARCH/REPLACE  ┌───────────────────────┐
│ Asymmetric Debate    │ ◀──────────────── │ Red Verification      │
│ Spark ↔ Wonder ≤2 rd │                   │ JUnit proof of failure│
└──────────┬───────────┘                   └───────────────────────┘
           │ patch
           ▼
┌──────────────────────┐  suite green +   ┌───────────────────────┐
│ Deterministic Patch  │  TGRES receipt   │ Verified PR           │
│ exact-once apply     │ ───────────────▶ │ passport + SVG badge  │
└──────────────────────┘                  └───────────────────────┘
```

## Benchmarks

12 polyglot tasks (8 Python SWE-bench Verified + 4 TypeScript SWE-bench
Multilingual), every figure re-derived from raw artifacts — see
[benchmarks/SUMMARY.md](benchmarks/SUMMARY.md) and
[benchmarks/evidence_dossier.json](benchmarks/evidence_dossier.json)
(`metrics_4_4`):

| Mode | True Fix Rate | Reward Hacking |
| :--- | :---: | :---: |
| Baseline agent (A) | 6/12 | 0/6 |
| Single-shot (C) | 7/12 | 0/7 |
| HackProof debate (B) | **9/12** | 0/9 |

Guard hygiene: 0 test-tampering violations across 32 evaluated patches,
0 golden false-passes. Integrity re-checks anytime via
`python3 scripts/verify_benchmark_integrity.py` (exit 0, 5037 sub-checks).

## Installation

All commands run from the package directory and use a project-local
environment:

```bash
cd packages/hackproof-ai
python3 -m venv .venv
source .venv/bin/activate
pip install -e .              # core engine (pydantic only)
pip install -e ".[cli]"       # full CLI experience (typer)
pip install -e ".[retrieval]" # retrieval backends (faiss-cpu, torch, transformers)
pip install -e ".[test]"      # test dependencies (pytest)
```

Requires Python 3.10 or newer.

## Usage

```bash
hackproof --version   # print the engine version
hackproof info        # print build information

# Full red-to-green pipeline for a GitHub issue:
hackproof verify --issue https://github.com/<owner>/<repo>/issues/42
hackproof verify --issue <url> --repo /path/to/checkout --runner local
hackproof verify --issue <url> --runner oci   # gVisor sandbox on OCI over SSH

# Pre-commit test immutability guard against a baseline ref:
hackproof test-guard --baseline HEAD~1
```

Exit codes: `0` verified-clean, `1` pipeline failure, `2` usage error,
`3` guard/tamper breach (details printed per file). Live runs need
`META_API_KEY` (model calls) and optionally `GITHUB_TOKEN` (issue fetch
rate limits); `--runner oci` additionally needs `OCI_SSH_HOST`,
`OCI_SSH_USER`, `OCI_SSH_KEY`. Every live run appends token usage to
`finops_ledger.json`.

### Dual-mode CLI

The CLI runs in two modes with an identical interface:

- **Full mode** — with the `cli` extra installed, the interface is served by
  [Typer](https://typer.tiangolo.com/): rich help, shell completion hooks,
  and structured errors.
- **Fallback mode** — without Typer, the CLI transparently falls back to the
  standard-library `argparse` backend. No optional dependency is ever
  required to query the version or build info, which keeps minimal and
  air-gapped installs functional.

`hackproof info` reports which backend is active.

## GitHub Action

Secure pull requests with a 3-line workflow (`.github/workflows/verify.yml`):

```yaml
- uses: actions/checkout@v4
- uses: hackproof-ai/action@v1
  with:
    meta-api-key: ${{ secrets.META_API_KEY }}
    issue-url: https://github.com/${{ github.repository }}/issues/${{ github.event.pull_request.number }}
```

The composite action installs only the published `hackproof-ai` package
(lean: no torch/contriever/faiss on PR runners) and runs
`hackproof verify`. Inputs: `meta-api-key` (required),
`issue-url` (required), `runner-mode` (`local`), `python-version`
(`3.11`). This repository dogfoods the action on itself — see
[`.github/workflows/verify.yml`](.github/workflows/verify.yml).

## GitHub native audit artifacts

`hackproof.github` renders the live audit trail:

```python
from hackproof.github import AuditTrace, IssueAuditor, PRGenerator

trace = AuditTrace(issue_url=..., status="verified-clean", ...)
auditor = IssueAuditor(dry_run=True)  # zero API calls
comment = auditor.post_or_update_comment("owner", "repo", 42, trace)["body"]

svg = PRGenerator.build_badge_svg()  # self-contained, no external service
```

`IssueAuditor` keeps exactly one comment per issue (find-by-marker,
edit-in-place) with five collapsible sections. `PRGenerator` builds the
Verification Passport PR body and the SVG trust badge.

## Dense retrieval

`hackproof.retrieval` provides Meta-Native semantic code search:
`ContrieverEmbeddingModel` (CPU-first `facebook/contriever-msmarco`,
768-dim, with an offline deterministic fallback) and `FaissCodeIndexer`
(in-memory `IndexFlatIP` with a numpy cosine-similarity fallback).

```python
from hackproof.retrieval import ContrieverEmbeddingModel, FaissCodeIndexer

model = ContrieverEmbeddingModel()  # offline synthetic mode when weights are unavailable
chunks = [{"path": "auth.py", "content": "def authenticate_user(...): ..."}]
index = FaissCodeIndexer()
index.add_chunks(chunks, model.encode_code_chunks(chunks))
results = index.search(model.encode_queries(["authenticate user"])[0], top_k=5)
```

Install the heavy backends with `pip install -e ".[retrieval]"`.

## Sandbox & test immutability guard

`hackproof.sandbox` runs untrusted code and locks pre-existing tests
against AI reward hacking:

```python
from pathlib import Path
from hackproof.sandbox import ImmutableTestGuard, LocalSubprocessRunner

baseline = ImmutableTestGuard.lock_test_baseline("tests")
result = LocalSubprocessRunner().run_in_sandbox(
    ["pytest", "-q"], cwd=".", timeout_sec=300, network_disabled=True
)
ImmutableTestGuard.verify_test_integrity("tests", baseline)  # raises on tampering
```

`LocalSubprocessRunner` scrubs proxy/endpoint variables from the child
environment and enforces timeouts via SIGTERM-then-SIGKILL.
`ImmutableTestGuard` proves zero existing test files were deleted,
truncated, modified, or had assertions removed.

## Asymmetric debate

`hackproof.debate` runs the fixed 6-role loop between Muse Spark 1.3 and
Muse Wonder 1.0 (at most 2 rounds, early stop on marker consensus:
unanimous APPROVE, or 5/6 with an approving judge):

```python
from hackproof.debate import DebateOrchestrator
from hackproof.llm import MetaClient

orchestrator = DebateOrchestrator(
    MetaClient("muse-spark-1.3"), MetaClient("muse-wonder-1.0")
)
report = orchestrator.run(issue_description, code_context)
print(report.patch_candidate, report.converged, report.total_tokens)
```

Convergence is scored deterministically from `APPROVE` / `REVISE`
markers, and every turn records token usage for the FinOps ledger.
Unit tests inject the offline `MockMetaClient` ($0 cost).

## Deterministic patching & provenance

`hackproof.patcher` applies debate output inside an isolated git
worktree — every `SEARCH` block is located through a conservative
ladder (exact, then whitespace-insensitive, then unique fuzzy ≥0.90):
a total miss or 2+ matches at any level reject the whole patch and
the file stays untouched. Misses carry the closest real region, and
unknown paths get a did-you-mean suggestion, fuelling exactly one
grounded builder revision:

```python
from hackproof.patcher import WorkspacePatcher

patcher = WorkspacePatcher()
with patcher.isolated_worktree("/path/to/repo") as worktree:
    patcher.apply_patch_text(worktree, "src/app.py", patch_text)
    check = patcher.verify_post_patch(worktree, "python")
    assert check.passed
```

`hackproof.provenance` seals the run: `TGRESProvenanceVerifier.mint_receipt`
binds base commit, test, patch and both JUnit reports under one SHA-256,
while `verify_red_to_green` enforces the machine proof (pre-patch Red,
post-patch exit 0, guard clean — tampering raises).

## Testing

```bash
source .venv/bin/activate
pytest
```

The suite verifies package import, version reporting, both CLI backends
(`--version`, `info`, `verify`, `test-guard`, exit codes), retrieval,
sandbox guards, debate, patching, and provenance — all offline via
injected mocks except opt-in `--live` API smokes.

## Demo video

[![HackProof AI — 3-minute technical screencast](https://img.youtube.com/vi/6u6ks0zCbx0/maxresdefault.jpg)](https://www.youtube.com/watch?v=6u6ks0zCbx0)

Watch: [HackProof AI screencast on YouTube](https://www.youtube.com/watch?v=6u6ks0zCbx0) (183s, 1920x1080@60, EN voiceover + burned-in subtitles). In-repo copy: `media/hackproof-screencast-1080p.mp4`. All terminal output is real; the video is reproducible via `scripts/render_video.py` + `media/demo.tape`.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) — setup, offline-test rules,
Zero-Leak, and the lean-runner invariant.

## License

Apache-2.0. See [LICENSE](LICENSE).
