from __future__ import annotations

import asyncio

from fusion_voice import VoiceMetrics, VoiceRegistry
from tests.conftest import payload, sign, vapi_config

SECRET = "metrics-secret"


def test_record_and_snapshot():
    metrics = VoiceMetrics()
    metrics.record_request("vapi", "TOOL_CALL", "ok", 1.5)
    metrics.record_request("vapi", "TOOL_CALL", "ok", 2.5)
    metrics.record_verification("vapi", True)
    metrics.record_verification("vapi", False)
    snapshot = metrics.snapshot()
    assert snapshot["requests"] == [
        {"provider_id": "vapi", "event_type": "TOOL_CALL", "outcome": "ok", "count": 2, "total_ms": 4.0}
    ]
    assert snapshot["verification"] == [
        {"provider_id": "vapi", "outcome": "fail", "count": 1},
        {"provider_id": "vapi", "outcome": "ok", "count": 1},
    ]


def test_normalization_recorded_in_snapshot_and_prometheus():
    metrics = VoiceMetrics()
    metrics.record_normalization("vapi", "TOOL_CALL", 0.4)
    metrics.record_normalization("vapi", "TOOL_CALL", 0.6)
    assert metrics.snapshot()["normalization"] == [
        {"provider_id": "vapi", "event_type": "TOOL_CALL", "count": 2, "total_ms": 1.0}
    ]
    text = metrics.render_prometheus()
    assert 'fusion_voice_normalization_duration_ms_count{event_type="TOOL_CALL",provider_id="vapi"} 2' in text
    assert 'fusion_voice_normalization_duration_ms_sum{event_type="TOOL_CALL",provider_id="vapi"} 1.0' in text


def test_prometheus_render():
    metrics = VoiceMetrics()
    metrics.record_request("vapi", "ASSISTANT_REQUEST", "ok", 12.5)
    metrics.record_verification("vapi", True)
    text = metrics.render_prometheus()
    assert 'fusion_voice_requests_total{event_type="ASSISTANT_REQUEST",outcome="ok",provider_id="vapi"} 1' in text
    assert (
        'fusion_voice_request_duration_ms_sum{event_type="ASSISTANT_REQUEST",outcome="ok",provider_id="vapi"} 12.5'
        in text
    )
    assert 'fusion_voice_verification_total{outcome="ok",provider_id="vapi"} 1' in text
    assert text.endswith("\n")


async def test_dispatch_records_verification_and_requests():
    registry = VoiceRegistry.from_config(vapi_config(SECRET))
    body = payload({"message": {"type": "status-update", "call": {"id": "c"}}})
    await registry.dispatch("vapi-main", {"x-vapi-signature": "bad"}, body)
    await registry.dispatch("vapi-main", {"x-vapi-signature": sign(SECRET, body)}, body)
    snapshot = registry.metrics.snapshot()
    assert {"provider_id": "vapi-main", "outcome": "fail", "count": 1} in snapshot["verification"]
    assert {"provider_id": "vapi-main", "outcome": "ok", "count": 1} in snapshot["verification"]
    assert any(
        item["event_type"] == "STATUS_UPDATE" and item["outcome"] == "ok" and item["count"] == 1
        for item in snapshot["requests"]
    )


async def test_assistant_timeout_recorded():
    registry = VoiceRegistry.from_config(vapi_config(SECRET, budgets={"assistant_request_s": 0.05, "tool_call_s": 1}))

    @registry.on_assistant_request
    async def handler(ctx):
        await asyncio.sleep(0.2)

    body = payload({"message": {"type": "assistant-request", "call": {"id": "c", "type": "inboundPhoneCall"}}})
    await registry.dispatch("vapi-main", {"x-vapi-signature": sign(SECRET, body)}, body)
    assert any(
        item["event_type"] == "ASSISTANT_REQUEST" and item["outcome"] == "timeout"
        for item in registry.metrics.snapshot()["requests"]
    )
