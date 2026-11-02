# fusion-voice

Voice-agent platform access layer for the Fusion ecosystem: one synchronous SPI and one normalized event model for hosted voice AI platforms (Vapi, Retell, Bland, ElevenLabs, Twilio ConversationRelay).

**Status: pre-implementation (PRD M0).** The SPI and the vapi adapter are incubating in the first consumer application. This package is extracted here at milestone M1, triggered by a second real consumer or a second adapter.

## What it will do

- Verify inbound webhooks fail-closed, normalize platform events into `VoiceEvent`, and format synchronous assistant-request and tool-call responses.
- Deploy assistant configurations as code (`AssistantSpec`), control live calls (transfer, hang-up), and fetch call records for reconciliation.
- Keep platform-specific payloads inside adapters; consumers never see provider field names.

## Non-goals

No audio runtime (STT/TTS/VAD), no LLM orchestration, and no outbound messaging (that is `fusion-channel`'s job). Voice traffic goes directly between the hosted platform and the consumer backend; the library has no resident service and no network port.

## Documentation

- [Technical architecture](docs/tech-architecture.md) — module layout, hot-path dispatch and timeout control, status-code matrix, config schema, extraction plan.

## License

Apache License 2.0. See [LICENSE](LICENSE).
