from __future__ import annotations

import httpx
import pytest

from fusion_voice import AssistantResponse, AssistantSpec, CallRef, ToolResult, ToolSpec, VoiceEventType
from fusion_voice.adapters.bland import BlandAdapter
from fusion_voice.spi import VerificationError
from tests.conftest import payload, sign


def _adapter(secret: str = "bland-secret", **config) -> BlandAdapter:
    return BlandAdapter({"webhook_secret": secret}, config)


def test_verify_accepts_signature():
    body = b'{"event":"call_started"}'
    _adapter().verify_request({"x-bland-signature": sign("bland-secret", body)}, body)


def test_verify_rejects_wrong_signature():
    with pytest.raises(VerificationError):
        _adapter().verify_request({"x-bland-signature": "nope"}, b"{}")


def test_verify_rejects_missing_secret():
    with pytest.raises(VerificationError, match="not configured"):
        BlandAdapter({}, {}).verify_request({"x-bland-signature": "x"}, b"{}")


def test_parse_call_started():
    event = _adapter().parse_event(payload({"event": "call_started", "call_id": "c", "started_at": "2026-11-02"}))
    assert event.type == VoiceEventType.CALL_STARTED
    assert event.payload == {"started_at": "2026-11-02"}


def test_parse_call_ended():
    event = _adapter().parse_event(
        payload(
            {
                "event": "call_ended",
                "call_id": "c",
                "ended_at": "2026-11-02",
                "transcript": "bye",
                "recording_url": "https://r/1.wav",
                "cost": {"total": 0.2},
            }
        )
    )
    assert event.type == VoiceEventType.CALL_ENDED
    assert event.payload["transcript"] == "bye"


def test_parse_transfer_and_hangup():
    transfer = _adapter().parse_event(payload({"event": "transfer_requested", "call_id": "c", "reason": "human"}))
    assert transfer.type == VoiceEventType.TRANSFER_REQUESTED
    hangup = _adapter().parse_event(payload({"event": "hangup_requested", "call_id": "c"}))
    assert hangup.type == VoiceEventType.HANGUP_REQUESTED


def test_parse_tool_call():
    event = _adapter().parse_event(
        payload({"call_id": "c", "tool": "book", "tool_call_id": "tc1", "parameters": {"slot": "10:00"}})
    )
    assert event.type == VoiceEventType.TOOL_CALL
    assert event.tool_calls[0].name == "book"
    assert event.tool_calls[0].args == {"slot": "10:00"}


def test_parse_unknown_keeps_raw():
    event = _adapter().parse_event(payload({"event": "brand-new", "call_id": "c"}))
    assert event.type == VoiceEventType.UNKNOWN
    assert event.raw["event"] == "brand-new"


def test_parse_malformed_is_error():
    assert _adapter().parse_event(b"oops").type == VoiceEventType.ERROR


def test_format_assistant_response():
    assert _adapter().format_assistant_response(AssistantResponse(assistant_id="agent_1")) == {
        "assistant_id": "agent_1"
    }


def test_format_tool_results():
    adapter = _adapter()
    assert adapter.format_tool_results(
        [
            ToolResult(tool_call_id="tc1", name="book", result={"ok": True}),
            ToolResult(tool_call_id="tc2", name="book", error="boom"),
        ]
    ) == {
        "results": [
            {"tool_call_id": "tc1", "result": {"ok": True}},
            {"tool_call_id": "tc2", "error": "boom"},
        ]
    }


async def test_fetch_call():
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            200,
            json={
                "call_id": "call_1",
                "status": "completed",
                "started_at": "2026-11-02",
                "ended_at": "2026-11-02",
                "transcript": "hi",
                "recording_url": "https://r/1.wav",
                "cost": {"total": 0.1},
            },
        )

    adapter = _adapter(http_transport=httpx.MockTransport(handler))
    record = await adapter.fetch_call(CallRef(provider_id="bland", call_id="call_1"))
    assert calls[0].url.path == "/v1/calls/call_1"
    assert record.status == "completed"
    assert record.transcript == "hi"


async def test_deploy_assistant_creates_agent():
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"assistant_id": "agent_1"})

    adapter = _adapter(http_transport=httpx.MockTransport(handler))
    spec = AssistantSpec(
        name="booking",
        prompt="Book appointments.",
        model="gpt-4o-mini",
        voice={"voice_id": "v1"},
        tools=[ToolSpec(name="book", description="Book", metadata={"url": "https://x"})],
    )
    ref = await adapter.deploy_assistant(spec)
    assert ref.assistant_id == "agent_1"
    assert calls[0].method == "POST"
    assert calls[0].url.path == "/v1/agents"
    sent = calls[0].read().decode()
    assert '"prompt":"Book appointments."' in sent
    assert '"url":"https://x"' in sent


async def test_deploy_assistant_updates_agent():
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"assistant_id": "agent_1"})

    adapter = _adapter(http_transport=httpx.MockTransport(handler))
    spec = AssistantSpec(name="booking", metadata={"assistant_id": "agent_1"})
    ref = await adapter.deploy_assistant(spec)
    assert ref.assistant_id == "agent_1"
    assert calls[0].method == "PATCH"
    assert calls[0].url.path == "/v1/agents/agent_1"


async def test_transfer_and_hang_up():
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={})

    adapter = _adapter(http_transport=httpx.MockTransport(handler))
    await adapter.transfer_call(CallRef(provider_id="bland", call_id="call_1"), "+15550000003")
    await adapter.hang_up(CallRef(provider_id="bland", call_id="call_1"))
    assert calls[0].url.path == "/v1/calls/call_1/transfer"
    assert b'"transfer_number":"+15550000003"' in calls[0].content
    assert calls[1].url.path == "/v1/calls/call_1/stop"
