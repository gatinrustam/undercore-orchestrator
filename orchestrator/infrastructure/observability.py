"""JSON stderr events and bounded Prometheus series. No default process collectors."""

import json
import logging
from datetime import datetime, timezone

from prometheus_client import CollectorRegistry, Counter, Histogram, Gauge, generate_latest
from prometheus_client.exposition import CONTENT_TYPE_LATEST

from orchestrator.application.telemetry import Action, Reason, Stage


def event_logger():
    logger = logging.getLogger("orchestrator.events")
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    return logger


class Observability:
    def __init__(self, logger=None, event_tail=None):
        self.event_tail = event_tail
        self.logger = logger if logger is not None else event_logger()
        self.registry = CollectorRegistry()
        self.calls = Counter(
            "orchestrator_operations_total",
            "Completed stage observations, not unique connections",
            ("stage", "action", "reason"),
            registry=self.registry,
        )
        self.duration = Histogram(
            "orchestrator_operation_duration_seconds",
            "Duration of an operation stage",
            ("stage", "action"),
            buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 20, 60),
            registry=self.registry,
        )
        self.backlog = Gauge(
            "orchestrator_pending_operations",
            "Durable pending operations in the local journal",
            ("kind",),
            registry=self.registry,
        )
        self.age = Gauge(
            "orchestrator_oldest_pending_seconds",
            "Age of oldest pending switch",
            registry=self.registry,
        )
        self.snapshot_ok = Gauge(
            "orchestrator_journal_snapshot_success",
            "Whether the latest journal snapshot succeeded",
            registry=self.registry,
        )

    def record(self, value):
        # The event has a fixed field set, enums and generated/digested identifiers.
        stage, reason, action = Stage(value.stage), Reason(value.reason), Action(value.action)
        self.calls.labels(stage.value, action.value, reason.value).inc()
        self.duration.labels(stage.value, action.value).observe(value.duration)
        event = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event": "operation_finished",
            "stage": stage.value,
            "reason": reason.value,
            "action": action.value,
            "duration_ms": round(value.duration * 1000, 3),
            "request_id": value.request_id,
        }
        if value.operation_ref:
            event["operation_ref"] = value.operation_ref
        if value.node_ref:
            event["node_ref"] = value.node_ref
        if 100 <= value.status <= 599:
            event["http_status"] = value.status
        self.logger.info(json.dumps(event, separators=(",", ":")))
        if self.event_tail is not None:
            self.event_tail.append(event)

    def render(self, summary):
        self.snapshot_ok.set(0 if summary is None else 1)
        if summary is not None:
            self.backlog.labels("create").set(summary["pending_creates"])
            self.backlog.labels("switch").set(summary["pending_switches"])
            self.age.set(summary["oldest_pending_seconds"])
        return generate_latest(self.registry), CONTENT_TYPE_LATEST
