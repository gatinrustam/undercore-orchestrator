from dataclasses import dataclass, field
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import Field


Identifier = Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,80}$")]


class OrchestratorError(Exception):
    def __init__(self, code: str, status: int = 409):
        self.code = code
        self.status = status
        super().__init__(code)


@dataclass(frozen=True)
class Node:
    id: str
    api_url: str
    server_id: str
    region: str
    capacity: int
    api_key: str = field(repr=False)
    mode: Literal["active", "draining", "disabled"] = "active"
    protocol: Literal["amneziawg", "trusttunnel"] = "amneziawg"

    lease_enabled: bool = False

    def __post_init__(self):
        url = urlsplit(self.api_url)
        if (
            url.scheme != "https"
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
            or url.path not in ("", "/")
        ):
            raise ValueError("Node API must be an HTTPS origin")
        if self.protocol not in ("amneziawg", "trusttunnel"):
            raise ValueError("Unsupported node protocol")
        if not self.id or not self.server_id or not self.region:
            raise ValueError("Node identity and region are required")
        if self.mode not in ("active", "draining", "disabled") or self.capacity < 1:
            raise ValueError("Invalid node policy")
        if len(self.api_key) < 32 or any(c.isspace() for c in self.api_key):
            raise ValueError("Node API key must have at least 32 non-whitespace characters")


@dataclass(frozen=True)
class Observation:
    node: Node
    total_peers: int
    max_peers: int
    started_at: float
