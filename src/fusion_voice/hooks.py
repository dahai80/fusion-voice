from __future__ import annotations

import json
import logging
import queue
import threading

from fusion_voice.models import VoiceEvent

logger = logging.getLogger(__name__)


class VoiceHooks:
    def on_event(self, provider_id: str, event: VoiceEvent, outcome: str, ms: float) -> None:
        pass

    def on_error(self, provider_id: str, reason: str) -> None:
        pass

    def on_raw_payload(self, provider_id: str, event: VoiceEvent) -> None:
        pass


class StructuredLogHooks(VoiceHooks):
    def __init__(self, logger_: logging.Logger | None = None, max_queue: int = 10000):
        self._logger = logger_ or logging.getLogger("fusion_voice.events")
        self._queue: queue.SimpleQueue = queue.SimpleQueue()
        self._max_queue = max_queue
        self._dropped = 0
        self._closed = False
        self._thread = threading.Thread(target=self._worker, name="fusion-voice-hooks", daemon=True)
        self._thread.start()

    @property
    def dropped(self) -> int:
        return self._dropped

    def _put(self, record: dict) -> None:
        if self._closed:
            return
        if self._queue.qsize() >= self._max_queue:
            self._dropped += 1
            return
        self._queue.put(record)

    def on_event(self, provider_id: str, event: VoiceEvent, outcome: str, ms: float) -> None:
        self._put(
            {
                "kind": "event",
                "provider_id": provider_id,
                "event_type": event.type,
                "call_id": event.call_id,
                "outcome": outcome,
                "total_ms": round(ms, 3),
            }
        )

    def on_error(self, provider_id: str, reason: str) -> None:
        self._put({"kind": "error", "provider_id": provider_id, "reason": reason})

    def on_raw_payload(self, provider_id: str, event: VoiceEvent) -> None:
        self._put(
            {
                "kind": "raw",
                "provider_id": provider_id,
                "event_type": event.type,
                "call_id": event.call_id,
                "raw": event.raw,
            }
        )

    def _worker(self) -> None:
        while True:
            record = self._queue.get()
            if record is None:
                return
            self._logger.info("%s", json.dumps(record, ensure_ascii=False, default=str))

    def close(self, timeout: float = 1.0) -> None:
        if self._closed:
            return
        self._closed = True
        self._queue.put(None)
        self._thread.join(timeout)
        logger.info("hooks closed dropped=%d", self._dropped)
