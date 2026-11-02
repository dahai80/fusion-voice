# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`fusion-voice` is the Fusion ecosystem's planned **voice-agent platform access layer**: a connector library that normalizes hosted voice AI platforms (Vapi, Retell, Bland, ElevenLabs, Twilio ConversationRelay) behind one SPI and one event model. Sister library of `fusion-channel` (messaging access layer) — same problem shape, but the **synchronous** variant: assistant-request (≤7.5s platform limit) and tool-call (default 5s budget) responses go back on the same HTTP connection, which is why the two libraries cannot merge.

**Current state: M2 and M3 implemented (2026-11-02).** All hot-path and cold-path features described below exist under `src/fusion_voice/`: SPI and event model (`spi.py`, `models.py`), config validation including per-provider adapter `options` (`config.py`), credential resolution (`credentials.py`), budgets (`budgets.py`), async structured logging (`hooks.py`), Prometheus-compatible metrics (`metrics.py`), dispatch/status matrix (`dispatch.py`), registry (`registry.py`), FastAPI webhook and metrics routers (`fastapi.py`), and five adapters (`adapters/vapi.py`, `retell.py`, `bland.py`, `twilio.py`, `elevenlabs.py`). Tests live in `tests/`, example config in `examples/voice.yaml`.

The PRD M0 incubation remains the behavioral baseline for Vapi: `business/ai-booking-agent/backend/booking_agent/voice/` (`spi.py` + `vapi_adapter.py`, interface verbatim per PRD §4). Extraction acceptance (PRD §9) is the consumer's `/vapi/webhook` integration suite staying green; switching the consumer over is that repo's change, not this one. Retell/Bland/Twilio/ElevenLabs hot paths follow public docs and still await sandbox validation (PRD V1). Twilio ConversationRelay media/tool traffic is WebSocket and outside this HTTP connector (PRD V4); ElevenLabs has no public REST live-call transfer/hang-up, so those methods raise `NotImplementedError`.

Authoritative spec: `/Users/dahai/fusion/architecture/fusion-voice-prd.md` (v0.1). Implementation-level design: `docs/tech-architecture.md` (module layout, hot-path dispatch and timeout control, status-code matrix, config schema, M0→M1 extraction plan). When SPI signatures, the event model, adapter roadmap, or open-question conclusions change, the PRD and tech-architecture doc must be updated in the same change (PRD §12 maintenance rule).

## Commands

```bash
cd /Users/dahai/fusion && source .venv/bin/activate   # REQUIRED before any Python work

cd fusion-voice
pip install -e ".[test,api]"      # setuptools, packages.find where=["src"]
pytest tests/                     # asyncio_mode=auto; 196 cases
FUSION_VOICE_BENCH=1 pytest tests/test_perf.py -q   # p95 overhead budgets
ruff check . && ruff format .     # E/F/W/I/UP/B/SIM, line-length 120
```

Migration acceptance gate (PRD §9): ai-booking-agent's existing `/vapi/webhook` integration tests must stay green against the extracted vapi adapter — extraction is a move, not a rewrite.

## Architecture (from PRD, target shape)

- **SPI** (`VoiceProvider` Protocol, PRD §4) — split hot/cold paths, a hard constraint:
  - Hot (inbound, zero network I/O / persistence inside): `verify_request` (fail-closed, raises `VerificationError` → 401), `parse_event` (raw payload → `VoiceEvent`; unknown → `UNKNOWN` keeping raw fields), `format_assistant_response`, `format_tool_results` (aligned by toolCallId).
  - Cold (outbound management, platform REST APIs): `deploy_assistant(AssistantSpec)`, `transfer_call`, `hang_up`, `fetch_call` (reconciliation pull when events are lost).
