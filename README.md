# HackProof AI

Autonomous Zero-Trust Adversarial Engineering Engine for the Meta Global AI
Hackathon 2026.

HackProof AI red-teams AI systems the way a paranoid security engineer would:
every claim is untrusted until proven, every component is probed for failure,
and every verdict is backed by reproducible evidence. This package is the
engine's core scaffolding — packaging, CLI, and test harness — on top of
which adversarial planners, executors, and judges will be built.

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
```

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

## Testing

```bash
source .venv/bin/activate
pytest
```

The suite verifies package import, version reporting, and both CLI backends
(`--version`, `info`, and bare invocation).

## License

Apache-2.0. See [LICENSE](LICENSE).
