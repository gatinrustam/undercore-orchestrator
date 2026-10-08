"""Adapter for kyoresuas/amnezia-api deff5b750e74b0c6b9b9fc96c168e00495deea4b.

No retries on mutations: this upstream has no durable create idempotency.
The returned vpn:// envelope is opaque; native-client conversion is a later step.
"""

import json
import re
import time

import httpx

from .models import Node, Observation, PilotError, ProvisionedClient


class AmneziaAPI:
    def __init__(self, transport: httpx.BaseTransport | None = None):
        self.transport = transport

    def _request(self, node: Node, method: str, path: str, payload=None):
        # Never inherit a proxy or forward the API key through a redirect.
        with httpx.Client(
            transport=self.transport, timeout=httpx.Timeout(12, connect=4),
            follow_redirects=False, trust_env=False,
        ) as client:
            with client.stream(
                method, node.api_url.rstrip("/") + path,
                headers={"x-api-key": node.api_key}, json=payload,
            ) as response:
                if response.status_code != 200:
                    raise PilotError("node_request_failed", 502)
                chunks = bytearray()
                for chunk in response.iter_bytes(chunk_size=8192):
                    chunks.extend(chunk)
                    if len(chunks) > 512 * 1024:
                        raise PilotError("node_response_invalid", 502)
                return json.loads(chunks)

    def observe(self, node: Node) -> Observation:
        started = time.time()
        try:
            data = self._request(node, "GET", "/server")
            total, maximum = data["totalPeers"], data["maxPeers"]
            if (
                data["id"] != node.server_id
                or "amneziawg3" not in data["protocols"]
                or type(total) is not int or type(maximum) is not int
                or total < 0 or maximum < 1
            ):
                raise ValueError()
            return Observation(node, total, maximum, started)
        except Exception:
            # Do not expose upstream bodies, URLs or authentication headers.
            raise PilotError("node_unavailable", 503) from None

    def create(self, node: Node, operation_id: str, expires_at: int) -> ProvisionedClient:
        try:
            data = self._request(node, "POST", "/clients", {
                "clientName": "uc_" + operation_id,
                "protocol": "amneziawg3",
                "expiresAt": expires_at,
            })["client"]
            peer_id, config = data["id"], data["config"]
            if (
                data["protocol"] != "amneziawg3"
                or not isinstance(peer_id, str)
                or re.fullmatch(r"[A-Za-z0-9+/=_-]{1,128}", peer_id) is None
                or not isinstance(config, str) or len(config) > 256 * 1024
                or re.fullmatch(r"vpn://[A-Za-z0-9_-]+", config) is None
            ):
                raise ValueError()
            return ProvisionedClient(peer_id, config)
        except Exception:
            # A timeout, 500, malformed response or unexpected response protocol
            # may all occur AFTER the peer has been added. Never retry blindly.
            raise PilotError("provisioning_uncertain", 409) from None
