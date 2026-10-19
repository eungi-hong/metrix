"""GET /events?symbol= -- live job progress and data changes, as server-sent events.

See `app.services.events` for the event types and where they come from.
Browsers' `EventSource` cannot send an Authorization header, so the web app
reads this with `fetch` and a stream reader; any SSE client that can set
headers works.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse

from app.api.deps import CurrentUser, SessionDep, SessionFactoryDep
from app.core.config import settings
from app.core.symbols import is_valid_symbol, normalize_symbol
from app.services import events, symbol_directory

router = APIRouter(tags=["events"])


@router.get(
    "/events",
    summary="Live updates for one ticker (server-sent events)",
    response_class=StreamingResponse,
    responses={
        200: {
            "content": {"text/event-stream": {}},
            "description": "`job` events (id, kind, status, progress) for the ticker's jobs, "
            "and `ticker_updated` (symbol, data_version) when its data changes. A comment "
            "line is sent as a heartbeat; the stream ends after a few minutes and the "
            "client reconnects.",
        },
        503: {"description": "This process has too many open streams; retry shortly."},
    },
)
async def ticker_events(
    principal: CurrentUser,
    session: SessionDep,
    factory: SessionFactoryDep,
    symbol: str = Query(..., description="The ticker to follow."),
) -> StreamingResponse:
    symbol = normalize_symbol(symbol)
    if not is_valid_symbol(symbol):
        raise HTTPException(status_code=422, detail=f"'{symbol}' is not a valid ticker symbol.")
    symbol = (await symbol_directory.check(session, symbol)).symbol  # BRK.B is BRK-B
    await session.close()  # the stream uses its own short sessions
    if events.open_streams >= settings.sse_max_connections:
        raise HTTPException(
            status_code=503, detail="Too many open event streams; retry shortly.", headers={"Retry-After": "5"}
        )
    return StreamingResponse(
        events.stream(factory, symbol),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
