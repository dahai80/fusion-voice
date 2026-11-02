from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

import pytest

from fusion_voice import VoiceRegistry


def sign(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def payload(data: dict[str, Any]) -> bytes:
    return json.dumps(data, separators=(",", ":")).encode("utf-8")


def vapi_config(secret: str, **overrides: Any) -> dict[str, Any]:
    provider: dict[str, Any] = {
        "id": "vapi-main",
        "adapter": "vapi",
        "credentials": {"webhook_secret": secret, "api_key": "test-key"},
    }
    provider.update(overrides)
    return {"providers": [provider]}


@pytest.fixture
def secret() -> str:
    return "test-secret"


@pytest.fixture
def vapi_registry(secret: str) -> VoiceRegistry:
    return VoiceRegistry.from_config(vapi_config(secret))
