"""Explicit read-only projections: no credentials or transport documents."""

from typing import Annotated, Literal
from datetime import datetime
from pydantic import BaseModel, ConfigDict, Field, AwareDatetime

Ref = Annotated[str, Field(pattern=r"^[a-f0-9]{24}$")]
Identifier = Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,100}$")]


class PanelModel(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class PanelEvent(PanelModel):
    timestamp: AwareDatetime
    event: Literal["operation_finished"]
    stage: str
    reason: str
    action: str
    duration_ms: float = Field(ge=0, allow_inf_nan=False)
    request_id: Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")]
    operation_ref: Ref | None = None
    node_ref: Ref | None = None
    http_status: int | None = Field(default=None, ge=100, le=599)


class PanelNode(PanelModel):
    id: Identifier
    protocol: Literal["amneziawg", "trusttunnel"]
    region: str
    mode: Literal["active", "draining", "disabled"]
    capacity: int
    revision: int
    assignments: int
    pending_targets: int
    lease_enabled: bool
    lease_verified: bool
    fenced: bool
    lease_valid_until: float | None


class PanelAssignment(PanelModel):
    connection_id: Identifier
    node_id: Identifier
    protocol: Literal["amneziawg", "trusttunnel"]
    state: Literal["assigned", "pending_create", "pending_switch", "denied"]
    revision: int
    checked_at: float | None
    last_operation: str | None
    last_outcome: Literal["ok", "error"] | None


class PanelOverview(PanelModel):
    generated_at: datetime
    nodes_total: int
    connections_total: int
    pending_creates: int
    pending_switches: int
    denied: int
    node_offset: int
    connection_offset: int
    page_size: int
    nodes: list[PanelNode]
    connections: list[PanelAssignment]
    events: list[PanelEvent] = Field(default_factory=list)
    events_available: bool = False
