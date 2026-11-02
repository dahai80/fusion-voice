from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from fusion_voice.models import (
    AssistantRef,
    AssistantResponse,
    AssistantSpec,
    CallRecord,
    CallRef,
    ToolCall,
    ToolResult,
    VoiceEvent,
    VoiceSessionContext,
)

logger = logging.getLogger(__name__)


class VerificationError(Exception):
    pass


ToolCallHandler = Callable[[VoiceSessionContext, ToolCall], Awaitable[ToolResult]]
AssistantRequestHandler = Callable[[VoiceSessionContext], Awaitable[AssistantResponse]]
EventSink = Callable[[str, VoiceEvent], None]


class VoiceProvider(Protocol):
    provider_id: str

    def verify_request(self, headers: dict[str, str], body: bytes) -> None: ...

    def parse_event(self, body: bytes) -> VoiceEvent: ...

    def format_assistant_response(self, resp: AssistantResponse) -> dict[str, Any]: ...

    def format_tool_results(self, results: list[ToolResult]) -> dict[str, Any]: ...

    async def deploy_assistant(self, spec: AssistantSpec) -> AssistantRef: ...

    async def transfer_call(self, call_ref: CallRef, target: str) -> None: ...

    async def hang_up(self, call_ref: CallRef) -> None: ...

    async def fetch_call(self, call_ref: CallRef) -> CallRecord: ...
