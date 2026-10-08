"""Durable node permission and fencing. Never infer revocation from an HTTP timeout."""
import json
import time
import uuid
from datetime import datetime, timezone
from .models import PilotError
from .operation_lock import connection_lock

LEASE_SECONDS = 90
# Includes the node runtime's independent 90-second watchdog and bounded clock skew.
FENCE_GRACE_SECONDS = 125
MAX_NODE_LEASE_SECONDS = 120
# Linux monotonic time is comparable between API/worker processes in the same boot.
# On other systems a process restart conservatively starts the wait again.
from pathlib import Path
try: BOOT_ID = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
except OSError: BOOT_ID = uuid.uuid4().hex


def timestamp(seconds):
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z')


def seconds(value):
    return datetime.fromisoformat(value.replace('Z','+00:00')).timestamp()


def initialize(store):
    with store.db() as db:
        db.execute('CREATE TABLE IF NOT EXISTS control_leases (node_id TEXT PRIMARY KEY, server_id TEXT NOT NULL, controller TEXT NOT NULL, sequence INTEGER NOT NULL DEFAULT 0, valid_until REAL NOT NULL DEFAULT 0, verified INTEGER NOT NULL DEFAULT 0, fenced INTEGER NOT NULL DEFAULT 0, fence_boot TEXT NOT NULL DEFAULT \'\', fence_after REAL NOT NULL DEFAULT 0)')
        db.execute('CREATE TABLE IF NOT EXISTS access_cache (client_id TEXT PRIMARY KEY, binding_key TEXT NOT NULL, payload TEXT NOT NULL, denied INTEGER NOT NULL DEFAULT 0)')
        db.execute('CREATE TABLE IF NOT EXISTS retired_bindings (node_id TEXT NOT NULL, server_id TEXT NOT NULL, remote_id TEXT NOT NULL, binding_key TEXT NOT NULL, cleaned INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(node_id,binding_key))')


