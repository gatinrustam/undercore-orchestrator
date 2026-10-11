"""Explicit opt-in mutation surface; session/Origin guards are owned by panel.py."""

import json
from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool
from orchestrator.domain.administration import Command
from orchestrator.domain.models import OrchestratorError


def attach_commands(app, administration):
    @app.get("/admin/v1/policy")
    async def policy():
        return await run_in_threadpool(administration.policies.read)

    @app.get("/admin/v1/audit")
    async def audit():
        return {"entries": await run_in_threadpool(administration.journal.recent)}

    @app.post("/admin/v1/commands")
    async def command(request: Request):
        body = bytearray()
        async for part in request.stream():
            body.extend(part)
            if len(body) > 16384:
                return JSONResponse({"detail": "request_too_large"}, status_code=413)
        try:
            value = Command.model_validate(json.loads(body))
        except (ValueError, TypeError):
            # Never echo a Pydantic input containing an agent credential.
            return JSONResponse({"detail": "invalid_request"}, status_code=422)
        try:
            result = await run_in_threadpool(administration.execute, value, "panel")
            return {"operation_id": value.operation_id, **result}
        except OrchestratorError as error:
            return JSONResponse(
                {"detail": error.code, "operation_id": value.operation_id}, status_code=error.status
            )
        except Exception:
            return JSONResponse(
                {"detail": "operator_review_required", "operation_id": value.operation_id},
                status_code=503,
            )
