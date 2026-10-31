"""Step 4.3c.0: A/B ablation of builder end-marker instructions (R13).

Wave 1 revealed a new dominant failure mode: 4/12 B + 8/12 A debates
produce no parseable SEARCH/REPLACE blocks. The leading hypothesis is
that the Step 4.3b.8 end-marker sentences in the ArchitecturalBuilder
role prompt teach verdict-tokens over block emission (the 1142 builder
returned a bare ``REVISE``; the starved synthesis then rambled to the
32768 cap and ``dev.meta.ai`` returned ``content=null`` for
``finish_reason=length`` — literally zero bytes).

This committed script tests exactly one variable: it replays the LAST
archived ArchitecturalBuilder turn of the 4 wave-1 no-blocks B cases
(1142, 5414, 5787, 11739) twice — variant A with the wave-1 prompt
byte-identical (reproduction control), variant B with ONLY the three
marker sentences removed — and records which variant emits complete
blocks. Inputs come from the archived ``debate_prompts.json`` (R12
evidence); history is replayed verbatim in both arms.

Decision rule (plan 4.3c.0): B stably emits blocks where A does not ->
markers guilty (narrow marker scope in 4.3c.1); otherwise a block-format
guardrail is needed regardless of markers.

Usage: ``python3 benchmarks/ablate_synthesis.py`` (default dry-run, $0:
builds variants, verifies the single-difference strip) or ``--live``
(8 Wonder calls, ledger-appended as ``bench4.3c:ablation:*``).
"""

from __future__ import annotations

import argparse
import difflib
import json
import sys
from pathlib import Path

BENCH_DIR = Path(__file__).resolve().parent
PKG_ROOT = BENCH_DIR.parent
sys.path.insert(0, str(PKG_ROOT / "src"))
sys.path.insert(0, str(BENCH_DIR))

from hackproof.debate.roles.architectural_builder import (  # noqa: E402
    extract_blocks,
)
from hackproof.debate.roles.consensus_judge import verdict_marker  # noqa: E402


def load_dotenv_meta() -> None:
    """Import META_* keys from the repo-root .env when unset (Zero-Leak).

    Mirrors run_benchmark.load_dotenv_defaults for the two keys the live
    path needs; values never enter logs or transcripts.
    """
    import os

    if os.environ.get("META_API_KEY") and os.environ.get("META_BASE_URL"):
        return
    env_path = PKG_ROOT.parent.parent / ".env"
    try:
        text = env_path.read_text(encoding="utf-8")
    except OSError:
        return
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key in ("META_API_KEY", "META_BASE_URL") and key not in os.environ:
            os.environ[key] = value.strip().strip('"').strip("'")

SUBJECTS = [
    "psf__requests-1142",
    "psf__requests-5414",
    "pytest-dev__pytest-5787",
    "vuejs__core-11739",
]

# The exact Step 4.3b.8 marker span inside the wave-1 builder prompt
# (the ONLY ablated variable). Kept as a literal so drift fails loud
# instead of silently ablating nothing.
MARKER_SPAN = (
    " After the blocks, end your verdict with exactly one marker word on "
    "its own final line: APPROVE if the patch is complete, or REVISE if "
    "the context is insufficient to write one."
)

# Frozen wave-1 ArchitecturalBuilder role prompt (byte-identical to the
# Step 4.3b.8 text the ablation replayed). Step 4.3c.1 removed the
# marker sentences from the live prompt, so replay fidelity is pinned
# to this literal — never to the evolving live prompt.
WAVE1_ROLE_SYSTEM_PROMPT = (
    "You are ArchitecturalBuilder, a System Two patch synthesizer in Active "
    "Focus mode. Output exact SEARCH/REPLACE blocks "
    "(<<<<<<< SEARCH ... ======= ... >>>>>>> REPLACE), one per hunk, no "
    "line numbers, no prose, no markdown fences. Prefix each block with "
    "its repo-relative target file path on its own line. Copy every "
    "SEARCH block byte-for-byte from the shown CODE CONTEXT (exact "
    "indentation and punctuation); never reconstruct code from memory. "
    "After the blocks, end your verdict with exactly one marker word on "
    "its own final line: APPROVE if the patch is complete, or REVISE if "
    "the context is insufficient to write one."
)

