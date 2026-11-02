from __future__ import annotations

import hashlib
import hmac
import json
import time

import httpx
import pytest

from fusion_voice import AssistantSpec, CallRef, ToolResult, ToolSpec, VoiceEventType
from fusion_voice.adapters.elevenlabs import ElevenLabsAdapter
from fusion_voice.spi import VerificationError

SECRET = "el-secret"


def _signature(body: bytes, timestamp: int | None = None, secret: str = SECRET) -> str:
    ts = int(time.time()) if timestamp is None else timestamp
    digest = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={ts},v1={digest}"


def _adapter(**config) -> ElevenLabsAdapter:
    return ElevenLabsAdapter({"webhook_secret": SECRET}, {"http_base_url": "https://api.elevenlabs.io", **config})


def test_verify_accepts_valid_signature():
    body = b'{"type":"call_started"}'
    _adapter().verify_request({"elevenlabs-signature": _signature(body)}, body)


def test_verify_rejects_wrong_signature():
    with pytest.raises(VerificationError):
        _adapter().verify_request({"elevenlabs-signature": _signature(b"{}", secret="other")}, b"{}")


def test_verify_rejects_stale_timestamp():
    body = b"{}"
    adapter = _adapter(signature_tolerance_s=10)
    old = _signature(body, timestamp=int(time.time()) - 1000)
    with pytest.raises(VerificationError, match="stale"):
        adapter.verify_request({"elevenlabs-signature": old}, body)


def test_verify_tolerance_zero_disables_freshness():
    body = b"{}"
    adapter = _adapter(signature_tolerance_s=0)
    old = _signature(body, timestamp=1)
    adapter.verify_request({"elevenlabs-signature": old}, body)


def test_verify_rejects_malformed_header():
    with pytest.raises(VerificationError):
        _adapter().verify_request({"elevenlabs-signature": "garbage"}, b"{}")


def test_parse_post_call_transcription():
    body = json.dumps(
        {
            "type": "post_call_transcription",
            "event_timestamp": 1700000000,
            "data": {
                "conversation_id": "conv_1",
                "status": "done",
                "transcript": [
                    {"role": "agent", "message": "Hello"},
                    {"role": "user", "message": "Hi"},
                ],
            },
        }
    ).encode()
    event = _adapter().parse_event(body)
    assert event.type == VoiceEventType.CALL_ENDED
    assert event.call_id == "conv_1"
    assert event.payload["transcript"] == "agent: Hello\nuser: Hi"


def test_parse_tool_call_webhook():
    body = json.dumps(
        {
            "tool_name": "book_slot",
            "tool_call_id": "tc1",
            "conversation_id": "conv_1",
            "parameters": {"slot": "10:00"},
        }
    ).encode()
    event = _adapter().parse_event(body)
    assert event.type == VoiceEventType.TOOL_CALL
    assert event.tool_calls[0].name == "book_slot"
    assert event.tool_calls[0].args == {"slot": "10:00"}


def test_parse_unknown_keeps_raw():
    event = _adapter().parse_event(b'{"type":"brand-new"}')
    assert event.type == VoiceEventType.UNKNOWN
    assert event.raw["type"] == "brand-new"


def test_parse_malformed_is_error():
    assert _adapter().parse_event(b"oops").type == VoiceEventType.ERROR


def test_format_tool_results_single_and_multiple():
    adapter = _adapter()
    assert adapter.format_tool_results([ToolResult(tool_call_id="tc1", name="book", result={"ok": True})]) == {
        "ok": True
    }
    assert adapter.format_tool_results([ToolResult(tool_call_id="tc1", name="book", error="boom")]) == {"error": "boom"}
    assert adapter.format_tool_results(
        [ToolResult(tool_call_id="tc1", name="book", result="plain"), ToolResult(tool_call_id="tc2", name="book")]
    ) == {"results": [{"tool_call_id": "tc1", "result": "plain"}, {"tool_call_id": "tc2", "error": "empty result"}]}


async def test_deploy_assistant_creates_agent():
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"agent_id": "agent_new"})

    adapter = _adapter(http_transport=httpx.MockTransport(handler))
    spec = AssistantSpec(
        name="booking",
        model="gpt-4o-mini",
        prompt="Book appointments.",
        voice={"voice_id": "v1"},
        transcriber={"provider": "sovereign"},
        tools=[ToolSpec(name="book", description="Book", parameters={"type": "object"}, metadata={"url": "https://x"})],
    )
    ref = await adapter.deploy_assistant(spec)
    assert ref.assistant_id == "agent_new"
    request = calls[0]
    assert request.method == "POST"
    assert request.url.path == "/v1/convai/agents/create"
    sent = json.loads(request.content)
    assert sent["name"] == "booking"
    assert sent["conversation_config"]["agent"]["prompt"] == {"prompt": "Book appointments.", "llm": "gpt-4o-mini"}
    assert sent["conversation_config"]["tts"] == {"voice_id": "v1"}
    assert sent["conversation_config"]["agent"]["tools"][0]["url"] == "https://x"


async def test_deploy_assistant_updates_agent():
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"agent_id": "agent_old"})

    adapter = _adapter(http_transport=httpx.MockTransport(handler))
    spec = AssistantSpec(name="booking", prompt="Hi", metadata={"agent_id": "agent_old"})
    ref = await adapter.deploy_assistant(spec)
    assert ref.assistant_id == "agent_old"
    assert calls[0].method == "PATCH"
    assert calls[0].url.path == "/v1/convai/agents/agent_old"


async def test_fetch_call():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/convai/conversations/conv_1"
        return httpx.Response(
            200,
            json={
                "conversation_id": "conv_1",
                "status": "done",
                "transcript": [{"role": "agent", "text": "Hello"}],
                "metadata": {"start_time_unix_secs": 1700000000},
                "cost": {"total": 0.1},
            },
        )

    record = await _adapter(http_transport=httpx.MockTransport(handler)).fetch_call(
        CallRef(provider_id="elevenlabs", call_id="conv_1")
    )
    assert record.status == "done"
    assert record.started_at == "1700000000"
    assert record.transcript == "agent: Hello"
    assert record.cost == {"total": 0.1}


async def test_live_call_control_not_supported():
    adapter = _adapter()
    with pytest.raises(NotImplementedError):
        await adapter.transfer_call(CallRef(provider_id="elevenlabs", call_id="conv_1"), "+1")
    with pytest.raises(NotImplementedError):
        await adapter.hang_up(CallRef(provider_id="elevenlabs", call_id="conv_1"))
