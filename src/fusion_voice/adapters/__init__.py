from __future__ import annotations

import logging
from typing import Any

from fusion_voice.config import ConfigError
from fusion_voice.spi import VoiceProvider

logger = logging.getLogger(__name__)

ADAPTERS: dict[str, type[Any]] = {}


def register(cls: type[Any]) -> type[Any]:
    provider_id = getattr(cls, "provider_id", "")
    if not provider_id:
        raise ConfigError(f"adapter {cls.__name__} is missing provider_id")
    if provider_id in ADAPTERS:
        raise ConfigError(f"duplicate adapter provider_id: {provider_id}")
    ADAPTERS[provider_id] = cls
    logger.debug("adapter registered provider=%s", provider_id)
    return cls


def assert_verify_implemented(adapter_cls: type[Any]) -> None:
    method = getattr(adapter_cls, "verify_request", None)
    if method is None or method is VoiceProvider.verify_request:
        raise ConfigError(f"adapter {adapter_cls.__name__} must implement verify_request")


from fusion_voice.adapters import bland, elevenlabs, retell, twilio, vapi  # noqa: E402,F401
