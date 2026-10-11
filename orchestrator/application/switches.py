"""Explicit same-protocol node switch. Revoke-before-create, durable at every step.

The stable connection alias and canonical device stay unchanged. A failed/uncertain
operation must resume on its pinned target; it may never pick a third node.
"""

from orchestrator.application.execution import (
    bounded,
    remaining,
    DeadlineExceeded,
    CapacityExceeded,
)

from orchestrator.application.telemetry import Stage, observed
from orchestrator.domain.records import SwitchState
from datetime import datetime, timezone
from orchestrator.application.ports import NodeGrant
from orchestrator.domain.models import OrchestratorError


class Switches:
    def __init__(self, gateway):
        self.gateway = gateway
        self.store = gateway.store

    def pending(self, client_id):
        return self.store.switches.pending(client_id)

    @bounded
    def switch(self, client_id, request):
        with self.store.lock(client_id):
            row = self.store.get(client_id=client_id)
            if row is None or row["device_id"] != request.device_id:
                raise OrchestratorError("not_found", 404)
            from orchestrator.application.leases import NodeLeases

            if not self.gateway.drivers.compatible(row["protocol"], request.capabilities):
                raise OrchestratorError("client_upgrade_required", 409)
            saved, _ = self.store.switches.history(client_id, request.idempotency_key)
            if saved:
                operation = dict(saved)
                if operation["source_node"] != request.expected_node_id:
                    raise OrchestratorError("switch_identity_conflict", 409)
                if operation["state"] == "cancelled":
                    raise OrchestratorError("switch_cancelled", 410)
                if operation["state"] == "complete":
                    # Never replay an older switch over a later successful switch.
                    if row["binding_key"] != operation["binding_key"]:
                        raise OrchestratorError("switch_superseded", 409)
                    return self.gateway.client(client_id)
            else:
                if NodeLeases(self.gateway).denied(row):
                    raise OrchestratorError("access_unavailable", 410)
                if self.pending(client_id):
                    raise OrchestratorError("switch_in_progress", 409)
                if row["node_id"] != request.expected_node_id:
                    raise OrchestratorError("assigned_node_changed", 409)
                source_offline = False
                try:
                    source = self.gateway.client(client_id)
                except OrchestratorError as error:
                    if (
                        isinstance(error, (DeadlineExceeded, CapacityExceeded))
                        or error.status != 503
                    ):
                        raise
                    from orchestrator.application.leases import NodeLeases

                    source = NodeLeases(self.gateway).cached_source(row)
                    source_offline = True
                if source["status"] != "active" or self.expired(source["expires_at"]):
                    raise OrchestratorError("access_unavailable", 410)
                observations = []
                for node in self.gateway.nodes.values():
                    if (
                        node.id != row["node_id"]
                        and node.protocol == row["protocol"]
                        and self.gateway.available(node)
                    ):
                        try:
                            observations.append(self.gateway.drivers.for_node(node).observe(node))
                        except OrchestratorError:
                            pass
                remaining()
                operation = self.reserve(row, request, source, observations)
                if source_offline:
                    NodeLeases(self.gateway).fence(self.gateway.node(row))
            return self.resume(row, operation)

    @staticmethod
    def expired(expires_at):
        return datetime.fromisoformat(expires_at.replace("Z", "+00:00")) <= datetime.now(
            timezone.utc
        )

    def reserve(self, row, request, source, observations):
        return self.store.switches.reserve(row, request, source, observations)

    @observed(
        Stage.SWITCH,
        identity=lambda self, row, operation: (row["client_id"], operation["operation_id"]),
    )
    @bounded
    def resume(self, row, operation):
        target = self.gateway.nodes.get(operation["target_node"])
        if (
            target is None
            or target.server_id != operation["target_server"]
            or target.protocol != row["protocol"]
        ):
            raise OrchestratorError("assigned_node_identity_changed", 503)
        if not self.gateway.available(target) and not operation["disable_requested"]:
            raise OrchestratorError("target_node_unavailable", 503)
        # Never grant an expired access, even when recovering a prior request.
        if self.expired(operation["expires_at"]):
            self.cancel_expired(row, operation, target)
            raise OrchestratorError("grant_expired", 410)
        if operation["state"] == "reserved":
            self.revoke_source(row)
            self.store.switches.state(operation, SwitchState.SOURCE_REVOKED)
        driver = self.gateway.drivers.for_node(target)
        grant = NodeGrant(operation["binding_key"], operation["name"], operation["expires_at"])
        value = driver.validate(driver.create(target, grant), external_id=grant.binding_key)
        allowed = (
            ("active", "disabled", "expired") if operation["disable_requested"] else ("active",)
        )
        if value["status"] not in allowed or value["expires_at"] != grant.expires_at:
            raise OrchestratorError("target_access_unavailable", 503)
        if operation["disable_requested"]:
            value = driver.validate(
                driver.mutate(target, value["client_id"], "disable", {}),
                external_id=grant.binding_key,
                client_id=value["client_id"],
            )
            if value["status"] not in ("disabled", "expired"):
                raise OrchestratorError("revoke_unconfirmed", 503)
        else:
            # Check this driver's payload before publishing the new binding.
            driver.configuration(target, value["client_id"])
        self.store.switches.complete(row, operation, value["client_id"])
        from orchestrator.application.leases import NodeLeases

        NodeLeases(self.gateway).remember(self.store.get(client_id=row["client_id"]), value)
        return {**value, "client_id": row["client_id"], "external_id": row["external_id"]}

    def revoke_source(self, row):
        source = self.gateway.node(row)
        driver = self.gateway.drivers.for_node(source)
        try:
            remote_id = self.gateway.resolve(row)
            value = driver.validate(
                driver.mutate(source, remote_id, "disable", {}),
                external_id=row["binding_key"],
                client_id=remote_id,
            )
            if value["status"] not in ("disabled", "expired"):
                raise OrchestratorError("revoke_unconfirmed", 503)
        except OrchestratorError as error:
            if isinstance(error, (DeadlineExceeded, CapacityExceeded)) or error.status != 503:
                raise
            from orchestrator.application.leases import NodeLeases

            leases = NodeLeases(self.gateway)
            leases.fence(source)
            leases.retire(row)

    def disable_pending(self, row, operation):
        # Persist revocation intent BEFORE recovery; a restart must not publish an
        # active target if the business authority has already requested disable.
        self.store.switches.request_disable(operation)
        try:
            self.resume(row, {**operation, "disable_requested": 1})
        except OrchestratorError as error:
            if error.code != "grant_expired":
                raise

    def cancel_expired(self, row, operation, target):
        # Node agents independently enforce expiry. Still confirm both sides before
        # releasing the pending operation; no new peer can be created at this point.
        self.revoke_source(row)
        driver = self.gateway.drivers.for_node(target)
        matches = [v for v in driver.list(target) if v["external_id"] == operation["binding_key"]]
        if len(matches) > 1:
            raise OrchestratorError("remote_identity_conflict", 409)
        for match in matches:
            value = driver.validate(
                driver.mutate(target, match["client_id"], "disable", {}),
                external_id=operation["binding_key"],
                client_id=match["client_id"],
            )
            if value["status"] not in ("disabled", "expired"):
                raise OrchestratorError("revoke_unconfirmed", 503)
        self.store.switches.state(operation, SwitchState.CANCELLED)
