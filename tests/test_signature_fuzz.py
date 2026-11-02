from __future__ import annotations

import pytest

from fusion_voice import VoiceRegistry
from fusion_voice.adapters.bland import BlandAdapter
from fusion_voice.adapters.retell import RetellAdapter
from fusion_voice.adapters.vapi import VapiAdapter
from fusion_voice.spi import VerificationError
from tests.conftest import payload, sign, vapi_config

BODY = payload({"message": {"type": "status-update", "call": {"id": "call_1"}, "status": "in-progress"}})
SECRET = "fuzz-secret"

ADAPTERS = [
    (VapiAdapter, "x-vapi-signature"),
    (RetellAdapter, "x-retell-signature"),
    (BlandAdapter, "x-bland-signature"),
]


def _adapter(cls, secret: str = SECRET):
    return cls({"webhook_secret": secret, "api_key": secret}, {})


@pytest.mark.parametrize(("cls", "header"), ADAPTERS)
def test_valid_signature_accepted(cls, header):
    _adapter(cls).verify_request({header: sign(SECRET, BODY)}, BODY)


@pytest.mark.parametrize(("cls", "header"), ADAPTERS)
@pytest.mark.parametrize(
    "variant",
    [
        "tampered-body",
        "truncated",
        "extra-char",
        "uppercase-prefix",
        "sha256-prefix-wrong",
        "zeros",
        "other-body",
        "empty",
        "binary",
    ],
)
def test_fuzzed_variants_rejected(cls, header, variant):
    good = sign(SECRET, BODY)
    body = BODY
    signature = good
    if variant == "tampered-body":
        body = BODY[:-1] + b"X"
    elif variant == "truncated":
        signature = good[:-1]
    elif variant == "extra-char":
        signature = good + "0"
    elif variant == "uppercase-prefix":
        signature = "SHA256=" + good
    elif variant == "sha256-prefix-wrong":
        signature = "sha256=" + "0" * 64
    elif variant == "zeros":
        signature = "0" * 64
    elif variant == "other-body":
        signature = sign(SECRET, b"{other}")
    elif variant == "empty":
        signature = ""
    elif variant == "binary":
        body = bytes(range(256))
        signature = sign(SECRET, b"{different}")

    with pytest.raises(VerificationError):
        _adapter(cls).verify_request({header: signature}, body)


@pytest.mark.parametrize(("cls", "header"), ADAPTERS)
@pytest.mark.parametrize("body", [b"", b"\x00\xff\xfe", b"%PDF-1.7", b'{"broken":', b"a" * 4096])
def test_unsigned_body_never_verifies(cls, header, body):
    with pytest.raises(VerificationError):
        _adapter(cls).verify_request({}, body)


@pytest.mark.parametrize(("cls", "header"), ADAPTERS)
@pytest.mark.parametrize("reason", ["signature", "parse"])
async def test_dispatch_attacker_input_never_5xx(cls, header, reason):
    registry = VoiceRegistry.from_config(
        {"providers": [{"id": "p", "adapter": cls.provider_id, "credentials": {"webhook_secret": SECRET}}]}
    )
    if reason == "signature":
        body = BODY
        headers = {header: "0" * 64}
    else:
        body = b"\xff\xfe not json"
        headers = {header: sign(SECRET, body)}
    result = await registry.dispatch("p", headers, body)
    assert result.status in {400, 401}


@pytest.mark.parametrize(("cls", "header"), ADAPTERS)
async def test_deeply_nested_body_returns_400(cls, header):
    registry = VoiceRegistry.from_config(
        {"providers": [{"id": "p", "adapter": cls.provider_id, "credentials": {"webhook_secret": SECRET}}]}
    )
    body = b"[" * 20000
    result = await registry.dispatch("p", {header: sign(SECRET, body)}, body)
    assert result.status == 400


async def test_unknown_provider_404_on_fuzz():
    registry = VoiceRegistry.from_config(vapi_config(SECRET))
    result = await registry.dispatch("../../etc/passwd", {}, b"%PDF")
    assert result.status == 404


async def test_signature_bypass_disabled_explicitly():
    registry = VoiceRegistry.from_config(vapi_config(SECRET, inbound={"verify": "disabled"}))
    result = await registry.dispatch("vapi-main", {}, payload({"message": {"type": "status-update"}}))
    assert result.status == 200
