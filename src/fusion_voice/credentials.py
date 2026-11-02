from __future__ import annotations

import logging
import os

from fusion_channel.credentials import (
    AESGCMCredentialProvider,
    CredentialError,
    CredentialProvider,
    EnvCredentialProvider,
    MemoryCredentialProvider,
    mask,
)

logger = logging.getLogger(__name__)

__all__ = [
    "AESGCMCredentialProvider",
    "CredentialError",
    "CredentialProvider",
    "EnvCredentialProvider",
    "MemoryCredentialProvider",
    "mask",
    "resolve_credentials",
    "resolve_value",
]


def resolve_value(ref: str, provider_id: str, provider: CredentialProvider | None) -> str:
    text = str(ref)
    if text.startswith("env:"):
        name = text[4:]
        value = os.environ.get(name, "")
        if not value:
            raise CredentialError(f"missing env variable: {name}")
        return value
    if ":" in text:
        prefix, key = text.split(":", 1)
        if prefix != provider_id:
            raise CredentialError(f"credential ref prefix {prefix!r} does not match provider {provider_id!r}")
        if provider is None:
            raise CredentialError(f"no CredentialProvider available for ref {text!r}")
        value = provider.get(key)
        if not value:
            raise CredentialError(f"missing credential: {key}")
        return value
    return text


def resolve_credentials(
    refs: dict[str, str],
    provider_id: str,
    provider: CredentialProvider | None,
) -> dict[str, str]:
    resolved = {name: resolve_value(ref, provider_id, provider) for name, ref in refs.items()}
    logger.info(
        "credentials resolved provider=%s keys=%s",
        provider_id,
        {name: mask(value) for name, value in resolved.items()},
    )
    return resolved
