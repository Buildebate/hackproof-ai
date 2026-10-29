"""Step 1.4: deterministic MockMetaClient + failure injection + guard path."""

import ast
import inspect
from pathlib import Path

import pytest

from hackproof.sandbox import (
    ImmutableTestGuard,
    RewardHackingDetectedError,
)
from mocks.mock_meta_client import (
    MockInjectedError,
    MockMetaClient,
)

ISSUE = "login crashes on empty password"
CONTEXT = "def login(pw): return check(pw)  # raises ValueError on error"


def test_regression_test_deterministic() -> None:
    client = MockMetaClient(model="muse-spark-1.3")
    first = client.generate_regression_test(ISSUE, CONTEXT)
    second = client.generate_regression_test(ISSUE, CONTEXT)
    assert first == second
    assert "def test_repro_" in first
    assert "assert observed is not None" in first
    assert "muse-spark-1.3" in first
    other = client.generate_regression_test("other issue", CONTEXT)
    assert other != first


def test_model_roles_differ() -> None:
    spark = MockMetaClient(model="muse-spark-1.3").generate_regression_test(
        ISSUE, CONTEXT
    )
    wonder = MockMetaClient(model="muse-wonder-1.0").generate_regression_test(
        ISSUE, CONTEXT
    )
    assert spark != wonder
    assert "muse-spark-1.3" in spark
    assert "muse-wonder-1.0" in wonder


def test_debate_round_deterministic_and_history_sensitive() -> None:
    client = MockMetaClient(model="muse-spark-1.3")
    history = [{"role": "EdgeScout", "text": "None input crashes"}]
    first = client.debate_round("ConsensusJudge", "close?", history)
    second = client.debate_round("ConsensusJudge", "close?", history)
    assert first == second
    assert "[muse-spark-1.3:ConsensusJudge]" in first
    assert "(history=1 turns)" in first
    changed = client.debate_round(
        "ConsensusJudge", "close?", history + [{"role": "X", "text": "Y"}]
    )
    assert changed != first
    other_role = client.debate_round("EdgeScout", "close?", history)
    assert other_role != first


def test_synthesize_patch_deterministic() -> None:
    client = MockMetaClient(model="muse-wonder-1.0")
    first = client.synthesize_patch("guard None inputs")
    second = client.synthesize_patch("guard None inputs")
    assert first == second
    assert "<<<<<<< SEARCH" in first
    assert ">>>>>>> REPLACE" in first
    assert "muse-wonder-1.0" in first
    assert client.synthesize_patch("other consensus") != first


def test_timeout_mode_raises_on_all_methods() -> None:
    client = MockMetaClient(fail_mode="timeout")
    with pytest.raises(TimeoutError, match="injected timeout"):
        client.generate_regression_test(ISSUE, CONTEXT)
    with pytest.raises(TimeoutError, match="injected timeout"):
        client.debate_round("EdgeScout", "p", [])
    with pytest.raises(TimeoutError, match="injected timeout"):
        client.synthesize_patch("c")


def test_fault_mode_raises_on_all_methods() -> None:
    client = MockMetaClient(fail_mode="fault")
    assert issubclass(MockInjectedError, RuntimeError)
    with pytest.raises(MockInjectedError, match="injected fault"):
        client.generate_regression_test(ISSUE, CONTEXT)
    with pytest.raises(MockInjectedError, match="injected fault"):
        client.debate_round("EdgeScout", "p", [])
    with pytest.raises(MockInjectedError, match="injected fault"):
        client.synthesize_patch("c")


def test_invalid_model_and_mode_rejected() -> None:
    with pytest.raises(ValueError, match="unknown model"):
        MockMetaClient(model="gpt-x")
    with pytest.raises(ValueError, match="unknown fail_mode"):
        MockMetaClient(fail_mode="explode")
    client = MockMetaClient(model="muse-wonder-1.0", fail_mode="tamper")
    assert client.model == "muse-wonder-1.0"
    assert client.fail_mode == "tamper"


def test_tamper_mode_triggers_guard_path(tmp_path: Path) -> None:
    target = tmp_path / "test_auth.py"
    pre = "def test_login():\n    assert 1 + 1 == 2\n    assert True\n"
    target.write_text(pre)
    baseline = ImmutableTestGuard.lock_test_baseline(tmp_path)
    tampered = MockMetaClient(fail_mode="tamper").generate_regression_test(
        ISSUE, CONTEXT
    )
    ast.parse(tampered)  # detection must come from count drop, not syntax break
    assert "assert observed" not in tampered
    target.write_text(tampered)
    assert (
        ImmutableTestGuard.verify_no_assertion_tampering(pre, tampered) is False
    )
    with pytest.raises(RewardHackingDetectedError) as exc_info:
        ImmutableTestGuard.verify_test_integrity(tmp_path, baseline)
    assert any("modified: test_auth.py" in v for v in exc_info.value.violations)


def test_clean_mode_output_passes_guard(tmp_path: Path) -> None:
    target = tmp_path / "test_auth.py"
    target.write_text("def test_login():\n    assert True\n")
    baseline = ImmutableTestGuard.lock_test_baseline(tmp_path)
    fresh = MockMetaClient().generate_regression_test(ISSUE, CONTEXT)
    (tmp_path / "test_repro_new.py").write_text(fresh)  # new files out of scope
    assert ImmutableTestGuard.verify_test_integrity(tmp_path, baseline) == (True, [])


def test_frozen_interface_signatures() -> None:
    """Pin the 3-method contract Step 2.1's production client must mirror."""
    assert list(
        inspect.signature(MockMetaClient.generate_regression_test).parameters
    ) == ["self", "issue_description", "code_context"]
    assert list(inspect.signature(MockMetaClient.debate_round).parameters) == [
        "self",
        "role",
        "prompt",
        "history",
    ]
    assert list(inspect.signature(MockMetaClient.synthesize_patch).parameters) == [
        "self",
        "debate_consensus",
    ]