- **Event model** (`VoiceEvent`, PRD §5): `ASSISTANT_REQUEST`, `TOOL_CALL`, `CALL_STARTED`, `CALL_ENDED`, `STATUS_UPDATE`, `TRANSFER/HANGUP_REQUESTED`, `ERROR`/`UNKNOWN`. Time budget travels in `VoiceSessionContext` (`call_id`, raw headers) from entry to handler.
- **`VoiceRegistry`** — `voice.yaml` data-driven registration (style of fusion-channel `channels.yaml`); per-provider enable switches; `@registry.on_tool_call` / `@registry.on_assistant_request` handler decorators; `build_voice_router(registry)` → `POST /voice/{provider_id}`.
- **Adapters** (PRD §6): `vapi` P0, `retell`/`bland` P1, `twilio`/`elevenlabs` P2. All five implement the hot path and cold path except ElevenLabs transfer/hang-up (no public REST). Twilio verifies URL + form params with HMAC-SHA1 via `fusion_channel.verify.verify_twilio_signature` and requires `options.webhook_url`; ElevenLabs verifies `t=<ts>,v1=<hex>` HMAC-SHA256 with replay tolerance. Open-source voice runtimes (LiveKit/Pipecat) are watchlist only.
- **Metrics** (M3, `metrics.py`): `VoiceMetrics` counters and duration sums per provider/event/outcome plus verification outcomes; `registry.metrics` is always present; `build_metrics_router(registry)` exposes `GET /voice/metrics` in Prometheus text format.
- **`AssistantSpec` / `ToolSpec`** — config-as-code assistant definitions deployed via REST; the same ToolSpec translates to each platform's function format. M1 covers only the cross-platform intersection (model/voice/transcriber/prompt/tools); platform-specific capabilities go through an escape hatch (PRD V3).

## Hard rules (PRD §8, enforced by tests)

- Adapter without `verify_request` cannot register — `ConfigError` at registration.
- Fail-closed: verify/parse errors return 4xx, never 500 on attacker input; body over limit (default 1 MiB) → 413.
- Credentials never live in the library: `CredentialProvider` (AES-256-GCM) + `env:VAR` refs, reads masked (port from fusion-channel `credentials.py`).
- Error messages redacted before logging/response; raw payloads only escape via `ERROR`/`UNKNOWN` events for consumer-side PII policy.
- Hot path: structured logging is async, zero synchronous I/O inside the response budget.
- Perf budgets (PRD §9): adapter overhead <50ms for ASSISTANT_REQUEST normalization (leaves ≥450ms of the 500ms consumer budget), <20ms for TOOL_CALL; zero signature-bypass on fuzzed variants.

## Non-goals (PRD §3.2 — do not pull these in)

No audio runtime (STT/TTS/VAD/barge-in belong to the platform or to `fusion-speech` — the mutually exclusive self-hosted alternative), no LLM orchestration, no number purchase/billing/ops console, no outbound SMS (fusion-channel), no async archive-first/outbox semantics (voice inbound is a short synchronous transaction; persistence is the consumer's choice).

**Gateway exemption**: hosted voice platforms run their own cloud LLM, so voice traffic does not pass through the local fusion-gateway (`127.0.0.1:11432`). This is an approved, narrow exemption to the "all LLM via local gateway" rule — it covers only the platform-hosted part.

## Conventions

- In-tree Python library, `pip install -e fusion-voice`, same distribution mode as fusion-core/fusion-channel. No resident service.
- Independent git repo. README.md English only; README_CN.md Chinese.
- 4-space indentation, no docstrings (comments only), logging on every module.
- The incubating SPI/adapter in ai-booking-agent is that repo's work; behavior changes there follow its own PRD/review flow. This repo only receives the M1 extraction.
- Hot-path parsing catches `ValueError`/`RecursionError` (not only `JSONDecodeError`) and dispatch wraps `parse_event` — malformed or deeply nested attacker input must return 400, never 500.
- `VoiceRegistry.from_config` skips disabled providers entirely, so their `env:` credential refs are not resolved at startup.
- Adapter-specific settings go in `voice.yaml` under the provider's `options:` mapping (or `provider_extra`), never as new top-level provider keys.

## Ports

None — library only, same as fusion-channel. If a resident service is ever added, register its port in `/Users/dahai/fusion/architecture/port-registry.yaml` and run `python scripts/check-ports.py` from the repo root.
