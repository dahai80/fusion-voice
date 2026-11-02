from __future__ import annotations

import pytest

from fusion_voice.config import ConfigError, load_config


def _minimal() -> dict:
    return {"providers": [{"id": "vapi-main", "adapter": "vapi"}]}


def test_minimal_defaults():
    config = load_config(_minimal())
    provider = config.providers["vapi-main"]
    assert provider.enabled is True
    assert provider.budgets.assistant_request_s == 7.0
    assert provider.budgets.tool_call_s == 5.0
    assert provider.inbound.max_body_bytes == 1048576
    assert provider.inbound.verify == "required"
    assert config.unknown_event_policy == "record"
    assert config.logging.raw_payload == "hooks"


def test_load_yaml_file(tmp_path):
    path = tmp_path / "voice.yaml"
    path.write_text(
        "providers:\n  - id: bland-main\n    adapter: bland\n    enabled: false\n"
        "    budgets:\n      tool_call_s: 2.5\n",
        encoding="utf-8",
    )
    config = load_config(path)
    assert config.providers["bland-main"].enabled is False
    assert config.providers["bland-main"].budgets.tool_call_s == 2.5


def test_missing_file():
    with pytest.raises(ConfigError, match="not found"):
        load_config("/nonexistent/voice.yaml")


def test_unknown_root_key():
    with pytest.raises(ConfigError, match="unknown keys"):
        load_config({"providers": [], "extra": 1})


def test_unknown_provider_key():
    with pytest.raises(ConfigError, match="unknown keys"):
        load_config({"providers": [{"id": "a", "adapter": "vapi", "foo": 1}]})


def test_duplicate_provider_id():
    with pytest.raises(ConfigError, match="duplicate"):
        load_config({"providers": [{"id": "a", "adapter": "vapi"}, {"id": "a", "adapter": "vapi"}]})


def test_invalid_provider_id():
    with pytest.raises(ConfigError, match="invalid provider id"):
        load_config({"providers": [{"id": "Bad Id", "adapter": "vapi"}]})


def test_missing_adapter():
    with pytest.raises(ConfigError, match="adapter is required"):
        load_config({"providers": [{"id": "a"}]})


def test_assistant_budget_out_of_range():
    with pytest.raises(ConfigError, match="assistant_request_s"):
        load_config({"providers": [{"id": "a", "adapter": "vapi", "budgets": {"assistant_request_s": 8}}]})


def test_tool_budget_out_of_range():
    with pytest.raises(ConfigError, match="tool_call_s"):
        load_config({"providers": [{"id": "a", "adapter": "vapi", "budgets": {"tool_call_s": 0}}]})


def test_body_limit_too_small():
    with pytest.raises(ConfigError, match="max_body_bytes"):
        load_config({"providers": [{"id": "a", "adapter": "vapi", "inbound": {"max_body_bytes": 10}}]})


def test_invalid_verify_mode():
    with pytest.raises(ConfigError, match="verify"):
        load_config({"providers": [{"id": "a", "adapter": "vapi", "inbound": {"verify": "maybe"}}]})


def test_invalid_unknown_policy():
    with pytest.raises(ConfigError, match="unknown_event_policy"):
        load_config({"providers": [], "unknown_event_policy": "explode"})


def test_invalid_raw_payload_mode():
    with pytest.raises(ConfigError, match="raw_payload"):
        load_config({"providers": [], "logging": {"raw_payload": "all"}})


def test_invalid_logging_level_rejected():
    with pytest.raises(ConfigError, match="logging.level"):
        load_config({"providers": [], "logging": {"level": "verbose"}})


def test_logging_level_normalized():
    config = load_config({"providers": [], "logging": {"level": "debug"}})
    assert config.logging.level == "DEBUG"


def test_options_mapping_passthrough():
    config = load_config(
        {"providers": [{"id": "a", "adapter": "twilio", "options": {"webhook_url": "https://example.com/voice/a"}}]}
    )
    assert config.providers["a"].options == {"webhook_url": "https://example.com/voice/a"}


def test_options_must_be_mapping():
    with pytest.raises(ConfigError, match="options must be a mapping"):
        load_config({"providers": [{"id": "a", "adapter": "twilio", "options": ["x"]}]})


def test_allowed_ips_normalized_to_networks():
    config = load_config(
        {"providers": [{"id": "a", "adapter": "vapi", "inbound": {"allowed_ips": ["10.0.0.1", "192.168.0.0/16"]}}]}
    )
    assert config.providers["a"].inbound.allowed_ips == ["10.0.0.1/32", "192.168.0.0/16"]


def test_invalid_allowed_ip_rejected():
    with pytest.raises(ConfigError, match="allowed_ips"):
        load_config({"providers": [{"id": "a", "adapter": "vapi", "inbound": {"allowed_ips": ["not-an-ip"]}}]})


def test_non_boolean_flags_rejected():
    with pytest.raises(ConfigError, match="enabled must be a boolean"):
        load_config({"providers": [{"id": "a", "adapter": "vapi", "enabled": "false"}]})
    with pytest.raises(ConfigError, match="trust_forwarded_for must be a boolean"):
        load_config({"providers": [{"id": "a", "adapter": "vapi", "inbound": {"trust_forwarded_for": "yes"}}]})


def test_verify_disabled_warns(caplog):
    config = load_config({"providers": [{"id": "a", "adapter": "vapi", "inbound": {"verify": "disabled"}}]})
    assert config.providers["a"].inbound.verify == "disabled"
    assert any("verification disabled" in record.message for record in caplog.records)
