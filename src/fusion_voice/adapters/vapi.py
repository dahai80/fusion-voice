from __future__ import annotations

import hashlib
import hmac
import json
import logging
from typing import Any

from fusion_voice.adapters import register
from fusion_voice.adapters._base import HttpAdapterMixin
from fusion_voice.models import (
    AssistantRef,
    AssistantResponse,
    AssistantSpec,
    CallRecord,
    CallRef,
    ToolCall,
    ToolResult,
    VoiceEvent,
    VoiceEventType,
)
from fusion_voice.spi import VerificationError

logger = logging.getLogger(__name__)

_SIGNATURE_HEADER = "x-vapi-signature"


@register
class VapiAdapter(HttpAdapterMixin):
    provider_id = "vapi"
    default_base_url = "https://api.vapi.ai"

    def __init__(self, credentials: dict[str, str], config: dict[str, Any] | None = None):
        super().__init__(credentials, config)
        self._secret = self.credentials.get("webhook_secret", "")

    def verify_request(self, headers: dict[str, str], body: bytes) -> None:
        if not self._secret:
            raise VerificationError("vapi webhook_secret is not configured")
        provided = headers.get(_SIGNATURE_HEADER)
        if not provided:
            raise VerificationError(f"missing {_SIGNATURE_HEADER} header")
        expected = hmac.new(self._secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(provided.strip().lower(), expected):
            raise VerificationError("invalid signature")

    def parse_event(self, body: bytes) -> VoiceEvent:
        try:
            raw = json.loads(body)
        except (ValueError, RecursionError) as exc:
            return VoiceEvent(type=VoiceEventType.ERROR, payload={"detail": str(exc)})
        if not isinstance(raw, dict):
            return VoiceEvent(type=VoiceEventType.ERROR, payload={"detail": "non-object body"})

        message = raw.get("message") or {}
        event_type = message.get("type") or raw.get("type") or ""
        call = message.get("call") or {}
        call_id = call.get("id") or raw.get("callId")

        if event_type == "assistant-request":
            direction = "inbound" if call.get("type") == "inboundPhoneCall" else "outbound"
            return VoiceEvent(
                type=VoiceEventType.ASSISTANT_REQUEST,
                call_id=call_id,
                payload={
                    "from": (call.get("customer") or {}).get("number"),
                    "to": (call.get("phoneNumber") or {}).get("number"),
                    "direction": direction,
                },
            )
        if event_type == "tool-calls":
            calls = []
            for item in message.get("toolCallList") or message.get("toolCalls") or []:
                function = item.get("function") or {}
                calls.append(
                    ToolCall(
                        name=function.get("name", ""),
                        args=_parse_arguments(function.get("arguments")),
                        tool_call_id=item.get("id") or item.get("toolCallId") or "",
                    )
                )
            return VoiceEvent(type=VoiceEventType.TOOL_CALL, call_id=call_id, tool_calls=calls)
        if event_type == "status-update":
            return VoiceEvent(
                type=VoiceEventType.STATUS_UPDATE,
                call_id=call_id,
                payload={"status": message.get("status")},
            )
        if event_type == "end-of-call-report":
            artifact = message.get("artifact") or {}
            return VoiceEvent(
                type=VoiceEventType.CALL_ENDED,
                call_id=call_id,
                payload={
                    "ended_at": message.get("endedAt"),
                    "transcript": artifact.get("transcript"),
                    "recording_url": artifact.get("recordingUrl"),
                    "cost": message.get("cost"),
                },
            )
        if event_type == "transfer-destination-request":
            return VoiceEvent(
                type=VoiceEventType.TRANSFER_REQUESTED,
                call_id=call_id,
                payload={"reason": message.get("destination")},
            )
        return VoiceEvent(type=VoiceEventType.UNKNOWN, call_id=call_id, raw=raw)

    def format_assistant_response(self, resp: AssistantResponse) -> dict[str, Any]:
        return {"assistantId": resp.assistant_id}

    def format_tool_results(self, results: list[ToolResult]) -> dict[str, Any]:
        formatted = []
        for result in results:
            if result.result is not None:
                formatted.append({"toolCallId": result.tool_call_id, "result": result.result})
            else:
                formatted.append({"toolCallId": result.tool_call_id, "error": result.error or "empty result"})
        return {"results": formatted}

    def _assistant_payload(self, spec: AssistantSpec) -> dict[str, Any]:
        model: dict[str, Any] = {}
        if spec.model:
            model["model"] = spec.model
        if spec.prompt:
            model["messages"] = [{"role": "system", "content": spec.prompt}]
        payload: dict[str, Any] = {"name": spec.name, "model": model}
        if spec.voice:
            payload["voice"] = spec.voice
        if spec.transcriber:
            payload["transcriber"] = spec.transcriber
        if spec.tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool.parameters,
                    },
                }
                for tool in spec.tools
            ]
        payload.update(spec.platform_extra)
        return payload

    async def deploy_assistant(self, spec: AssistantSpec) -> AssistantRef:
        assistant_id = spec.metadata.get("assistant_id") or spec.metadata.get("vapi_assistant_id")
        if assistant_id:
            response = self._request("PATCH", f"/assistant/{assistant_id}", self._assistant_payload(spec))
        else:
            response = self._request("POST", "/assistant", self._assistant_payload(spec))
        data = response.json()
        resolved = str(data.get("id") or assistant_id or "")
        logger.info("vapi assistant deployed id=%s", resolved)
        return AssistantRef(provider_id=self.provider_id, assistant_id=resolved)

    async def transfer_call(self, call_ref: CallRef, target: str) -> None:
        path = self.config.get("transfer_path") or f"/call/{call_ref.call_id}/transfer"
        self._request("POST", path, {"destination": target})
        logger.info("vapi call transfer call_id=%s", call_ref.call_id)

    async def hang_up(self, call_ref: CallRef) -> None:
        method = self.config.get("hang_up_method", "DELETE")
        path = self.config.get("hang_up_path") or f"/call/{call_ref.call_id}"
        self._request(method, path)
        logger.info("vapi call hang_up call_id=%s", call_ref.call_id)

    async def fetch_call(self, call_ref: CallRef) -> CallRecord:
        response = self._request("GET", f"/call/{call_ref.call_id}")
        data = response.json()
        artifact = data.get("artifact") or {}
        return CallRecord(
            call_id=str(data.get("id") or call_ref.call_id),
            status=str(data.get("status") or ""),
            started_at=data.get("startedAt"),
            ended_at=data.get("endedAt"),
            transcript=artifact.get("transcript"),
            recording_url=artifact.get("recordingUrl"),
            cost=data.get("cost"),
            raw=data,
        )


def _parse_arguments(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value:
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            logger.warning("vapi tool arguments are not valid JSON")
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}
