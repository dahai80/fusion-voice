from __future__ import annotations

import logging
import threading
from collections import defaultdict
from typing import Any

logger = logging.getLogger(__name__)

_PREFIX = "fusion_voice"


class VoiceMetrics:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._requests: dict[tuple[str, str, str], int] = defaultdict(int)
        self._request_ms: dict[tuple[str, str, str], float] = defaultdict(float)
        self._verification: dict[tuple[str, str], int] = defaultdict(int)
        self._normalization: dict[tuple[str, str], int] = defaultdict(int)
        self._normalization_ms: dict[tuple[str, str], float] = defaultdict(float)

    def record_request(self, provider_id: str, event_type: str, outcome: str, ms: float) -> None:
        key = (provider_id, event_type, outcome)
        with self._lock:
            self._requests[key] += 1
            self._request_ms[key] += ms

    def record_verification(self, provider_id: str, ok: bool) -> None:
        key = (provider_id, "ok" if ok else "fail")
        with self._lock:
            self._verification[key] += 1

    def record_normalization(self, provider_id: str, event_type: str, ms: float) -> None:
        key = (provider_id, event_type)
        with self._lock:
            self._normalization[key] += 1
            self._normalization_ms[key] += ms

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "requests": [
                    {
                        "provider_id": provider_id,
                        "event_type": event_type,
                        "outcome": outcome,
                        "count": count,
                        "total_ms": round(self._request_ms[(provider_id, event_type, outcome)], 3),
                    }
                    for (provider_id, event_type, outcome), count in sorted(self._requests.items())
                ],
                "verification": [
                    {"provider_id": provider_id, "outcome": outcome, "count": count}
                    for (provider_id, outcome), count in sorted(self._verification.items())
                ],
                "normalization": [
                    {
                        "provider_id": provider_id,
                        "event_type": event_type,
                        "count": count,
                        "total_ms": round(self._normalization_ms[(provider_id, event_type)], 3),
                    }
                    for (provider_id, event_type), count in sorted(self._normalization.items())
                ],
            }

    def render_prometheus(self) -> str:
        with self._lock:
            requests = sorted(self._requests.items())
            request_ms = dict(self._request_ms)
            verification = sorted(self._verification.items())
            normalization = sorted(self._normalization.items())
            normalization_ms = dict(self._normalization_ms)

        lines = [
            f"# HELP {_PREFIX}_requests_total Webhook requests by provider, event type and outcome",
            f"# TYPE {_PREFIX}_requests_total counter",
        ]
        for (provider_id, event_type, outcome), count in requests:
            labels = _labels(provider_id=provider_id, event_type=event_type, outcome=outcome)
            lines.append(f"{_PREFIX}_requests_total{labels} {count}")

        lines.extend(
            [
                f"# HELP {_PREFIX}_request_duration_ms_sum Total request handling time",
                f"# TYPE {_PREFIX}_request_duration_ms_sum counter",
            ]
        )
        for (provider_id, event_type, outcome), total in sorted(request_ms.items()):
            labels = _labels(provider_id=provider_id, event_type=event_type, outcome=outcome)
            lines.append(f"{_PREFIX}_request_duration_ms_sum{labels} {round(total, 3)}")

        lines.extend(
            [
                f"# HELP {_PREFIX}_verification_total Signature verification outcomes",
                f"# TYPE {_PREFIX}_verification_total counter",
            ]
        )
        for (provider_id, outcome), count in verification:
            lines.append(f"{_PREFIX}_verification_total{_labels(provider_id=provider_id, outcome=outcome)} {count}")

        lines.extend(
            [
                f"# HELP {_PREFIX}_normalization_duration_ms_count Adapter normalization samples",
                f"# TYPE {_PREFIX}_normalization_duration_ms_count counter",
            ]
        )
        for (provider_id, event_type), count in normalization:
            labels = _labels(provider_id=provider_id, event_type=event_type)
            lines.append(f"{_PREFIX}_normalization_duration_ms_count{labels} {count}")

        lines.extend(
            [
                f"# HELP {_PREFIX}_normalization_duration_ms_sum Adapter normalization time",
                f"# TYPE {_PREFIX}_normalization_duration_ms_sum counter",
            ]
        )
        for (provider_id, event_type), total in sorted(normalization_ms.items()):
            labels = _labels(provider_id=provider_id, event_type=event_type)
            lines.append(f"{_PREFIX}_normalization_duration_ms_sum{labels} {round(total, 3)}")
        return "\n".join(lines) + "\n"


def _labels(**values: str) -> str:
    inner = ",".join(f'{key}="{value}"' for key, value in sorted(values.items()))
    return "{" + inner + "}"
