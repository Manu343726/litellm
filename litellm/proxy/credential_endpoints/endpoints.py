"""
CRUD endpoints for storing reusable credentials.
"""

from collections.abc import Mapping
from typing import (
    Annotated,
    Final,
    cast,  # noqa: TID251  # jsonify_object in proxy/utils.py is annotated with a bare dict
)

from fastapi import APIRouter, Depends, HTTPException, Path, Request, Response
from pydantic import TypeAdapter

import litellm
from litellm._logging import verbose_proxy_logger
from litellm.litellm_core_utils.credential_accessor import CredentialAccessor
from litellm.litellm_core_utils.litellm_logging import _get_masked_values
from litellm.models.credentials import UpdateCredentialItem
from litellm.proxy._types import CommonProxyErrors, UserAPIKeyAuth
from litellm.proxy.auth.user_api_key_auth import user_api_key_auth
from litellm.proxy.common_utils.encrypt_decrypt_utils import encrypt_value_helper
from litellm.proxy.utils import handle_exception_on_proxy, jsonify_object
from litellm.repositories.base_repository import is_unique_violation
from litellm.repositories.credentials_repository import CredentialsRepository
from litellm.types.utils import CreateCredentialItem, CredentialItem

router: Final = APIRouter()
_CREDENTIAL_DICT_ADAPTER: Final = TypeAdapter(dict[str, object])


class CredentialHelperUtils:
    @staticmethod
    def encrypt_credential_values(credential: CredentialItem, new_encryption_key: str | None = None) -> CredentialItem:
        """Encrypt values in credential.credential_values and add to DB"""
        encrypted_credential_values: Final = {}
        for key, value in (credential.credential_values or {}).items():
            encrypted_credential_values[key] = encrypt_value_helper(value, new_encryption_key)

        # Return a new object to avoid mutating the caller's credential, which
        # is kept in memory and should remain unencrypted.
        return CredentialItem(
            credential_name=credential.credential_name,
            credential_values=encrypted_credential_values,
            credential_info=credential.credential_info or {},
        )


def _credential_exists_detail(credential_name: str) -> str:
    return (
        f"Credential '{credential_name}' already exists. "
        f"Update it with PATCH /credentials/{credential_name}, or delete it first."
    )


def _verify_provider_credential(credential: CredentialItem) -> None:
    """Reject a credential the provider would refuse, before it is stored.

    Storing an unchecked key leaves the operator with a deployment that looks configured and
    fails on every request, which is worse than a rejected credential. Only providers that
    implement `verify_credential` are checked, so this is a no-op everywhere else.
    """
    provider: Final = credential.credential_info.get("custom_llm_provider") if credential.credential_info else None
    if not isinstance(provider, str) or not provider:
        return

    from litellm.types.utils import LlmProviders
    from litellm.utils import ProviderConfigManager

    try:
        provider_enum: Final = LlmProviders(provider)
    except ValueError:
        # An unknown or aliased provider slug has no config to validate against.
        return

    config: Final = ProviderConfigManager.get_provider_chat_config(provider=provider_enum, model="")
    verify = getattr(config, "verify_credential", None)
    if verify is None:
        return

    api_key: Final = credential.credential_values.get("api_key")
    if not isinstance(api_key, str) or not api_key:
        return

    api_base: Final = credential.credential_values.get("api_base")
    failure: Final = verify(api_key, api_base if isinstance(api_base, str) else None)
    if failure is not None:
        raise HTTPException(status_code=400, detail={"error": failure})


def get_llm_router() -> litellm.Router | None:
    from litellm.proxy.proxy_server import llm_router

    return llm_router


def _resolve_deployment_credentials(llm_router: litellm.Router | None, model_id: str) -> Mapping[str, object]:
    if llm_router is None:
        raise HTTPException(
            status_code=500,
            detail="LLM router not found. Please ensure you have a valid router instance.",
        )
    if llm_router.get_deployment(model_id) is None:
        raise HTTPException(status_code=404, detail="Model not found")
    credential_values: Final = llm_router.get_deployment_credentials(model_id)
    if credential_values is None:
        raise HTTPException(status_code=404, detail="Model not found")
    return _CREDENTIAL_DICT_ADAPTER.validate_python(credential_values)


