"""Config endpoint the frontend boots from. Never echoes keys or the token."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from app.config import Settings
from app.deps import get_config
from app.pricing import ANTHROPIC_PRICES, JEV_PRICE_IN, JEV_PRICE_OUT

router = APIRouter(prefix="/api", tags=["meta"])


@router.get("/config")
async def config(request: Request, settings: Settings = Depends(get_config)):
    return {
        "jev": {
            "model": settings.jev_model,
            "price_in": JEV_PRICE_IN,
            "price_out": JEV_PRICE_OUT,
        },
        "anthropic_models": [
            {"id": mid, "price_in": p_in, "price_out": p_out}
            for mid, (p_in, p_out) in ANTHROPIC_PRICES.items()
        ]
        if settings.anthropic_api_key
        else [],
        "openai_compat": {
            "enabled": settings.openai_compat_enabled,
            "model": settings.openai_compat_model if settings.openai_compat_enabled else None,
            "price_source": "env"
            if settings.openai_compat_price_in is not None
            else "unknown",
        },
        "dataset_size": len(request.app.state.tickets),
        "race_max_items": min(settings.race_max_items, len(request.app.state.tickets)),
        "token_required": bool(settings.race_token),
    }


@router.get("/health")
async def health():
    return {"ok": True}
