"""Protocol-neutral observation port; no payloads, framework or storage dependencies."""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from enum import StrEnum
from functools import wraps
from hashlib import sha256
from time import monotonic
from typing import Protocol
from uuid import uuid4

from orchestrator.application.execution import DeadlineExceeded
from orchestrator.domain.models import OrchestratorError


class Stage(StrEnum):
    QUEUE = "agent_queue"
    HTTP = "http"
    CREATE = "create"
    SELECT = "select_node"
    OBSERVE = "observe_node"
    AGENT = "agent_request"
    MUTATE = "agent_operation"
    ACCESS = "access"
    CONFIGURATION = "configuration"
    EXPORT = "export"
    SWITCH = "switch"
    RECOVERY = "recovery"
    RECONCILE = "reconcile"
    HEARTBEAT = "heartbeat"


class Action(StrEnum):
    NONE = "none"
    HEALTH = "health"
    LIST = "list"
    CREATE = "create"
    GET = "get"
    CONNECTION = "connection"
    CONFIGURATION = "configuration"
    AMNEZIA = "amnezia"
    RENEW = "renew"
    REPLACE = "replace"
    DISABLE = "disable"
    ENABLE = "enable"
    LEASE = "lease"
    OTHER = "other"


class Reason(StrEnum):
    OK = "ok"
    TIMEOUT = "timeout"
    DEADLINE = "deadline"
    SATURATED = "saturated"
    TLS = "tls"
    NETWORK = "network"
    UPSTREAM = "upstream_status"
    INVALID = "invalid_response"
    IDENTITY = "identity_mismatch"
    WAITING = "waiting"
    UNAVAILABLE = "unavailable"
    REJECTED = "rejected"
    INTERNAL = "internal_error"
    CANCELLED = "cancelled"
    UNAUTHORIZED = "unauthorized"
    VALIDATION = "validation"


def error_reason(error):
    if isinstance(error, DeadlineExceeded):
        return Reason.DEADLINE
    if not isinstance(error, OrchestratorError):
        return Reason.INTERNAL
    if error.code in ("node_identity_invalid", "assigned_node_identity_changed"):
        return Reason.IDENTITY
    if error.code == "node_response_invalid":
        return Reason.INVALID
    if error.code in ("lease_waiting", "connection_busy", "switch_in_progress"):
        return Reason.WAITING
    return Reason.UNAVAILABLE if error.status == 503 else Reason.REJECTED


@dataclass(frozen=True)
class Measurement:
    stage: Stage
    reason: Reason
    duration: float
    request_id: str
    operation_ref: str = ""
    node_ref: str = ""
    status: int = 0
    action: Action = Action.NONE


class Observer(Protocol):
    def record(self, value: Measurement) -> None: ...
    def render(self, summary: dict | None) -> tuple[bytes, str]: ...


class NullObserver:
    def record(self, value):
        pass

    def render(self, summary):
        return b"# Observability is not configured\n", "text/plain; version=0.0.4"


@dataclass(frozen=True)
class Trace:
    observer: Observer
    request_id: str
    operation_ref: str = ""


_trace: ContextVar[Trace | None] = ContextVar("orchestrator_trace", default=None)


def reference(*parts):
    # Length prefixes avoid ambiguous concatenations. Never expose supplied identifiers.
    digest = sha256()
    for part in parts:
        value = str(part).encode()
        digest.update(len(value).to_bytes(8, "big"))
        digest.update(value)
    return digest.hexdigest()[:24]


@contextmanager
def trace_scope(observer):
    token = _trace.set(Trace(observer, uuid4().hex))
    try:
        yield _trace.get()
    finally:
        _trace.reset(token)


@contextmanager
def operation_scope(*identity):
    current = _trace.get()
    token = _trace.set(replace(current, operation_ref=reference(*identity))) if current else None
    try:
        yield
    finally:
        if token is not None:
            _trace.reset(token)


def trace_headers():
    current = _trace.get()
    if current is None:
        return {}
    headers = {"X-Request-ID": current.request_id}
    if current.operation_ref:
        headers["X-Operation-Ref"] = current.operation_ref
    return headers


@dataclass
class Outcome:
    reason: Reason = Reason.OK
    status: int = 0


@contextmanager
def span(stage, *, node_id=None, action=Action.NONE):
    current = _trace.get()
    result = Outcome()
    start = monotonic()
    try:
        yield result
    except BaseException as error:
        if result.reason == Reason.OK:
            result.reason = (
                error_reason(error) if isinstance(error, Exception) else Reason.CANCELLED
            )
        raise
    finally:
        if current:
            try:
                current.observer.record(
                    Measurement(
                        stage,
                        result.reason,
                        max(0.0, monotonic() - start),
                        current.request_id,
                        current.operation_ref,
                        reference(node_id) if node_id is not None else "",
                        result.status,
                        action,
                    )
                )
            except Exception:
                # Observability must not change the result of a VPN operation.
                pass


def observed(stage, identity=None):
    """Trace synchronous entry points, retaining context inherited from HTTP/workers."""

    def decorate(call):
        @wraps(call)
        def wrapped(owner, *args, **kwargs):
            gateway = getattr(owner, "gateway", owner)
            observer = getattr(gateway, "telemetry", NullObserver())

            def run():
                if identity is None:
                    with span(stage):
                        return call(owner, *args, **kwargs)
                with operation_scope(*identity(owner, *args, **kwargs)), span(stage):
                    return call(owner, *args, **kwargs)

            if _trace.get() is not None:
                return run()
            with trace_scope(observer):
                return run()

        return wrapped

    return decorate
