"""Independent loopback-only viewer. No backend routes or mutation service attached."""

import json
import re
import secrets
import time
from importlib.resources import files
from urllib.parse import urlsplit
from collections import deque

from fastapi import FastAPI, Request, Query
from fastapi.responses import JSONResponse, Response
from starlette.concurrency import run_in_threadpool
from orchestrator.application.repository_ports import PanelReader
from orchestrator.domain.panel import PanelOverview

COOKIE = "orchestrator_panel"
SESSION_SECONDS = 3600


def create_panel_app(reader: PanelReader, events, token: str, origin: str, administration=None):
    address = urlsplit(origin)
    if (
        address.scheme != "http"
        or address.hostname != "127.0.0.1"
        or address.path
        or address.query
        or address.fragment
        or address.username
        or not address.port
    ):
        raise ValueError("Loopback origin required")
    if not re.fullmatch(r"[A-Za-z0-9_-]{32,256}", token):
        raise ValueError("Invalid panel credential")
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    sessions = {}
    attempts = deque(maxlen=20)
    headers = {
        "Cache-Control": "private, no-store, max-age=0",
        "Pragma": "no-cache",
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "Referrer-Policy": "no-referrer",
        "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
    }

    def authenticated(request):
        auth = request.headers.getlist("authorization")
        if auth:
            return len(auth) == 1 and secrets.compare_digest(
                auth[0].encode(), ("Bearer " + token).encode()
            )
        value = request.cookies.get(COOKIE, "")
        return sessions.get(value, 0) > time.monotonic()

    @app.middleware("http")
    async def guard(request, call_next):
        if (
            request.headers.getlist("host") != [address.netloc]
            or request.headers.get("sec-fetch-site") == "cross-site"
        ):
            result = JSONResponse({"detail": "forbidden_origin"}, status_code=403)
        elif request.method not in ("GET", "HEAD") and request.headers.getlist("origin") != [
            origin
        ]:
            result = JSONResponse({"detail": "forbidden_origin"}, status_code=403)
        elif request.url.path.startswith("/admin/") and not authenticated(request):
            result = JSONResponse({"detail": "unauthorized"}, status_code=401)
        else:
            result = await call_next(request)
        result.headers.update(headers)
        return result

    @app.exception_handler(Exception)
    async def failure(request, error):
        return JSONResponse({"detail": "panel_unavailable"}, status_code=503, headers=headers)

    @app.get("/")
    async def index():
        return Response(
            files("orchestrator.interfaces.http").joinpath("panel_assets/index.html").read_bytes(),
            media_type="text/html",
        )

    @app.get("/assets/{name}")
    async def asset(name: str):
        types = {
            "panel.css": "text/css",
            "panel.js": "text/javascript",
            "commands.js": "text/javascript",
        }
        if name not in types:
            return Response(status_code=404)
        return Response(
            files("orchestrator.interfaces.http").joinpath("panel_assets/" + name).read_bytes(),
            media_type=types[name],
        )

    @app.post("/session")
    async def login(request: Request):
        now = time.monotonic()
        if len(attempts) == attempts.maxlen and now - attempts[0] < 60:
            return JSONResponse(
                {"detail": "try_later"}, status_code=429, headers={"Retry-After": "60"}
            )
        attempts.append(now)
        body = bytearray()
        async for part in request.stream():
            body.extend(part)
            if len(body) > 1024:
                return Response(status_code=413)
        try:
            payload = json.loads(body)
            credential = payload["token"]
            valid = (
                isinstance(credential, str)
                and secrets.compare_digest(credential.encode(), token.encode())
                and set(payload) == {"token"}
            )
        except (ValueError, TypeError, KeyError):
            valid = False
        if not valid:
            return JSONResponse({"detail": "unauthorized"}, status_code=401)
        for key in list(sessions):
            if sessions[key] <= now:
                del sessions[key]
        if len(sessions) >= 64:
            del sessions[next(iter(sessions))]
        previous = request.cookies.get(COOKIE)
        sessions.pop(previous, None)
        session = secrets.token_urlsafe(32)
        sessions[session] = now + SESSION_SECONDS
        result = JSONResponse({"status": "signed_in"})
        # Plain HTTP is restricted to loopback; remote access uses SSH forwarding.
        result.set_cookie(
            COOKIE, session, max_age=SESSION_SECONDS, httponly=True, samesite="strict", path="/"
        )
        return result

    @app.post("/admin/v1/logout")
    async def logout(request: Request):
        sessions.pop(request.cookies.get(COOKIE), None)
        result = JSONResponse({"status": "signed_out"})
        result.delete_cookie(COOKIE, path="/", httponly=True, samesite="strict")
        return result

    @app.get("/admin/v1/overview", response_model=PanelOverview)
    async def overview(
        node_offset: int = Query(default=0, ge=0, le=1000000),
        connection_offset: int = Query(default=0, ge=0, le=1000000),
    ):
        result = await run_in_threadpool(reader.overview, node_offset, connection_offset)
        result.events, result.events_available = await run_in_threadpool(events.recent)
        return result

    if administration is not None:
        from orchestrator.interfaces.http.panel_commands import attach_commands

        attach_commands(app, administration)

    @app.get("/admin/v1/capabilities")
    async def capabilities():
        return {"management": administration is not None}

    return app
