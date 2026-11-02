from __future__ import annotations

import asyncio
import ipaddress
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from fusion_voice.budgets import budget_for, remaining
from fusion_voice.models import ToolResult, VoiceEvent, VoiceEventType, VoiceSessionContext
from fusion_voice.spi import VerificationError

logger = logging.getLogger(__name__)

_MAX_RAW_BODY = 4096


@dataclass(slots=True)
class WebhookResponse:
    status: int
    body: dict[str, Any] = field(default_factory=dict)


async def dispatch(
    registry: Any,
    provider_id: str,
    headers: dict[str, str],
    body: bytes,
    client_ip: str | None = None,
) -> WebhookResponse:
    started = time.monotonic()
    headers = {str(key).lower(): value for key, value in headers.items()}
    adapter = registry.get_adapter(provider_id)
    if adapter is None:
        return WebhookResponse(404, {"error": "not found"})

    provider = registry.provider_config(provider_id)
    if not _ip_allowed(provider.inbound, headers, client_ip):
        registry.hooks.on_error(provider_id, "ip_blocked")
        _record(registry, provider_id, VoiceEventType.UNKNOWN, "ip_blocked", started)
        return WebhookResponse(403, {"error": "forbidden"})

    if len(body) > provider.inbound.max_body_bytes:
        registry.hooks.on_error(provider_id, "body_too_large")
        _record(registry, provider_id, VoiceEventType.UNKNOWN, "body_too_large", started)
        return WebhookResponse(413, {"error": "body too large"})

    if provider.inbound.verify != "disabled":
        try:
            adapter.verify_request(headers, body)
        except VerificationError:
            registry.metrics.record_verification(provider_id, False)
            registry.hooks.on_error(provider_id, "verification")
            _record(registry, provider_id, VoiceEventType.UNKNOWN, "unauthorized", started)
            return WebhookResponse(401, {"error": "unauthorized"})
        except Exception:
            logger.exception("adapter verification failed unexpectedly provider=%s", provider_id)
            registry.metrics.record_verification(provider_id, False)
            registry.hooks.on_error(provider_id, "verification")
            _record(registry, provider_id, VoiceEventType.UNKNOWN, "unauthorized", started)
            return WebhookResponse(401, {"error": "unauthorized"})
        registry.metrics.record_verification(provider_id, True)

    parse_started = time.monotonic()
    try:
        event = adapter.parse_event(body)
    except Exception:
        logger.exception("adapter parse failed provider=%s", provider_id)
        registry.hooks.on_error(provider_id, "parse_exception")
        registry.metrics.record_normalization(provider_id, VoiceEventType.ERROR, _ms(parse_started))
        _record(registry, provider_id, VoiceEventType.UNKNOWN, "parse_error", started)
        _emit_error_raw(registry, provider_id, None, body)
        return WebhookResponse(400, {"error": "bad payload"})
    normalized_type = VoiceEventType.ERROR if event.type == VoiceEventType.ERROR else event.type
    registry.metrics.record_normalization(provider_id, normalized_type, _ms(parse_started))
    if event.type == VoiceEventType.ERROR:
        error_event = _emit_error_raw(registry, provider_id, event, body)
        registry.hooks.on_event(provider_id, error_event, "parse_error", _ms(started))
        _record(registry, provider_id, VoiceEventType.UNKNOWN, "parse_error", started)
        return WebhookResponse(400, {"error": "bad payload"})

    ctx = VoiceSessionContext(
        call_id=event.call_id or "",
        received_at=started,
        deadline_seconds=budget_for(provider, event.type),
        headers=headers,
    )

    if event.type == VoiceEventType.TOOL_CALL:
        results = await asyncio.gather(*(_run_tool(registry, ctx, call) for call in event.tool_calls))
        registry.hooks.on_event(provider_id, event, "ok", _ms(started))
        _record(registry, provider_id, event.type, "ok", started)
        return WebhookResponse(200, adapter.format_tool_results(list(results)))

    if event.type == VoiceEventType.ASSISTANT_REQUEST:
        return await _dispatch_assistant(registry, provider_id, adapter, ctx, event, started)

    if event.raw and registry.raw_payload_policy == "hooks":
        registry.hooks.on_raw_payload(provider_id, event)
    if event.type == VoiceEventType.UNKNOWN and registry.unknown_event_policy == "drop":
        registry.hooks.on_event(provider_id, event, "ignored", _ms(started))
        _record(registry, provider_id, event.type, "ignored", started)
        return WebhookResponse(200, {})

    registry.emit_event(provider_id, event)
    registry.hooks.on_event(provider_id, event, "ok", _ms(started))
    _record(registry, provider_id, event.type, "ok", started)
    return WebhookResponse(200, {})


