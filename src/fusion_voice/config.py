from __future__ import annotations

import ipaddress
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)


class ConfigError(Exception):
    pass


@dataclass(slots=True)
class BudgetConfig:
    assistant_request_s: float = 7.0
    tool_call_s: float = 5.0


@dataclass(slots=True)
class InboundConfig:
    max_body_bytes: int = 1048576
    verify: str = "required"
    allowed_ips: list[str] = field(default_factory=list)
    trust_forwarded_for: bool = False


@dataclass(slots=True)
class ProviderConfig:
    id: str
    adapter: str
    enabled: bool = True
    credentials: dict[str, str] = field(default_factory=dict)
    options: dict[str, Any] = field(default_factory=dict)
    budgets: BudgetConfig = field(default_factory=BudgetConfig)
    inbound: InboundConfig = field(default_factory=InboundConfig)


@dataclass(slots=True)
class LoggingConfig:
    level: str = "INFO"
    raw_payload: str = "hooks"


@dataclass(slots=True)
class RootConfig:
    providers: dict[str, ProviderConfig] = field(default_factory=dict)
    unknown_event_policy: str = "record"
    logging: LoggingConfig = field(default_factory=LoggingConfig)


_ROOT_KEYS = {"providers", "unknown_event_policy", "logging"}
_PROVIDER_KEYS = {"id", "adapter", "enabled", "credentials", "options", "budgets", "inbound"}
_BUDGET_KEYS = {"assistant_request_s", "tool_call_s"}
_INBOUND_KEYS = {"max_body_bytes", "verify", "allowed_ips", "trust_forwarded_for"}
_LOGGING_KEYS = {"level", "raw_payload"}
_ID_PATTERN = re.compile(r"[a-z0-9_-]+")


def _reject_unknown(section: dict[str, Any], allowed: set[str], where: str) -> None:
    unknown = set(section) - allowed
    if unknown:
        raise ConfigError(f"unknown keys in {where}: {sorted(unknown)}")


def _parse_budgets(raw: Any, index: int) -> BudgetConfig:
    if not isinstance(raw, dict):
        raise ConfigError(f"providers[{index}].budgets must be a mapping")
    _reject_unknown(raw, _BUDGET_KEYS, f"providers[{index}].budgets")
    assistant = float(raw.get("assistant_request_s", 7.0))
    tool = float(raw.get("tool_call_s", 5.0))
    if not 0 < assistant <= 7.5:
        raise ConfigError(f"providers[{index}].budgets.assistant_request_s must be in (0, 7.5]")
    if not 0 < tool <= 10:
        raise ConfigError(f"providers[{index}].budgets.tool_call_s must be in (0, 10]")
    return BudgetConfig(assistant_request_s=assistant, tool_call_s=tool)


def _parse_inbound(raw: Any, index: int) -> InboundConfig:
    if not isinstance(raw, dict):
        raise ConfigError(f"providers[{index}].inbound must be a mapping")
    _reject_unknown(raw, _INBOUND_KEYS, f"providers[{index}].inbound")
    max_body = int(raw.get("max_body_bytes", 1048576))
    verify = str(raw.get("verify", "required"))
    if max_body < 1024:
        raise ConfigError(f"providers[{index}].inbound.max_body_bytes must be >= 1024")
    if verify not in {"required", "disabled"}:
        raise ConfigError(f"providers[{index}].inbound.verify must be required|disabled")
    if verify == "disabled":
        logger.warning("inbound verification disabled provider_index=%d", index)

    raw_allowed = raw.get("allowed_ips") or []
    if not isinstance(raw_allowed, list):
        raise ConfigError(f"providers[{index}].inbound.allowed_ips must be a list")
    allowed_ips = []
    for item in raw_allowed:
        try:
            allowed_ips.append(str(ipaddress.ip_network(str(item), strict=False)))
        except ValueError as exc:
            raise ConfigError(f"providers[{index}].inbound.allowed_ips invalid entry {item!r}: {exc}") from exc
    trust_forwarded_for = bool(raw.get("trust_forwarded_for", False))
    return InboundConfig(
        max_body_bytes=max_body,
        verify=verify,
        allowed_ips=allowed_ips,
        trust_forwarded_for=trust_forwarded_for,
    )


def _parse_logging(raw: Any) -> LoggingConfig:
    if not isinstance(raw, dict):
        raise ConfigError("logging must be a mapping")
    _reject_unknown(raw, _LOGGING_KEYS, "logging")
    raw_payload = str(raw.get("raw_payload", "hooks"))
    if raw_payload not in {"hooks", "off"}:
        raise ConfigError("logging.raw_payload must be hooks|off")
    return LoggingConfig(level=str(raw.get("level", "INFO")), raw_payload=raw_payload)


def load_config(source: str | Path | dict[str, Any]) -> RootConfig:
    if isinstance(source, dict):
        data: Any = source
    else:
        path = Path(source)
        if not path.exists():
            raise ConfigError(f"config file not found: {path}")
        with path.open("r", encoding="utf-8") as handle:
            data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        raise ConfigError("config root must be a mapping")
    _reject_unknown(data, _ROOT_KEYS, "root")

    raw_providers = data.get("providers") or []
    if not isinstance(raw_providers, list):
        raise ConfigError("providers must be a list")

    providers: dict[str, ProviderConfig] = {}
    for index, raw in enumerate(raw_providers):
        if not isinstance(raw, dict):
            raise ConfigError(f"providers[{index}] must be a mapping")
        _reject_unknown(raw, _PROVIDER_KEYS, f"providers[{index}]")
        provider_id = str(raw.get("id") or "").strip()
        if not provider_id:
            raise ConfigError(f"providers[{index}].id is required")
        if not _ID_PATTERN.fullmatch(provider_id):
            raise ConfigError(f"invalid provider id: {provider_id}")
        if provider_id in providers:
            raise ConfigError(f"duplicate provider id: {provider_id}")
        adapter = str(raw.get("adapter") or "").strip()
        if not adapter:
            raise ConfigError(f"providers[{index}].adapter is required")
        credentials = raw.get("credentials") or {}
        if not isinstance(credentials, dict):
            raise ConfigError(f"providers[{index}].credentials must be a mapping")
        options = raw.get("options") or {}
        if not isinstance(options, dict):
            raise ConfigError(f"providers[{index}].options must be a mapping")
        providers[provider_id] = ProviderConfig(
            id=provider_id,
            adapter=adapter,
            enabled=bool(raw.get("enabled", True)),
            credentials={str(key): str(value) for key, value in credentials.items()},
            options={str(key): value for key, value in options.items()},
            budgets=_parse_budgets(raw.get("budgets") or {}, index),
            inbound=_parse_inbound(raw.get("inbound") or {}, index),
        )

    unknown_policy = str(data.get("unknown_event_policy", "record"))
    if unknown_policy not in {"record", "drop"}:
        raise ConfigError(f"invalid unknown_event_policy: {unknown_policy}")

    logger.info("config loaded providers=%d", len(providers))
    return RootConfig(
        providers=providers,
        unknown_event_policy=unknown_policy,
        logging=_parse_logging(data.get("logging") or {}),
    )
