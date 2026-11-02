from __future__ import annotations

import base64
import hashlib
import hmac
import time
from urllib.parse import urlencode

import pytest

from fusion_voice import VoiceRegistry
from fusion_voice.adapters.bland import BlandAdapter
from fusion_voice.adapters.elevenlabs import ElevenLabsAdapter
from fusion_voice.adapters.retell import RetellAdapter
from fusion_voice.adapters.twilio import TwilioAdapter
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


TWILIO_URL = "https://example.com/voice/twilio-main"
TWILIO_BODY = urlencode({"CallSid": "CA1", "CallStatus": "ringing"}).encode()


def _twilio_adapter(url: str = TWILIO_URL) -> TwilioAdapter:
    return TwilioAdapter({"account_sid": "AC1", "auth_token": SECRET}, {"webhook_url": url})


def _twilio_sign(body: bytes, url: str = TWILIO_URL) -> str:
    params = dict(item.split("=", 1) for item in body.decode().split("&") if item)
    payload = url + "".join(f"{key}{value}" for key, value in sorted(params.items()))
    return base64.b64encode(hmac.new(SECRET.encode(), payload.encode(), hashlib.sha1).digest()).decode()


def _elevenlabs_sign(body: bytes, timestamp: int | None = None) -> str:
    stamp = int(time.time()) if timestamp is None else timestamp
    digest = hmac.new(SECRET.encode(), f"{stamp}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={stamp},v1={digest}"


def test_twilio_valid_signature_accepted():
    _twilio_adapter().verify_request({"x-twilio-signature": _twilio_sign(TWILIO_BODY)}, TWILIO_BODY)


@pytest.mark.parametrize(
    ("body", "signature", "url"),
    [
        (TWILIO_BODY[:-1] + b"X", _twilio_sign(TWILIO_BODY), TWILIO_URL),
        (TWILIO_BODY, _twilio_sign(TWILIO_BODY)[:-1], TWILIO_URL),
        (TWILIO_BODY, _twilio_sign(TWILIO_BODY, "https://evil.example/hook"), TWILIO_URL),
        (TWILIO_BODY, "", TWILIO_URL),
        (b"", _twilio_sign(TWILIO_BODY), TWILIO_URL),
    ],
)
def test_twilio_fuzzed_variants_rejected(body, signature, url):
    with pytest.raises(VerificationError):
        _twilio_adapter(url).verify_request({"x-twilio-signature": signature}, body)


def test_twilio_missing_header_rejected():
    with pytest.raises(VerificationError):
        _twilio_adapter().verify_request({}, TWILIO_BODY)


def test_elevenlabs_valid_signature_accepted():
    ElevenLabsAdapter({"webhook_secret": SECRET}, {}).verify_request(
        {"elevenlabs-signature": _elevenlabs_sign(BODY)}, BODY
    )


@pytest.mark.parametrize(
    "header",
    [
        "",
        "t=not-a-number,v1=deadbeef",
        "v1=deadbeef",
        "t=" + str(int(time.time())) + ",v1=deadbeef",
        "t=" + str(int(time.time())) + ",v1=" + "0" * 64,
        _elevenlabs_sign(BODY)[:-1],
        _elevenlabs_sign(BODY, timestamp=int(time.time()) - 3600),
    ],
)
def test_elevenlabs_fuzzed_variants_rejected(header):
    with pytest.raises(VerificationError):
        ElevenLabsAdapter({"webhook_secret": SECRET}, {}).verify_request({"elevenlabs-signature": header}, BODY)


def test_elevenlabs_tampered_body_rejected():
    header = _elevenlabs_sign(BODY)
    with pytest.raises(VerificationError):
        ElevenLabsAdapter({"webhook_secret": SECRET}, {}).verify_request({"elevenlabs-signature": header}, BODY[:-1])


async def test_twilio_dispatch_attacker_input_never_5xx():
    registry = VoiceRegistry.from_config(
        {
            "providers": [
                {
                    "id": "twilio-main",
                    "adapter": "twilio",
                    "credentials": {"account_sid": "AC1", "auth_token": SECRET},
                    "options": {"webhook_url": TWILIO_URL},
                }
            ]
        }
    )
    bad = await registry.dispatch("twilio-main", {"x-twilio-signature": "bad"}, TWILIO_BODY)
    unsigned = await registry.dispatch("twilio-main", {}, TWILIO_BODY)
    assert bad.status == 401
    assert unsigned.status == 401


async def test_elevenlabs_dispatch_attacker_input_never_5xx():
    registry = VoiceRegistry.from_config(
        {
            "providers": [
                {
                    "id": "el-main",
                    "adapter": "elevenlabs",
                    "credentials": {"webhook_secret": SECRET},
                }
            ]
        }
    )
    bad = await registry.dispatch("el-main", {"elevenlabs-signature": "bad"}, BODY)
    unsigned = await registry.dispatch("el-main", {}, BODY)
    malformed = await registry.dispatch("el-main", {"elevenlabs-signature": _elevenlabs_sign(b"{broken")}, b"{broken")
    assert bad.status == 401
    assert unsigned.status == 401
    assert malformed.status == 400


async def test_unknown_provider_404_on_fuzz():
    registry = VoiceRegistry.from_config(vapi_config(SECRET))
    result = await registry.dispatch("../../etc/passwd", {}, b"%PDF")
    assert result.status == 404


async def test_signature_bypass_disabled_explicitly():
    registry = VoiceRegistry.from_config(vapi_config(SECRET, inbound={"verify": "disabled"}))
    result = await registry.dispatch("vapi-main", {}, payload({"message": {"type": "status-update"}}))
    assert result.status == 200
