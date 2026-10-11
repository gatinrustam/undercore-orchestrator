"""Outer ASGI boundary: correlation and safe errors, including failures before routing."""

import asyncio

from starlette.responses import JSONResponse

from orchestrator.application.telemetry import Reason, Stage, span, trace_scope


def status_reason(status):
    known = {401: Reason.UNAUTHORIZED, 413: Reason.VALIDATION, 422: Reason.VALIDATION}
    if status in known:
        return known[status]
    if status >= 500:
        return Reason.UNAVAILABLE
    return Reason.REJECTED if status >= 400 else Reason.OK


class RequestObservability:
    def __init__(self, app, observer):
        self.app, self.observer = app, observer

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        with trace_scope(self.observer) as trace, span(Stage.HTTP) as result:
            started = finished = False

            async def response(message):
                nonlocal started, finished
                if message["type"] == "http.response.start":
                    started = True
                    result.status = message["status"]
                    result.reason = status_reason(result.status)
                    headers = [
                        (k, v)
                        for k, v in message.get("headers", [])
                        if k.lower() != b"x-request-id"
                    ]
                    message = {
                        **message,
                        "headers": [*headers, (b"x-request-id", trace.request_id.encode())],
                    }
                await send(message)
                if message["type"] == "http.response.body" and not message.get("more_body", False):
                    finished = True

            try:
                await self.app(scope, receive, response)
            except asyncio.CancelledError:
                result.reason = Reason.CANCELLED
                raise
            except Exception:
                result.reason = Reason.INTERNAL
                # Do not let Uvicorn print arbitrary exception messages/tracebacks.
                if not started:
                    await JSONResponse(
                        {"detail": "orchestrator_unavailable"},
                        status_code=503,
                        headers={"Cache-Control": "private, no-store", "Pragma": "no-cache"},
                    )(scope, receive, response)
                    result.reason = Reason.INTERNAL
                elif not finished:
                    # A partial response cannot be replaced; let the server close it
                    # using an exception whose message and chain disclose no secrets.
                    raise RuntimeError("response_delivery_failed") from None
