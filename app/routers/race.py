"""Race endpoints: start, SSE stream (with Last-Event-ID replay), cancel."""

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
    race_bucket,
    require_token,
    resolve_opponent,
)
from app.race import manager
from app.schemas import RaceItem, RaceRequest
from app.streaming import sse_events

router = APIRouter(prefix="/api/race", tags=["race"])

TERMINAL_EVENTS = frozenset({"race_done", "cancelled", "race_error"})


@router.post("", status_code=201)
async def start_race(
    body: RaceRequest,
    request: Request,
    settings: Settings = Depends(get_config),
    providers: Providers = Depends(get_providers),
):
    # The run owns the providers once it exists; until then this handler
    # does, and every refusal after a client was built must close it.
    run = None
    try:
        require_token(request, settings)
        if manager.race_in_progress:
            raise HTTPException(409, detail="a race is already running")
        if body.tickets is not None:
            # Pasted tickets: the operator's cap alone, never clamped to the
            # bundled dataset's size. Ids are minted here so no client text
            # is ever echoed in a frame.
            if len(body.tickets) > settings.race_max_items:
                raise HTTPException(422, detail=f"tickets exceed cap ({settings.race_max_items})")
            items = [RaceItem(id=f"p{i}", text=t) for i, t in enumerate(body.tickets, start=1)]
        else:
            dataset = request.app.state.tickets
            cap = min(settings.race_max_items, len(dataset))
            if body.ticket_count > cap:
                raise HTTPException(422, detail=f"ticket_count exceeds cap ({cap})")
            items = dataset[: body.ticket_count]
        llm, llm_cost_fn, llm_label = resolve_opponent(body.opponent, settings, providers)
        # Last gate: only requests that will actually start work spend a token.
        consume_rate_limit(request, settings, race_bucket)
        run = await manager.start(
            tickets=items,
            classifiers={"jev": providers.jev, "llm": llm},
            cost_fns={"jev": jev_cost_fn, "llm": llm_cost_fn},
            concurrency={"jev": settings.jev_concurrency, "llm": settings.llm_concurrency},
            per_ticket_timeout=settings.per_ticket_timeout,
            sides_meta={"jev": settings.jev_model, "llm": llm_label},
            providers=providers,
        )
    finally:
        if run is None:
            await providers.close()
    if run is None:
        raise HTTPException(409, detail="a race is already running")
    return {"race_id": run.id, "total": run.total}


@router.get("/{race_id}/stream")
async def stream_race(race_id: str, request: Request):
    run = manager.get(race_id)
    if run is None:
        raise HTTPException(404, detail="unknown race")
    return StreamingResponse(
        sse_events(run, request.headers.get("last-event-id"), TERMINAL_EVENTS),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.delete("/{race_id}", status_code=204)
async def cancel_race(
    race_id: str,
    request: Request,
    settings: Settings = Depends(get_config),
):
    # When RACE_TOKEN is set it gates cancels too; otherwise possessing the
    # unguessable race_id is the bearer credential.
    require_token(request, settings)
    found = await manager.cancel(race_id)
    if not found:
        raise HTTPException(404, detail="unknown race")
