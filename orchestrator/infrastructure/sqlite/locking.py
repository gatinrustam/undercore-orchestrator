"""Reentrant process/thread lock shared by export, lifecycle and node switches."""

import fcntl
import hashlib
import os
import threading
import time
from orchestrator.domain.models import OrchestratorError
from contextlib import contextmanager

_local = threading.local()


@contextmanager
def connection_lock(store, client_id):
    path = store.path.parent / (
        "operation-" + hashlib.sha256(client_id.encode()).hexdigest() + ".lock"
    )
    held = getattr(_local, "held", None)
    if held is None:
        held = _local.held = set()
    key = str(path.resolve())
    if key in held:
        yield
        return
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        deadline = time.monotonic() + 10
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise OrchestratorError("connection_busy", 503)
                time.sleep(0.02)
        held.add(key)
        try:
            yield
        finally:
            held.remove(key)
    finally:
        os.close(fd)
