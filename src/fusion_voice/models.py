from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


class VoiceEventType:
    ASSISTANT_REQUEST = "ASSISTANT_REQUEST"
    TOOL_CALL = "TOOL_CALL"
    CALL_STARTED = "CALL_STARTED"
    CALL_ENDED = "CALL_ENDED"
    STATUS_UPDATE = "STATUS_UPDATE"
    TRANSFER_REQUESTED = "TRANSFER_REQUESTED"
    HANGUP_REQUESTED = "HANGUP_REQUESTED"
    ERROR = "ERROR"
    UNKNOWN = "UNKNOWN"


@dataclass(slots=True)
class ToolCall:
    name: str
    args: dict[str, Any]
    tool_call_id: str


@dataclass(slots=True)
class ToolResult:
    tool_call_id: str
    name: str
    result: Any = None
    error: str | None = None


@dataclass(slots=True)
class AssistantResponse:
    assistant_id: str
    context: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class VoiceSessionContext:
    call_id: str
    received_at: float = field(default_factory=time.monotonic)
    deadline_seconds: float | None = None


@dataclass(slots=True)
class VoiceEvent:
    type: str
    call_id: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    payload: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class AssistantSpec:
    name: str
    model: str = ""
    voice: dict[str, Any] = field(default_factory=dict)
    transcriber: dict[str, Any] = field(default_factory=dict)
    prompt: str = ""
    tools: list[ToolSpec] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    platform_extra: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class AssistantRef:
    provider_id: str
    assistant_id: str


@dataclass(slots=True)
class CallRef:
    provider_id: str
    call_id: str


@dataclass(slots=True)
class CallRecord:
    call_id: str
    status: str = ""
    started_at: str | None = None
    ended_at: str | None = None
    transcript: str | None = None
    recording_url: str | None = None
    cost: dict[str, Any] | None = None
    raw: dict[str, Any] = field(default_factory=dict)
