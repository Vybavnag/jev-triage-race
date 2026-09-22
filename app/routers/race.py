"""Race endpoints: start, SSE stream (with Last-Event-ID replay), cancel."""

from __future__ import annotations

import asyncio
import json

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
from app.schemas import RaceRequest

router = APIRouter(prefix="/api/race", tags=["race"])

TERMINAL_EVENTS = {"race_done", "cancelled", "race_error"}
HEARTBEAT_SECONDS = 15


@router.post("", status_code=201)
async def start_race(
    body: RaceRequest,
    request: Request,
    settings: Settings = Depends(get_config),
    providers: Providers = Depends(get_providers),
):
    require_token(request, settings)
    if manager.race_in_progress:
        raise HTTPException(409, detail="a race is already running")
    if not settings.typesafe_api_key:
        raise HTTPException(503, detail="TYPESAFE_API_KEY not configured")
    tickets = request.app.state.tickets
    cap = min(settings.race_max_items, len(tickets))
    if body.ticket_count > cap:
        raise HTTPException(422, detail=f"ticket_count exceeds cap ({cap})")
    llm, llm_cost_fn, llm_label = resolve_opponent(body.opponent, settings, providers)
    # Last gate: only requests that will actually start work spend a token.
    consume_rate_limit(request, settings, race_bucket)
    run = await manager.start(
        tickets=tickets[: body.ticket_count],
        classifiers={"jev": providers.jev, "llm": llm},
        cost_fns={"jev": jev_cost_fn, "llm": llm_cost_fn},
        concurrency={"jev": settings.jev_concurrency, "llm": settings.llm_concurrency},
        per_ticket_timeout=settings.per_ticket_timeout,
        sides_meta={"jev": settings.jev_model, "llm": llm_label},
    )
    if run is None:
        raise HTTPException(409, detail="a race is already running")
    return {"race_id": run.id, "total": run.total}


def _frame(seq: int, event: str, data: dict) -> str:
    return f"id: {seq}\nevent: {event}\ndata: {json.dumps(data, default=str)}\n\n"


@router.get("/{race_id}/stream")
async def stream_race(race_id: str, request: Request):
    run = manager.get(race_id)
    if run is None:
        raise HTTPException(404, detail="unknown race")

    last_id_header = request.headers.get("last-event-id")
    try:
        last_id = int(last_id_header) if last_id_header else 0
    except ValueError:
        last_id = 0

    async def generate():
        queue = run.subscribe()
        try:
            gapped, backlog = run.replay_from(last_id)
            if gapped:
                yield _frame(run.floor_seq, "gap", {"from": last_id, "to": run.floor_seq})
            replayed_to = last_id
            for seq, event, data in backlog:
                yield _frame(seq, event, data)
                replayed_to = seq
                if event in TERMINAL_EVENTS:
                    return
            if run.is_finished:
                return
            while True:
                try:
                    seq, event, data = await asyncio.wait_for(
                        queue.get(), timeout=HEARTBEAT_SECONDS
                    )
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
                    continue
                if seq <= replayed_to:
                    continue
                yield _frame(seq, event, data)
                if event in TERMINAL_EVENTS:
                    return
        finally:
            run.unsubscribe(queue)

    return StreamingResponse(
        generate(),
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