@router.post(
    "/credentials",
    dependencies=[Depends(user_api_key_auth)],
    tags=["credential management"],
)
async def create_credential(
    request: Request,
    fastapi_response: Response,
    credential: CreateCredentialItem,
    user_api_key_dict: UserAPIKeyAuth = Depends(user_api_key_auth),
    llm_router: Annotated[litellm.Router | None, Depends(get_llm_router)] = None,
):
    """
    [BETA] endpoint. This might change unexpectedly.
    Stores credential in DB.
    Reloads credentials in memory.
    """
    from litellm.proxy.proxy_server import prisma_client

    try:
        if prisma_client is None:
            raise HTTPException(
                status_code=500,
                detail={"error": CommonProxyErrors.db_not_connected_error.value},
            )
        credential_values: Final = (
            _resolve_deployment_credentials(llm_router, credential.model_id)
            if credential.model_id
            else credential.credential_values
        )
        if credential_values is None:
            raise HTTPException(
                status_code=400,
                detail="Credential values are required. Unable to infer credential values from model ID.",
            )
        processed_credential: Final = CredentialItem(
            credential_name=credential.credential_name,
            credential_values=_CREDENTIAL_DICT_ADAPTER.validate_python(credential_values),
            credential_info=credential.credential_info,
        )
        _verify_provider_credential(processed_credential)
        encrypted_credential: Final = CredentialHelperUtils.encrypt_credential_values(processed_credential)
        credentials_dict: Final = encrypted_credential.model_dump()
        credentials_dict_jsonified: Final = cast(  # cast-ok: deep-copies a model_dump, so keys are str
            "dict[str, object]", jsonify_object(credentials_dict)
        )
        try:
            await CredentialsRepository(prisma_client).create(
                data={
                    **credentials_dict_jsonified,
                    "created_by": user_api_key_dict.user_id,
                    "updated_by": user_api_key_dict.user_id,
                }
            )
        except Exception as e:
            if not is_unique_violation(e):
                raise
            raise HTTPException(status_code=409, detail=_credential_exists_detail(credential.credential_name))

        ## ADD TO LITELLM ##
        CredentialAccessor.upsert_credentials([processed_credential])

        return {"success": True, "message": "Credential created successfully"}
    except Exception as e:
        verbose_proxy_logger.exception(e)
        raise handle_exception_on_proxy(e)


@router.post(
    "/credentials/validate",
    dependencies=[Depends(user_api_key_auth)],
    tags=["credential management"],
)
async def validate_credential(
    request: Request,
    credential: CreateCredentialItem,
    user_api_key_dict: UserAPIKeyAuth = Depends(user_api_key_auth),
    llm_router: Annotated[litellm.Router | None, Depends(get_llm_router)] = None,
):
    """Check a credential against its provider without storing it.

    Lets the UI prove a key works before the credential is created, so a typo is caught here
    rather than at the first request through a deployment that references it.
    """
    try:
        credential_values: Final = (
            _resolve_deployment_credentials(llm_router, credential.model_id)
            if credential.model_id
            else credential.credential_values
        )
        if credential_values is None:
            raise HTTPException(
                status_code=400,
                detail="Credential values are required. Unable to infer credential values from model ID.",
            )
        candidate: Final = CredentialItem(
            credential_name=credential.credential_name,
            credential_values=_CREDENTIAL_DICT_ADAPTER.validate_python(credential_values),
            credential_info=credential.credential_info,
        )
        _verify_provider_credential(candidate)
        return {"valid": True, "message": "Credential is valid for this provider."}
    except HTTPException:
        raise
    except Exception as e:
        verbose_proxy_logger.exception(e)
        raise handle_exception_on_proxy(e)


@router.get(
    "/credentials",
    dependencies=[Depends(user_api_key_auth)],
    tags=["credential management"],
)
async def get_credentials(
    request: Request,
    fastapi_response: Response,
    user_api_key_dict: UserAPIKeyAuth = Depends(user_api_key_auth),
):
    """
    [BETA] endpoint. This might change unexpectedly.
    """
    try:
        masked_credentials: Final = [
            {
                "credential_name": credential.credential_name,
                "credential_values": _get_masked_values(credential.credential_values),
                "credential_info": credential.credential_info,
            }
            for credential in litellm.credential_list
        ]
        return {"success": True, "credentials": masked_credentials}
    except Exception as e:
        raise handle_exception_on_proxy(e)


