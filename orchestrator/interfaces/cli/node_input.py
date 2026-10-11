"""Load private files at the CLI boundary; the application accepts data only."""

import json
from orchestrator.infrastructure.secrets import read_secret
from orchestrator.domain.models import OrchestratorError


def load_node_input(file):
    if file is None:
        return None
    data = json.loads(read_secret(file))
    if not isinstance(data, dict):
        raise OrchestratorError("invalid_request", 422)
    if "api_key_file" in data:
        if "api_key" in data:
            raise OrchestratorError("invalid_request", 422)
        data["api_key"] = read_secret(data.pop("api_key_file")).decode()
    return data
