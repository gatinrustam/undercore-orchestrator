"""Separate operator bearer boundary, unavailable to ordinary connection callers."""
from fastapi import Request
from starlette.concurrency import run_in_threadpool
from .http_v2 import json_body
from .models import PilotError
from .leases import NodeLeases


def mount(app,gateway):
    def registry():
        if gateway.registry is None:raise PilotError('management_unavailable',503)
        return gateway.registry

    @app.get('/internal/admin/nodes')
    async def nodes():
        return await run_in_threadpool(registry().snapshot,gateway)

    @app.post('/internal/admin/nodes')
    async def save(request:Request):
        data=await json_body(request)
        return await run_in_threadpool(registry().save,gateway,data)

    @app.post('/internal/admin/nodes/{node_id}/{action}')
    async def action(node_id:str,action:str,request:Request):
        body=await json_body(request)
        if action not in ('probe','restore') or body:raise PilotError('invalid_request',422)
        registry()
        node=gateway.nodes.get(node_id)
        if node is None:raise PilotError('not_found',404)
        if action=='probe':await run_in_threadpool(gateway.api.verify,node)
        else:
            if not node.lease_enabled:raise PilotError('control_lease_unavailable',409)
            await run_in_threadpool(NodeLeases(gateway).restore,node)
        return {'id':node_id,'status':'ok'}