@router.get(
    "/credentials/by_name/{credential_name:path}",
    dependencies=[Depends(user_api_key_auth)],
    tags=["credential management"],
    response_model=CredentialItem,
)
async def get_credential_by_name(
    request: Request,
    fastapi_response: Response,
    credential_name: str = Path(..., description="The credential name, percent-decoded; may contain slashes"),
    user_api_key_dict: UserAPIKeyAuth = Depends(user_api_key_auth),
):
    """
    [BETA] endpoint. This might change unexpectedly.
    """
    try:
        for credential in litellm.credential_list:
            if credential.credential_name == credential_name:
                masked_credential = CredentialItem(
                    credential_name=credential.credential_name,
                    credential_values=_get_masked_values(
                        credential.credential_values,
                        unmasked_length=4,
                        number_of_asterisks=4,
                    ),
                    credential_info=credential.credential_info,
                )
                return masked_credential
        raise HTTPException(
            status_code=404,
            detail="Credential not found. Got credential name: " + credential_name,
        )
    except Exception as e:
        verbose_proxy_logger.exception(e)
        raise handle_exception_on_proxy(e)


@router.get(
    "/credentials/by_model/{model_id}",
    dependencies=[Depends(user_api_key_auth)],
    tags=["credential management"],
    response_model=CredentialItem,
)
async def get_credential_by_model(
    request: Request,
    fastapi_response: Response,
    model_id: str = Path(..., description="The model ID to look up credentials for"),
    user_api_key_dict: UserAPIKeyAuth = Depends(user_api_key_auth),
):
    """
    [BETA] endpoint. This might change unexpectedly.
    """
    from litellm.proxy.proxy_server import llm_router

    try:
        if llm_router is None:
            raise HTTPException(status_code=500, detail="LLM router not found")
        model: Final = llm_router.get_deployment(model_id)
        if model is None:
            raise HTTPException(status_code=404, detail="Model not found")
        credential_values: Final = llm_router.get_deployment_credentials(model_id)
        if credential_values is None:
            raise HTTPException(status_code=404, detail="Model not found")
        masked_credential_values: Final = _get_masked_values(
            credential_values,
            unmasked_length=4,
            number_of_asterisks=4,
        )
        credential: Final = CredentialItem(
            credential_name=f"{model.model_name}-credential-{model_id}",
            credential_values=masked_credential_values,
            credential_info={},
        )
        return credential
    except Exception as e:
        verbose_proxy_logger.exception(e)
        raise handle_exception_on_proxy(e)


@router.delete(
    "/credentials/{credential_name:path}",
    dependencies=[Depends(user_api_key_auth)],
    tags=["credential management"],
)
async def delete_credential(
    request: Request,
    fastapi_response: Response,
    credential_name: str = Path(..., description="The credential name, percent-decoded; may contain slashes"),
    user_api_key_dict: UserAPIKeyAuth = Depends(user_api_key_auth),
):
    """
    [BETA] endpoint. This might change unexpectedly.
    """
    from litellm.proxy.proxy_server import prisma_client

    try:
        if prisma_client is None:
            raise HTTPException(
                status_code=500,
                detail={"error": CommonProxyErrors.db_not_connected_error.value},
            )
        deleted: Final = await CredentialsRepository(prisma_client).delete_by_name(credential_name)
        if deleted is None:
            raise HTTPException(
                status_code=404,
                detail="Credential not found. Got credential name: " + credential_name,
            )

        ## DELETE FROM LITELLM ##
        litellm.credential_list = [cred for cred in litellm.credential_list if cred.credential_name != credential_name]
        return {"success": True, "message": "Credential deleted successfully"}
    except Exception as e:
        raise handle_exception_on_proxy(e)


