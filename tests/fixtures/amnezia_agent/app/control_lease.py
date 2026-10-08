"""Fail-closed node permission, independent of paid peer expiry.

A process restart closes the gate. Replaying a prior sequence cannot reopen it.
Monotonic time prevents wall-clock rollback from extending an accepted lease.
"""
import time
from datetime import datetime, timezone
from .domain import Fault, expiry, now, stamp

from pydantic import BaseModel, ConfigDict, Field

class LeaseCommand(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, hide_input_in_errors=True)
    operation: str = Field(pattern='^control_lease$')
    client_id: None = None
    controller_id: str = Field(pattern=r'^ctl_[a-f0-9]{32}$')
    sequence: int = Field(ge=1, le=2147483647)
    valid_until: str

MAX_SECONDS = 120


class ControlLease:
    def __init__(self, store, clock=time.monotonic):
        self.store, self.clock = store, clock
        self.deadline = 0.0
        with store.db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS control_lease (id INTEGER PRIMARY KEY CHECK(id=1), controller TEXT NOT NULL, sequence INTEGER NOT NULL, valid_until TEXT NOT NULL)')

    def row(self):
        with self.store.db() as db:
            row = db.execute('SELECT * FROM control_lease WHERE id=1').fetchone()
            return dict(row) if row else None

    def allowed(self):
        row = self.row()
        if row is None:
            return True
        if self.clock() >= self.deadline or row['valid_until'] <= stamp(now()):
            self.deadline = 0.0
            return False
        return True

    def describe(self):
        row = self.row()
        return {'version': 1, 'required': row is not None, 'active': self.allowed(),
                'controller_id': row['controller'] if row else None,
                'sequence': row['sequence'] if row else 0,
                'valid_until': row['valid_until'] if row else None,
                'server_time': stamp(now()), 'max_seconds': MAX_SECONDS}

    def grant(self, controller, sequence, valid_until):
        end = expiry(valid_until)
        remaining = (datetime.fromisoformat(end.replace('Z', '+00:00')) - now()).total_seconds()
        if not controller or sequence is None or not 0 < remaining <= MAX_SECONDS:
            raise Fault('invalid_control_lease', 422)
        with self.store.db() as db:
            old = db.execute('SELECT * FROM control_lease WHERE id=1').fetchone()
            if old:
                if old['controller'] != controller:
                    raise Fault('controller_conflict')
                if sequence < old['sequence'] or (sequence == old['sequence'] and end != old['valid_until']):
                    raise Fault('lease_sequence_conflict')
                if sequence == old['sequence']:
                    return  # Idempotent replay never rearms an expired/restarted gate.
            db.execute('INSERT OR REPLACE INTO control_lease VALUES (1,?,?,?)', (controller, sequence, end))
        self.deadline = self.clock() + remaining

    def effective_expiry(self, row):
        lease = self.row()
        return min(row['expires_at'], lease['valid_until']) if lease else row['expires_at']
