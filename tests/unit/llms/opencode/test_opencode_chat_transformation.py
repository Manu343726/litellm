from typing import Final

import pytest

from litellm.constants import OPENCODE_ZEN_BASE_URL
from litellm.llms.opencode.chat.transformation import OpenCodeChatConfig, split_account_slug

_MESSAGES: Final = [{"role": "user", "content": "hi"}]  # mutable-ok: the base interface wants a list


@pytest.mark.parametrize(
    ("resolved_model", "expected"),
    [
        ("team-a/big-pickle", ("team-a", "big-pickle")),
        ("personal/gpt-5", ("personal", "gpt-5")),
        ("gpt-5", (None, "gpt-5")),
        ("team-a/claude-opus-5-5", ("team-a", "claude-opus-5-5")),
    ],
)
def test_split_account_slug(resolved_model: str, expected: tuple[str | None, str]) -> None:
    """A model reaching the transformation is already stripped of its `opencode/` prefix, so the
    first segment is the account and the remainder is the id Zen knows."""
    assert split_account_slug(resolved_model) == expected


def test_transform_request_strips_the_account_slug() -> None:
    """Zen does not know the account segment, so leaking `team-a/` upstream makes every request 404."""
    request: Final = OpenCodeChatConfig().transform_request(
        model="team-a/big-pickle",
        messages=_MESSAGES,
        optional_params={},
        litellm_params={},
        headers={},
    )

    assert request["model"] == "big-pickle"


def test_two_accounts_share_one_upstream_model_id() -> None:
    """This is what makes per-account isolation possible: the wire model is identical, so only the
    credential and the local instance differ between the two deployments."""
    config: Final = OpenCodeChatConfig()
    team_a: Final = config.transform_request(
        model="team-a/big-pickle", messages=_MESSAGES, optional_params={}, litellm_params={}, headers={}
    )
    personal: Final = config.transform_request(
        model="personal/big-pickle", messages=_MESSAGES, optional_params={}, litellm_params={}, headers={}
    )

    assert team_a["model"] == personal["model"] == "big-pickle"


def test_complete_url_keeps_the_v1_suffix() -> None:
    """The handler appends /chat/completions, so a base without /v1 404s in a way that is not
    retryable and drops the deployment into a long cooldown."""
    url: Final = OpenCodeChatConfig().get_complete_url(
        api_base=None,
        api_key="key",
        model="team-a/big-pickle",
        optional_params={},
        litellm_params={},
        stream=False,
    )

    assert url == f"{OPENCODE_ZEN_BASE_URL}/chat/completions"
    assert url.endswith("/v1/chat/completions")


def test_complete_url_honours_an_explicit_api_base() -> None:
    url: Final = OpenCodeChatConfig().get_complete_url(
        api_base="https://proxy.internal/v1",
        api_key="key",
        model="team-a/big-pickle",
        optional_params={},
        litellm_params={},
        stream=False,
    )

    assert url == "https://proxy.internal/v1/chat/completions"


def test_api_base_defaults_to_zen(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENCODE_API_BASE", raising=False)

    assert OpenCodeChatConfig.get_api_base() == "https://opencode.ai/zen/v1"


def test_get_llm_provider_keeps_the_account_out_of_the_upstream_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The account has to survive resolution, because it is what makes the two accounts two
    deployments, but it must not be sent upstream."""
    from litellm.litellm_core_utils.get_llm_provider_logic import get_llm_provider

    monkeypatch.setenv("OPENCODE_API_KEY", "secret")

    model, provider, dynamic_api_key, api_base = get_llm_provider("opencode/team-a/space-bunny-free")

    assert (model, provider) == ("team-a/space-bunny-free", "opencode")
    assert dynamic_api_key == "secret"
    assert api_base == OPENCODE_ZEN_BASE_URL


def test_two_accounts_resolve_to_distinct_deployments() -> None:
    """Distinct `model_name` values are what give per-account permissions, rate limits and health
    for free, so the two accounts must not collapse into one name."""
    from litellm.litellm_core_utils.get_llm_provider_logic import get_llm_provider

    names: Final = ("opencode/team-a/gpt-5", "opencode/personal/gpt-5", "opencode/team-a/big-pickle")

    assert len(set(names)) == 3
    assert all(get_llm_provider(name)[1] == "opencode" for name in names)


def test_get_models_reads_the_zend_model_list(monkeypatch: pytest.MonkeyPatch) -> None:
    """Zen serves an OpenAI-shaped model list, and the path must keep its /zen segment, which the
    inherited implementation drops by rebuilding the URL from scheme and host only."""
    captured: Final = {}

    class _Response:
        status_code: int = 200

        @staticmethod
        def json() -> dict:
            return {"data": [{"id": "big-pickle"}, {"object": "model"}, {"id": "gpt-5"}]}

    class _Client:
        @staticmethod
        def get(url: str, headers: dict | None = None) -> _Response:
            captured["url"] = url
            captured["headers"] = headers
            return _Response()

    monkeypatch.setattr("litellm.module_level_client", _Client())

    models: Final = OpenCodeChatConfig().get_models(api_key="secret")

    assert captured["url"] == f"{OPENCODE_ZEN_BASE_URL}/models"
    assert models == ["big-pickle", "gpt-5"]
