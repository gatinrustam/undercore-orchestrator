"""Operator inputs and safe audit output. Credentials are write-only inputs."""

from typing import Literal
from pydantic import BaseModel, ConfigDict, Field
from orchestrator.domain.models import Identifier


class Command(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    operation_id: Identifier
    action: Literal[
        "node.save", "node.mode", "node.probe", "node.restore", "connection.disable", "policy.set"
    ]
    target: Identifier
    data: dict = Field(default_factory=dict)


class AuditEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    operation_id: str
    actor: Literal["cli", "panel"]
    action: str
    target: str
    started_at: float
    finished_at: float | None = None
    outcome: Literal["started", "succeeded", "failed"]
    result: dict = Field(default_factory=dict)
