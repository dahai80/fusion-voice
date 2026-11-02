from __future__ import annotations

import json

import httpx
import pytest

from fusion_voice import (
    AssistantResponse,
    AssistantSpec,
    CallRef,
    ToolResult,
    ToolSpec,
    VoiceEventType,
)
from fusion_voice.adapters.vapi import VapiAdapter
from fusion_voice.spi import VerificationError
from tests.conftest import payload, sign


def _adapter(secret: str = "s3cret", **config) -> VapiAdapter:
    return VapiAdapter({"webhook_secret": secret, "api_key": "key"}, config)


def _transport(calls: list[httpx.Request], response: httpx.Response) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return response

    return httpx.MockTransport(handler)


def test_verify_accepts_valid_signature():
    adapter = _adapter()
    body = b'{"a":1}'
    adapter.verify_request({"x-vapi-signature": sign("s3cret", body)}, body)


def test_verify_accepts_uppercase_signature():
    adapter = _adapter()
    body = b'{"a":1}'
    adapter.verify_request({"x-vapi-signature": sign("s3cret", body).upper()}, body)


def test_verify_rejects_wrong_signature():
    adapter = _adapter()
    with pytest.raises(VerificationError):
        adapter.verify_request({"x-vapi-signature": "0" * 64}, b"{}")


def test_verify_rejects_missing_secret():
    adapter = VapiAdapter({}, {})
    with pytest.raises(VerificationError, match="not configured"):
        adapter.verify_request({"x-vapi-signature": "anything"}, b"{}")


def test_parse_assistant_request():
    body = payload(
        {
            "message": {
                "type": "assistant-request",
                "call": {
                    "id": "call_1",
                    "type": "inboundPhoneCall",
                    "customer": {"number": "+15550000001"},
                    "phoneNumber": {"number": "+15550000002"},
                },
            }
        }
    )
    event = _adapter().parse_event(body)
    assert event.type == VoiceEventType.ASSISTANT_REQUEST
    assert event.call_id == "call_1"
    assert event.payload == {"from": "+15550000001", "to": "+15550000002", "direction": "inbound"}


def test_parse_tool_calls_string_arguments():
    body = payload(
        {
            "message": {
                "type": "tool-calls",
                "call": {"id": "call_1"},
                "toolCallList": [
                    {"id": "tc1", "function": {"name": "book", "arguments": json.dumps({"slot": "10:00"})}},
                    {"toolCallId": "tc2", "function": {"name": "lookup", "arguments": {"id": 7}}},
                ],
            }
        }
    )
    event = _adapter().parse_event(body)
    assert event.type == VoiceEventType.TOOL_CALL
    assert [call.tool_call_id for call in event.tool_calls] == ["tc1", "tc2"]
    assert event.tool_calls[0].args == {"slot": "10:00"}
    assert event.tool_calls[1].args == {"id": 7}


def test_parse_invalid_arguments_falls_back_to_empty():
    body = payload(
        {
            "message": {
                "type": "tool-calls",
                "toolCallList": [{"id": "tc1", "function": {"name": "book", "arguments": "{oops"}}],
            }
        }
    )
    event = _adapter().parse_event(body)
    assert event.tool_calls[0].args == {}


def test_parse_status_update():
    body = payload({"message": {"type": "status-update", "call": {"id": "c"}, "status": "in-progress"}})
    event = _adapter().parse_event(body)
    assert event.type == VoiceEventType.STATUS_UPDATE
    assert event.payload == {"status": "in-progress"}


def test_parse_end_of_call_report():
    body = payload(
        {
            "message": {
                "type": "end-of-call-report",
                "call": {"id": "c"},
                "endedAt": "2026-11-02T10:00:00Z",
                "artifact": {"transcript": "hello", "recordingUrl": "https://r/1.wav"},
                "cost": {"total": 0.12},
            }
        }
    )
    event = _adapter().parse_event(body)
    assert event.type == VoiceEventType.CALL_ENDED
    assert event.payload["transcript"] == "hello"
    assert event.payload["cost"] == {"total": 0.12}


def test_parse_transfer_destination_request():
    body = payload({"message": {"type": "transfer-destination-request", "call": {"id": "c"}, "destination": "sales"}})
    event = _adapter().parse_event(body)
    assert event.type == VoiceEventType.TRANSFER_REQUESTED
    assert event.payload == {"reason": "sales"}


def test_parse_malformed_json_is_error():
    event = _adapter().parse_event(b"{nope")
    assert event.type == VoiceEventType.ERROR


def test_parse_non_object_is_error():
    event = _adapter().parse_event(b"[1,2]")
    assert event.type == VoiceEventType.ERROR


def test_parse_unknown_keeps_raw():
    body = payload({"message": {"type": "brand-new", "call": {"id": "c"}, "extra": 1}})
    event = _adapter().parse_event(body)
    assert event.type == VoiceEventType.UNKNOWN
    assert event.raw["message"]["extra"] == 1


