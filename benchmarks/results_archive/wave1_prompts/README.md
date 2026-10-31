# Wave 1 debate-prompt archive (immutable replay inputs for the R13 ablation)

Extracted byte-identical from monorepo commit `7f597e2`
(`packages/hackproof-ai/benchmarks/results/prompts/B/<task>/debate_prompts.json`)
on 2026-10-31 (step 12a.0 fallout repair).

Why this exists: Wave 2 reruns overwrote `results/prompts/B/` in place with
the marker-stripped builder prompt (4.3c.1), which broke the ablation
fidelity gate (`load_builder_turn` requires the frozen Wave 1 role prompt).
Replay inputs must be immutable across waves, so they live here now.
`ablate_synthesis.py` reads from this directory; provenance is the commit
SHA above (verify: `git show 7f597e2:<path>` and compare bytes).
