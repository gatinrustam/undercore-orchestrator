"""Cooperative operation budget shared across nested calls and copied contexts."""

from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from time import monotonic

from orchestrator.domain.models import OrchestratorError

_deadline: ContextVar[float | None] = ContextVar("operation_deadline", default=None)


class CapacityExceeded(OrchestratorError):
    def __init__(self):
        super().__init__("connection_busy", 503)


class DeadlineExceeded(OrchestratorError):
    def __init__(self):
        super().__init__("node_unavailable", 503)


def remaining():
    deadline = _deadline.get()
    if deadline is None:
        return None
    value = deadline - monotonic()
    if value <= 0:
        raise DeadlineExceeded()
    return value


@contextmanager
def budget(seconds):
    parent = _deadline.get()
    deadline = monotonic() + seconds
    token = _deadline.set(min(parent, deadline) if parent is not None else deadline)
    try:
        remaining()
        yield
    finally:
        _deadline.reset(token)


def bounded(call):
    """Entry-point deadline; nested scenarios can never extend their parent's budget."""

    @wraps(call)
    def wrapped(owner, *args, **kwargs):
        policy = getattr(owner, "gateway", owner).policy
        policy = getattr(policy, "agents", policy)
        with budget(policy.operation_timeout_seconds):
            return call(owner, *args, **kwargs)

    return wrapped
