"""ASGI middleware.

A pure ASGI middleware rather than Starlette's `BaseHTTPMiddleware`, which
runs the endpoint in a separate task: context variables set there would not
reliably reach the route, and here they must.
"""

from __future__ import annotations

from starlette.types import ASGIApp, Receive, Scope, Send

from app.core.context import CallClass, attributed


class InteractiveAttributionMiddleware:
    """Mark everything an HTTP request does as interactive spend.

    A request has someone waiting on it by definition. Who that someone is
    (`current_user_id`) is added once requests are authenticated.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        with attributed(call_class=CallClass.INTERACTIVE):
            await self.app(scope, receive, send)
