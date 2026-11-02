from __future__ import annotations

import logging
from typing import Any
from urllib.parse import parse_qsl
from xml.sax.saxutils import escape

import httpx
from fusion_channel.verify import verify_twilio_signature

from fusion_voice.adapters import register
from fusion_voice.adapters._base import HttpAdapterMixin
from fusion_voice.models import (
    AssistantRef,
    AssistantResponse,
    AssistantSpec,
    CallRecord,
    CallRef,
    ToolResult,
    VoiceEvent,
    VoiceEventType,
)
from fusion_voice.spi import VerificationError

logger = logging.getLogger(__name__)

_SIGNATURE_HEADER = "x-twilio-signature"
_ENDED_STATUSES = {"completed", "busy", "failed", "no-answer", "canceled"}
_STARTED_STATUSES = {"queued", "initiated", "ringing"}


@register
class TwilioAdapter(HttpAdapterMixin):
    provider_id = "twilio"
    default_base_url = "https://api.twilio.com"

    def __init__(self, credentials: dict[str, str], config: dict[str, Any] | None = None):
        super().__init__(credentials, config)
        self.account_sid = self.credentials.get("account_sid", "")
        self.auth_token = self.credentials.get("auth_token", "")
        self.webhook_url = str(self.config.get("webhook_url", ""))

    def _client(self) -> httpx.Client:
        transport = self.config.get("http_transport")
        base_url = self.config.get("http_base_url") or self.default_base_url
        timeout = float(self.config.get("http_timeout_s", 10.0))
        auth = (self.account_sid, self.auth_token) if self.account_sid and self.auth_token else None
        return httpx.Client(base_url=base_url, transport=transport, timeout=timeout, auth=auth)

    def verify_request(self, headers: dict[str, str], body: bytes) -> None:
        if not self.auth_token:
            raise VerificationError("twilio auth_token is not configured")
        provided = headers.get(_SIGNATURE_HEADER)
        if not provided:
            raise VerificationError(f"missing {_SIGNATURE_HEADER} header")
        if not self.webhook_url:
            raise VerificationError("twilio webhook_url is required for signature verification")
        params = dict(parse_qsl(body.decode("utf-8", errors="replace"), keep_blank_values=True))
        if not verify_twilio_signature(self.webhook_url, params, provided, self.auth_token):
            raise VerificationError("invalid signature")

    def parse_event(self, body: bytes) -> VoiceEvent:
        try:
            params = dict(parse_qsl(body.decode("utf-8"), keep_blank_values=True))
        except (UnicodeDecodeError, ValueError) as exc:
            return VoiceEvent(type=VoiceEventType.ERROR, payload={"detail": str(exc)})
        if not params:
            return VoiceEvent(type=VoiceEventType.ERROR, payload={"detail": "empty form body"})

        call_id = params.get("CallSid")
        status = params.get("CallStatus", "")
        payload = {
            "status": status,
            "from": params.get("From"),
            "to": params.get("To"),
            "direction": params.get("Direction"),
        }
        if status in _STARTED_STATUSES:
            return VoiceEvent(type=VoiceEventType.CALL_STARTED, call_id=call_id, payload=payload)
        if status in _ENDED_STATUSES:
            return VoiceEvent(type=VoiceEventType.CALL_ENDED, call_id=call_id, payload=payload)
        if status:
            return VoiceEvent(type=VoiceEventType.STATUS_UPDATE, call_id=call_id, payload=payload)
        return VoiceEvent(type=VoiceEventType.UNKNOWN, call_id=call_id, raw=params)

    def format_assistant_response(self, resp: AssistantResponse) -> dict[str, Any]:
        logger.warning("twilio does not use synchronous assistant requests")
        return {}

    def format_tool_results(self, results: list[ToolResult]) -> dict[str, Any]:
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

    async def deploy_assistant(self, spec: AssistantSpec) -> AssistantRef:
        phone_sid = spec.metadata.get("phone_number_sid") or self.config.get("phone_number_sid", "")
        if not phone_sid:
            raise ValueError("twilio deploy_assistant requires metadata.phone_number_sid")
        voice_url = spec.platform_extra.get("voice_url") or self.config.get("voice_url", "")
        status_callback = spec.platform_extra.get("status_callback") or self.config.get("status_callback", "")
        data: dict[str, Any] = {"VoiceUrl": voice_url}
        if status_callback:
            data["StatusCallback"] = status_callback
        self._request(
            "POST",
            f"/2010-04-01/Accounts/{self.account_sid}/IncomingPhoneNumbers/{phone_sid}.json",
            data=data,
        )
        logger.info("twilio phone number configured phone_sid=%s", phone_sid)
        return AssistantRef(provider_id=self.provider_id, assistant_id=str(phone_sid))

    async def transfer_call(self, call_ref: CallRef, target: str) -> None:
        twiml = f"<Response><Dial><Number>{escape(target)}</Number></Dial></Response>"
        self._request(
            "POST",
            f"/2010-04-01/Accounts/{self.account_sid}/Calls/{call_ref.call_id}.json",
            data={"Twiml": twiml},
        )
        logger.info("twilio call transfer call_id=%s", call_ref.call_id)

    async def hang_up(self, call_ref: CallRef) -> None:
        self._request(
            "POST",
            f"/2010-04-01/Accounts/{self.account_sid}/Calls/{call_ref.call_id}.json",
            data={"Status": "completed"},
        )
        logger.info("twilio call hang_up call_id=%s", call_ref.call_id)

    async def fetch_call(self, call_ref: CallRef) -> CallRecord:
        response = self._request("GET", f"/2010-04-01/Accounts/{self.account_sid}/Calls/{call_ref.call_id}.json")
        data = response.json()
        return CallRecord(
            call_id=str(data.get("sid") or call_ref.call_id),
            status=str(data.get("status") or ""),
            started_at=data.get("start_time"),
            ended_at=data.get("end_time"),
            cost={"price": data.get("price"), "currency": data.get("price_unit")},
            raw=data,
        )
