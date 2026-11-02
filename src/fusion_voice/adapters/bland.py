from __future__ import annotations

import json
import logging
from typing import Any

from fusion_channel.verify import verify_hmac_sha256_hex

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

_SIGNATURE_HEADER = "x-bland-signature"

# Bland signature/event field names follow the public webhook docs. Sandbox
# validation is still pending (PRD V1); adjust only with fixture evidence.


@register
class BlandAdapter(HttpAdapterMixin):
    provider_id = "bland"
    default_base_url = "https://api.bland.ai"

    def __init__(self, credentials: dict[str, str], config: dict[str, Any] | None = None):
        super().__init__(credentials, config)
        self._secret = self.credentials.get("webhook_secret") or self.credentials.get("api_key", "")

    def verify_request(self, headers: dict[str, str], body: bytes) -> None:
        if not self._secret:
            raise VerificationError("bland webhook_secret is not configured")
        provided = headers.get(_SIGNATURE_HEADER)
        if not provided:
            raise VerificationError(f"missing {_SIGNATURE_HEADER} header")
        if not verify_hmac_sha256_hex(self._secret, body, provided):
            raise VerificationError("invalid signature")

    def parse_event(self, body: bytes) -> VoiceEvent:
        try:
            raw = json.loads(body)
        except (ValueError, RecursionError) as exc:
            return VoiceEvent(type=VoiceEventType.ERROR, payload={"detail": str(exc)})
        if not isinstance(raw, dict):
            return VoiceEvent(type=VoiceEventType.ERROR, payload={"detail": "non-object body"})

        event = str(raw.get("event") or raw.get("type") or "")
        call = raw.get("call") or {}
        call_id = raw.get("call_id") or call.get("call_id")

        if event == "call_started":
            return VoiceEvent(
                type=VoiceEventType.CALL_STARTED,
                call_id=call_id,
                payload={"started_at": raw.get("started_at")},
            )
        if event == "call_ended":
            return VoiceEvent(
                type=VoiceEventType.CALL_ENDED,
                call_id=call_id,
                payload={
                    "ended_at": raw.get("ended_at"),
                    "transcript": raw.get("transcript"),
                    "recording_url": raw.get("recording_url"),
                    "cost": raw.get("cost"),
                },
            )
        if event == "transfer_requested":
            return VoiceEvent(
                type=VoiceEventType.TRANSFER_REQUESTED,
                call_id=call_id,
                payload={"reason": raw.get("reason")},
            )
        if event == "hangup_requested":
            return VoiceEvent(
                type=VoiceEventType.HANGUP_REQUESTED,
                call_id=call_id,
                payload={"reason": raw.get("reason")},
            )
        if raw.get("tool") or raw.get("tool_name"):
            return VoiceEvent(
                type=VoiceEventType.TOOL_CALL,
                call_id=call_id,
                tool_calls=[
                    ToolCall(
                        name=str(raw.get("tool") or raw.get("tool_name") or ""),
                        args=raw.get("parameters") or raw.get("args") or {},
                        tool_call_id=str(raw.get("tool_call_id") or ""),
                    )
                ],
            )
        return VoiceEvent(type=VoiceEventType.UNKNOWN, call_id=call_id, raw=raw)

    def format_assistant_response(self, resp: AssistantResponse) -> dict[str, Any]:
        return {"assistant_id": resp.assistant_id}

    def format_tool_results(self, results: list[ToolResult]) -> dict[str, Any]:
        formatted = []
        for result in results:
            if result.result is not None:
                formatted.append({"tool_call_id": result.tool_call_id, "result": result.result})
            else:
                formatted.append({"tool_call_id": result.tool_call_id, "error": result.error or "empty result"})
        return {"results": formatted}

    async def deploy_assistant(self, spec: AssistantSpec) -> AssistantRef:
        raise NotImplementedError("bland deploy_assistant lands in M2 (PRD section 10)")

    async def transfer_call(self, call_ref: CallRef, target: str) -> None:
        raise NotImplementedError("bland transfer_call lands in M2 (PRD section 10)")

    async def hang_up(self, call_ref: CallRef) -> None:
        raise NotImplementedError("bland hang_up lands in M2 (PRD section 10)")

    async def fetch_call(self, call_ref: CallRef) -> CallRecord:
        response = self._request("GET", f"/v1/calls/{call_ref.call_id}")
        data = response.json()
        return CallRecord(
            call_id=str(data.get("call_id") or call_ref.call_id),
            status=str(data.get("status") or ""),
            started_at=data.get("started_at"),
            ended_at=data.get("ended_at"),
            transcript=data.get("transcript"),
            recording_url=data.get("recording_url"),
            cost=data.get("cost"),
            raw=data,
        )
