# HackProof AI — Devpost Submission Dossier (Step 5.3)

Submission packet for the Meta Global AI Hackathon 2026 (Startup Track).
All links below are final and machine-checked by
`tests/unit/test_release_dossier.py`.

## Final links

- GitHub repo: https://github.com/Buildebate/hackproof-ai
- PyPI: https://pypi.org/project/hackproof-ai/ (version 1.0.0)
- Demo video (YouTube): https://m.youtube.com/watch?v=6u6ks0zCbx0
- Screencast artifact (in-repo): `media/hackproof-screencast-1080p.mp4`
  (183.2s, 1920x1080@60, EN voiceover + burned-in subtitles)
- License: Apache-2.0 (`LICENSE`)

## Architecture memo

Diagram: [README.md#architecture](README.md#architecture).

1. An issue is localized to ranked files (hybrid BM25 + Contriever retrieval).
2. Red: Muse Spark synthesizes a failing regression test, executed in an
   isolated git worktree (never in the developer checkout).
3. Debate: 6 asymmetric roles (Muse Spark + Muse Wonder) converge on a
   minimal SEARCH/REPLACE patch with per-block provenance.
4. Green: the full suite must pass and a SHA-256 guard proves that every
   pre-existing test file is byte-identical to baseline.
5. Ship: TGRES receipt + verified PR with badge and passport, via CLI or
   a 3-line GitHub Action.

## Benchmark headline (honest, integrity-verified)

- True Fix Rate: 9/12 = 75% (Mode B, 12-task polyglot suite; Mode A 6/12).
- Reward hacking: 0 violations across 22 evaluated patches (12 B + 10 A).
- Cost: mean $1.55 per issue on the Mode B wave ($18.59 / 12 issues,
  retries included); $2.07 per resolved issue ($18.59 / 9).
- Evidence: `benchmarks/evidence_dossier.json` (SHA-pinned raw tree,
  `scripts/verify_benchmark_integrity.py` exit 0).

## Release provenance

- Monorepo tag: `v1.0.0` ("HackProof AI v1.0.0 — Meta Global AI
  Hackathon 2026 submission"). The tag's `packages/hackproof-ai`
  subtree equals the public repo HEAD tree (parity proof runs live in
  `test_release_dossier.py` and requires post-export state).
- `src/` is byte-identical to the PyPI 1.0.0 build tree (no `src/`
  changes since the 5.2 release commit); post-5.2 commits touch only
  tests, docs, media, and CI manifests.
