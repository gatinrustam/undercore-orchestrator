"""Bounded client recovery, pinned to the revision the client actually received."""

import time
from orchestrator.application.connections import Connections
from orchestrator.domain.contracts import SwitchRequest
from orchestrator.domain.models import OrchestratorError
from orchestrator.infrastructure.sqlite.locking import connection_lock
from orchestrator.application.switches import Switches


class Recovery:
    cooldown = 1800

    def __init__(self, gateway):
        self.gateway = gateway
        self.cooldown = gateway.policy.recovery.cooldown_seconds
        self.connections = Connections(gateway)
        self.switches = Switches(gateway)

    def recover(self, client_id, request):
        with connection_lock(self.gateway.store, client_id):
            row = self.connections.owned(client_id, request.device_id)
            if not self.gateway.drivers.compatible(row["protocol"], request.capabilities):
                raise OrchestratorError("client_upgrade_required", 409)
            # The same revision always addresses the same operation, even after
            # a timeout, app restart, background completion or another HTTP worker.
            key = "recovery-v1-r" + str(request.expected_revision)
            with self.gateway.store.db() as db:
                saved = db.execute(
                    "SELECT * FROM switches WHERE client_id=? AND operation_id=?", (client_id, key)
                ).fetchone()
                latest = db.execute(
                    "SELECT MAX(created_at) FROM switches WHERE client_id=?", (client_id,)
                ).fetchone()[0]
            if saved and saved["state"] not in ("complete", "cancelled"):
                self.switches.resume(row, dict(saved))
            elif not saved:
                try:
                    current = self.connections.configuration_locked(client_id, request)
                except OrchestratorError as error:
                    if (
                        error.status != 503
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


def reconcile(gateway, limit=None):
    """One bounded pass; failures rotate to the end instead of starving other devices."""
    limit = limit if limit is not None else gateway.policy.recovery.reconcile_batch_size
    with gateway.store.db() as db:
        rows = db.execute(
            "SELECT s.client_id FROM switches s JOIN assignments a USING(client_id) WHERE s.state NOT IN ('complete','cancelled') ORDER BY COALESCE(a.checked_at,0), s.created_at LIMIT ?",
            (limit,),
        ).fetchall()
    completed = failed = 0
    switches = Switches(gateway)
    for item in rows:
        try:
            with connection_lock(gateway.store, item["client_id"]):
                operation = switches.pending(item["client_id"])
                if operation is None:
                    continue
                row = gateway.store.get(client_id=item["client_id"])
                try:
                    switches.resume(row, operation)
                    completed += 1
                finally:
                    with gateway.store.db() as db:
                        db.execute(
                            "UPDATE assignments SET checked_at=? WHERE client_id=?",
                            (time.time(), item["client_id"]),
                        )
        except OrchestratorError:
            failed += 1
    return {"completed": completed, "pending_or_busy": failed}


if __name__ == "__main__":
    import json
    from orchestrator.bootstrap import agent_service

    service, _ = agent_service()
    print(json.dumps(reconcile(service)))  # Counts only, never identities/configurations.
