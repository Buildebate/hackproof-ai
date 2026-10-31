"""Step 2.1 (Part A): production MetaClient caps, auth, retry, parity."""

import inspect
import os

import pytest

from hackproof.llm import (
    SPARK_SYSTEM_PROMPT,
    WONDER_SYSTEM_PROMPT,
    MetaAPIError,
    MetaAuthError,
    MetaClient,
    MetaRateLimitError,
    MetaTimeoutError,
)
from mocks.mock_meta_client import MockMetaClient


def _ok_response(content: str = "artifact") -> dict:
    return {
        "choices": [{"message": {"content": content}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }


class _ScriptedTransport:
    """Fake transport replaying (status, body) steps or raising signals."""

    def __init__(self, steps: list) -> None:
        self._steps = list(steps)
        self.requests: list[dict] = []

    def __call__(self, request: dict) -> tuple[int, dict]:
        self.requests.append(request)
        step = self._steps.pop(0) if len(self._steps) > 1 else self._steps[0]
        if isinstance(step, BaseException):
            raise step
        return step


def _client(transport, **kwargs) -> MetaClient:
    kwargs.setdefault("api_key", "TESTKEY")
    return MetaClient(transport=transport, **kwargs)


def test_default_caps_per_model() -> None:
    spark_t = _ScriptedTransport([(200, _ok_response())])
    _client(spark_t).generate_regression_test("i", "c")
    assert spark_t.requests[0]["body"]["max_tokens"] == 12288
    assert spark_t.requests[0]["body"]["model"] == "muse-spark-1.3"
    wonder_t = _ScriptedTransport([(200, _ok_response())])
    _client(wonder_t, model="muse-wonder-1.0").synthesize_patch("c")
    assert wonder_t.requests[0]["body"]["max_tokens"] == 32768


def test_cap_overrides_rejected() -> None:
    with pytest.raises(ValueError, match="exceeds.*cap"):
        MetaClient(api_key="k", model="muse-spark-1.3", max_tokens=12289)
    with pytest.raises(ValueError, match="exceeds.*cap"):
        MetaClient(api_key="k", model="muse-wonder-1.0", max_tokens=32769)
    with pytest.raises(ValueError, match="unknown model"):
        MetaClient(api_key="k", model="gpt-x")
    ok = MetaClient(api_key="k", model="muse-spark-1.3", max_tokens=100)
    assert ok.max_tokens == 100


def test_env_credential_loading(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("META_API_KEY", "ENVKEY")
    monkeypatch.setenv("META_BASE_URL", "https://env.example/v1/")
    client = MetaClient()
    assert client._base_url == "https://env.example/v1"
    assert client._api_key == "ENVKEY"
    explicit = MetaClient(api_key="ARG", base_url="https://arg.example")
    assert explicit._api_key == "ARG"
    monkeypatch.delenv("META_API_KEY")
    with pytest.raises(MetaAuthError, match="META_API_KEY"):
        MetaClient()


def test_zero_leak_repr_and_errors() -> None:
    transport = _ScriptedTransport([(401, {"error": "bad key"})])
    client = _client(transport, api_key="SUPERSECRETKEY")
    assert "SUPERSECRETKEY" not in repr(client)
    with pytest.raises(MetaAuthError) as exc_info:
        client.debate_round("R", "p", [])
    assert "SUPERSECRETKEY" not in str(exc_info.value)
    assert client.usage_log == []
    assert client.last_usage is None


def test_interface_parity_with_mock() -> None:
    for name in (
        "generate_regression_test",
        "debate_round",
        "synthesize_patch",
    ):
        mock_params = list(inspect.signature(getattr(MockMetaClient, name)).parameters)
        live_params = list(inspect.signature(getattr(MetaClient, name)).parameters)
        assert live_params == mock_params, name


def test_system_prompt_constants_identity() -> None:
    spark = _client(_ScriptedTransport([(200, _ok_response())]))
    assert spark.system_prompt is SPARK_SYSTEM_PROMPT
    wonder = _client(
        _ScriptedTransport([(200, _ok_response())]), model="muse-wonder-1.0"
    )
    assert wonder.system_prompt is WONDER_SYSTEM_PROMPT
    spark.generate_regression_test("i", "c")
    body = spark._transport.requests[0]["body"]
    assert body["messages"][0] == {"role": "system", "content": SPARK_SYSTEM_PROMPT}


def test_retry_then_success_on_transient() -> None:
    transport = _ScriptedTransport([(503, {}), (200, _ok_response("recovered"))])
    assert _client(transport).synthesize_patch("c") == "recovered"
    assert len(transport.requests) == 2


def test_no_retry_on_auth_and_rate_limit() -> None:
    auth_t = _ScriptedTransport([(401, {}), (200, _ok_response())])
    with pytest.raises(MetaAuthError, match="401"):
        _client(auth_t).debate_round("R", "p", [])
    assert len(auth_t.requests) == 1
    limit_t = _ScriptedTransport([(429, {}), (200, _ok_response())])
    with pytest.raises(MetaRateLimitError, match="429"):
        _client(limit_t).debate_round("R", "p", [])
    assert len(limit_t.requests) == 1


def test_timeout_retries_once_then_raises() -> None:
    transport = _ScriptedTransport([TimeoutError("boom"), TimeoutError("boom")])
    with pytest.raises(MetaTimeoutError, match="timed out after 2 attempts"):
        _client(transport).generate_regression_test("i", "c")
    assert len(transport.requests) == 2


def test_persistent_5xx_raises_api_error() -> None:
    transport = _ScriptedTransport([(500, {"error": "x"}), (500, {"error": "x"})])
    with pytest.raises(MetaAPIError, match="HTTP 500"):
        _client(transport).synthesize_patch("c")
    assert len(transport.requests) == 2


def test_usage_recorded_per_call() -> None:
    transport = _ScriptedTransport([(200, _ok_response("a"))])
    client = _client(transport)
    client.generate_regression_test("i", "c")
    client.debate_round("R", "p", [{"role": "user", "text": "t"}])
    assert len(client.usage_log) == 2
    assert client.last_usage == {
        "prompt_tokens": 10,
        "completion_tokens": 5,
        "total_tokens": 15,
        "finish_reason": None,
    }
    assert transport.requests[1]["body"]["messages"][-2]["content"] == "t"


def test_malformed_200_payload_raises() -> None:
    transport = _ScriptedTransport([(200, {"nope": True})])
    with pytest.raises(MetaAPIError, match="malformed"):
        _client(transport).synthesize_patch("c")
    assert transport.requests[0]["url"].endswith("/chat/completions")


def test_null_content_on_length_returns_empty_with_reason() -> None:
    body = {
        "choices": [{"message": {"content": None}, "finish_reason": "length"}],
        "usage": {
            "prompt_tokens": 13,
            "completion_tokens": 10,
            "total_tokens": 23,
        },
    }
    transport = _ScriptedTransport([(200, body)])
    client = _client(transport, model="muse-wonder-1.0")
    out = client.synthesize_patch("c")
    assert out == ""
    assert transport.requests[0]["body"]["max_tokens"] == 32768
    assert client.last_usage is not None
    assert client.last_usage["finish_reason"] == "length"
    assert client.last_usage["total_tokens"] == 23


def test_stop_finish_reason_recorded() -> None:
    body = dict(_ok_response("artifact"))
    body["choices"] = [
        {"message": {"content": "artifact"}, "finish_reason": "stop"}
    ]
    client = _client(_ScriptedTransport([(200, body)]))
    assert client.synthesize_patch("c") == "artifact"
    assert client.last_usage is not None
    assert client.last_usage["finish_reason"] == "stop"


def test_red_prompt_requires_failing_assert_and_bans_skip() -> None:
    """Step 4.3b.6 (R10): vacuous reds are refused at the prompt."""
    transport = _ScriptedTransport([(200, _ok_response())])
    _client(transport).generate_regression_test("issue", "ctx")
    user_text = transport.requests[0]["body"]["messages"][-1]["content"]
    assert "MUST FAIL" in user_text
    assert "assertion failure" in user_text
    assert "FORBIDDEN" in user_text
    assert "skip" in user_text
    assert "ISSUE:\nissue" in user_text
    system_text = transport.requests[0]["body"]["messages"][0]["content"]
    assert system_text == SPARK_SYSTEM_PROMPT


def test_live_smoke(pytestconfig: pytest.Config) -> None:
    if not pytestconfig.getoption("live", default=False) or not os.environ.get(
        "META_API_KEY"
    ):
        pytest.skip("live API test requires --live + META_API_KEY")
    client = MetaClient(model="muse-spark-1.3")
    out = client.debate_round("EdgeScout", "reply with the word OK", [])
    assert isinstance(out, str) and len(out) > 0
    assert client.last_usage is not None
    assert client.last_usage["total_tokens"] > 0
