# fusion-voice

Voice-agent platform access layer for the Fusion ecosystem: one synchronous SPI and one normalized event model for hosted voice AI platforms.

**Status: M1 implemented (2026-11-02).** Hot path (webhook verification, event normalization, assistant-request and tool-call responses) and cold path (assistant deployment, call transfer, hang-up, call fetch) are implemented for Vapi, Retell, and Bland. Vapi cold-path management is complete; Retell and Bland expose call fetch only, with deploy/transfer/hang-up deferred to M2 per the PRD.

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

`voice.yaml` registers providers, credentials references, and per-provider time budgets. See [examples/voice.yaml](examples/voice.yaml). Credentials support literal values, `env:VAR`, and CredentialProvider references (`<provider_id>:<key>`); nothing is persisted by the library.

Request handling contract:

| Condition | Status |
|-----------|--------|
| Unknown or disabled provider | 404 |
| Body over `max_body_bytes` (default 1 MiB) | 413 |
| Signature verification failed | 401 |
| Malformed payload | 400 |
| Assistant handler timeout or tool timeout | 504 for assistant; per-tool error result for tools |
| Handler crash | 500 with redacted body |

Assistant-request and tool-call responses are returned on the same HTTP connection. Tool calls run concurrently, each bounded by the provider's `tool_call_s` budget; a timeout or crash becomes that tool's `error` result and does not fail the batch.

## Supported platforms

| Provider | Hot path | Cold path |
|----------|----------|-----------|
| Vapi (P0) | yes | deploy, transfer, hang-up, fetch |
| Retell (P1) | yes | fetch |
| Bland (P1) | yes | fetch |

Twilio ConversationRelay and ElevenLabs are P2 and not implemented. LiveKit/Pipecat are watchlist only.

Retell and Bland webhook field names follow their public documentation; sandbox validation is tracked under PRD V1. Adapters are isolated, so a field-name correction is a single-file change.

## Non-goals

No audio runtime (STT/TTS/VAD/barge-in), no LLM orchestration, no number purchase/billing console, no outbound messaging (that is `fusion-channel`), and no archive-first/outbox persistence. Voice traffic goes directly between the hosted platform and the consumer backend; the library has no resident service and no network port.

## Documentation

- [Technical architecture](docs/tech-architecture.md) — module layout, hot-path dispatch and timeout control, status-code matrix, config schema, testing strategy.
- [Architecture plan](../../architecture/fusion-voice-archi-plan-1102.md) — milestones M0–M3 and ecosystem boundaries.
- [PRD v0.1](../../architecture/fusion-voice-prd.md) — requirements, security red lines, performance budgets.

## License

Apache License 2.0. See [LICENSE](LICENSE).
