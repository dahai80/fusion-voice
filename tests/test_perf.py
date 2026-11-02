from __future__ import annotations

import os
import time

import pytest

from fusion_voice import AssistantResponse, ToolResult, VoiceRegistry
from fusion_voice.adapters.vapi import VapiAdapter
from tests.conftest import payload, sign, vapi_config

pytestmark = pytest.mark.skipif(os.environ.get("FUSION_VOICE_BENCH") != "1", reason="set FUSION_VOICE_BENCH=1")

SECRET = "bench-secret"
ASSISTANT_BODY = payload(
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
TOOL_BODY = payload(
    {
        "message": {
            "type": "tool-calls",
            "call": {"id": "call_1"},
            "toolCallList": [
                {"id": f"tc{i}", "function": {"name": "book", "arguments": {"slot": "10:00"}}} for i in range(5)
            ],
        }
    }
)


def _p95(samples: list[float]) -> float:
    ordered = sorted(samples)
    return ordered[int(len(ordered) * 0.95) - 1]


def test_assistant_request_overhead_under_50ms():
    adapter = VapiAdapter({"webhook_secret": SECRET}, {})
    samples = []
    for _ in range(200):
        started = time.perf_counter()
        event = adapter.parse_event(ASSISTANT_BODY)
        adapter.format_assistant_response(AssistantResponse(assistant_id="asst_1"))
        samples.append((time.perf_counter() - started) * 1000)
        assert event.type == "ASSISTANT_REQUEST"
    assert _p95(samples) < 50


def test_tool_call_overhead_under_20ms():
    adapter = VapiAdapter({"webhook_secret": SECRET}, {})
    samples = []
    for _ in range(200):
        started = time.perf_counter()
        event = adapter.parse_event(TOOL_BODY)
        results = [
            ToolResult(tool_call_id=call.tool_call_id, name=call.name, result={"ok": True}) for call in event.tool_calls
        ]
        adapter.format_tool_results(results)
        samples.append((time.perf_counter() - started) * 1000)
    assert _p95(samples) < 20


async def test_full_dispatch_overhead_under_50ms():
    registry = VoiceRegistry.from_config(vapi_config(SECRET))

    @registry.on_assistant_request
    async def handler(ctx):
        return AssistantResponse(assistant_id="asst_1")

    headers = {"x-vapi-signature": sign(SECRET, ASSISTANT_BODY)}
    samples = []
    for _ in range(100):
        started = time.perf_counter()
        response = await registry.dispatch("vapi-main", headers, ASSISTANT_BODY)
        samples.append((time.perf_counter() - started) * 1000)
        assert response.status == 200
    assert _p95(samples) < 50
