"""Playground: classify one message on both sides, with inspector payloads."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Request

from app.config import Settings
from app.deps import (
    Providers,
    consume_rate_limit,
    get_config,
    get_providers,
    jev_cost_fn,
    playground_bucket,
    require_token,
    resolve_opponent,
)
from app.schemas import PlaygroundRequest, PlaygroundResponse, SideResult

router = APIRouter(prefix="/api/playground", tags=["playground"])


@router.post("", response_model=PlaygroundResponse)
async def playground(
    body: PlaygroundRequest,
    request: Request,
    settings: Settings = Depends(get_config),
    providers: Providers = Depends(get_providers),
):
    require_token(request, settings)
    if not settings.typesafe_api_key:
        raise HTTPException(503, detail="TYPESAFE_API_KEY not configured")
    llm, llm_cost_fn, llm_label = resolve_opponent(body.opponent, settings, providers)
    consume_rate_limit(request, settings, playground_bucket)

    jev_verdict, llm_verdict = await asyncio.gather(
        providers.jev.classify(body.text),
        llm.classify(body.text),
    )
    return PlaygroundResponse(
        jev=SideResult(
            provider=settings.jev_model,
            verdict=jev_verdict,
            cost_usd=jev_cost_fn(jev_verdict.usage) if jev_verdict.error is None else None,
        ),
        llm=SideResult(
            provider=llm_label,
            verdict=llm_verdict,
            cost_usd=llm_cost_fn(llm_verdict.usage) if llm_verdict.error is None else None,
        ),
    )
