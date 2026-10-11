"""Adapter to Undercore's durable agent. Configuration bodies are never persisted."""

from typing import cast
from orchestrator.domain.records import NodeAccess
import json
import re
import time
import httpx
import ssl
from orchestrator.application.execution import bounded, DeadlineExceeded, CapacityExceeded
from orchestrator.infrastructure.drivers.http_pool import HTTPPool, QueueFull
from orchestrator.application.telemetry import Action, Reason, Stage, observed, span, trace_headers
from orchestrator.domain.models import Observation, OrchestratorError


class AgentAPI:
    def __init__(self, transport=None, policy=None):
        from orchestrator.config.policy import AgentPolicy

        self.policy = policy or AgentPolicy()
        self._transport = transport
        self._pool = HTTPPool(self.policy, transport)

    @property
    def transport(self):
        return self._transport

    @transport.setter
    def transport(self, transport):
        self._pool.close()
        self._transport = transport
        self._pool = HTTPPool(self.policy, transport)

    def close(self):
        self._pool.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    @bounded
    def request(self, node, method, path, payload=None, text=False, *, snapshot=False):
        if path != "/v1/health" and not snapshot:
            self.verify(node)
        with span(Stage.AGENT, node_id=node.id, action=self.action(method, path)) as observation:
            try:

                async def send(client):
                    async with client.stream(
                        method,
                        node.api_url.rstrip("/") + path,
                        headers={"Authorization": "Bearer " + node.api_key, **trace_headers()},
                        json=payload,
                    ) as response:
                        observation.status = response.status_code
                        if response.status_code != 200:
                            observation.reason = Reason.UPSTREAM
                            # Never forward arbitrary response text or exception details.
                            status = (
                                response.status_code
                                if response.status_code in (404, 409, 410, 422)
                                else 503
                            )
                            # The old agent's POST /clients/{id}/{operation} wildcard
                            # makes an unknown GET export return 405 with Allow: POST.
                            if (
                                snapshot
                                and response.status_code == 405
                                and response.headers.get("allow") == "POST"
                            ):
                                status = 405
                            raise OrchestratorError("node_request_failed", status)
                        body = bytearray()
                        limit = 65536 if text else 4 * 1024 * 1024
                        async for chunk in response.aiter_bytes(chunk_size=8192):
                            body.extend(chunk)
                            if len(body) > limit:
                                raise ValueError()
                        if (text or snapshot) and "no-store" not in response.headers.get(
                            "cache-control", ""
                        ):
                            raise ValueError()
                        if text:
                            return body.decode()
                        return json.loads(body)

                return self._pool.run(node.id, send)
            except DeadlineExceeded:
                observation.reason = Reason.DEADLINE
                raise
            except QueueFull:
                observation.reason = Reason.SATURATED
                raise CapacityExceeded() from None
            except OrchestratorError:
                raise
            except httpx.TimeoutException:
                observation.reason = Reason.TIMEOUT
                raise OrchestratorError("node_unavailable", 503) from None
            except httpx.TransportError as error:
                cause = error
                seen = set()
                while cause is not None and id(cause) not in seen:
                    seen.add(id(cause))
                    if isinstance(cause, ssl.SSLError):
                        observation.reason = Reason.TLS
                        break
                    cause = cause.__cause__ or cause.__context__
                else:
                    observation.reason = Reason.NETWORK
                raise OrchestratorError("node_unavailable", 503) from None
            except (ValueError, UnicodeError):
                observation.reason = Reason.INVALID
                raise OrchestratorError("node_unavailable", 503) from None
            except Exception:
                observation.reason = Reason.INTERNAL
                raise OrchestratorError("node_unavailable", 503) from None

    @staticmethod
    def action(method, path):
        if path == "/v1/health":
            return Action.HEALTH
        if path == "/v1/clients":
            return Action.LIST if method == "GET" else Action.CREATE
        if path.startswith("/v1/control-lease"):
            return Action.LEASE
        match = re.fullmatch(r"/v1/clients/wgapi_[a-f0-9]{32}(?:/([a-z]+))?", path)
        if match:
            try:
                return Action(match[1] or "get")
            except ValueError:
                pass
        return Action.OTHER

    def verify(self, node):
        health = self.request(node, "GET", "/v1/health")
        self.validate_identity(node, health)

    @staticmethod
    def validate_identity(node, health):
        if (
            not isinstance(health, dict)
            or health.get("server_id") != node.server_id
            or health.get("status") != "ok"
            or health.get("protocol") != "amneziawg"
        ):
            raise OrchestratorError("node_identity_invalid", 503)

    def connection(self, node, remote_id):
        # The combined response authenticates the node itself. Only an absent route
        # falls back to the legacy protocol (including its health verification).
        try:
            value = self.request(
                node, "GET", "/v1/clients/" + remote_id + "/connection", snapshot=True
            )
        except OrchestratorError as error:
            if error.code == "node_request_failed" and error.status in (404, 405):
                return None
            raise
        self.validate_identity(node, value)
        if type(value.get("schema_version")) is not int or value["schema_version"] != 1:
            raise OrchestratorError("node_response_invalid", 503)
        return value

    @observed(Stage.OBSERVE)
    def observe(self, node):
        started = time.time()
        # list() already verifies this node immediately before its read.
        clients = self.list(node)
        return Observation(node, len(clients), node.capacity, started)

    def list(self, node):
        data = self.request(node, "GET", "/v1/clients")
        if (
            not isinstance(data, dict)
            or not isinstance(data.get("clients"), list)
            or len(data["clients"]) > 10000
        ):
            raise OrchestratorError("node_response_invalid", 503)
        return [self.validate(value) for value in data["clients"]]

    @staticmethod
    def validate(
        data: object, external_id: str | None = None, client_id: str | None = None
    ) -> NodeAccess:
        fields = (
            "client_id",
            "external_id",
            "device_id",
            "name",
            "expires_at",
            "status",
            "created_at",
            "updated_at",
        )
        if (
            not isinstance(data, dict)
            or any(not isinstance(data.get(key), str) for key in fields)
            or not re.fullmatch(r"wgapi_[a-f0-9]{32}", data["client_id"])
            or data["device_id"] != "account-v1"
            or (external_id is not None and data["external_id"] != external_id)
            or (client_id is not None and data["client_id"] != client_id)
        ):
            raise OrchestratorError("node_identity_invalid", 503)
        allowed = (
            *fields,
            "connection_checked_at",
            "last_handshake_at",
            "replacement_id",
            "replacement_status",
        )
        return cast(NodeAccess, {key: data[key] for key in allowed if key in data})
