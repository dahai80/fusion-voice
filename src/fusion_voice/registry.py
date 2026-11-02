from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fusion_voice.adapters import ADAPTERS, assert_verify_implemented
from fusion_voice.budgets import budget_for
from fusion_voice.config import ConfigError, RootConfig, load_config
from fusion_voice.credentials import CredentialError, CredentialProvider, resolve_credentials
from fusion_voice.dispatch import WebhookResponse
from fusion_voice.dispatch import dispatch as dispatch_webhook
from fusion_voice.hooks import VoiceHooks
from fusion_voice.metrics import VoiceMetrics
from fusion_voice.models import VoiceEvent
from fusion_voice.spi import AssistantRequestHandler, EventSink, ToolCallHandler, VoiceProvider

logger = logging.getLogger(__name__)


class VoiceRegistry:
    def __init__(
        self,
        config: RootConfig,
        adapters: dict[str, VoiceProvider],
        hooks: VoiceHooks | None = None,
        metrics: VoiceMetrics | None = None,
    ):
        self._config = config
        self._adapters = adapters
        self._tool_handlers: dict[str, ToolCallHandler] = {}
        self._assistant_handler: AssistantRequestHandler | None = None
        self._event_sinks: list[EventSink] = []
        self.hooks = hooks or VoiceHooks()
        self.metrics = metrics or VoiceMetrics()

    @classmethod
    def from_config(
        cls,
        source: str | Path | dict[str, Any],
        credential_provider: CredentialProvider | None = None,
        hooks: VoiceHooks | None = None,
        provider_extra: dict[str, dict[str, Any]] | None = None,
        metrics: VoiceMetrics | None = None,
    ) -> VoiceRegistry:
        config = load_config(source)
        extra = provider_extra or {}
        adapters: dict[str, VoiceProvider] = {}
        for provider_id, provider in config.providers.items():
            if not provider.enabled:
                logger.info("provider disabled provider=%s", provider_id)
                continue
            adapter_cls = ADAPTERS.get(provider.adapter)
            if adapter_cls is None:
                raise ConfigError(f"provider {provider_id}: unknown adapter {provider.adapter!r}")
            assert_verify_implemented(adapter_cls)
            try:
                credentials = resolve_credentials(provider.credentials, provider_id, credential_provider)
            except CredentialError as exc:
                raise ConfigError(f"provider {provider_id}: {exc}") from exc
            adapter_config = {
                "budgets": provider.budgets,
                "inbound": provider.inbound,
                **provider.options,
                **(extra.get(provider_id) or {}),
            }
            adapters[provider_id] = adapter_cls(credentials=credentials, config=adapter_config)
        logger.info("registry built providers=%s", sorted(adapters))
        return cls(config, adapters, hooks, metrics)

    @property
    def assistant_handler(self) -> AssistantRequestHandler | None:
        return self._assistant_handler

    @property
    def unknown_event_policy(self) -> str:
        return self._config.unknown_event_policy

    @property
    def raw_payload_policy(self) -> str:
        return self._config.logging.raw_payload

    def get_adapter(self, provider_id: str) -> VoiceProvider | None:
        provider = self._config.providers.get(provider_id)
        if provider is None or not provider.enabled:
            return None
        return self._adapters.get(provider_id)

    def provider_config(self, provider_id: str) -> Any:
        return self._config.providers[provider_id]

    def on_assistant_request(self, handler: AssistantRequestHandler) -> AssistantRequestHandler:
        self._assistant_handler = handler
        return handler

    def on_tool_call(self, name: str):
        def decorator(handler: ToolCallHandler) -> ToolCallHandler:
            self._tool_handlers[name] = handler
            return handler

        return decorator

    def on_event(self, sink: EventSink) -> EventSink:
        self._event_sinks.append(sink)
        return sink

    def tool_handler(self, name: str) -> ToolCallHandler | None:
        return self._tool_handlers.get(name)

    def emit_event(self, provider_id: str, event: VoiceEvent) -> None:
        for sink in self._event_sinks:
            try:
                sink(provider_id, event)
            except Exception:
                logger.exception("event sink failed provider=%s", provider_id)

    async def dispatch(self, provider_id: str, headers: dict[str, str], body: bytes) -> WebhookResponse:
        return await dispatch_webhook(self, provider_id, headers, body)

    def budget_for(self, provider_id: str, event_type: str) -> float | None:
        return budget_for(self._config.providers[provider_id], event_type)