DEFAULT_EVIDENCE = BENCH_DIR / "evidence" / "synthesis_ablation_wave1.json"
DEFAULT_LEDGER = PKG_ROOT / "finops_ledger.json"


def load_builder_turn(task_id: str) -> tuple[str, list[dict]]:
    """Return the LAST archived builder (prompt, history) for a task.

    Fails loud when the wave-1 archive lacks the turn or the prompt no
    longer starts with the frozen wave-1 role prompt (replay would be
    unfaithful).
    """
    path = (
        BENCH_DIR
        / "results_archive"
        / "wave1_prompts"
        / "B"
        / task_id
        / "debate_prompts.json"
    )
    try:
        turns = json.loads(path.read_text(encoding="utf-8"))["turns"]
    except (OSError, ValueError, KeyError) as exc:
        raise SystemExit(f"ablate: cannot load {path}: {exc}")
    builder = [t for t in turns if t.get("role") == "ArchitecturalBuilder"]
    if not builder:
        raise SystemExit(f"ablate: no ArchitecturalBuilder turn for {task_id}")
    prompt = builder[-1].get("prompt", "")
    if not prompt.startswith(WAVE1_ROLE_SYSTEM_PROMPT):
        raise SystemExit(
            f"ablate: {task_id} archived prompt diverges from "
            "WAVE1_ROLE_SYSTEM_PROMPT; replay would be unfaithful"
        )
    history = builder[-1].get("history", [])
    if not isinstance(history, list):
        raise SystemExit(f"ablate: {task_id} archived history is malformed")
    return prompt, history


def build_variant_b(prompt_a: str) -> str:
    """Return variant B: prompt A minus exactly the marker span.

    Raises ``ValueError`` when the span is absent (ablating nothing) or
    appears more than once (ambiguous strip) — the single difference
    must be provable, not assumed.
    """
    found = prompt_a.count(MARKER_SPAN)
    if found != 1:
        raise ValueError(
            f"marker span found {found}x (need exactly 1x); refusing to guess"
        )
    return prompt_a.replace(MARKER_SPAN, "")


def single_difference_proof(prompt_a: str, prompt_b: str) -> bool:
    """True iff B differs from A by exactly one deletion hunk."""
    hunks = [
        h
        for h in difflib.unified_diff(
            prompt_a.splitlines(), prompt_b.splitlines(), lineterm=""
        )
        if h.startswith("@@")
    ]
    return len(hunks) == 1


def analyze_response(text: str, usage: dict | None) -> dict:
    """Score one builder response (blocks, marker, tokens)."""
    blocks = extract_blocks(text or "")
    record: dict[str, object] = {
        "chars": len(text or ""),
        # By product semantics (_iter_block_spans), only blocks with
        # non-empty SEARCH and REPLACE count as complete.
        "complete_blocks": len(blocks),
        "verdict_marker": verdict_marker(text or ""),
        "response_text": text or "",
    }
    if usage:
        record["prompt_tokens"] = int(usage.get("prompt_tokens", 0))
        record["completion_tokens"] = int(usage.get("completion_tokens", 0))
        record["total_tokens"] = int(usage.get("total_tokens", 0))
        record["finish_reason"] = usage.get("finish_reason", "")
    return record


def build_plan(subjects: list[str]) -> dict[str, dict]:
    """Dry-run plan: variants + single-difference proofs ($0)."""
    plan = {}
    for task_id in subjects:
        prompt_a, history = load_builder_turn(task_id)
        prompt_b = build_variant_b(prompt_a)
        plan[task_id] = {
            "prompt_a_chars": len(prompt_a),
            "prompt_b_chars": len(prompt_b),
            "history_items": len(history),
            "single_difference": single_difference_proof(prompt_a, prompt_b),
        }
        if not plan[task_id]["single_difference"]:
            raise SystemExit(f"ablate: {task_id} variant strip is not singular")
    return plan


