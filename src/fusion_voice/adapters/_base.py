from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class HttpAdapterMixin:
    default_base_url = ""

    def __init__(self, credentials: dict[str, str], config: dict[str, Any] | None = None):
        self.credentials = dict(credentials)
        self.config = dict(config or {})
        self.api_key = self.credentials.get("api_key", "")

    def _client(self) -> httpx.Client:
        transport = self.config.get("http_transport")
        base_url = self.config.get("http_base_url") or self.default_base_url
        timeout = float(self.config.get("http_timeout_s", 10.0))
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        return httpx.Client(base_url=base_url, transport=transport, timeout=timeout, headers=headers)

    def _request(self, method: str, path: str, json_body: dict[str, Any] | None = None) -> httpx.Response:
        with self._client() as client:
            response = client.request(method, path, json=json_body)
        response.raise_for_status()
        return response
