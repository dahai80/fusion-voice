from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from fusion_voice.budgets import budget_for
from fusion_voice.models import ToolResult, VoiceEventType, VoiceSessionContext
from fusion_voice.spi import VerificationError

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class WebhookResponse:
    status: int
    body: dict[str, Any] = field(default_factory=dict)


async def dispatch(registry: Any, provider_id: str, headers: dict[str, str], body: bytes) -> WebhookResponse:
    started = time.monotonic()
    adapter = registry.get_adapter(provider_id)
    if adapter is None:
        return WebhookResponse(404, {"error": "not found"})

    provider = registry.provider_config(provider_id)
    if len(body) > provider.inbound.max_body_bytes:
        registry.hooks.on_error(provider_id, "body_too_large")
        return WebhookResponse(413, {"error": "body too large"})

    if provider.inbound.verify != "disabled":
        try:
            adapter.verify_request(headers, body)
        except VerificationError:
            registry.hooks.on_error(provider_id, "verification")
            return WebhookResponse(401, {"error": "unauthorized"})

    try:
        event = adapter.parse_event(body)
    except Exception:
        logger.exception("adapter parse failed provider=%s", provider_id)
        registry.hooks.on_error(provider_id, "parse_exception")
        return WebhookResponse(400, {"error": "bad payload"})
    if event.type == VoiceEventType.ERROR:
        registry.hooks.on_event(provider_id, event, "parse_error", _ms(started))
        return WebhookResponse(400, {"error": "bad payload"})

    ctx = VoiceSessionContext(
        call_id=event.call_id or "",
        deadline_seconds=budget_for(provider, event.type),
    )

    if event.type == VoiceEventType.TOOL_CALL:
        results = await asyncio.gather(*(_run_tool(registry, ctx, call) for call in event.tool_calls))
        registry.hooks.on_event(provider_id, event, "ok", _ms(started))
        return WebhookResponse(200, adapter.format_tool_results(list(results)))

    if event.type == VoiceEventType.ASSISTANT_REQUEST:
        return await _dispatch_assistant(registry, provider_id, adapter, ctx, event, started)

    if event.raw and registry.raw_payload_policy == "hooks":
        registry.hooks.on_raw_payload(provider_id, event)
    if event.type == VoiceEventType.UNKNOWN and registry.unknown_event_policy == "drop":
        registry.hooks.on_event(provider_id, event, "ignored", _ms(started))
        return WebhookResponse(200, {})

    registry.emit_event(provider_id, event)
    registry.hooks.on_event(provider_id, event, "ok", _ms(started))
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
        return WebhookResponse(500, {"error": "internal error"})
    try:
        response = await asyncio.wait_for(handler(ctx), timeout=ctx.deadline_seconds)
    except TimeoutError:
        registry.hooks.on_error(provider_id, "assistant_timeout")
        return WebhookResponse(504, {"error": "assistant timeout"})
    except Exception:
        logger.exception("assistant handler failed provider=%s", provider_id)
        registry.hooks.on_error(provider_id, "assistant_handler_error")
        return WebhookResponse(500, {"error": "internal error"})
    registry.hooks.on_event(provider_id, event, "ok", _ms(started))
    return WebhookResponse(200, adapter.format_assistant_response(response))


async def _run_tool(registry: Any, ctx: VoiceSessionContext, call: Any) -> ToolResult:
    handler = registry.tool_handler(call.name)
    if handler is None:
        return ToolResult(tool_call_id=call.tool_call_id, name=call.name, error="unknown tool")
    try:
        return await asyncio.wait_for(handler(ctx, call), timeout=ctx.deadline_seconds)
    except TimeoutError:
        return ToolResult(tool_call_id=call.tool_call_id, name=call.name, error="timeout")
    except Exception:
        logger.exception("tool handler failed tool=%s", call.name)
        return ToolResult(tool_call_id=call.tool_call_id, name=call.name, error="tool failed")


def _ms(started: float) -> float:
    return (time.monotonic() - started) * 1000.0
