from __future__ import annotations

import httpx
import pytest

from fusion_voice import AssistantResponse, CallRef, ToolResult, VoiceEventType
from fusion_voice.adapters.retell import RetellAdapter
from fusion_voice.spi import VerificationError
from tests.conftest import payload, sign


def _adapter(secret: str = "retell-secret", **config) -> RetellAdapter:
    return RetellAdapter({"api_key": secret}, config)


def test_verify_accepts_signature_with_and_without_prefix():
    adapter = _adapter()
    body = b'{"event":"call_started"}'
    digest = sign("retell-secret", body)
    adapter.verify_request({"x-retell-signature": digest}, body)
    adapter.verify_request({"x-retell-signature": f"sha256={digest}"}, body)


def test_verify_rejects_wrong_signature():
    with pytest.raises(VerificationError):
        _adapter().verify_request({"x-retell-signature": "nope"}, b"{}")


def test_verify_rejects_missing_secret():
    with pytest.raises(VerificationError, match="not configured"):
        RetellAdapter({}, {}).verify_request({"x-retell-signature": "x"}, b"{}")


def test_parse_call_inbound():
    body = payload(
        {
            "event": "call_inbound",
            "call": {"call_id": "call_1"},
            "call_inbound": {"from_number": "+15550000001", "to_number": "+15550000002"},
        }
    )
    event = _adapter().parse_event(body)
    assert event.type == VoiceEventType.ASSISTANT_REQUEST
    assert event.call_id == "call_1"
    assert event.payload == {"from": "+15550000001", "to": "+15550000002", "direction": "inbound"}


def test_parse_tool_call():
    body = payload(
        {
            "event": "tool_call_invocation",
            "call": {"call_id": "call_1"},
            "tool_call_id": "tc1",
            "name": "book",
            "args": {"slot": "10:00"},
        }
    )
    event = _adapter().parse_event(body)
    assert event.type == VoiceEventType.TOOL_CALL
    assert event.tool_calls[0].tool_call_id == "tc1"
    assert event.tool_calls[0].args == {"slot": "10:00"}


def test_parse_call_started():
    body = payload({"event": "call_started", "call": {"call_id": "c", "start_timestamp": 1000}})
    event = _adapter().parse_event(body)
    assert event.type == VoiceEventType.CALL_STARTED
    assert event.payload == {"started_at": 1000}


def test_parse_call_ended_and_analyzed():
    body = payload(
        {
            "event": "call_ended",
            "call": {
                "call_id": "c",
                "end_timestamp": 2000,
                "transcript": "bye",
                "recording_url": "https://r/1.wav",
                "call_cost": {"total": 0.2},
            },
        }
    )
    event = _adapter().parse_event(body)
    assert event.type == VoiceEventType.CALL_ENDED
    assert event.payload["transcript"] == "bye"
    assert event.payload["cost"] == {"total": 0.2}

    analyzed = _adapter().parse_event(payload({"event": "call_analyzed", "call": {"call_id": "c"}}))
    assert analyzed.type == VoiceEventType.CALL_ENDED


def test_parse_unknown_keeps_raw():
    event = _adapter().parse_event(payload({"event": "new-thing", "call": {"call_id": "c"}}))
    assert event.type == VoiceEventType.UNKNOWN
    assert event.raw["event"] == "new-thing"


def test_parse_malformed_is_error():
    assert _adapter().parse_event(b"oops").type == VoiceEventType.ERROR


def test_format_assistant_response_includes_context():
    adapter = _adapter()
    assert adapter.format_assistant_response(AssistantResponse(assistant_id="agent_1")) == {
        "call_inbound": {"agent_id": "agent_1"}
    }
    assert adapter.format_assistant_response(AssistantResponse(assistant_id="agent_1", context={"name": "Ann"})) == {
        "call_inbound": {"agent_id": "agent_1", "dynamic_variables": {"name": "Ann"}}
    }


def test_format_tool_results_single_and_multiple():
    adapter = _adapter()
    assert adapter.format_tool_results([ToolResult(tool_call_id="tc1", name="book", result={"ok": True})]) == {
        "result": '{"ok": true}'
    }
    assert adapter.format_tool_results([ToolResult(tool_call_id="tc1", name="book", error="boom")]) == {"error": "boom"}
    assert adapter.format_tool_results(
        [
            ToolResult(tool_call_id="tc1", name="book", result="plain"),
            ToolResult(tool_call_id="tc2", name="book", error="boom"),
        ]
    ) == {"results": [{"tool_call_id": "tc1", "result": "plain"}, {"tool_call_id": "tc2", "error": "boom"}]}


async def test_fetch_call():
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            200,
            json={
                "call_id": "call_1",
                "call_status": "ended",
                "start_timestamp": 1000,
                "end_timestamp": 2000,
                "transcript": "hi",
                "recording_url": "https://r/1.wav",
                "call_cost": {"total": 0.1},
            },
        )

    adapter = _adapter(http_transport=httpx.MockTransport(handler))
    record = await adapter.fetch_call(CallRef(provider_id="retell", call_id="call_1"))
    assert calls[0].url.path == "/get-call/call_1"
    assert record.status == "ended"
    assert record.transcript == "hi"


async def test_cold_management_not_implemented():
    adapter = _adapter()
    with pytest.raises(NotImplementedError):
        await adapter.deploy_assistant(None)
    with pytest.raises(NotImplementedError):
        await adapter.transfer_call(CallRef(provider_id="retell", call_id="c"), "+1")
    with pytest.raises(NotImplementedError):
        await adapter.hang_up(CallRef(provider_id="retell", call_id="c"))