def test_format_assistant_response():
    assert _adapter().format_assistant_response(AssistantResponse(assistant_id="asst_1")) == {"assistantId": "asst_1"}


def test_format_tool_results_success_and_error():
    adapter = _adapter()
    results = [
        ToolResult(tool_call_id="tc1", name="book", result={"ok": True}),
        ToolResult(tool_call_id="tc2", name="book", error="boom"),
        ToolResult(tool_call_id="tc3", name="book"),
    ]
    assert adapter.format_tool_results(results) == {
        "results": [
            {"toolCallId": "tc1", "result": {"ok": True}},
            {"toolCallId": "tc2", "error": "boom"},
            {"toolCallId": "tc3", "error": "empty result"},
        ]
    }


async def test_deploy_assistant_posts_new():
    calls: list[httpx.Request] = []
    adapter = _adapter(
        http_transport=_transport(calls, httpx.Response(201, json={"id": "asst_new"})),
        http_base_url="https://api.vapi.ai",
    )
    spec = AssistantSpec(
        name="booking",
        model="gpt-4o",
        prompt="You book appointments.",
        voice={"provider": "11labs", "voiceId": "v1"},
        transcriber={"provider": "deepgram"},
        tools=[ToolSpec(name="book", description="Book a slot", parameters={"type": "object"})],
        platform_extra={"silenceTimeoutSeconds": 30},
    )
    ref = await adapter.deploy_assistant(spec)
    assert ref.provider_id == "vapi"
    assert ref.assistant_id == "asst_new"
    request = calls[0]
    assert request.method == "POST"
    assert request.url.path == "/assistant"
    assert request.headers["authorization"] == "Bearer key"
    sent = json.loads(request.content)
    assert sent["model"]["model"] == "gpt-4o"
    assert sent["model"]["messages"] == [{"role": "system", "content": "You book appointments."}]
    assert sent["tools"][0]["function"]["name"] == "book"
    assert sent["silenceTimeoutSeconds"] == 30


async def test_deploy_assistant_patches_existing():
    calls: list[httpx.Request] = []
    adapter = _adapter(http_transport=_transport(calls, httpx.Response(200, json={"id": "asst_old"})))
    spec = AssistantSpec(name="booking", metadata={"assistant_id": "asst_old"})
    ref = await adapter.deploy_assistant(spec)
    assert ref.assistant_id == "asst_old"
    assert calls[0].method == "PATCH"
    assert calls[0].url.path == "/assistant/asst_old"


async def test_deploy_assistant_requires_returned_id():
    adapter = _adapter(http_transport=httpx.MockTransport(lambda request: httpx.Response(200, json={})))
    with pytest.raises(RuntimeError, match="no assistant id"):
        await adapter.deploy_assistant(AssistantSpec(name="booking"))


async def test_transfer_call():
    calls: list[httpx.Request] = []
    adapter = _adapter(http_transport=_transport(calls, httpx.Response(200, json={})))
    await adapter.transfer_call(CallRef(provider_id="vapi", call_id="call_1"), "+15550000003")
    assert calls[0].method == "POST"
    assert calls[0].url.path == "/call/call_1/transfer"
    assert json.loads(calls[0].content) == {"destination": "+15550000003"}


async def test_hang_up():
    calls: list[httpx.Request] = []
    adapter = _adapter(http_transport=_transport(calls, httpx.Response(200, json={})))
    await adapter.hang_up(CallRef(provider_id="vapi", call_id="call_1"))
    assert calls[0].method == "DELETE"
    assert calls[0].url.path == "/call/call_1"


async def test_fetch_call():
    calls: list[httpx.Request] = []
    adapter = _adapter(
        http_transport=_transport(
            calls,
            httpx.Response(
                200,
                json={
                    "id": "call_1",
                    "status": "ended",
                    "startedAt": "2026-11-02T09:00:00Z",
                    "endedAt": "2026-11-02T09:05:00Z",
                    "artifact": {"transcript": "hi", "recordingUrl": "https://r/1.wav"},
                    "cost": {"total": 0.1},
                },
            ),
        )
    )
    record = await adapter.fetch_call(CallRef(provider_id="vapi", call_id="call_1"))
    assert calls[0].method == "GET"
    assert calls[0].url.path == "/call/call_1"
    assert record.call_id == "call_1"
    assert record.status == "ended"
    assert record.transcript == "hi"
    assert record.cost == {"total": 0.1}


async def test_fetch_call_http_error_raises():
    adapter = _adapter(http_transport=httpx.MockTransport(lambda request: httpx.Response(500)))
    with pytest.raises(httpx.HTTPStatusError):
        await adapter.fetch_call(CallRef(provider_id="vapi", call_id="call_1"))
