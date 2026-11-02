from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
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

_SIGNATURE_HEADER = "elevenlabs-signature"
_DEFAULT_TOLERANCE_S = 300


@register
class ElevenLabsAdapter(HttpAdapterMixin):
    provider_id = "elevenlabs"
    default_base_url = "https://api.elevenlabs.io"

    def __init__(self, credentials: dict[str, str], config: dict[str, Any] | None = None):
        super().__init__(credentials, config)
        self._secret = self.credentials.get("webhook_secret", "")
        self._tolerance_s = int(self.config.get("signature_tolerance_s", _DEFAULT_TOLERANCE_S))

    def verify_request(self, headers: dict[str, str], body: bytes) -> None:
        if not self._secret:
            raise VerificationError("elevenlabs webhook_secret is not configured")
        provided = headers.get(_SIGNATURE_HEADER)
        if not provided:
            raise VerificationError(f"missing {_SIGNATURE_HEADER} header")
        timestamp, signature = _parse_signature(provided)
        if timestamp is None or not signature:
            raise VerificationError("malformed elevenlabs signature header")
        if self._tolerance_s > 0 and abs(time.time() - timestamp) > self._tolerance_s:
            raise VerificationError("stale elevenlabs signature")
        message = f"{timestamp}.".encode() + body
        expected = hmac.new(self._secret.encode(), message, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signature.lower()):
            raise VerificationError("invalid signature")

    def parse_event(self, body: bytes) -> VoiceEvent:
        try:
            raw = json.loads(body)
        except (ValueError, RecursionError) as exc:
            return VoiceEvent(type=VoiceEventType.ERROR, payload={"detail": str(exc)})
        if not isinstance(raw, dict):
            return VoiceEvent(type=VoiceEventType.ERROR, payload={"detail": "non-object body"})

        data = raw.get("data") if isinstance(raw.get("data"), dict) else {}
        call_id = raw.get("conversation_id") or data.get("conversation_id")

        tool_name = raw.get("tool_name") or data.get("tool_name")
        if tool_name:
            args = raw.get("parameters") or raw.get("arguments") or data.get("parameters") or {}
            return VoiceEvent(
                type=VoiceEventType.TOOL_CALL,
                call_id=call_id,
                tool_calls=[
                    ToolCall(
                        name=str(tool_name),
                        args=args if isinstance(args, dict) else {},
                        tool_call_id=str(raw.get("tool_call_id") or data.get("tool_call_id") or ""),
                    )
                ],
            )

        event_type = str(raw.get("type") or "")
        if event_type in {"post_call_transcription", "post_call_audio", "call_ended"}:
            return VoiceEvent(
                type=VoiceEventType.CALL_ENDED,
                call_id=call_id,
                payload={
                    "ended_at": raw.get("event_timestamp") or data.get("event_timestamp"),
                    "transcript": _transcript_text(data.get("transcript") or raw.get("transcript")),
                    "status": data.get("status"),
                },
            )
        if event_type in {"call_started", "conversation_started"}:
            return VoiceEvent(
                type=VoiceEventType.CALL_STARTED,
                call_id=call_id,
                payload={"started_at": raw.get("event_timestamp")},
            )
        return VoiceEvent(type=VoiceEventType.UNKNOWN, call_id=call_id, raw=raw)

    def format_assistant_response(self, resp: AssistantResponse) -> dict[str, Any]:
        logger.warning("elevenlabs does not use synchronous assistant requests")
        return {"agent_id": resp.assistant_id}

    def format_tool_results(self, results: list[ToolResult]) -> dict[str, Any]:
        if len(results) == 1:
            result = results[0]
            if result.result is None:
                return {"error": result.error or "empty result"}
            return result.result if isinstance(result.result, dict) else {"result": result.result}
        return {
            "results": [
                (
                    {"tool_call_id": result.tool_call_id, "result": result.result}
                    if result.result is not None
                    else {"tool_call_id": result.tool_call_id, "error": result.error or "empty result"}
                )
                for result in results
            ]
        }

    def _agent_payload(self, spec: AssistantSpec) -> dict[str, Any]:
        agent: dict[str, Any] = {}
        prompt: dict[str, Any] = {}
        if spec.prompt:
            prompt["prompt"] = spec.prompt
        if spec.model:
            prompt["llm"] = spec.model
        if prompt:
            agent["prompt"] = prompt
        conversation: dict[str, Any] = {"agent": agent}
        if spec.voice:
            conversation["tts"] = spec.voice
        if spec.transcriber:
            conversation["asr"] = spec.transcriber
        if spec.tools:
            conversation["agent"]["tools"] = [
                {
                    "type": "webhook",
                    "name": tool.name,
                    "description": tool.description,
                    "request_body_schema": tool.parameters,
                    **tool.metadata,
                }
                for tool in spec.tools
            ]
        payload: dict[str, Any] = {"name": spec.name, "conversation_config": conversation}
        payload.update(spec.platform_extra)
        return payload

    async def deploy_assistant(self, spec: AssistantSpec) -> AssistantRef:
        agent_id = spec.metadata.get("agent_id") or spec.metadata.get("assistant_id")
        if agent_id:
            response = self._request("PATCH", f"/v1/convai/agents/{agent_id}", self._agent_payload(spec))
        else:
            response = self._request("POST", "/v1/convai/agents/create", self._agent_payload(spec))
        data = response.json()
        resolved = str(data.get("agent_id") or data.get("id") or agent_id or "")
        logger.info("elevenlabs agent deployed id=%s", resolved)
        return AssistantRef(provider_id=self.provider_id, assistant_id=resolved)

    async def transfer_call(self, call_ref: CallRef, target: str) -> None:
        raise NotImplementedError("elevenlabs has no public REST live-call transfer")

    async def hang_up(self, call_ref: CallRef) -> None:
        raise NotImplementedError("elevenlabs has no public REST live-call hang-up")

    async def fetch_call(self, call_ref: CallRef) -> CallRecord:
        response = self._request("GET", f"/v1/convai/conversations/{call_ref.call_id}")
        data = response.json()
        metadata = data.get("metadata") or {}
        return CallRecord(
            call_id=str(data.get("conversation_id") or call_ref.call_id),
            status=str(data.get("status") or ""),
            started_at=_as_text(metadata.get("start_time_unix_secs")),
            ended_at=_as_text(data.get("ended_at") or metadata.get("end_time_unix_secs")),
            transcript=_transcript_text(data.get("transcript")),
            cost=data.get("cost") if isinstance(data.get("cost"), dict) else None,
            raw=data,
        )


def _parse_signature(header: str) -> tuple[int | None, str]:
    parts: dict[str, str] = {}
    for part in header.split(","):
        key, _, value = part.strip().partition("=")
        if key and value:
            parts[key] = value
    try:
        timestamp = int(parts.get("t", ""))
    except ValueError:
        return None, ""
    return timestamp, parts.get("v1", "")


def _transcript_text(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        lines = []
        for turn in value:
            if not isinstance(turn, dict):
                continue
            role = turn.get("role") or turn.get("speaker") or ""
            text = turn.get("message") or turn.get("text") or ""
            lines.append(f"{role}: {text}".strip())
        return "\n".join(lines) if lines else None
    return None


def _as_text(value: Any) -> str | None:
    return None if value is None else str(value)