async def _dispatch_assistant(
    registry: Any,
    provider_id: str,
    adapter: Any,
    ctx: VoiceSessionContext,
    event: Any,
    started: float,
) -> WebhookResponse:
    handler = registry.assistant_handler
    if handler is None:
        registry.hooks.on_error(provider_id, "assistant_handler_missing")
        _record(registry, provider_id, event.type, "handler_missing", started)
        return WebhookResponse(500, {"error": "internal error"})
    try:
        response = await asyncio.wait_for(handler(ctx), timeout=remaining(ctx))
    except TimeoutError:
        registry.hooks.on_error(provider_id, "assistant_timeout")
        _record(registry, provider_id, event.type, "timeout", started)
        return WebhookResponse(504, {"error": "assistant timeout"})
    except Exception:
        logger.exception("assistant handler failed provider=%s", provider_id)
        registry.hooks.on_error(provider_id, "assistant_handler_error")
        _record(registry, provider_id, event.type, "handler_error", started)
        return WebhookResponse(500, {"error": "internal error"})
    registry.hooks.on_event(provider_id, event, "ok", _ms(started))
    _record(registry, provider_id, event.type, "ok", started)
    return WebhookResponse(200, adapter.format_assistant_response(response))


async def _run_tool(registry: Any, ctx: VoiceSessionContext, call: Any) -> ToolResult:
    handler = registry.tool_handler(call.name)
    if handler is None:
        return ToolResult(tool_call_id=call.tool_call_id, name=call.name, error="unknown tool")
    try:
        return await asyncio.wait_for(handler(ctx, call), timeout=remaining(ctx))
    except TimeoutError:
        return ToolResult(tool_call_id=call.tool_call_id, name=call.name, error="timeout")
    except Exception:
        logger.exception("tool handler failed tool=%s", call.name)
        return ToolResult(tool_call_id=call.tool_call_id, name=call.name, error="tool failed")


def _emit_error_raw(registry: Any, provider_id: str, event: VoiceEvent | None, body: bytes) -> VoiceEvent:
    raw = dict(event.raw) if event is not None else {}
    if not raw:
        raw = {"_body": body.decode("utf-8", errors="replace")[:_MAX_RAW_BODY]}
    error_event = VoiceEvent(
        type=VoiceEventType.ERROR,
        payload=event.payload if event is not None else {},
        raw=raw,
    )
    if registry.raw_payload_policy == "hooks":
        registry.hooks.on_raw_payload(provider_id, error_event)
    return error_event


def _ms(started: float) -> float:
    return (time.monotonic() - started) * 1000.0


def _record(registry: Any, provider_id: str, event_type: str, outcome: str, started: float) -> None:
    registry.metrics.record_request(provider_id, event_type, outcome, _ms(started))


def _ip_allowed(inbound: Any, headers: dict[str, str], client_ip: str | None) -> bool:
    if not inbound.allowed_ips:
        return True
    candidate = client_ip
    if inbound.trust_forwarded_for:
        forwarded = headers.get("x-forwarded-for", "")
        if forwarded:
            candidate = forwarded.split(",")[-1].strip()
    if not candidate:
        return False
    try:
        address = ipaddress.ip_address(candidate)
        networks = [ipaddress.ip_network(item, strict=False) for item in inbound.allowed_ips]
    except ValueError:
        logger.warning("invalid client ip blocked ip=%s", candidate)
        return False
    return any(address in network for network in networks)
