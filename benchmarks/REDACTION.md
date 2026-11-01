# Benchmark Redaction Policy (Step 4.3, Zero-Falsification)

Raw material under `benchmarks/results/` (transcripts, JUnit XML, diffs,
guard reports, prompts, collect streams) is published **byte-identical** to
what the OCI sandbox produced. Nothing is summarized away; nothing is
hand-edited.

## Redaction rule

If a log ever contains a secret value (API keys in env dumps, tokens in
tracebacks), ONLY the secret value is replaced with the literal
`***REDACTED***`. Results, verdicts, timings, and hashes are never redacted.
Each redaction is listed in this file with the artifact path and the rule
that matched.

## Scan record

- 2026-11-01 (Step 4.3): pattern scan over `benchmarks/results/`
  (`META_API_KEY`, `ELEVENLABS`, `PYPI_API_TOKEN`, `bearer <token>`,
  `ghp_*`, `sk-ant-*`) — **0 hits, 0 redactions applied**.
  Raw tree ships byte-identical to sandbox output.
