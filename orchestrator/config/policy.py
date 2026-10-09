"""Validated runtime limits. Defaults preserve the deployed behaviour."""

from pydantic import BaseModel, ConfigDict, Field, model_validator


class PolicyModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)


class AgentPolicy(PolicyModel):
    connect_timeout_seconds: int = Field(default=4, ge=1, le=10)
    response_timeout_seconds: int = Field(default=12, ge=1, le=15)


class RecoveryPolicy(PolicyModel):
    cooldown_seconds: int = Field(default=1800, ge=60, le=86400)
    reconcile_batch_size: int = Field(default=16, ge=1, le=100)


class SelectionPolicy(PolicyModel):
    observation_workers: int = Field(default=4, ge=1, le=16)
    observation_max_age_seconds: int = Field(default=30, ge=1, le=300)


class LeasePolicy(PolicyModel):
    duration_seconds: int = Field(default=90, ge=90, le=120)
    max_node_lease_seconds: int = Field(default=120, ge=120, le=300)
    fence_grace_seconds: int = Field(default=125, ge=125, le=600)
    heartbeat_workers: int = Field(default=64, ge=64, le=64)

    @model_validator(mode="after")
    def safe_bounds(self):
        if self.duration_seconds > self.max_node_lease_seconds:
            raise ValueError("Lease duration exceeds the node lease bound")
        return self


class RuntimePolicy(PolicyModel):
    agents: AgentPolicy = Field(default_factory=AgentPolicy)
    recovery: RecoveryPolicy = Field(default_factory=RecoveryPolicy)
    selection: SelectionPolicy = Field(default_factory=SelectionPolicy)
    leases: LeasePolicy = Field(default_factory=LeasePolicy)
