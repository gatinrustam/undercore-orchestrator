"""Bounded client recovery, pinned to the revision the client actually received."""

import time
from orchestrator.application.execution import bounded, budget, DeadlineExceeded, CapacityExceeded

from orchestrator.application.telemetry import Stage, observed
from orchestrator.application.connections import Connections
from orchestrator.domain.contracts import SwitchRequest
from orchestrator.domain.models import OrchestratorError
from orchestrator.application.switches import Switches


class Recovery:
    cooldown = 1800

    def __init__(self, gateway):
        self.gateway = gateway
        self.cooldown = gateway.policy.recovery.cooldown_seconds
        self.connections = Connections(gateway)
        self.switches = Switches(gateway)

    @observed(
        Stage.RECOVERY,
        identity=lambda self, client_id, request: (
            client_id,
            "recovery-v1-r" + str(request.expected_revision),
        ),
    )
    @bounded
    def recover(self, client_id, request):
        with self.gateway.store.lock(client_id):
            row = self.connections.owned(client_id, request.device_id)
            if not self.gateway.drivers.compatible(row["protocol"], request.capabilities):
                raise OrchestratorError("client_upgrade_required", 409)
            # The same revision always addresses the same operation, even after
            # a timeout, app restart, background completion or another HTTP worker.
            key = "recovery-v1-r" + str(request.expected_revision)
            saved, latest = self.gateway.store.switches.history(client_id, key)
            if saved and saved["state"] not in ("complete", "cancelled"):
                self.switches.resume(row, dict(saved))
            elif not saved:
                try:
                    current = self.connections.configuration_locked(client_id, request)
                except OrchestratorError as error:
                    if (
                        isinstance(error, (DeadlineExceeded, CapacityExceeded))
                        or error.status != 503
                        or request.expected_revision != row["configuration_revision"]
                    ):
                        raise
                    from orchestrator.application.leases import NodeLeases

                    # Only a lease-backed, previously verified grant can survive an outage.
                    NodeLeases(self.gateway).cached_source(row)
                    from types import SimpleNamespace

                    current = SimpleNamespace(revision=row["configuration_revision"])
                if request.expected_revision > current.revision:
                    raise OrchestratorError("revision_conflict", 409)
                if request.expected_revision < current.revision:
                    return current
                if latest is not None and time.time() - latest < self.cooldown:
                    raise OrchestratorError("recovery_cooldown", 429)
                self.switches.switch(
                    client_id,
                    SwitchRequest(
                        schema_version=1,
                        device_id=request.device_id,
                        capabilities=request.capabilities,
                        expected_node_id=row["node_id"],
                        idempotency_key=key,
                    ),
                )
            # A completed/superseded request only reads the current binding. Never
            # switch back when a delayed client repeats an old revision.
            return self.connections.configuration_locked(client_id, request)


@observed(Stage.RECONCILE)
def reconcile(gateway, limit=None):
    """One bounded pass; failures rotate to the end instead of starving other devices."""
    limit = limit if limit is not None else gateway.policy.recovery.reconcile_batch_size
    rows = gateway.store.switches.queued(limit)
    completed = failed = 0
    switches = Switches(gateway)
    for item in rows:
        try:
            with (
                budget(gateway.policy.agents.operation_timeout_seconds),
                gateway.store.lock(item),
            ):
                operation = switches.pending(item)
                if operation is None:
                    continue
                row = gateway.store.get(client_id=item)
                try:
                    switches.resume(row, operation)
                    completed += 1
                finally:
                    gateway.store.switches.touch(item)
        except OrchestratorError:
            failed += 1
    return {"completed": completed, "pending_or_busy": failed}
