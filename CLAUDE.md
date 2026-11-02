# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`fusion-voice` is the Fusion ecosystem's planned **voice-agent platform access layer**: a connector library that normalizes hosted voice AI platforms (Vapi, Retell, Bland, ElevenLabs, Twilio ConversationRelay) behind one SPI and one event model. Sister library of `fusion-channel` (messaging access layer) — same problem shape, but the **synchronous** variant: assistant-request (≤7.5s platform limit) and tool-call (default 5s budget) responses go back on the same HTTP connection, which is why the two libraries cannot merge.

**Current state: pre-implementation (PRD M0).** This directory is intentionally empty — no code, no pyproject, no git. Per PRD §10, the SPI and the vapi adapter incubate inside the first consumer repo at `business/ai-booking-agent/backend/booking_agent/voice/` (`spi.py` + `vapi_adapter.py`, interface verbatim per PRD §4). Extraction into this repo is milestone M1, triggered by a second real consumer or a second adapter. Do not start building here ahead of that trigger, and do not modify ai-booking-agent from this repo.

Authoritative spec: `/Users/dahai/fusion/architecture/fusion-voice-prd.md` (v0.1). Implementation-level design: `docs/tech-architecture.md` (module layout, hot-path dispatch and timeout control, status-code matrix, config schema, M0→M1 extraction plan). When SPI signatures, the event model, adapter roadmap, or open-question conclusions change, the PRD and tech-architecture doc must be updated in the same change (PRD §12 maintenance rule).

## Commands

None yet — no code exists here. When M1 extraction happens, bootstrap as an in-tree Python library following the `fusion-channel` template (the closest sibling):

```bash
cd /Users/dahai/fusion && source .venv/bin/activate   # REQUIRED before any Python work

cd fusion-voice
pip install -e ".[test]"          # setuptools, packages.find where=["src"]
pytest tests/                     # asyncio_mode=auto
ruff check . && ruff format .     # E/F/W/I/UP/B/SIM, line-length 120
```

Migration acceptance gate (PRD §9): ai-booking-agent's existing `/vapi/webhook` integration tests must stay green against the extracted vapi adapter — extraction is a move, not a rewrite.

## Architecture (from PRD, target shape)

- **SPI** (`VoiceProvider` Protocol, PRD §4) — split hot/cold paths, a hard constraint:
  - Hot (inbound, zero network I/O / persistence inside): `verify_request` (fail-closed, raises `VerificationError` → 401), `parse_event` (raw payload → `VoiceEvent`; unknown → `UNKNOWN` keeping raw fields), `format_assistant_response`, `format_tool_results` (aligned by toolCallId).
  - Cold (outbound management, platform REST APIs): `deploy_assistant(AssistantSpec)`, `transfer_call`, `hang_up`, `fetch_call` (reconciliation pull when events are lost).
- **Event model** (`VoiceEvent`, PRD §5): `ASSISTANT_REQUEST`, `TOOL_CALL`, `CALL_STARTED`, `CALL_ENDED`, `STATUS_UPDATE`, `TRANSFER/HANGUP_REQUESTED`, `ERROR`/`UNKNOWN`. Time budget travels in `VoiceSessionContext` (`call_id`, raw headers) from entry to handler.
- **`VoiceRegistry`** — `voice.yaml` data-driven registration (style of fusion-channel `channels.yaml`); per-provider enable switches; `@registry.on_tool_call` / `@registry.on_assistant_request` handler decorators; `build_voice_router(registry)` → `POST /voice/{provider_id}`.
- **Adapters** (PRD §6): `vapi` P0 (HMAC header + IP allowlist), `retell`/`bland` P1, `twilio`/`elevenlabs` P2 (Twilio HMAC-SHA1 verify can be ported from fusion-channel). Open-source voice runtimes (LiveKit/Pipecat) are watchlist only.
- **`AssistantSpec` / `ToolSpec`** — config-as-code assistant definitions deployed via REST; the same ToolSpec translates to each platform's function format. M1 covers only the cross-platform intersection (model/voice/transcriber/prompt/tools); platform-specific capabilities go through an escape hatch (PRD V3).

## Hard rules (PRD §8, enforced by tests when built)

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
- Independent git repo (like every fusion-* sub-project; this one not yet initialized). README.md English only; README_CN.md Chinese.
- 4-space indentation, no docstrings (comments only), logging on every module.
- The incubating SPI/adapter in ai-booking-agent is that repo's work; behavior changes there follow its own PRD/review flow. This repo only receives the M1 extraction.

## Ports

None — library only, same as fusion-channel. If a resident service is ever added, register its port in `/Users/dahai/fusion/architecture/port-registry.yaml` and run `python scripts/check-ports.py` from the repo root.
