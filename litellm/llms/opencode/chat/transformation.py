"""
Translate from OpenAI's `/v1/chat/completions` to OpenCode Zen's
`https://opencode.ai/zen/v1/chat/completions`.

Model names are account-slugged as `opencode/<account>/<model>` so that the same model on two
accounts stays two distinct deployments, and therefore two distinct permission, rate limit and
health scopes. `get_llm_provider` splits on the first `/` only, so by the time a request reaches
this class the provider prefix is gone and `model` is `"<account>/<model>"`. The account segment
selects the credential and is not part of the upstream model id, so it is stripped here.
"""

from collections.abc import Mapping
from typing import Final

import litellm
from litellm.constants import OPENCODE_ZEN_BASE_URL
from litellm.secret_managers.main import get_secret_str
from litellm.types.llms.openai import AllMessageValues

from ...openai.chat.gpt_transformation import OpenAIGPTConfig

_ACCOUNT_SEGMENT_INDEX: Final = 0
_ZEN_MODELS_PATH: Final = "/models"


def split_account_slug(model: str) -> tuple[str | None, str]:
    """Split a resolved model name into its account slug and the upstream model id.

    `opencode/team-a/big-pickle` reaches this module as `team-a/big-pickle`. A bare model name
    means the caller asked for `opencode/gpt-5` with no account segment, so the upstream id is
    the whole string and there is no account to report.
    """
    account, separator, remainder = model.partition("/")
    if not separator:
        return None, model
    return account, remainder


class OpenCodeChatConfig(OpenAIGPTConfig):
    """OpenCode Zen speaks the OpenAI chat-completions wire format.

    Every Zen model is pinned to one of several upstream paths by OpenCode itself; this class
    targets the `/chat/completions` family, which is the one the free and cheap models use. The
    provider carries no per-request translation of its own beyond dropping the account segment.
    """

    @property
    def custom_llm_provider(self) -> str:
        return "opencode"

    @staticmethod
    def get_api_base(api_base: str | None = None) -> str:
        """Zen's base URL must keep its `/v1` suffix.

        The handler appends `/chat/completions` to whatever it is given, so a base that stops at
        `/zen` produces a 404 that is not retryable, which drops the deployment into a long
        cooldown and surfaces as "No deployments available".
        """
        return api_base or litellm.api_base or get_secret_str("OPENCODE_API_BASE") or OPENCODE_ZEN_BASE_URL

    @staticmethod
    def get_api_key(api_key: str | None = None) -> str | None:
        return api_key or litellm.api_key or get_secret_str("OPENCODE_API_KEY")

    def get_complete_url(
        self,
        api_base: str | None,
        api_key: str | None,
        model: str,
        optional_params: dict,
        litellm_params: dict,
        stream: bool | None = None,
    ) -> str:
        return super().get_complete_url(
            api_base=self.get_api_base(api_base),
            api_key=api_key,
            model=model,
            optional_params=optional_params,
            litellm_params=litellm_params,
            stream=stream,
        )

    def transform_request(
        self,
        model: str,
        messages: list[AllMessageValues],
        optional_params: dict,
        litellm_params: dict,
        headers: dict,
    ) -> dict:
        _, upstream_model = split_account_slug(model)
        request: Final = super().transform_request(
            model=upstream_model,
            messages=messages,
            optional_params=optional_params,
            litellm_params=litellm_params,
            headers=headers,
        )
        return request

    def get_models(self, api_key: str | None = None, api_base: str | None = None) -> list[str]:
        """List the account's models from Zen's OpenAI-shaped `/v1/models`.

        The parent implementation rebuilds the URL from scheme and host only, which drops the
        `/zen` path segment and would query a host root that does not exist, so the path is
        appended to the configured base instead.
        """
        resolved_base: Final = self.get_api_base(api_base)
        resolved_key: Final = self.get_api_key(api_key)

        if resolved_key is None:
            raise ValueError(
                "OPENCODE_API_KEY is not set, so the OpenCode model list cannot be read. Attach a credential "
                "holding the account key, or set the environment variable."
            )

        response: Final = litellm.module_level_client.get(
            url=f"{resolved_base.rstrip('/')}{_ZEN_MODELS_PATH}",
            headers={"Authorization": f"Bearer {resolved_key}"},
        )
        if response.status_code != 200:
            raise ValueError(f"Failed to list OpenCode models. Status code: {response.status_code}")

        entries: Final = response.json().get("data", [])
        return [str(entry["id"]) for entry in entries if isinstance(entry, Mapping) and "id" in entry]

    def get_supported_openai_params(self, model: str) -> list:
        inherited: Final = tuple(super().get_supported_openai_params(model))
        return [*inherited]  # mutable-ok: the base interface returns a list
