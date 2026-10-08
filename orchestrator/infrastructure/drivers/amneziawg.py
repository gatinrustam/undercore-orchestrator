"""Amnezia implementation of the node lifecycle port."""

import re
from orchestrator.domain.contracts import TransportConfiguration
from orchestrator.domain.models import OrchestratorError
from orchestrator.application.ports import DriverCapabilities


class AmneziaAgentDriver:
    capabilities = DriverCapabilities(
        "amneziawg", idempotent_create=True, enforces_expiry=True, confirmed_revoke=True
    )

    def __init__(self, api):
        self.api = api

    def observe(self, node):
        return self.api.observe(node)

    def list(self, node):
        return self.api.list(node)

    def create(self, node, grant):
        payload = {
            "external_id": grant.binding_key,
            "device_id": "account-v1",
            "name": grant.name,
            "expires_at": grant.expires_at,
        }
        return self.api.request(node, "POST", "/v1/clients", payload)

    def get(self, node, remote_id):
        return self.api.request(node, "GET", "/v1/clients/" + remote_id)

    def mutate(self, node, remote_id, operation, payload):
        if operation not in ("renew", "replace", "enable", "disable"):
            raise OrchestratorError("unsupported_operation", 422)
        return self.api.request(node, "POST", "/v1/clients/" + remote_id + "/" + operation, payload)

    def validate(self, data, external_id=None, client_id=None):
        return self.api.validate(data, external_id, client_id)

    def legacy_export(self, node, remote_id, format):
        if format not in ("configuration", "amnezia"):
            raise OrchestratorError("unsupported_format", 422)
        body = self.api.request(node, "GET", "/v1/clients/" + remote_id + "/" + format, text=True)
        if (format == "configuration" and ("[Interface]" not in body or "[Peer]" not in body)) or (
            format == "amnezia" and not re.fullmatch(r"vpn://[A-Za-z0-9_-]+", body)
        ):
            raise OrchestratorError("node_response_invalid", 503)
        return body

    def configuration(self, node, remote_id):
        return TransportConfiguration(
            protocol="amneziawg",
            format="awg-quick",
            data=self.legacy_export(node, remote_id, "configuration"),
        )
