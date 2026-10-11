"""Shared CLI/GUI commands. Record intent before any side effect; never log inputs."""

from orchestrator.application.management import manage_node
from orchestrator.application.repository_ports import OperatorJournal, PolicyEditor
from orchestrator.application.execution import budget
from orchestrator.application.telemetry import trace_scope
from orchestrator.config.policy import RuntimePolicy
from orchestrator.domain.administration import Command
from orchestrator.domain.inventory import NodeSettings
from orchestrator.domain.models import OrchestratorError


class Administration:
    def __init__(self, factory, journal: OperatorJournal, policies: PolicyEditor):
        self.factory, self.journal, self.policies = factory, journal, policies

    def execute(self, command: Command, actor: str):
        gateway = self.factory()
        try:
            with (
                budget(gateway.policy.agents.operation_timeout_seconds),
                gateway.store.lock("operator"),
            ):
                entry = self.journal.begin(command.model_dump(), actor)
                if entry["outcome"] == "succeeded":
                    return entry["result"]
                if not entry.pop("new", False):
                    raise OrchestratorError("operator_review_required", 409)
                try:
                    with trace_scope(gateway.telemetry):
                        result = self.apply(gateway, command)
                except Exception:
                    # Remote effects can survive a failed call. Do not label a retry safe.
                    self.journal.finish(command.operation_id, {"status": "review_required"}, False)
                    raise
                self.journal.finish(command.operation_id, result, True)
                return result
        finally:
            gateway.close()

    def apply(self, gateway, command):
        data = dict(command.data)
        if command.action == "node.save":
            if data.get("id") != command.target:
                raise OrchestratorError("invalid_request", 422)
            return manage_node(gateway, "save", data)
        if command.action == "node.mode":
            if set(data) != {"expected_revision", "mode", "capacity"}:
                raise OrchestratorError("invalid_request", 422)
            record = next(
                (r for r in gateway.registry.records() if r["id"] == command.target), None
            )
            if record is None:
                raise OrchestratorError("not_found", 404)
            config = NodeSettings.model_validate_json(record["configuration"])
            update = config.model_dump(exclude={"api_key_file"}) | data
            return manage_node(gateway, "save", update)
        if command.action in ("node.probe", "node.restore"):
            if data:
                raise OrchestratorError("invalid_request", 422)
            return manage_node(gateway, command.action.split(".")[1], node_id=command.target)
        if command.action == "connection.disable":
            if data:
                raise OrchestratorError("invalid_request", 422)
            # Existing lifecycle persists denial even when the node is unavailable.
            gateway.client(command.target, "disable")
            return {"id": command.target, "status": "disable_requested"}
        if command.target != "runtime" or set(data) != {"expected_revision", "policy"}:
            raise OrchestratorError("invalid_request", 422)
        try:
            policy = RuntimePolicy.model_validate(data["policy"]).model_dump()
        except ValueError:
            raise OrchestratorError("invalid_policy", 422) from None
        return self.policies.save(data["expected_revision"], policy)
