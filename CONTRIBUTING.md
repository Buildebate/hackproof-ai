# Contributing to HackProof AI

Thanks for helping force AI agents to prove their fixes. This package lives
in the Buildebate monorepo (`packages/hackproof-ai/`); the public mirror
[`Buildebate/hackproof-ai`](https://github.com/Buildebate/hackproof-ai)
is exported automatically, so always work in the monorepo.

## Development setup

```bash
cd packages/hackproof-ai
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[cli,retrieval,test]"
```

Requires Python 3.10 or newer.

## Checks before every pull request

```bash
pytest tests/                                  # full offline suite, must be green
ruff check src/ tests/ scripts/                # lint gate (E/F/W)
python3 scripts/verify_benchmark_integrity.py  # benchmark claims still re-derive
```

Live boundaries stay injected: unit tests never touch the network, the Meta
API, or OCI — use `issue_fetcher` / `client_factory` / `runner_override`
stubs (see `tests/unit/test_cli.py`). Opt-in live smokes hide behind
`pytest --live`.

## Rules that are never negotiable

- **Zero-Leak**: tokens (`META_API_KEY`, `GITHUB_TOKEN`, PyPI tokens) travel
  only via environment, never in code, logs, URLs, or test fixtures.
- **No surrogates**: a test without assertions proves nothing; every claim
  in `benchmarks/` must re-derive from raw artifacts.
- **Lean runners**: `action.yml` installs the package plus `pytest`/`numpy`
  only — never `torch`/`contriever`/`faiss`.
- **Docs with code**: user-facing changes update this package's
  `CHANGELOG.md` (`[Unreleased]`) and `README.md` in the same commit.

## License

By contributing you agree your work is licensed under
[Apache-2.0](LICENSE).
