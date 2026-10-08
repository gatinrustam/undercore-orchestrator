import secrets

from fastapi import Depends, FastAPI, Header
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .models import ConnectionIntent, PilotError
from .service import Orchestrator


def create_app(service: Orchestrator, backend_token: str) -> FastAPI:
    if len(backend_token) < 32:
        raise ValueError("A separate backend token of at least 32 characters is required")
    app = FastAPI(title="Undercore orchestration pilot", docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def no_cache(request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(PilotError)
    async def pilot_error(request, error):
        return JSONResponse({"error": error.code}, status_code=error.status)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, error):
        # Pydantic's default response includes the submitted input.
        return JSONResponse({"error": "invalid_intent"}, status_code=422)

    @app.exception_handler(Exception)
    async def internal_error(request, error):
        return JSONResponse({"error": "orchestrator_unavailable"}, status_code=503,
                            headers={"Cache-Control": "no-store"})

    def authenticate(authorization: str | None = Header(default=None)):
        expected = "Bearer " + backend_token
        if not authorization or not secrets.compare_digest(authorization.encode(), expected.encode()):
            raise PilotError("unauthorized", 401)

    @app.get("/healthz")
    def health():
        # Process liveness only. Not a claim about tunnel/node connectivity.
        return {"status": "ok", "mode": "local-pilot"}

    @app.post("/internal/v1/connections", dependencies=[Depends(authenticate)])
    def connect(intent: ConnectionIntent):
        return service.connect(intent)

    return app
