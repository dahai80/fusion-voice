# fusion-voice

Voice-agent platform access layer for the Fusion ecosystem: one synchronous SPI and one normalized event model for hosted voice AI platforms.

**Status: M1, M2 and M3 implemented (2026-11-02).** Hot path (webhook verification, event normalization, assistant-request and tool-call responses), cold path (assistant deployment, call transfer, hang-up, call fetch), five platform adapters, and Prometheus-compatible metrics are implemented.

## Install

```bash
cd /Users/dahai/fusion && source .venv/bin/activate
pip install -e "fusion-voice[test,api]"
```

Core library has no web-framework dependency; `[api]` adds FastAPI, `[test]` adds pytest.

## Quick start

```python
from fastapi import FastAPI
from fusion_voice import AssistantResponse, ToolResult, VoiceRegistry
from fusion_voice.fastapi import build_voice_router

registry = VoiceRegistry.from_config("voice.yaml")


@registry.on_assistant_request
async def on_assistant_request(ctx):
    return AssistantResponse(assistant_id="asst_123")


@registry.on_tool_call("book_slot")
async def book_slot(ctx, call):
    return ToolResult(tool_call_id=call.tool_call_id, name=call.name, result={"slot": call.args["slot"]})


@registry.on_event
def on_event(provider_id, event):
    # call started/ended, status updates, transfer/hangup requests, unknown events
    ...


app = FastAPI()
app.include_router(build_voice_router(registry))
```

`voice.yaml` registers providers, credentials references, per-provider time budgets, and adapter `options` (for example Twilio's `webhook_url` or a configured transfer path). See [examples/voice.yaml](examples/voice.yaml). Credentials support literal values, `env:VAR`, and CredentialProvider references (`<provider_id>:<key>`); nothing is persisted by the library.

Request handling contract:

| Condition | Status |
|-----------|--------|
| Unknown or disabled provider | 404 |
| Body over `max_body_bytes` (default 1 MiB) | 413 |
| Client IP outside `inbound.allowed_ips` (opt-in) | 403 |
| Signature verification failed | 401 |
| Malformed payload | 400 |
| Assistant handler timeout or tool timeout | 504 for assistant; per-tool error result for tools |
| Handler crash | 500 with redacted body |

Assistant-request and tool-call responses are returned on the same HTTP connection. Budgets are measured from webhook entry, so signature verification and normalization time is deducted before the handler runs. Tool calls run concurrently, each bounded by the provider's `tool_call_s` budget; a timeout or crash becomes that tool's `error` result and does not fail the batch. Handlers receive the raw request headers (lowercased) in `ctx.headers` alongside `ctx.call_id` and the remaining budget.

Optional IP allowlisting is configured per provider under `inbound.allowed_ips` (CIDR or plain address). Set `inbound.trust_forwarded_for: true` only behind a trusted proxy that overwrites or appends the real client address; the rightmost `X-Forwarded-For` entry is then used, so clients cannot prepend a spoofed address. A provider with a non-empty allowlist rejects requests whose client IP is missing or outside every network.

## Supported platforms

| Provider | Hot path | Cold path |
|----------|----------|-----------|
| Vapi (P0) | assistant request, tools, lifecycle | deploy, transfer, hang-up, fetch |
| Retell (P1) | assistant request, tools, lifecycle | deploy (LLM + agent), transfer, hang-up, fetch |
| Bland (P1) | assistant request, tools, lifecycle | deploy, transfer, hang-up, fetch |
| Twilio (P2) | status callbacks | phone-number deploy, transfer, hang-up, fetch |
| ElevenLabs (P2) | server tools, post-call events | agent deploy, fetch |

Twilio ConversationRelay media/tool traffic runs over WebSocket and is outside the synchronous HTTP connector (PRD V4); the Twilio adapter covers signed status callbacks and REST call control. ElevenLabs exposes no public REST live-call transfer or hang-up, so those two methods raise `NotImplementedError`. LiveKit/Pipecat are watchlist only.

Retell, Bland, Twilio, and ElevenLabs field names follow public documentation; sandbox validation is tracked under PRD V1. Adapters are isolated, so a field-name correction is a single-file change.

## Metrics

`VoiceMetrics` records per-provider request counts, outcomes, durations, and signature-verification outcomes. Mount the optional Prometheus text endpoint:

```python
from fusion_voice.fastapi import build_metrics_router

app.include_router(build_metrics_router(registry))  # GET /voice/metrics
```

Exposed series: `fusion_voice_requests_total`, `fusion_voice_request_duration_ms_sum`, `fusion_voice_verification_total`, and `fusion_voice_normalization_duration_ms_count`/`_sum` (adapter parse overhead per event type). `registry.metrics.snapshot()` returns the same data as a dict for direct integration with another observability backend. Pass a shared `VoiceMetrics()` to `VoiceRegistry.from_config(..., metrics=...)` when multiple apps should report into one collector.

## Non-goals

No audio runtime (STT/TTS/VAD/barge-in), no LLM orchestration, no number purchase/billing console, no outbound messaging (that is `fusion-channel`), and no archive-first/outbox persistence. Voice traffic goes directly between the hosted platform and the consumer backend; the library has no resident service and no network port.

## Documentation

- [Changelog](CHANGELOG.md) — release history.
- [Technical architecture](docs/tech-architecture.md) — module layout, hot-path dispatch and timeout control, status-code matrix, config schema, testing strategy.
- [Architecture plan](../../architecture/fusion-voice-archi-plan-1102.md) — milestones M0–M3 and ecosystem boundaries.
- [PRD v0.1](../../architecture/fusion-voice-prd.md) — requirements, security red lines, performance budgets.

## License

Apache License 2.0. See [LICENSE](LICENSE).
