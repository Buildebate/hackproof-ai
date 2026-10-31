"""Step 4.3c.0: synthesis-discipline ablation harness (offline, $0).

Covers ``benchmarks/ablate_synthesis.py`` without live calls: the marker
strip is exactly singular, replay inputs are faithful to the wave-1
archive, response scoring counts blocks/markers, and the dry-run plan
builds for all subjects. Live A/B execution (``--live``) is verified by
the committed evidence table, not by unit tests.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

BENCH_DIR = Path(__file__).resolve().parent.parent.parent / "benchmarks"
sys.path.insert(0, str(BENCH_DIR))

import ablate_synthesis
from ablate_synthesis import (
    MARKER_SPAN,
    SUBJECTS,
    analyze_response,
    build_plan,
    build_variant_b,
    load_builder_turn,
    load_dotenv_meta,
    single_difference_proof,
)
from hackproof.debate.roles.architectural_builder import ROLE_SYSTEM_PROMPT

TWO_BLOCKS_ONE_EMPTY = """requests/models.py
<<<<<<< SEARCH
=======
def invented():
    return True
>>>>>>> REPLACE
requests/sessions.py
<<<<<<< SEARCH
def real():
    pass
=======
def real():
    return 1
>>>>>>> REPLACE
APPROVE"""


def test_marker_span_is_verbatim_suffix_of_role_prompt() -> None:
    assert ROLE_SYSTEM_PROMPT.endswith(MARKER_SPAN.strip())
    assert MARKER_SPAN.strip() in ROLE_SYSTEM_PROMPT


def test_variant_b_removes_exactly_the_span() -> None:
    prompt_a = ROLE_SYSTEM_PROMPT + "\n\nISSUE:\nx\n\nCODE CONTEXT:\ny\n\nROUND: 2"
    prompt_b = build_variant_b(prompt_a)
    assert MARKER_SPAN not in prompt_b
    assert len(prompt_a) - len(prompt_b) == len(MARKER_SPAN)
    assert single_difference_proof(prompt_a, prompt_b) is True


def test_variant_b_refuses_absent_or_ambiguous_span() -> None:
    with pytest.raises(ValueError):
        build_variant_b("no markers here")
    with pytest.raises(ValueError):
        build_variant_b(MARKER_SPAN + " x " + MARKER_SPAN)


def test_single_difference_proof_rejects_extra_edits() -> None:
    filler = "\n".join(f"pad{i}" for i in range(12))
    distant = f"TOP-CHANGED\n{filler}\nBOTTOM-CHANGED"
    assert single_difference_proof(f"top\n{filler}\nbottom", distant) is False
    assert single_difference_proof("same", "same") is False


def test_analyze_response_counts_blocks_and_marker() -> None:
    row = analyze_response(
        TWO_BLOCKS_ONE_EMPTY,
        {
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "total_tokens": 150,
            "finish_reason": "stop",
        },
    )
    # Empty-SEARCH spans are NOT complete by product semantics.
    assert row["complete_blocks"] == 1
    assert row["verdict_marker"] == "APPROVE"
    assert row["finish_reason"] == "stop"
    assert row["total_tokens"] == 150


def test_analyze_response_bare_revise_is_zero_blocks() -> None:
    row = analyze_response("REVISE", {"finish_reason": "stop"})
    assert row["complete_blocks"] == 0
    assert row["verdict_marker"] == "REVISE"


def test_analyze_response_tolerates_empty_text() -> None:
    row = analyze_response("", {"finish_reason": "length"})
    assert row["complete_blocks"] == 0
    assert row["verdict_marker"] is None
    assert row["finish_reason"] == "length"


def test_dry_run_plan_builds_all_subjects() -> None:
    plan = build_plan(list(SUBJECTS))
    assert len(plan) == 4
    for task_id, row in plan.items():
        assert row["single_difference"] is True, task_id
        assert row["prompt_a_chars"] - row["prompt_b_chars"] == len(MARKER_SPAN)


def test_replay_inputs_are_faithful_to_wave1_archive() -> None:
    for task_id in SUBJECTS:
        prompt, history = load_builder_turn(task_id)
        assert prompt.startswith(ROLE_SYSTEM_PROMPT)
        assert isinstance(history, list) and history
        assert MARKER_SPAN in prompt


def test_unknown_subject_fails_loud() -> None:
    with pytest.raises(SystemExit):
        load_builder_turn("nonexistent__task-0")


def test_load_dotenv_meta_imports_keys_without_logging(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("META_API_KEY", raising=False)
    monkeypatch.delenv("META_BASE_URL", raising=False)
    load_dotenv_meta()
    import os

    assert len(os.environ.get("META_API_KEY", "")) > 10
    assert os.environ.get("META_BASE_URL", "").startswith("http")
