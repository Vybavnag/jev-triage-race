"""Config endpoint the frontend boots from. Never echoes keys or the token."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from app.config import Settings
from app.deps import get_config
from app.pricing import ANTHROPIC_PRICES, JEV_PRICE_IN, JEV_PRICE_OUT
from app.review import DEFAULT_QUESTIONS
from app.schemas import MAX_CODE_CHARS, MAX_QUESTION_CHARS, MAX_QUESTIONS, MAX_TICKET_CHARS

router = APIRouter(prefix="/api", tags=["meta"])


@router.get("/config")
async def config(request: Request, settings: Settings = Depends(get_config)):
    dataset_size = len(request.app.state.tickets)
    return {
        "jev": {
            "model": settings.jev_model,
            "price_in": JEV_PRICE_IN,
            "price_out": JEV_PRICE_OUT,
        },
        # Every Claude opponent is offered: the visitor brings the key.
        "anthropic_models": [
            {"id": mid, "price_in": p_in, "price_out": p_out}
            for mid, (p_in, p_out) in ANTHROPIC_PRICES.items()
        ],
        "openai_compat": {
            "enabled": settings.openai_compat_enabled,
            "model": settings.openai_compat_model if settings.openai_compat_enabled else None,
            "price_source": "env"
            if settings.openai_compat_price_in is not None
            else "unknown",
        },
        "dataset_size": dataset_size,
        # Bundled tickets are clamped to what the dataset holds; pasted
        # tickets are bounded by the operator's cap alone.
        "race_max_items": min(settings.race_max_items, dataset_size),
        "max_own_tickets": settings.race_max_items,
        "max_ticket_chars": MAX_TICKET_CHARS,
        "review": {
            "default_questions": DEFAULT_QUESTIONS,
            "max_questions": MAX_QUESTIONS,
            "max_question_chars": MAX_QUESTION_CHARS,
            "max_code_chars": MAX_CODE_CHARS,
        },
        "token_required": bool(settings.race_token),
    }


@router.get("/health")
async def health():
    return {"ok": True}
