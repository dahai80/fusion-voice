from __future__ import annotations

import json
import logging

from fusion_voice.hooks import StructuredLogHooks, VoiceHooks
from fusion_voice.models import VoiceEvent, VoiceEventType


def test_base_hooks_are_noops():
    hooks = VoiceHooks()
    event = VoiceEvent(type=VoiceEventType.STATUS_UPDATE)
    hooks.on_event("p", event, "ok", 1.0)
    hooks.on_error("p", "reason")
    hooks.on_raw_payload("p", event)


def test_structured_log_hooks_emit_json(caplog):
    caplog.set_level(logging.INFO, logger="fusion_voice.events")
    hooks = StructuredLogHooks()
    event = VoiceEvent(type=VoiceEventType.STATUS_UPDATE, call_id="c1")
    hooks.on_event("vapi", event, "ok", 12.3456)
    hooks.on_error("vapi", "verification")
    hooks.on_raw_payload("vapi", VoiceEvent(type=VoiceEventType.UNKNOWN, call_id="c1", raw={"x": 1}))
    hooks.close()

    records = [json.loads(record.message) for record in caplog.records if record.name == "fusion_voice.events"]
    kinds = [record["kind"] for record in records]
    assert kinds == ["event", "error", "raw"]
    assert records[0]["total_ms"] == 12.346
    assert records[2]["raw"] == {"x": 1}


def test_queue_overflow_counts_dropped():
    hooks = StructuredLogHooks(max_queue=0)
    hooks.on_event("vapi", VoiceEvent(type=VoiceEventType.STATUS_UPDATE), "ok", 1.0)
    assert hooks.dropped == 1
    hooks.close()


def test_close_is_idempotent():
    hooks = StructuredLogHooks()
    hooks.close()
    hooks.close()
    hooks.on_event("vapi", VoiceEvent(type=VoiceEventType.STATUS_UPDATE), "ok", 1.0)
    assert hooks.dropped == 0
