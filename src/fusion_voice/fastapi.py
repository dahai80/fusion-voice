from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from fusion_voice.registry import VoiceRegistry

logger = logging.getLogger(__name__)


def build_voice_router(registry: VoiceRegistry, prefix: str = "/voice") -> APIRouter:
    router = APIRouter(prefix=prefix)

    @router.post("/{provider_id}")
    async def handle_webhook(provider_id: str, request: Request) -> JSONResponse:
        body = await request.body()
        headers = {key.lower(): value for key, value in request.headers.items()}
        result = await registry.dispatch(provider_id, headers, body)
        logger.debug("webhook handled provider=%s status=%d", provider_id, result.status)
        return JSONResponse(status_code=result.status, content=result.body)

    return router
