"""Explicit same-protocol node switch. Revoke-before-create, durable at every step.

The stable connection alias and canonical device stay unchanged. A failed/uncertain
operation must resume on its pinned target; it may never pick a third node.
"""
import time
import uuid
from datetime import datetime, timezone
from .drivers import NodeGrant
from .models import PilotError
from .operation_lock import connection_lock


class Switches:
    def __init__(self, gateway):
        self.gateway = gateway
        self.store = gateway.store

    def pending(self, client_id):
        with self.store.db() as db:
            row = db.execute("SELECT * FROM switches WHERE client_id=? AND state NOT IN ('complete', 'cancelled')", (client_id,)).fetchone()
            return dict(row) if row else None

    def switch(self, client_id, request):
        with connection_lock(self.store, client_id):
            row = self.store.get(client_id=client_id)
            if row is None or row['device_id'] != request.device_id:
                raise PilotError('not_found', 404)
            from .leases import NodeLeases
            if not self.gateway.drivers.compatible(row['protocol'], request.capabilities):
                raise PilotError('client_upgrade_required', 409)
            with self.store.db() as db:
                saved = db.execute('SELECT * FROM switches WHERE client_id=? AND operation_id=?',
                                   (client_id, request.idempotency_key)).fetchone()
            if saved:
                operation = dict(saved)
                if operation['source_node'] != request.expected_node_id:
                    raise PilotError('switch_identity_conflict', 409)
                if operation['state'] == 'cancelled':
                    raise PilotError('switch_cancelled', 410)
                if operation['state'] == 'complete':
                    # Never replay an older switch over a later successful switch.
                    if row['binding_key'] != operation['binding_key']:
                        raise PilotError('switch_superseded', 409)
                    return self.gateway.client(client_id)
            else:
                if NodeLeases(self.gateway).denied(row): raise PilotError('access_unavailable',410)
                if self.pending(client_id):
                    raise PilotError('switch_in_progress', 409)
                if row['node_id'] != request.expected_node_id:
                    raise PilotError('assigned_node_changed', 409)
                source_offline = False
                try:
                    source = self.gateway.client(client_id)
                except PilotError as error:
                    if error.status != 503: raise
                    from .leases import NodeLeases
                    source = NodeLeases(self.gateway).cached_source(row)
                    source_offline = True
                if source['status'] != 'active' or self.expired(source['expires_at']):
                    raise PilotError('access_unavailable', 410)
                observations = []
                for node in self.gateway.nodes.values():
                    if node.id != row['node_id'] and node.protocol == row['protocol'] and self.gateway.available(node):
                        try:
                            observations.append(self.gateway.drivers.for_node(node).observe(node))
                        except PilotError:
                            pass
                operation = self.reserve(row, request, source, observations)
                if source_offline:
                    NodeLeases(self.gateway).fence(self.gateway.node(row))
            return self.resume(row, operation)

    @staticmethod
    def expired(expires_at):
        return datetime.fromisoformat(expires_at.replace('Z', '+00:00')) <= datetime.now(timezone.utc)

    def reserve(self, row, request, source, observations):
        with self.store.db() as db:
            choices = []
            for observation in observations:
                if not 0 <= time.time() - observation.started_at <= 30:
                    continue
                node = observation.node
                assigned = db.execute('SELECT created_at FROM assignments WHERE node_id=?', (node.id,)).fetchall()
                pending = db.execute("SELECT created_at FROM switches WHERE target_node=? AND state NOT IN ('complete', 'cancelled')", (node.id,)).fetchall()
                occupied = max(len(assigned) + len(pending), observation.total_peers + sum(r['created_at'] >= observation.started_at for r in [*assigned, *pending]))
                capacity = min(node.capacity, observation.max_peers)
                if occupied < capacity:
                    choices.append((occupied / capacity, node.id, node.server_id))
            if not choices:
                raise PilotError('no_alternative_node', 503)
            _, target, identity = min(choices)
            value = (row['client_id'], request.idempotency_key, row['node_id'], target, identity,
                     'switch_' + uuid.uuid4().hex, source['name'], source['expires_at'], 'reserved', time.time())
            db.execute('INSERT INTO switches (client_id,operation_id,source_node,target_node,target_server,binding_key,name,expires_at,state,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)', value)
            return dict(db.execute('SELECT * FROM switches WHERE client_id=? AND operation_id=?', value[:2]).fetchone())

    def resume(self, row, operation):
        target = self.gateway.nodes.get(operation['target_node'])
        if target is None or target.server_id != operation['target_server'] or target.protocol != row['protocol']:
            raise PilotError('assigned_node_identity_changed', 503)
        if not self.gateway.available(target) and not operation['disable_requested']:
            raise PilotError('target_node_unavailable', 503)
        # Never grant an expired access, even when recovering a prior request.
        if self.expired(operation['expires_at']):
            self.cancel_expired(row, operation, target)
            raise PilotError('grant_expired', 410)
        if operation['state'] == 'reserved':
            self.revoke_source(row)
            with self.store.db() as db:
                db.execute("UPDATE switches SET state='source_revoked' WHERE client_id=? AND operation_id=?",
                           (row['client_id'], operation['operation_id']))
        driver = self.gateway.drivers.for_node(target)
        grant = NodeGrant(operation['binding_key'], operation['name'], operation['expires_at'])
        value = driver.validate(driver.create(target, grant), external_id=grant.binding_key)
        allowed = ('active', 'disabled', 'expired') if operation['disable_requested'] else ('active',)
        if value['status'] not in allowed or value['expires_at'] != grant.expires_at:
            raise PilotError('target_access_unavailable', 503)
        if operation['disable_requested']:
            value = driver.validate(driver.mutate(target, value['client_id'], 'disable', {}),
                                    external_id=grant.binding_key, client_id=value['client_id'])
            if value['status'] not in ('disabled', 'expired'):
                raise PilotError('revoke_unconfirmed', 503)
        else:
            # Check this driver's payload before publishing the new binding.
            driver.configuration(target, value['client_id'])
        with self.store.db() as db:
            db.execute("UPDATE assignments SET node_id=?, server_id=?, remote_id=?, binding_key=?, last_operation='switch', last_outcome='ok', checked_at=? WHERE client_id=?",
                       (target.id, target.server_id, value['client_id'], grant.binding_key, time.time(), row['client_id']))
            db.execute("UPDATE switches SET state='complete' WHERE client_id=? AND operation_id=?",
                       (row['client_id'], operation['operation_id']))
        from .leases import NodeLeases
        NodeLeases(self.gateway).remember(self.store.get(client_id=row['client_id']), value)
        return {**value, 'client_id': row['client_id'], 'external_id': row['external_id']}

    def revoke_source(self, row):
        source = self.gateway.node(row)
        driver = self.gateway.drivers.for_node(source)
        try:
            remote_id = self.gateway.resolve(row)
            value = driver.validate(driver.mutate(source, remote_id, 'disable', {}),
                                    external_id=row['binding_key'], client_id=remote_id)
            if value['status'] not in ('disabled', 'expired'):
                raise PilotError('revoke_unconfirmed', 503)
        except PilotError as error:
            if error.status != 503: raise
            from .leases import NodeLeases
            leases = NodeLeases(self.gateway)
            leases.fence(source)
            leases.retire(row)

    def disable_pending(self, row, operation):
        # Persist revocation intent BEFORE recovery; a restart must not publish an
        # active target if the business authority has already requested disable.
        with self.store.db() as db:
            db.execute('UPDATE switches SET disable_requested=1 WHERE client_id=? AND operation_id=?',
                       (row['client_id'], operation['operation_id']))
        try:
            self.resume(row, {**operation, 'disable_requested': 1})
        except PilotError as error:
            if error.code != 'grant_expired':
                raise

    def cancel_expired(self, row, operation, target):
        # Node agents independently enforce expiry. Still confirm both sides before
        # releasing the pending operation; no new peer can be created at this point.
        self.revoke_source(row)
        driver = self.gateway.drivers.for_node(target)
        matches = [v for v in driver.list(target) if v['external_id'] == operation['binding_key']]
        if len(matches) > 1:
            raise PilotError('remote_identity_conflict', 409)
        for match in matches:
            value = driver.validate(driver.mutate(target, match['client_id'], 'disable', {}),
                                    external_id=operation['binding_key'], client_id=match['client_id'])
            if value['status'] not in ('disabled', 'expired'):
                raise PilotError('revoke_unconfirmed', 503)
        with self.store.db() as db:
            db.execute("UPDATE switches SET state='cancelled' WHERE client_id=? AND operation_id=?",
                       (row['client_id'], operation['operation_id']))
