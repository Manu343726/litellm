from typing import Final

import pytest

import litellm
from litellm.llms.opencode.chat.transformation import OpenCodeChatConfig


@pytest.fixture
def config() -> OpenCodeChatConfig:
    return OpenCodeChatConfig()


def _completion(monkeypatch: pytest.MonkeyPatch, error: Exception | None) -> list[dict]:
    """Capture what verify_credential sends, and fail it the requested way."""
    sent: Final[list[dict]] = []

    def _fake_completion(**kwargs):
        sent.append(kwargs)
        if error is not None:
            raise error
        return litellm.ModelResponse(choices=[], model=kwargs["model"])

    monkeypatch.setattr(litellm, "completion", _fake_completion)
    return sent


def test_verify_credential_accepts_a_working_key(config: OpenCodeChatConfig, monkeypatch: pytest.MonkeyPatch) -> None:
    sent: Final = _completion(monkeypatch, None)

    assert config.verify_credential("good-key") is None
    assert sent[0]["api_key"] == "good-key"
    assert sent[0]["model"] == OpenCodeChatConfig.PROBE_MODEL


def test_verify_credential_rejects_an_invalid_key(config: OpenCodeChatConfig, monkeypatch: pytest.MonkeyPatch) -> None:
    """The whole point: a wrong key must not be stored."""
    _completion(
        monkeypatch,
        litellm.AuthenticationError(message="Invalid API key.", llm_provider="opencode", model="space-bunny-free"),
    )

    failure: Final = config.verify_credential("bad-key")

    assert failure is not None
    assert "Invalid API key" in failure


def test_verify_credential_treats_rate_limit_as_authenticated(
    config: OpenCodeChatConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A throttled account is still a valid account, so it must not be reported as a bad key."""
    _completion(
        monkeypatch, litellm.RateLimitError(message="slow down", llm_provider="opencode", model="space-bunny-free")
    )

    assert config.verify_credential("throttled-key") is None


def test_verify_credential_reports_an_unreachable_provider(
    config: OpenCodeChatConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    _completion(monkeypatch, ValueError("connection refused"))

    failure: Final = config.verify_credential("some-key")

    assert failure is not None
    assert "connection refused" in failure


def test_probe_model_is_free_and_direct(config: OpenCodeChatConfig) -> None:
    """The probe must be a model that answers a direct call.

    The other free-tier models refuse outside the OpenCode client with a FreeTierError, so
    probing one of those would report a working key as broken.
    """
    assert config.PROBE_MODEL == "space-bunny-free"


def test_get_models_does_not_send_authorization(config: OpenCodeChatConfig, monkeypatch: pytest.MonkeyPatch) -> None:
    """Zen's model list is unauthenticated: it answers 200 to a wrong key and to no key.

    Sending the key would suggest it is being validated when it is not, so a caller that trusts
    a successful listing would store a bad credential believing it checked out.
    """
    captured: Final = {}

    class _Response:
        status_code: int = 200

        @staticmethod
        def json() -> dict:
            return {"data": [{"id": "space-bunny-free"}]}

    class _Client:
        @staticmethod
        def get(url: str, headers: dict | None = None) -> _Response:
            captured["url"] = url
            captured["headers"] = headers
            return _Response()

    monkeypatch.setattr(litellm, "module_level_client", _Client())

    assert config.get_models(api_key="ignored") == ["space-bunny-free"]
    assert captured["headers"] is None