class NodeLeases:
    def __init__(self, gateway):
        self.gateway, self.store = gateway, gateway.store

    def state(self, node):
        with self.store.db() as db:
            row = db.execute('SELECT * FROM control_leases WHERE node_id=?',(node.id,)).fetchone()
        if row and row['server_id'] != node.server_id:
            raise PilotError('lease_node_identity_changed',503)
        return dict(row) if row else None

    def ready(self, node):
        if not node.lease_enabled: return True
        state = self.state(node)
        return bool(state and state['verified'] and not state['fenced'] and state['valid_until'] > time.time()+10)

    def heartbeat(self, node):
        if not node.lease_enabled: return
        with connection_lock(self.store,'node-'+node.id):
            state = self.state(node)
            if state and state['fenced']: return
            self.renew_locked(node)

    def renew_locked(self,node):
        health = self.gateway.api.request(node,'GET','/v1/health')
        lease = health.get('control_lease',{})
        if (health.get('server_id')!=node.server_id or health.get('status')!='ok'
            or lease.get('version')!=1 or abs(seconds(lease.get('server_time',''))-time.time())>5):
            raise PilotError('control_lease_unavailable',503)
        with self.store.db() as db:
            row=db.execute('SELECT * FROM control_leases WHERE node_id=?',(node.id,)).fetchone()
            controller=row['controller'] if row else 'ctl_'+uuid.uuid4().hex
            sequence=(row['sequence'] if row else 0)+1
            if lease.get('required') and lease.get('controller_id')!=controller:
                raise PilotError('controller_conflict',409)
            if lease.get('required') and lease.get('sequence',0)>=sequence:
                raise PilotError('lease_sequence_conflict',409)
            deadline=time.time()+LEASE_SECONDS
            # Before HTTP: a lost response can only have granted this upper bound.
            db.execute('INSERT INTO control_leases (node_id,server_id,controller,sequence,valid_until) VALUES (?,?,?,?,?) ON CONFLICT(node_id) DO UPDATE SET sequence=excluded.sequence,valid_until=MAX(valid_until,excluded.valid_until)',
                       (node.id,node.server_id,controller,sequence,deadline))
        until=timestamp(deadline)
        reply=self.gateway.api.request(node,'POST','/v1/control-lease',{'controller_id':controller,'sequence':sequence,'valid_until':until})
        if (reply.get('required') is not True or reply.get('active') is not True or reply.get('controller_id')!=controller
            or reply.get('sequence')!=sequence or reply.get('valid_until')!=until):
            raise PilotError('control_lease_unconfirmed',503)
        with self.store.db() as db:
            db.execute('UPDATE control_leases SET verified=1 WHERE node_id=?',(node.id,))

    def fence(self,node):
        with connection_lock(self.store,'node-'+node.id):
            state=self.state(node)
            if not node.lease_enabled or not state or not state['verified']:
                raise PilotError('revoke_unconfirmed',503)
            with self.store.db() as db:
                if not state['fenced'] or state['fence_boot'] != BOOT_ID:
                    db.execute('UPDATE control_leases SET fenced=1,fence_boot=?,fence_after=? WHERE node_id=?',
                               (BOOT_ID,time.monotonic()+MAX_NODE_LEASE_SECONDS+FENCE_GRACE_SECONDS,node.id))

    def expired(self,node):
        state=self.state(node)
        if not (node.lease_enabled and state and state['verified'] and state['fenced']): return False
        if state['fence_boot'] != BOOT_ID:
            self.fence(node)  # Reboot/unknown clock epoch: wait the full bound again.
            return False
        return time.monotonic() >= state['fence_after']

    def remember(self,row,data):
        safe={k:data[k] for k in ('client_id','external_id','device_id','name','expires_at','status','created_at','updated_at') if k in data}
        with self.store.db() as db:
            db.execute('INSERT INTO access_cache (client_id,binding_key,payload) VALUES (?,?,?) ON CONFLICT(client_id) DO UPDATE SET binding_key=excluded.binding_key,payload=excluded.payload',
                       (row['client_id'],row['binding_key'],json.dumps(safe)))

    def deny(self,row,value=True):
        with self.store.db() as db:
            # A missing snapshot must not lose a disable intent.
            db.execute('INSERT INTO access_cache (client_id,binding_key,payload,denied) VALUES (?,?,?,?) ON CONFLICT(client_id) DO UPDATE SET denied=excluded.denied',
                       (row['client_id'],row['binding_key'],'{}',int(value)))

    def denied(self,row):
        with self.store.db() as db:
            saved=db.execute("SELECT denied FROM access_cache WHERE client_id=?",(row["client_id"],)).fetchone()
        return bool(saved and saved[0])

    def cached_source(self,row):
        node=self.gateway.node(row)
        with self.store.db() as db:
            saved=db.execute('SELECT * FROM access_cache WHERE client_id=?',(row['client_id'],)).fetchone()
        if not saved or saved['denied'] or saved['binding_key']!=row['binding_key'] or not row['remote_id']:
            raise PilotError('offline_grant_unavailable',503)
        data=json.loads(saved['payload'])
        if data.get('status')!='active' or seconds(data['expires_at'])<=time.time():
            raise PilotError('access_unavailable',410)
        state=self.state(node)
        if not node.lease_enabled or not state or not state["verified"]:
            raise PilotError("offline_grant_unavailable",503)
        return data

    def disabled_view(self,row):
        node=self.gateway.node(row)
        self.fence(node)
        self.retire(row)
        with self.store.db() as db:
            saved=db.execute('SELECT * FROM access_cache WHERE client_id=?',(row['client_id'],)).fetchone()
        if not saved or saved['binding_key']!=row['binding_key']:
            raise PilotError('offline_grant_unavailable',503)
        data=json.loads(saved['payload'])
        if not {'name','expires_at','created_at','updated_at'} <= data.keys():
            raise PilotError('offline_grant_unavailable',503)
        return {**data,'client_id':row['client_id'],'external_id':row['external_id'],'status':'disabled'}

    def retire(self,row):
        node=self.gateway.node(row)
        if not self.expired(node):raise PilotError('lease_waiting',503)
        with self.store.db() as db:
            db.execute('INSERT OR IGNORE INTO retired_bindings (node_id,server_id,remote_id,binding_key) VALUES (?,?,?,?)',
                       (node.id,node.server_id,row['remote_id'],row['binding_key']))

    def restore(self,node):
        """Operator-only: revoke stale bindings BEFORE opening the old node again."""
        with connection_lock(self.store,'node-'+node.id):
            with self.store.db() as db:
                if db.execute("SELECT 1 FROM switches WHERE source_node=? AND state NOT IN ('complete','cancelled')",(node.id,)).fetchone():
                    raise PilotError('node_has_pending_switches',409)
                stale=[dict(r) for r in db.execute('SELECT * FROM retired_bindings WHERE node_id=? AND cleaned=0',(node.id,))]
                denied=[dict(r) for r in db.execute('SELECT a.* FROM assignments a JOIN access_cache c USING(client_id) WHERE a.node_id=? AND c.denied=1',(node.id,))]
            driver=self.gateway.drivers.for_node(node)
            for row in [*stale,*denied]:
                if row['server_id']!=node.server_id or not row['remote_id']:raise PilotError('node_identity_invalid',503)
                data=driver.validate(driver.mutate(node,row['remote_id'],'disable',{}),external_id=row['binding_key'],client_id=row['remote_id'])
                if data['status'] not in ('disabled','expired'):raise PilotError('revoke_unconfirmed',503)
                with self.store.db() as db:
                    db.execute('UPDATE retired_bindings SET cleaned=1 WHERE node_id=? AND binding_key=?',(node.id,row['binding_key']))
            self.renew_locked(node)
            with self.store.db() as db:db.execute('UPDATE control_leases SET fenced=0 WHERE node_id=?',(node.id,))


def heartbeat_all(gateway):
    from concurrent.futures import ThreadPoolExecutor
    nodes=[n for n in gateway.nodes.values() if n.lease_enabled]
    def one(node):
        try:NodeLeases(gateway).heartbeat(node);return True
        except Exception:return False  # Counts only, no response bodies or tokens.
    with ThreadPoolExecutor(max_workers=64) as pool:results=list(pool.map(one,nodes))
    return {'checked':len(results),'failed':results.count(False)}


if __name__=='__main__':
    from .runtime import agent_service
    gateway,_=agent_service()
    print(json.dumps(heartbeat_all(gateway)))