def run_live(
    subjects: list[str], ledger_path: Path, evidence_path: Path
) -> dict:
    """Execute the A/B replay (8 Wonder calls) and record evidence."""
    import hackproof.cli as cli
    from hackproof.llm.meta_client import MetaClient

    from bench_lib import utcnow_iso

    load_dotenv_meta()
    client = MetaClient("muse-wonder-1.0")
    evidence: dict[str, object] = {
        "ts": utcnow_iso(),
        "model": "muse-wonder-1.0",
        "hypothesis": (
            "4.3b.8 end-marker sentences divert the builder into "
            "verdict-tokens instead of SEARCH/REPLACE blocks"
        ),
        "subjects": {},
    }
    subjects_out = evidence["subjects"]
    assert isinstance(subjects_out, dict)
    for task_id in subjects:
        prompt_a, history = load_builder_turn(task_id)
        prompt_b = build_variant_b(prompt_a)
        rows = []
        subject_out: dict[str, object] = {}
        for treatment, prompt in (("A", prompt_a), ("B", prompt_b)):
            text = client.debate_round("ArchitecturalBuilder", prompt, history)
            usage = client.last_usage or {}
            row = analyze_response(text, usage)
            row["treatment"] = treatment
            subject_out[f"variant_{treatment}"] = row
            rows.append(
                {
                    "role": f"AblationBuilder-{treatment}",
                    "model": "muse-wonder-1.0",
                    "prompt_tokens": row.get("prompt_tokens", 0),
                    "completion_tokens": row.get("completion_tokens", 0),
                    "total_tokens": row.get("total_tokens", 0),
                    "finish_reason": row.get("finish_reason", ""),
                }
            )
            print(
                f"ablate: {task_id} [{treatment}] blocks={row['complete_blocks']} "
                f"marker={row['verdict_marker']} "
                f"tok={row.get('total_tokens', 0)} fin={row.get('finish_reason', '')}",
                flush=True,
            )
        subjects_out[task_id] = subject_out
        cli.append_finops_entry(
            ledger_path,
            f"bench4.3c:ablation:{task_id}",
            rounds_used=0,
            converged=False,
            prompt_tokens=sum(r["prompt_tokens"] for r in rows),
            completion_tokens=sum(r["completion_tokens"] for r in rows),
            total_tokens=sum(r["total_tokens"] for r in rows),
            per_call=rows,
        )
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    evidence_path.write_text(json.dumps(evidence, indent=1), encoding="utf-8")
    print(f"ablate: evidence -> {evidence_path}", flush=True)
    return evidence


def main(argv: list[str] | None = None) -> int:
    """CLI entry point (dry-run default; --live spends ~$1-3)."""
    parser = argparse.ArgumentParser(
        description="A/B ablation of builder end-marker instructions."
    )
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--evidence", default=str(DEFAULT_EVIDENCE))
    parser.add_argument("--ledger", default=str(DEFAULT_LEDGER))
    parser.add_argument("--subject", action="append", default=[])
    args = parser.parse_args(argv)
    subjects = args.subject or SUBJECTS
    unknown = [s for s in subjects if s not in SUBJECTS]
    if unknown:
        print(f"ablate: unknown subject(s): {unknown}", file=sys.stderr)
        return 2
    plan = build_plan(subjects)
    print("ablate: DRY-RUN plan ($0):")
    for task_id, row in plan.items():
        print(
            f"  {task_id}: A={row['prompt_a_chars']}ch B={row['prompt_b_chars']}ch "
            f"hist={row['history_items']} single_diff={row['single_difference']}"
        )
    if not args.live:
        return 0
    run_live(subjects, Path(args.ledger), Path(args.evidence))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
