from pathlib import Path
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator
from orchestrator.domain.models import Node


class NodeSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,80}$")
    api_url: str
    server_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,100}$")
    region: str = Field(pattern=r"^[A-Za-z0-9_-]{1,40}$")
    capacity: int = Field(ge=1, le=100000)
    mode: Literal["active", "draining", "disabled"]
    protocol: Literal["amneziawg", "trusttunnel"] = "amneziawg"
    lease_enabled: bool = False
    api_key_file: str

    @field_validator("api_key_file")
    @classmethod
    def absolute_file(cls, value):
        if not Path(value).is_absolute():
            raise ValueError("Absolute secret path required")
        return value

    def node(self, api_key: str):
        return Node(
            self.id,
            self.api_url,
            self.server_id,
            self.region,
            self.capacity,
            api_key,
            mode=self.mode,
            protocol=self.protocol,
            lease_enabled=self.lease_enabled,
        )
