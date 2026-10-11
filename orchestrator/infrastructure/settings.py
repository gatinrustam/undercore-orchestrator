"""Private files and read-only inventory loading, without driver construction."""

import os
from pathlib import Path
from orchestrator.infrastructure.secrets import read_secret


def validate_nodes(settings, secrets=True):
    nodes = [
        n.node(read_secret(n.api_key_file).decode() if secrets else "x" * 40)
        for n in settings.nodes
    ]
    if sum(n.lease_enabled for n in nodes) > 64:
        raise ValueError("At most 64 leased nodes per controller")
    for values in (
        [n.id for n in nodes],
        [n.server_id for n in nodes],
        [n.api_url.lower().rstrip("/") for n in nodes],
    ):
        if len(values) != len(set(values)):
            raise ValueError("Duplicate node")
    return nodes


def read_settings(path, secrets, decode):
    file = Path(
        path or os.environ.get("ORCHESTRATOR_SETTINGS", "/etc/vpn-orchestrator/settings.json")
    )
    value = decode(file.read_bytes())
    if secrets and (Path(value.state_directory) / ".restore-review-required").exists():
        raise ValueError("restore_review_required")
    # Validate bootstrap nodes only until the authoritative inventory exists.
    from orchestrator.infrastructure.sqlite.inventory import read_inventory

    records = read_inventory(Path(value.state_directory) / "assignments.sqlite3")
    effective = value.model_copy(update={"nodes": records}) if records is not None else value
    validate_nodes(effective, secrets)
    if secrets:
        import re

        backend = read_secret(value.backend_token_file)
        if not re.fullmatch(rb"[A-Za-z0-9_-]{32,256}", backend):
            raise ValueError("Invalid backend credential")
    return value
