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

## Testing

```bash
source .venv/bin/activate
pytest
```

The suite verifies package import, version reporting, and both CLI backends
(`--version`, `info`, and bare invocation).

## License

Apache-2.0. See [LICENSE](LICENSE).
