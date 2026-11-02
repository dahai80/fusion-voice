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

_SIGNATURE_HEADER = "x-retell-signature"

# Retell signature/event field names follow the public webhook docs. Sandbox
# validation is still pending (PRD V1); adjust only with fixture evidence.


@register
class RetellAdapter(HttpAdapterMixin):
    provider_id = "retell"
    default_base_url = "https://api.retellai.com"

    def __init__(self, credentials: dict[str, str], config: dict[str, Any] | None = None):
        super().__init__(credentials, config)
        self._secret = self.credentials.get("api_key") or self.credentials.get("webhook_secret", "")

    def verify_request(self, headers: dict[str, str], body: bytes) -> None:
        if not self._secret:
            raise VerificationError("retell signature secret is not configured")
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
        call_id = call.get("call_id") or raw.get("call_id")

        if event == "call_started":
            return VoiceEvent(
                type=VoiceEventType.CALL_STARTED,
                call_id=call_id,
                payload={"started_at": call.get("start_timestamp")},
            )
        if event in {"call_ended", "call_analyzed"}:
            return VoiceEvent(
                type=VoiceEventType.CALL_ENDED,
                call_id=call_id,
                payload={
                    "ended_at": call.get("end_timestamp"),
                    "transcript": call.get("transcript"),
                    "recording_url": call.get("recording_url"),
                    "cost": call.get("call_cost"),
                },
            )
        if event == "call_inbound":
            inbound = raw.get("call_inbound") or {}
            return VoiceEvent(
                type=VoiceEventType.ASSISTANT_REQUEST,
                call_id=call_id,
                payload={
                    "from": inbound.get("from_number"),
                    "to": inbound.get("to_number"),
                    "direction": "inbound",
                },
            )
        if raw.get("name") and raw.get("args") is not None:
            return VoiceEvent(
                type=VoiceEventType.TOOL_CALL,
                call_id=call_id,
                tool_calls=[
                    ToolCall(
                        name=str(raw.get("name") or ""),
                        args=raw.get("args") if isinstance(raw.get("args"), dict) else {},
                        tool_call_id=str(raw.get("tool_call_id") or ""),
                    )
                ],
            )
        return VoiceEvent(type=VoiceEventType.UNKNOWN, call_id=call_id, raw=raw)

    def format_assistant_response(self, resp: AssistantResponse) -> dict[str, Any]:
        inbound: dict[str, Any] = {"agent_id": resp.assistant_id}
        if resp.context:
            inbound["dynamic_variables"] = resp.context
        return {"call_inbound": inbound}

    def format_tool_results(self, results: list[ToolResult]) -> dict[str, Any]:
        if len(results) == 1:
            result = results[0]
            if result.result is not None:
                return {"result": _jsonable(result.result)}
            return {"error": result.error or "empty result"}
        return {
            "results": [
                {"tool_call_id": result.tool_call_id, "result": _jsonable(result.result)}
                if result.result is not None
                else {"tool_call_id": result.tool_call_id, "error": result.error or "empty result"}
                for result in results
            ]
        }

    def _llm_payload(self, spec: AssistantSpec) -> dict[str, Any]:
        payload: dict[str, Any] = {"model": spec.model or "gpt-4o-mini"}
        if spec.prompt:
            payload["general_prompt"] = spec.prompt
        if spec.tools:
            payload["general_tools"] = [
                {
                    "type": "custom",
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters,
                    **tool.metadata,
                }
                for tool in spec.tools
            ]
        return payload

    async def deploy_assistant(self, spec: AssistantSpec) -> AssistantRef:
        assistant_id = spec.metadata.get("assistant_id") or spec.metadata.get("agent_id")
        llm_id = spec.metadata.get("llm_id")
        response_engine: dict[str, Any] = {}
        if llm_id or spec.prompt or spec.model or spec.tools:
            if not llm_id and assistant_id:
                raise ValueError("retell update requires metadata.llm_id for prompt/model/tools changes")
            if llm_id:
                response = self._request("PATCH", f"/update-retell-llm/{llm_id}", self._llm_payload(spec))
            else:
                response = self._request("POST", "/create-retell-llm", self._llm_payload(spec))
            data = response.json()
            llm_id = str(data.get("llm_id") or data.get("id") or llm_id or "")
            response_engine = {"type": "retell-llm", "llm_id": llm_id}

        agent_payload: dict[str, Any] = {"agent_name": spec.name}
        if spec.voice.get("voice_id"):
            agent_payload["voice_id"] = spec.voice["voice_id"]
        if response_engine:
            agent_payload["response_engine"] = response_engine
        agent_payload.update(spec.platform_extra)
        if assistant_id:
            response = self._request("PATCH", f"/update-agent/{assistant_id}", agent_payload)
        else:
            response = self._request("POST", "/create-agent", agent_payload)
        data = response.json()
        resolved = str(data.get("agent_id") or data.get("id") or assistant_id or "")
        if not resolved:
            raise RuntimeError("retell deploy_assistant returned no agent id")
        logger.info("retell assistant deployed id=%s", resolved)
        return AssistantRef(provider_id=self.provider_id, assistant_id=resolved)

    async def transfer_call(self, call_ref: CallRef, target: str) -> None:
        path = self.config.get("transfer_path") or "/transfer-call"
        self._request("POST", path, {"call_id": call_ref.call_id, "transfer_phone_number": target})
        logger.info("retell call transfer call_id=%s", call_ref.call_id)

    async def hang_up(self, call_ref: CallRef) -> None:
        path = self.config.get("end_call_path") or f"/end-call/{call_ref.call_id}"
        self._request("POST", path)
        logger.info("retell call hang_up call_id=%s", call_ref.call_id)

    async def fetch_call(self, call_ref: CallRef) -> CallRecord:
        response = self._request("GET", f"/get-call/{call_ref.call_id}")
        data = response.json()
        return CallRecord(
            call_id=str(data.get("call_id") or call_ref.call_id),
            status=str(data.get("call_status") or ""),
            started_at=data.get("start_timestamp"),
            ended_at=data.get("end_timestamp"),
            transcript=data.get("transcript"),
            recording_url=data.get("recording_url"),
            cost=data.get("call_cost"),
            raw=data,
        )


def _jsonable(value: Any) -> Any:
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
