from __future__ import annotations

import logging
import time

from fusion_voice.config import ProviderConfig
from fusion_voice.models import VoiceEventType, VoiceSessionContext

logger = logging.getLogger(__name__)


def budget_for(provider: ProviderConfig, event_type: str) -> float | None:
    if event_type == VoiceEventType.TOOL_CALL:
        return provider.budgets.tool_call_s
    if event_type == VoiceEventType.ASSISTANT_REQUEST:
        return provider.budgets.assistant_request_s
    return None


def remaining(ctx: VoiceSessionContext) -> float | None:
    if ctx.deadline_seconds is None:
        return None
    elapsed = time.monotonic() - ctx.received_at
    return max(0.0, ctx.deadline_seconds - elapsed)
