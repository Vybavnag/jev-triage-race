"""Review endpoints: start, SSE stream (with Last-Event-ID replay), cancel."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

from app.config import Settings
from app.deps import (
    Providers,
    consume_rate_limit,
    get_config,
    get_providers,
    jev_cost_fn,
    require_token,
    resolve_opponent,
    review_bucket,
    review_slots,
)
from app.review_run import manager
from app.schemas import ReviewRequest
from app.streaming import sse_events

router = APIRouter(prefix="/api/review", tags=["review"])

TERMINAL_EVENTS = frozenset({"review_done", "cancelled", "review_error"})
BUSY = "busy: too many reviews in flight, try again shortly"


@router.post("", status_code=201)
async def start_review(
    body: ReviewRequest,
    request: Request,
    settings: Settings = Depends(get_config),
    providers: Providers = Depends(get_providers),
):
    # The run owns the providers once it exists; until then this handler
    # does, and every refusal after a client was built must close it.
    run = None
    try:
        require_token(request, settings)
        llm, llm_cost_fn, llm_label = resolve_opponent(body.opponent, settings, providers)
        # Two gates, both last: the in-flight cap so a slow provider can't
        # stack up work, then the bucket so only work that starts costs a token.
        if not review_slots.available:
            raise HTTPException(503, detail=BUSY, headers={"Retry-After": "10"})
        consume_rate_limit(request, settings, review_bucket)
        run = await manager.start(
            code=body.code,
            questions=body.questions,
            reviewers={"jev": providers.jev, "llm": llm},
            cost_fns={"jev": jev_cost_fn, "llm": llm_cost_fn},
            sides_meta={"jev": settings.jev_model, "llm": llm_label},
            attempt_timeout=settings.review_attempt_timeout,
            timeout=settings.review_timeout,
            providers=providers,
        )
    finally:
        if run is None:
            await providers.close()
    if run is None:
        raise HTTPException(503, detail=BUSY, headers={"Retry-After": "10"})
    return {"review_id": run.id}


@router.get("/{review_id}/stream")
async def stream_review(review_id: str, request: Request):
    run = manager.get(review_id)
    if run is None:
        raise HTTPException(404, detail="unknown review")
    return StreamingResponse(
        sse_events(run, request.headers.get("last-event-id"), TERMINAL_EVENTS),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.delete("/{review_id}", status_code=204)
async def cancel_review(
    review_id: str,
    request: Request,
    settings: Settings = Depends(get_config),
):
    # When RACE_TOKEN is set it gates cancels too; otherwise possessing the
    # unguessable review_id is the bearer credential.
    require_token(request, settings)
    found = await manager.cancel(review_id)
    if not found:
        raise HTTPException(404, detail="unknown review")
