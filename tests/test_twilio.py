from __future__ import annotations

import base64
import hashlib
import hmac
from urllib.parse import parse_qs, urlencode

import httpx
import pytest

from fusion_voice import AssistantRef, AssistantSpec, CallRef, VoiceEventType
from fusion_voice.adapters.twilio import TwilioAdapter
from fusion_voice.spi import VerificationError

URL = "https://example.com/voice/twilio-main"
TOKEN = "twilio-token"


def _sign(params: dict[str, str], token: str = TOKEN, url: str = URL) -> str:
    payload = url + "".join(f"{key}{value}" for key, value in sorted(params.items()))
    digest = hmac.new(token.encode(), payload.encode(), hashlib.sha1).digest()
    return base64.b64encode(digest).decode()


def _adapter(**config) -> TwilioAdapter:
    return TwilioAdapter(
        {"account_sid": "AC123", "auth_token": TOKEN},
        {"webhook_url": URL, "http_base_url": "https://api.twilio.com", **config},
    )


def test_verify_accepts_valid_signature():
    params = {"CallSid": "CA1", "CallStatus": "ringing"}
    body = urlencode(params).encode()
    _adapter().verify_request({"x-twilio-signature": _sign(params)}, body)


def test_verify_rejects_wrong_signature():
    body = urlencode({"CallSid": "CA1"}).encode()
    with pytest.raises(VerificationError):
        _adapter().verify_request({"x-twilio-signature": "bad"}, body)


def test_verify_requires_webhook_url():
    body = urlencode({"CallSid": "CA1"}).encode()
    adapter = TwilioAdapter({"auth_token": TOKEN}, {"webhook_url": ""})
    with pytest.raises(VerificationError, match="webhook_url"):
        adapter.verify_request({"x-twilio-signature": "bad"}, body)


def test_parse_started_status():
    body = urlencode({"CallSid": "CA1", "CallStatus": "ringing", "From": "+1", "To": "+2"}).encode()
    event = _adapter().parse_event(body)
    assert event.type == VoiceEventType.CALL_STARTED
    assert event.call_id == "CA1"
    assert event.payload["from"] == "+1"


def test_parse_ended_status():
    event = _adapter().parse_event(urlencode({"CallSid": "CA1", "CallStatus": "completed"}).encode())
    assert event.type == VoiceEventType.CALL_ENDED


def test_parse_maps_platform_timestamps():
    started = _adapter().parse_event(
        urlencode({"CallSid": "CA1", "CallStatus": "ringing", "Timestamp": "2026-11-02T10:00:00Z"}).encode()
    )
    ended = _adapter().parse_event(
        urlencode({"CallSid": "CA1", "CallStatus": "completed", "Timestamp": "2026-11-02T10:05:00Z"}).encode()
    )
    assert started.payload["started_at"] == "2026-11-02T10:00:00Z"
    assert ended.payload["ended_at"] == "2026-11-02T10:05:00Z"


def test_parse_in_progress_status():
    event = _adapter().parse_event(urlencode({"CallSid": "CA1", "CallStatus": "in-progress"}).encode())
    assert event.type == VoiceEventType.STATUS_UPDATE


def test_parse_missing_status_is_unknown():
    event = _adapter().parse_event(urlencode({"CallSid": "CA1", "Foo": "bar"}).encode())
    assert event.type == VoiceEventType.UNKNOWN
    assert event.raw["CallSid"] == "CA1"


def test_parse_empty_body_is_error():
    assert _adapter().parse_event(b"").type == VoiceEventType.ERROR


def test_parse_invalid_utf8_is_error():
    assert _adapter().parse_event(b"\xff\xfe").type == VoiceEventType.ERROR


async def test_deploy_assistant_configures_phone_number():
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={})

    adapter = _adapter(http_transport=httpx.MockTransport(handler))
    spec = AssistantSpec(
        name="booking",
        metadata={"phone_number_sid": "PN123"},
        platform_extra={"voice_url": "https://example.com/twiml", "status_callback": "https://example.com/status"},
    )
    ref = await adapter.deploy_assistant(spec)
    assert ref == AssistantRef(provider_id="twilio", assistant_id="PN123")
    request = calls[0]
    assert request.method == "POST"
    assert request.url.path == "/2010-04-01/Accounts/AC123/IncomingPhoneNumbers/PN123.json"
    assert request.headers["authorization"].startswith("Basic ")
    form = parse_qs(request.content.decode())
    assert form["VoiceUrl"] == ["https://example.com/twiml"]
    assert form["StatusCallback"] == ["https://example.com/status"]


async def test_deploy_requires_phone_number_sid():
    with pytest.raises(ValueError, match="phone_number_sid"):
        await _adapter().deploy_assistant(AssistantSpec(name="booking"))


async def test_transfer_call_sets_twiml():
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={})

    adapter = _adapter(http_transport=httpx.MockTransport(handler))
    await adapter.transfer_call(CallRef(provider_id="twilio", call_id="CA1"), "+15550000003")
    form = parse_qs(calls[0].content.decode())
    assert form["Twiml"] == ["<Response><Dial><Number>+15550000003</Number></Dial></Response>"]


async def test_transfer_call_escapes_target():
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={})

    adapter = _adapter(http_transport=httpx.MockTransport(handler))
    await adapter.transfer_call(CallRef(provider_id="twilio", call_id="CA1"), "+1<&>")
    form = parse_qs(calls[0].content.decode())
    assert "&lt;&amp;&gt;" in form["Twiml"][0]


async def test_hang_up_completes_call():
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={})

    adapter = _adapter(http_transport=httpx.MockTransport(handler))
    await adapter.hang_up(CallRef(provider_id="twilio", call_id="CA1"))
    assert parse_qs(calls[0].content.decode())["Status"] == ["completed"]


async def test_fetch_call():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"sid": "CA1", "status": "completed", "start_time": "t0", "end_time": "t1", "price": "-0.02"},
        )

    adapter = _adapter(http_transport=httpx.MockTransport(handler))
    record = await adapter.fetch_call(CallRef(provider_id="twilio", call_id="CA1"))
    assert record.status == "completed"
    assert record.started_at == "t0"
    assert record.cost["price"] == "-0.02"