def update_db_credential(
    db_credential: CredentialItem,
    updated_patch: CredentialItem,
    new_encryption_key: str | None = None,
) -> CredentialItem:
    """
    Update a credential in the DB.
    """
    merged_credential: Final = CredentialItem(
        credential_name=db_credential.credential_name,
        credential_info=db_credential.credential_info,
        credential_values=db_credential.credential_values,
    )

    encrypted_credential: Final = CredentialHelperUtils.encrypt_credential_values(
        updated_patch,
        new_encryption_key,
    )
    # update model name
    if encrypted_credential.credential_name:
        merged_credential.credential_name = encrypted_credential.credential_name

    # update litellm params
    if encrypted_credential.credential_values:
        # Encrypt any sensitive values
        encrypted_params: Final = {k: v for k, v in encrypted_credential.credential_values.items()}

        merged_credential.credential_values.update(encrypted_params)

    # update model info
    if encrypted_credential.credential_info:
        """Update credential info"""
        if "credential_info" not in merged_credential.credential_info:
            merged_credential.credential_info = {}
        merged_credential.credential_info.update(encrypted_credential.credential_info)

    return merged_credential


@router.patch(
    "/credentials/{credential_name:path}",
    dependencies=[Depends(user_api_key_auth)],
    tags=["credential management"],
)
async def update_credential(
    request: Request,
    fastapi_response: Response,
    credential: UpdateCredentialItem,
    credential_name: str = Path(..., description="The credential name, percent-decoded; may contain slashes"),
    user_api_key_dict: UserAPIKeyAuth = Depends(user_api_key_auth),
    llm_router: Annotated[litellm.Router | None, Depends(get_llm_router)] = None,
):
    """
    [BETA] endpoint. This might change unexpectedly.
    """
    from litellm.proxy.proxy_server import prisma_client

    try:
        if prisma_client is None:
            raise HTTPException(
                status_code=500,
                detail={"error": CommonProxyErrors.db_not_connected_error.value},
            )
        credentials_repository: Final = CredentialsRepository(prisma_client)
        db_credential: Final = await credentials_repository.find_by_name(credential_name)
        if db_credential is None:
            raise HTTPException(status_code=404, detail="Credential not found in DB.")
        patch: Final = CredentialItem(
            credential_name=credential.credential_name,
            credential_info=_CREDENTIAL_DICT_ADAPTER.validate_python(credential.credential_info),
            credential_values=_CREDENTIAL_DICT_ADAPTER.validate_python(
                _resolve_deployment_credentials(llm_router, credential.model_id)
                if credential.model_id
                else credential.credential_values or {}
            ),
        )
        merged_credential: Final = update_db_credential(db_credential, patch)
        # Rotating a key is the moment a bad one is most likely to be typed, so the same check
        # the create path runs applies here.
        _verify_provider_credential(merged_credential)
        credential_object_jsonified: Final = cast(  # cast-ok: deep-copies a model_dump, so keys are str
            "dict[str, object]", jsonify_object(merged_credential.model_dump())
        )
        await credentials_repository.update_by_name(
            credential_name,
            data={
                **credential_object_jsonified,
                "updated_by": user_api_key_dict.user_id,
            },
        )

        # Sync in-memory credential_list (skip if not in memory - e.g., proxy restarted)
        new_name: Final = merged_credential.credential_name
        existing_in_memory: CredentialItem | None = None
        for cred in litellm.credential_list:
            if cred.credential_name == credential_name:
                existing_in_memory = cred
                break

        if existing_in_memory is not None:
            in_memory_values: Final = dict(existing_in_memory.credential_values or {})
            if patch.credential_values:
                in_memory_values.update(patch.credential_values)
            in_memory_info: Final = dict(existing_in_memory.credential_info or {})
            if patch.credential_info:
                in_memory_info.update(patch.credential_info)
            updated_in_memory: Final = CredentialItem(
                credential_name=new_name,
                credential_values=in_memory_values,
                credential_info=in_memory_info,
            )
            # Remove old entry if renamed, then use upsert_credentials to handle duplicates
            if new_name != credential_name:
                litellm.credential_list = [c for c in litellm.credential_list if c.credential_name != credential_name]
            CredentialAccessor.upsert_credentials([updated_in_memory])

        return {"success": True, "message": "Credential updated successfully"}
    except Exception as e:
        raise handle_exception_on_proxy(e)
