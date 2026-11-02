# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] - 2026-11-02

First release of the Fusion voice-agent platform access layer.

### Added

- Synchronous `VoiceProvider` SPI with hot path (`verify_request`, `parse_event`, `format_assistant_response`, `format_tool_results`) and cold path (`deploy_assistant`, `transfer_call`, `hang_up`, `fetch_call`).
- Normalized `VoiceEvent` model with nine event types and `VoiceSessionContext` carrying call id, raw headers, entry time, and remaining budget.
- `VoiceRegistry` with YAML-driven provider registration, per-provider switches, handler decorators, and event sinks.
- Adapters for Vapi, Retell, Bland, Twilio, and ElevenLabs, including platform-specific signature verification and assistant deployment.
- FastAPI webhook router (`POST /voice/{provider_id}`) and Prometheus text metrics endpoint (`GET /voice/metrics`).
- Time-budget enforcement measured from webhook entry, concurrent tool execution, and per-tool failure isolation.
- Credential resolution through literal values, `env:VAR`, and `fusion-channel` `CredentialProvider` references.
- Optional inbound IP allowlisting with trusted-proxy `X-Forwarded-For` handling.
- Strict configuration validation, including unknown-key rejection and per-provider adapter options.
- Structured logging hooks and request, verification, and normalization metrics.
- 236 tests covering the dispatch status-code matrix, signature fuzzing, budgets, configuration, adapters, routing, and performance gates.

### Security

- Signature verification is fail-closed: missing, malformed, or unexpectedly failing verification returns 401.
- Request bodies are limited to 1 MiB by default and oversized bodies return 413.
- Handler errors return redacted responses; raw payloads are only exposed through the configured hooks policy.
- Credentials are never persisted by the library.

### Documentation

- English and Chinese READMEs, technical architecture, example `voice.yaml`, and this changelog.

[0.1.0]: https://github.com/dahai80/fusion-voice/releases/tag/v0.1.0
