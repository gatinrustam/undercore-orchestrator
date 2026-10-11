from contextlib import asynccontextmanager
import json
import re
import secrets
from datetime import datetime
from fastapi import FastAPI, Request
from pydantic import ValidationError
from orchestrator.domain.contracts import Renewal, Replacement
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from orchestrator.interfaces.http.observability import RequestObservability
from starlette.concurrency import run_in_threadpool
from orchestrator.domain.models import OrchestratorError


def validate(operation, payload):
    schemas = {
        "create": ({"external_id", "device_id", "name", "expires_at"}, set()),
        "lookup": ({"external_id", "device_id"}, set()),
        "renew": ({"expires_at", "idempotency_key"}, set()),
        "replace": ({"expected_external_id", "expires_at", "idempotency_key"}, {"allow_create"}),
        "enable": (set(), set()),
        "disable": (set(), set()),
    }
    if operation in ("renew", "replace"):
        try:
            model = Renewal if operation == "renew" else Replacement
            return model.model_validate(payload).model_dump(exclude_unset=True)
        except ValidationError:
            raise OrchestratorError("invalid_request", 422) from None
    required, optional = schemas[operation]
    if (
        not isinstance(payload, dict)
        or not required <= payload.keys()
        or payload.keys() - required - optional
    ):
        raise OrchestratorError("invalid_request", 422)
    for key, value in payload.items():
        if key == "allow_create":
            if type(value) is not bool:
                raise OrchestratorError("invalid_request", 422)
            continue
        if not isinstance(value, str):
            raise OrchestratorError("invalid_request", 422)
        if key in ("external_id", "expected_external_id") and not re.fullmatch(
            r"[A-Za-z0-9_-]{1,100}", value
        ):
            raise OrchestratorError("invalid_request", 422)
        if key == "device_id" and value != "account-v1":
            raise OrchestratorError("invalid_request", 422)
        if key == "name" and (
            not 1 <= len(value) <= 120 or any(ord(c) < 32 or ord(c) == 127 for c in value)
        ):
            raise OrchestratorError("invalid_request", 422)
        if key == "idempotency_key" and not re.fullmatch(r"[A-Za-z0-9_:.+-]{1,160}", value):
            raise OrchestratorError("invalid_request", 422)
        if key == "expires_at":
            try:
                date = datetime.fromisoformat(value.replace("Z", "+00:00"))
                if (
                    not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z", value)
                    or date.year > 2100
                ):
                    raise ValueError()
            except ValueError:
                raise OrchestratorError("invalid_request", 422) from None
    return payload


def create_agent_app(service, token):
    if not re.fullmatch(r"[A-Za-z0-9_-]{32,256}", token):
        raise ValueError("Invalid backend token")

    @asynccontextmanager
    async def lifespan(app):
        try:
            yield
        finally:
            await run_in_threadpool(service.close)

    app = FastAPI(
        title="Undercore VPN orchestration",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )

    @app.middleware("http")
    async def guard(request, call_next):
        values = request.headers.getlist("authorization")
        expected = None if request.url.path.startswith("/internal/admin/") else token
        if (
            expected is None
            or len(values) != 1
            or not secrets.compare_digest(values[0].encode(), ("Bearer " + expected).encode())
        ):
            response = JSONResponse({"detail": "unauthorized"}, status_code=401)
        else:
            response = await call_next(request)
        response.headers.update(
            {"Cache-Control": "private, no-store, max-age=0", "Pragma": "no-cache"}
        )
        return response

    @app.exception_handler(OrchestratorError)
    async def failure(request, error):
        return JSONResponse({"detail": error.code}, status_code=error.status)

    @app.exception_handler(Exception)
    async def unavailable(request, error):
        return JSONResponse(
            {"detail": "orchestrator_unavailable"},
            status_code=503,
            headers={"Cache-Control": "no-store"},
        )

    async def body(request, operation):
        data = bytearray()
        async for chunk in request.stream():
            data.extend(chunk)
            if len(data) > 8192:
                raise OrchestratorError("request_too_large", 413)
        try:
            return validate(operation, json.loads(data) if data else {})
        except (ValueError, UnicodeError):
            raise OrchestratorError("invalid_request", 422) from None

    @app.get("/v1/health")
    async def health():
        from orchestrator.version import current_version

        version = current_version()
        return {
            "status": "ok",
            "protocol": "amneziawg",
            "mode": "pilot",
            "formats": ["conf", "amnezia-vpn"],
            "service": "undercore-orchestrator",
            "version": version,
            "contract_version": 2,
        }

    @app.get("/internal/v1/metrics")
    async def metrics():
        try:
            summary = await run_in_threadpool(service.store.diagnostic_counts)
        except Exception:
            summary = None
        content, content_type = service.telemetry.render(summary)
        return Response(content, headers={"Content-Type": content_type})

    @app.get("/internal/v1/nodes")
    async def nodes():
        return await run_in_threadpool(service.overview)

    @app.get("/v1/clients")
    async def listing():
        return await run_in_threadpool(service.listing)

    @app.post("/v1/clients")
    async def create(request: Request):
        return await run_in_threadpool(service.create, await body(request, "create"))

    @app.post("/v1/clients/lookup")
    async def lookup(request: Request):
        payload = await body(request, "lookup")
        return await run_in_threadpool(service.lookup, payload["external_id"])

    def legacy_only(client_id):
        row = service.store.get(client_id=client_id)
        if row is not None and row["protocol"] != "amneziawg":
            raise OrchestratorError("not_found", 404)

    @app.api_route("/v1/clients/{client_id}", methods=["GET"])
    async def get(client_id: str):
        legacy_only(client_id)
        return await run_in_threadpool(service.client, client_id)

    @app.api_route("/v1/clients/{client_id}/{operation}", methods=["GET", "POST"])
    async def client(client_id: str, operation: str, request: Request):
        legacy_only(client_id)
        if not re.fullmatch(r"wgapi_[a-f0-9]{32}", client_id):
            raise OrchestratorError("not_found", 404)
        if request.method == "GET" and operation in ("configuration", "amnezia"):
            return PlainTextResponse(await run_in_threadpool(service.client, client_id, operation))
        if request.method == "POST" and operation in ("renew", "replace", "disable", "enable"):
            return await run_in_threadpool(
                service.client, client_id, operation, await body(request, operation)
            )
        raise OrchestratorError("not_found", 404)

    from orchestrator.interfaces.http.connections import mount

    mount(app, service)
    app.add_middleware(RequestObservability, observer=service.telemetry)
    return app
