from __future__ import annotations

import asyncio
import logging

import pytest

from fusion_voice import AssistantResponse, ToolResult, VoiceEventType, VoiceRegistry
from fusion_voice.adapters import assert_verify_implemented
from fusion_voice.config import ConfigError
from tests.conftest import payload, sign, vapi_config


def _assistant_body() -> bytes:
    return payload(
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


def _tool_body(*calls: tuple[str, str, dict]) -> bytes:
    return payload(
        {
            "message": {
                "type": "tool-calls",
                "call": {"id": "call_1"},
                "toolCallList": [
                    {"id": tool_call_id, "function": {"name": name, "arguments": arguments}}
                    for tool_call_id, name, arguments in calls
                ],
            }
        }
    )


def _signed(secret: str, body: bytes) -> dict[str, str]:
    return {"x-vapi-signature": sign(secret, body)}


def test_unknown_adapter_rejected():
    with pytest.raises(ConfigError, match="unknown adapter"):
        VoiceRegistry.from_config({"providers": [{"id": "a", "adapter": "nope"}]})


def test_unimplemented_verify_rejected():
    class NoVerify:
        provider_id = "no-verify"

    with pytest.raises(ConfigError, match="must implement verify_request"):
        assert_verify_implemented(NoVerify)


def test_env_credentials_resolved(monkeypatch):
    monkeypatch.setenv("FUSION_VOICE_TEST_SECRET", "from-env")
    registry = VoiceRegistry.from_config(
        {
            "providers": [
                {
                    "id": "vapi-main",
                    "adapter": "vapi",
                    "credentials": {"webhook_secret": "env:FUSION_VOICE_TEST_SECRET"},
                }
            ]
        }
    )
    adapter = registry.get_adapter("vapi-main")
    assert adapter._secret == "from-env"


def test_missing_env_credential_rejected(monkeypatch):
    monkeypatch.delenv("FUSION_VOICE_TEST_SECRET", raising=False)
    with pytest.raises(ConfigError, match="missing env variable"):
        VoiceRegistry.from_config(
            {
                "providers": [
                    {
                        "id": "vapi-main",
                        "adapter": "vapi",
                        "credentials": {"webhook_secret": "env:FUSION_VOICE_TEST_SECRET"},
                    }
                ]
            }
        )


def test_provider_options_reach_adapter():
    registry = VoiceRegistry.from_config(
        {
            "providers": [
                {
                    "id": "twilio-main",
                    "adapter": "twilio",
                    "credentials": {"account_sid": "AC1", "auth_token": "tok"},
                    "options": {"webhook_url": "https://example.com/voice/twilio-main"},
                }
            ]
        }
    )
    adapter = registry.get_adapter("twilio-main")
    assert adapter.webhook_url == "https://example.com/voice/twilio-main"


def test_disabled_provider_not_adapted(secret):
    registry = VoiceRegistry.from_config(vapi_config(secret, enabled=False))
    assert registry.get_adapter("vapi-main") is None


def test_logging_level_applied(secret):
    logger = logging.getLogger("fusion_voice")
    original = logger.level
    try:
        VoiceRegistry.from_config({**vapi_config(secret), "logging": {"level": "debug"}})
        assert logger.level == logging.DEBUG
    finally:
        logger.setLevel(original)


async def test_unknown_provider_404(vapi_registry):
    response = await vapi_registry.dispatch("ghost", {}, b"{}")
    assert response.status == 404


async def test_disabled_provider_404(secret):
    registry = VoiceRegistry.from_config(vapi_config(secret, enabled=False))
    response = await registry.dispatch("vapi-main", {}, b"{}")
    assert response.status == 404


async def test_body_too_large_413(secret):
    registry = VoiceRegistry.from_config(vapi_config(secret, inbound={"max_body_bytes": 1024}))
    response = await registry.dispatch("vapi-main", {}, b"x" * 2048)
    assert response.status == 413


async def test_bad_signature_401(vapi_registry):
    body = _assistant_body()
    response = await vapi_registry.dispatch("vapi-main", {"x-vapi-signature": "deadbeef"}, body)
    assert response.status == 401


async def test_missing_signature_401(vapi_registry):
    response = await vapi_registry.dispatch("vapi-main", {}, _assistant_body())
    assert response.status == 401


async def test_unexpected_verify_exception_401(vapi_registry, secret):
    adapter = vapi_registry.get_adapter("vapi-main")
    adapter.verify_request = lambda headers, body: (_ for _ in ()).throw(RuntimeError("boom"))
    body = _assistant_body()
    response = await vapi_registry.dispatch("vapi-main", _signed(secret, body), body)
    assert response.status == 401


async def test_assistant_budget_counts_verify_and_parse(secret):
    import time as time_module

    registry = VoiceRegistry.from_config(vapi_config(secret, budgets={"assistant_request_s": 0.1, "tool_call_s": 1}))
    adapter = registry.get_adapter("vapi-main")
    original = adapter.parse_event

    def slow_parse(body):
        time_module.sleep(0.08)
        return original(body)

    adapter.parse_event = slow_parse

    @registry.on_assistant_request
    async def handler(ctx):
        await asyncio.sleep(0.08)
        return AssistantResponse(assistant_id="late")

    body = _assistant_body()
    response = await registry.dispatch("vapi-main", _signed(secret, body), body)
    assert response.status == 504


async def test_malformed_json_400(vapi_registry, secret):
    body = b"{not-json"
    response = await vapi_registry.dispatch("vapi-main", _signed(secret, body), body)
    assert response.status == 400


async def test_error_event_retains_raw_body(secret):
    registry = VoiceRegistry.from_config(vapi_config(secret))
    raw = []
    registry.hooks.on_raw_payload = lambda provider_id, event: raw.append(event.raw)
    body = b"{not-json"
    response = await registry.dispatch("vapi-main", _signed(secret, body), body)
    assert response.status == 400
    assert raw == [{"_body": "{not-json"}]


async def test_parse_exception_400(vapi_registry, secret):
    adapter = vapi_registry.get_adapter("vapi-main")
    adapter.parse_event = lambda body: (_ for _ in ()).throw(RuntimeError("boom"))
    body = _assistant_body()
    response = await vapi_registry.dispatch("vapi-main", _signed(secret, body), body)
    assert response.status == 400


def test_credential_provider_ref_resolved():
    from fusion_channel.credentials import MemoryCredentialProvider

    registry = VoiceRegistry.from_config(
        {
            "providers": [
                {
                    "id": "vapi-main",
                    "adapter": "vapi",
                    "credentials": {"webhook_secret": "vapi-main:webhook_secret"},
                }
            ]
        },
        credential_provider=MemoryCredentialProvider({"webhook_secret": "from-provider"}),
    )
    assert registry.get_adapter("vapi-main")._secret == "from-provider"


async def test_verification_disabled_skips_signature(secret):
    registry = VoiceRegistry.from_config(vapi_config(secret, inbound={"verify": "disabled"}))
    response = await registry.dispatch("vapi-main", {}, payload({"message": {"type": "status-update"}}))
    assert response.status == 200


async def test_assistant_request_success(vapi_registry, secret):
    @vapi_registry.on_assistant_request
    async def handler(ctx):
        assert ctx.call_id == "call_1"
        assert ctx.deadline_seconds == 7.0
        assert ctx.headers["x-vapi-signature"] == _signed(secret, _assistant_body())["x-vapi-signature"]
        return AssistantResponse(assistant_id="asst_1")

    body = _assistant_body()
    response = await vapi_registry.dispatch("vapi-main", _signed(secret, body), body)
    assert response.status == 200
    assert response.body == {"assistantId": "asst_1"}


async def test_assistant_handler_missing_500(vapi_registry, secret):
    body = _assistant_body()
    response = await vapi_registry.dispatch("vapi-main", _signed(secret, body), body)
    assert response.status == 500


async def test_assistant_handler_error_500(vapi_registry, secret):
    @vapi_registry.on_assistant_request
    async def handler(ctx):
        raise RuntimeError("boom")

    body = _assistant_body()
    response = await vapi_registry.dispatch("vapi-main", _signed(secret, body), body)
    assert response.status == 500
    assert response.body == {"error": "internal error"}


async def test_assistant_handler_timeout_504(secret):
    registry = VoiceRegistry.from_config(vapi_config(secret, budgets={"assistant_request_s": 0.05, "tool_call_s": 1}))

    @registry.on_assistant_request
    async def handler(ctx):
        await asyncio.sleep(0.2)
        return AssistantResponse(assistant_id="late")

    body = _assistant_body()
    response = await registry.dispatch("vapi-main", _signed(secret, body), body)
    assert response.status == 504


async def test_tool_call_success(vapi_registry, secret):
    @vapi_registry.on_tool_call("book")
    async def book(ctx, call):
        return ToolResult(tool_call_id=call.tool_call_id, name=call.name, result={"slot": call.args["slot"]})

    body = _tool_body(("tc1", "book", {"slot": "10:00"}))
    response = await vapi_registry.dispatch("vapi-main", _signed(secret, body), body)
    assert response.status == 200
    assert response.body == {"results": [{"toolCallId": "tc1", "result": {"slot": "10:00"}}]}


async def test_tool_call_unknown_tool_error(vapi_registry, secret):
    body = _tool_body(("tc1", "ghost", {}))
    response = await vapi_registry.dispatch("vapi-main", _signed(secret, body), body)
    assert response.status == 200
    assert response.body == {"results": [{"toolCallId": "tc1", "error": "unknown tool"}]}


async def test_tool_call_timeout_per_tool(secret):
    registry = VoiceRegistry.from_config(vapi_config(secret, budgets={"assistant_request_s": 1, "tool_call_s": 0.05}))

    @registry.on_tool_call("slow")
    async def slow(ctx, call):
        await asyncio.sleep(0.2)
        return ToolResult(tool_call_id=call.tool_call_id, name=call.name, result={})

    body = _tool_body(("tc1", "slow", {}))
    response = await registry.dispatch("vapi-main", _signed(secret, body), body)
    assert response.status == 200
    assert response.body == {"results": [{"toolCallId": "tc1", "error": "timeout"}]}


async def test_tool_call_handler_error(vapi_registry, secret):
    @vapi_registry.on_tool_call("bad")
    async def bad(ctx, call):
        raise ValueError("nope")

    body = _tool_body(("tc1", "bad", {}))
    response = await vapi_registry.dispatch("vapi-main", _signed(secret, body), body)
    assert response.body == {"results": [{"toolCallId": "tc1", "error": "tool failed"}]}


async def test_lifecycle_event_emitted(vapi_registry, secret):
    seen = []
    vapi_registry.on_event(lambda provider_id, event: seen.append((provider_id, event.type)))

    body = payload({"message": {"type": "status-update", "call": {"id": "call_1"}, "status": "in-progress"}})
    response = await vapi_registry.dispatch("vapi-main", _signed(secret, body), body)
    assert response.status == 200
    assert seen == [("vapi-main", VoiceEventType.STATUS_UPDATE)]


async def test_event_sink_failure_isolated(vapi_registry, secret):
    def boom(provider_id, event):
        raise RuntimeError("sink down")

    seen = []
    vapi_registry.on_event(boom)
    vapi_registry.on_event(lambda provider_id, event: seen.append(event.type))

    body = payload({"message": {"type": "status-update", "call": {"id": "call_1"}, "status": "in-progress"}})
    response = await vapi_registry.dispatch("vapi-main", _signed(secret, body), body)
    assert response.status == 200
    assert seen == [VoiceEventType.STATUS_UPDATE]


async def test_ip_allowlist_blocks_unknown_client(secret):
    registry = VoiceRegistry.from_config(vapi_config(secret, inbound={"allowed_ips": ["10.0.0.0/8"]}))
    body = payload({"message": {"type": "status-update", "call": {"id": "c"}}})
    response = await registry.dispatch("vapi-main", _signed(secret, body), body, client_ip="192.168.1.1")
    assert response.status == 403


async def test_ip_allowlist_allows_network(secret):
    registry = VoiceRegistry.from_config(vapi_config(secret, inbound={"allowed_ips": ["10.0.0.0/8"]}))
    body = payload({"message": {"type": "status-update", "call": {"id": "c"}}})
    response = await registry.dispatch("vapi-main", _signed(secret, body), body, client_ip="10.1.2.3")
    assert response.status == 200


async def test_ip_allowlist_denies_missing_client_ip(secret):
    registry = VoiceRegistry.from_config(vapi_config(secret, inbound={"allowed_ips": ["10.0.0.0/8"]}))
    body = payload({"message": {"type": "status-update", "call": {"id": "c"}}})
    response = await registry.dispatch("vapi-main", _signed(secret, body), body)
    assert response.status == 403


async def test_ip_allowlist_trusts_forwarded_for(secret):
    registry = VoiceRegistry.from_config(
        vapi_config(secret, inbound={"allowed_ips": ["10.0.0.0/8"], "trust_forwarded_for": True})
    )
    body = payload({"message": {"type": "status-update", "call": {"id": "c"}}})
    headers = {**_signed(secret, body), "x-forwarded-for": "203.0.113.9, 10.1.2.3"}
    response = await registry.dispatch("vapi-main", headers, body, client_ip="127.0.0.1")
    assert response.status == 200


async def test_ip_allowlist_ignores_spoofed_left_forwarded_for(secret):
    registry = VoiceRegistry.from_config(
        vapi_config(secret, inbound={"allowed_ips": ["10.0.0.0/8"], "trust_forwarded_for": True})
    )
    body = payload({"message": {"type": "status-update", "call": {"id": "c"}}})
    headers = {**_signed(secret, body), "x-forwarded-for": "10.1.2.3, 192.168.1.1"}
    response = await registry.dispatch("vapi-main", headers, body, client_ip="127.0.0.1")
    assert response.status == 403


async def test_unknown_event_recorded(secret):
    registry = VoiceRegistry.from_config(vapi_config(secret))
    seen = []
    raw = []
    registry.on_event(lambda provider_id, event: seen.append(event))
    registry.hooks.on_raw_payload = lambda provider_id, event: raw.append(event.raw)

    body = payload({"message": {"type": "mystery", "call": {"id": "call_1"}}})
    response = await registry.dispatch("vapi-main", _signed(secret, body), body)
    assert response.status == 200
    assert [event.type for event in seen] == [VoiceEventType.UNKNOWN]
    assert raw and raw[0]["message"]["type"] == "mystery"


async def test_raw_payload_off_skips_hook(secret):
    config = {**vapi_config(secret), "logging": {"raw_payload": "off"}}
    registry = VoiceRegistry.from_config(config)
    raw = []
    registry.hooks.on_raw_payload = lambda provider_id, event: raw.append(event.raw)

    body = payload({"message": {"type": "mystery"}})
    response = await registry.dispatch("vapi-main", _signed(secret, body), body)
    assert response.status == 200
    assert raw == []


async def test_unknown_event_dropped(secret):
    registry = VoiceRegistry.from_config({**vapi_config(secret), "unknown_event_policy": "drop"})
    seen = []
    registry.on_event(lambda provider_id, event: seen.append(event))

    body = payload({"message": {"type": "mystery"}})
    response = await registry.dispatch("vapi-main", _signed(secret, body), body)
    assert response.status == 200
    assert seen == []
