from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from fusion_voice import AssistantResponse, VoiceRegistry
from fusion_voice.fastapi import build_metrics_router, build_voice_router
from tests.conftest import payload, sign, vapi_config

SECRET = "router-secret"
BODY = payload(
    {
        "message": {
            "type": "assistant-request",
            "call": {"id": "call_1", "type": "inboundPhoneCall"},
        }
    }
)


def _client(registry: VoiceRegistry) -> TestClient:
    app = FastAPI()
    app.include_router(build_voice_router(registry))
    return TestClient(app)


def test_route_mounted_at_default_prefix(secret):
    registry = VoiceRegistry.from_config(vapi_config(SECRET))
    client = _client(registry)
    response = client.post("/voice/vapi-main", content=b"{}", headers={"x-vapi-signature": "bad"})
    assert response.status_code == 401


def test_custom_prefix(secret):
    app = FastAPI()
    app.include_router(build_voice_router(VoiceRegistry.from_config(vapi_config(SECRET)), prefix="/hooks"))
    client = TestClient(app)
    response = client.post("/hooks/vapi-main", content=b"{}")
    assert response.status_code == 401


def test_assistant_request_round_trip(secret):
    registry = VoiceRegistry.from_config(vapi_config(SECRET))

    @registry.on_assistant_request
    async def handler(ctx):
        return AssistantResponse(assistant_id="asst_1")

    client = _client(registry)
    response = client.post(
        "/voice/vapi-main",
        content=BODY,
        headers={"x-vapi-signature": sign(SECRET, BODY)},
    )
    assert response.status_code == 200
    assert response.json() == {"assistantId": "asst_1"}


def test_unknown_provider_404(secret):
    client = _client(VoiceRegistry.from_config(vapi_config(SECRET)))
    response = client.post("/voice/ghost", content=b"{}")
    assert response.status_code == 404


def test_oversized_body_413(secret):
    registry = VoiceRegistry.from_config(vapi_config(SECRET, inbound={"max_body_bytes": 1024}))
    client = _client(registry)
    response = client.post("/voice/vapi-main", content=b"x" * 2048)
    assert response.status_code == 413


def test_metrics_endpoint(secret):
    registry = VoiceRegistry.from_config(vapi_config(SECRET))

    @registry.on_assistant_request
    async def handler(ctx):
        return AssistantResponse(assistant_id="asst_1")

    app = FastAPI()
    app.include_router(build_voice_router(registry))
    app.include_router(build_metrics_router(registry))
    client = TestClient(app)
    client.post("/voice/vapi-main", content=BODY, headers={"x-vapi-signature": sign(SECRET, BODY)})

    response = client.get("/voice/metrics")
    assert response.status_code == 200
    assert "fusion_voice_requests_total" in response.text
    assert 'outcome="ok"' in response.text


def test_header_case_insensitive(secret):
    registry = VoiceRegistry.from_config(vapi_config(SECRET))

    @registry.on_assistant_request
    async def handler(ctx):
        return AssistantResponse(assistant_id="asst_1")

    client = _client(registry)
    response = client.post(
        "/voice/vapi-main",
        content=BODY,
        headers={"X-VAPI-Signature": sign(SECRET, BODY)},
    )
    assert response.status_code == 200
    assert response.json() == {"assistantId": "asst_1"}
