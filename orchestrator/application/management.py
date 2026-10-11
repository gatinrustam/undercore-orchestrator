"""Local operator use cases. All mutations use the same journal locks as HTTP."""

from orchestrator.domain.models import OrchestratorError
from orchestrator.application.leases import NodeLeases


def manage_node(gateway, operation, data=None, node_id=None):
    registry = gateway.registry
    if registry is None:
        raise OrchestratorError("management_unavailable", 503)
    if operation == "list":
        return registry.snapshot(gateway)
    if operation in ("add", "update", "save"):
        # Private input; tokens never enter command arguments, stdout or errors.
        if not isinstance(data, dict):
            raise OrchestratorError("invalid_request", 422)
        if operation == "add":
            if data.get("expected_revision", 0) != 0:
                raise OrchestratorError("invalid_request", 422)
            data["expected_revision"] = 0
        if operation == "update":
            if (
                data.get("id") != node_id
                or type(data.get("expected_revision")) is not int
                or data["expected_revision"] < 1
            ):
                raise OrchestratorError("invalid_request", 422)
        return registry.save(gateway, data)
    node = gateway.nodes.get(node_id)
    if node is None:
        raise OrchestratorError("not_found", 404)
    if operation == "probe":
        gateway.drivers.for_node(node).verify(node)
    elif operation == "restore":
        if not node.lease_enabled:
            raise OrchestratorError("control_lease_unavailable", 409)
        NodeLeases(gateway).restore(node)
    else:
        raise OrchestratorError("unsupported_operation", 422)
    return {"id": node_id, "status": "ok"}
