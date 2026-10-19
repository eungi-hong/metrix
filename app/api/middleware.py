"""ASGI middleware.

A pure ASGI middleware rather than Starlette's `BaseHTTPMiddleware`, which
runs the endpoint in a separate task: context variables set there would not
reliably reach the route, and here they must.
"""

from __future__ import annotations

import re
import time
import uuid

import structlog
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core import metrics
from app.core.context import CallClass, attributed

REQUEST_ID_HEADER = "X-Request-ID"
# A caller's id is echoed only if it is short and plain, so it cannot inject
# into logs or headers; otherwise a fresh one is issued.
_VALID_REQUEST_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


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


class RequestContextMiddleware:
    """A request id on every request and response, bound into every log line
    the request writes, and the request's latency by route template.

    The route template (`/tickers/{symbol}`, not `/tickers/NVDA`) keeps the
    metric's label set bounded; a request no route matched is `unmatched`.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self._paths: dict[object, str] = {}

    def _route_of(self, scope: Scope) -> str:
        route = scope.get("route")
        if route is not None and getattr(route, "path", None):
            return route.path
        endpoint = scope.get("endpoint")
        if endpoint is None:
            return "unmatched"
        if endpoint not in self._paths:
            app = scope.get("app")
            for candidate in getattr(getattr(app, "router", None), "routes", []):
                if getattr(candidate, "endpoint", None) is endpoint:
                    self._paths[endpoint] = candidate.path
                    break
            else:
                self._paths[endpoint] = "unmatched"
        return self._paths[endpoint]

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        supplied = None
        for name, value in scope.get("headers", []):
            if name == b"x-request-id":
                supplied = value.decode("latin-1")
                break
        request_id = supplied if supplied and _VALID_REQUEST_ID.match(supplied) else uuid.uuid4().hex
        status = 500

        async def send_with_id(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                MutableHeaders(scope=message)[REQUEST_ID_HEADER] = request_id
            await send(message)

        started = time.perf_counter()
        with structlog.contextvars.bound_contextvars(request_id=request_id):
            try:
                await self.app(scope, receive, send_with_id)
            finally:
                metrics.HTTP_LATENCY.labels(scope["method"], self._route_of(scope), str(status)).observe(
                    time.perf_counter() - started
                )
